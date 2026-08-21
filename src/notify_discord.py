"""Discord notifications for sweep results.

Two kinds of message:

1. **Price improvement** - posted only when a scenario's cheapest total drops
   below the best previously recorded, or falls under an absolute threshold.
   The previous behaviour alerted on any content hash it had not seen before,
   which meant a single flight parsed ten times produced "10 New Flight Offers".

2. **Health** - posted when a sweep finds nothing, or fails more than half its
   searches. Without this, a broken scraper is indistinguishable from a quiet
   day: the old workflow died in December 2025 and went unnoticed for 8 months.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import requests

from .webhook_store import SECRETS_DIR, load_webhook

COLOR_GOOD = 0x27AB83   # palette green500
COLOR_INFO = 0x1980D4   # palette blue600
COLOR_RISE = 0xF0B429   # palette yellow600
COLOR_ALERT = 0xE12D39  # palette red500

# How far a total must move before the move is worth saying anything about.
#
# The same bar `watch.py` applies to a fall, and for the same reason: below it a
# fare has not moved, it has rounded, and a watcher that announces rounding
# nightly is a watcher you mute.
MEANINGFUL_MOVE_PCT = 1.0


def _read_state(directory: Path | str) -> dict:
    """Every pick's recorded best, keyed by pick name.

    Reads the flat `{"best_total": ...}` shape written before there was more
    than one pick, and treats it as the cheapest - which is what it recorded.
    A corrupt file yields {} rather than raising: bad state must never stop a
    sweep from reporting.
    """
    path = Path(directory) / "best.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    if "best_total" in payload:
        return {"cheapest": payload}
    return {k: v for k, v in payload.items() if isinstance(v, dict)}


def _figure(directory: Path | str, name: str, field: str) -> float | None:
    entry = _read_state(directory).get(name)
    if not entry:
        return None
    try:
        return float(entry[field])
    except (ValueError, KeyError, TypeError):
        return None


def load_best(directory: Path | str, name: str = "cheapest") -> float | None:
    """Best total previously recorded for one pick, if any."""
    return _figure(directory, name, "best_total")


def load_last(directory: Path | str, name: str = "cheapest") -> float | None:
    """What this pick cost the *previous* time anything was reported, if ever.

    Distinct from the best on purpose. `best_total` only ever walks downward, so
    it cannot answer "is this climbing" - which is the question a fare rising
    towards departure is asking, and the one nothing here could answer at all.

    Every `best.json` written before this field existed reads as None, which is
    exactly right: there is nothing to compare against.
    """
    return _figure(directory, name, "last_total")


def save_best(
    directory: Path | str, best_total: float, currency: str, name: str = "cheapest"
) -> None:
    """Record what this pick cost, and a new best only when it really is one.

    Two figures, because they answer two questions. `best_total` used to be
    written unconditionally whenever an alert was sent: with `alert_threshold`
    set, an alert fires on any total under the threshold - including one *worse*
    than the recorded best - and the recorded best then walked upward, quietly
    destroying the "only alert on genuine improvement" guarantee for every later
    run. It only ever walks downward now.

    Which is exactly why `last_total` had to be kept beside it. A figure that
    only falls cannot say whether a fare is climbing, and climbing is the thing
    worth knowing when the departure is fixed and the window is closing.

    Picks are recorded separately because they move separately: a tier-1 trip
    dropping 3,000 CZK is news even on a day Frankfurt did not move, and one
    shared figure hid exactly that.
    """
    directory = Path(directory)
    state = _read_state(directory)
    previous = load_best(directory, name)
    now = datetime.now(UTC).isoformat(timespec="seconds")

    entry = dict(state.get(name) or {})
    # Always. This is what the next run is compared against, and without it a
    # rise can never be seen - the best walks downward by design, so a total
    # above it is indistinguishable from a total above it yesterday.
    entry["last_total"] = best_total
    entry["currency"] = currency
    entry["recorded_at"] = now
    if previous is None or best_total < previous:
        entry["best_total"] = best_total
        entry["best_at"] = now

    state[name] = entry
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "best.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def should_alert(
    best_total: float | None, previous_best: float | None, threshold: float | None
) -> bool:
    """True only when this is genuinely news."""
    if best_total is None:
        return False
    if threshold is not None and best_total <= threshold:
        return True
    if previous_best is None:
        return True
    return best_total < previous_best


def _pick_field(pick, bag_estimate: float = 0.0) -> dict:
    lines = []
    for leg in pick.itinerary.legs:
        when = leg.depart_date.isoformat() if leg.depart_date else "?"
        times = f" {leg.depart_time}→{leg.arrive_time}" if leg.depart_time else ""
        stops = f" · {leg.stops} stop(s)" if leg.stops is not None else ""
        lines.append(
            f"**{leg.origin}→{leg.destination}** {when}{times} · {leg.airline}{stops} · "
            f"{leg.price_amount:,.0f} {leg.price_currency}"
        )
    if pick.premium:
        # What the preference costs, stated on the card rather than left for
        # the reader to subtract. It is the whole reason both are sent.
        lines.append(f"_{pick.premium:,.0f} {pick.itinerary.currency} more than the cheapest_")
    # When these prices were read. A sweep runs up to 97 minutes and fares have
    # moved 21% inside two hours, so "now" is not a safe assumption.
    if pick.itinerary.observed_at:
        span = pick.itinerary.observed_span_minutes or 0
        measured = f"_measured {pick.itinerary.observed_at}"
        measured += f", legs up to {span} min apart_" if span >= 20 else "_"
        lines.append(measured)
    if pick.itinerary.legs[0].url:
        lines.append(f"[Open search]({pick.itinerary.legs[0].url})")

    total = pick.itinerary.total_with_bags(bag_estimate)
    return {
        "name": f"{pick.label} — {total:,.0f} {pick.itinerary.currency}",
        "value": "\n".join(line for line in lines if line),
        "inline": False,
    }


def describe_move(
    total: float, previous_best: float | None, previous_last: float | None
) -> tuple[str, str, int]:
    """Which way this total went, said in a sentence, with a colour.

    Returns `(kind, sentence, colour)`. The kinds are ordered by what a reader
    needs first: a new best is the thing worth acting on, a rise is the thing
    worth knowing, and everything else is context that stops the message reading
    as news when nothing has happened.

    A rise must clear `MEANINGFUL_MOVE_PCT` before it is called one. Fares wobble
    by tens of crowns between readings, and "up 40 since yesterday" every night
    is how a watcher stops being read at all.
    """
    if previous_best is None and previous_last is None:
        return "first", "the first reading of this trip", COLOR_INFO

    if previous_best is not None and total < previous_best:
        return (
            "best",
            f"a new best, down {previous_best - total:,.0f} from {previous_best:,.0f}",
            COLOR_GOOD,
        )

    if previous_last is None:
        # A `best.json` written before `last_total` existed: there is a best to
        # measure against but no previous run, so the only honest thing to say
        # is where this sits against the best.
        if total == previous_best:
            return "flat", f"level with the best of {previous_best:,.0f}", COLOR_INFO
        return "flat", f"above the best of {previous_best:,.0f}", COLOR_INFO

    move = total - previous_last
    big_enough = previous_last > 0 and abs(move) / previous_last * 100 >= MEANINGFUL_MOVE_PCT
    # Skipped when the last run *was* the best: "up 4,600 since the last run's
    # 31,200, still above the best of 31,200" states one number as two facts.
    still_above = (
        f", still above the best of {previous_best:,.0f}"
        if previous_best is not None
        and total > previous_best
        and previous_best != previous_last
        else ""
    )
    if move > 0 and big_enough:
        return (
            "up",
            f"**up {move:,.0f}** since the last run's {previous_last:,.0f}{still_above}",
            COLOR_RISE,
        )
    if move < 0 and big_enough:
        return (
            "down",
            f"down {-move:,.0f} since the last run's {previous_last:,.0f}{still_above}",
            COLOR_INFO,
        )
    return "flat", f"unchanged since the last run{still_above}", COLOR_INFO


def build_price_embed(
    scenario_name: str,
    picks: list,
    previous_best: float | None = None,
    bag_estimate: float = 0.0,
    coverage: float | None = None,
    previous_last: float | None = None,
) -> dict | None:
    """One embed carrying every pick worth reporting, or None if there are none.

    Picks replaced a fixed "same airport vs open jaw" pair, which answered a
    question about trip shape when the one actually being asked was where you
    would rather fly from.

    `coverage` is stated whenever the sweep behind these prices did not answer
    everything it planned to. A price you are about to book on has to come with
    how much of the trip was actually priced to find it - a sweep with holes in
    its date grid reports a cheapest total in exactly the same words as a
    complete one.
    """
    if not picks:
        return None

    fields = [_pick_field(pick, bag_estimate) for pick in picks]
    headline = min(pick.itinerary.total_with_bags(bag_estimate) for pick in picks)

    # Which way it went, not just what it is. A message that restates a number
    # every night is one nobody opens; the movement is the whole reason to look.
    kind, moved, colour = describe_move(headline, previous_best, previous_last)
    description = f"Best total: **{headline:,.0f}** — {moved}"
    if coverage is not None and coverage < 1.0:
        description += (
            f"\n⚠️ Only **{coverage:.0%}** of the planned searches were answered, "
            "so a cheaper trip may simply not have been seen."
        )

    # Said in the title as well, because that is all a phone notification shows.
    mark = "↗️" if kind == "up" else "✈️"
    suffix = {"best": " — new best", "up": " — up since the last run"}.get(kind, "")

    return {
        "title": f"{mark} {scenario_name}{suffix}",
        "description": description,
        "color": colour,
        "fields": fields,
        "timestamp": datetime.now(UTC).isoformat(),
        "footer": {"text": "Flight scenario watcher"},
    }


def build_leg_watch_embed(scenario_name: str, drops: list[dict]) -> dict | None:
    """One embed for the watched legs that fell, or None.

    Its own embed rather than more fields on the trip one, because it is a
    different claim. That one says a whole trip on a given day got cheaper;
    this says one ticket did, and a ticket price sitting unlabelled beside trip
    totals reads as an impossibly cheap trip.
    """
    if not drops:
        return None

    fields = []
    for drop in drops:
        lines = [
            f"down **{drop['drop']:,.0f} {drop['currency']}** ({drop['drop_pct']:.1f}%) "
            f"from {drop['previous_best']:,.0f}"
        ]
        if drop.get("airline"):
            lines.append(f"_{drop['airline']}_")
        # The site substitutes nearby dates. A price found for the 23rd is not a
        # price for the 22nd, and this message is read away from the app where
        # nothing else can say so.
        if not drop.get("exact") and drop.get("found_date"):
            lines.append(f"_priced on {drop['found_date']}, not the day watched_")
        fields.append(
            {
                "name": f"{drop['route']} {drop['depart_date']} — "
                        f"{drop['price']:,.0f} {drop['currency']}",
                "value": "\n".join(lines),
                "inline": False,
            }
        )

    return {
        "title": f"📉 {scenario_name} — a watched flight got cheaper",
        "description": (
            f"{len(drops)} of the individual flights you are following fell since "
            f"the last time this said anything. These are single tickets, not trips."
        ),
        "color": COLOR_GOOD,
        "fields": fields,
        "timestamp": datetime.now(UTC).isoformat(),
        "footer": {"text": "Flight scenario watcher — watched legs"},
    }


def build_watch_embed(scenario_name: str, drops: list[dict]) -> dict | None:
    """One embed for the watched days that actually fell, or None.

    A different message from `build_price_embed` on purpose. That one answers
    "what is the cheapest this trip has been"; this one answers "one of the
    days you are choosing between just moved", which is only useful if it says
    *which* day and *by how much against what it was*. A shared embed would
    have had to drop one of those to fit the other's shape.

    Nothing is filtered here. `watch.drops` has already decided what counts as
    a fall worth sending - anything reaching this has cleared it.
    """
    if not drops:
        return None

    fields = []
    for drop in drops:
        lines = [f"**{drop['route']}**"]
        lines.append(
            f"down **{drop['drop']:,.0f} {drop['currency']}** ({drop['drop_pct']:.1f}%) "
            f"from {drop['previous_best']:,.0f}"
        )
        if drop.get("total_with_bags") and drop["total_with_bags"] != drop["total"]:
            lines.append(f"_{drop['total_with_bags']:,.0f} once bags are estimated in_")
        # A total that includes a journey nobody booked has to say so here too:
        # this message is the one read away from the app, where the ⇢ in the
        # route has nothing beside it to explain what it means.
        if drop.get("has_overland"):
            lines.append("_⇢ is a hop you make overland yourself, not a flight_")
        fields.append(
            {
                "name": f"Leaving {drop['depart_date']} — {drop['total']:,.0f} {drop['currency']}",
                "value": "\n".join(lines),
                "inline": False,
            }
        )

    return {
        "title": f"📉 {scenario_name} — a watched day got cheaper",
        "description": (
            f"{len(drops)} of the days you are watching fell since the last time "
            f"this said anything."
        ),
        "color": COLOR_GOOD,
        "fields": fields,
        "timestamp": datetime.now(UTC).isoformat(),
        "footer": {"text": "Flight scenario watcher — watch"},
    }


def build_health_alert(
    scenario_name: str,
    legs_found: int,
    errors: int,
    total: int,
    dark_routes: list[str] | None = None,
    itineraries: int | None = None,
) -> dict | None:
    """A red embed when the sweep looks broken, otherwise None."""
    if total == 0:
        reason = "the sweep planned no searches at all"
    elif legs_found == 0:
        reason = f"all {total} searches completed but returned no flights"
    elif itineraries == 0:
        # Distinct from "no flights", and it used to be reported as that because
        # this call site passed a hardcoded legs_found=0. It sent you to debug
        # the scraper when the scraper had worked perfectly and the trip shape
        # was what could not be satisfied.
        reason = (
            f"{legs_found} flights were found but none of them chain into a "
            "complete trip — check the stay ranges and the date window rather "
            "than the scraper"
        )
    elif errors > total / 2:
        reason = f"{errors} of {total} searches failed"
    elif dark_routes:
        # A route searched on every date in the window without ever returning an
        # offer is breakage. This went unnoticed once: MNL->VIE was dark for a
        # whole sweep, and it was the return leg of the cheapest real itinerary.
        listed = ", ".join(dark_routes[:6])
        more = f" (+{len(dark_routes) - 6} more)" if len(dark_routes) > 6 else ""
        reason = f"{len(dark_routes)} route(s) returned nothing on every date: {listed}{more}"
    else:
        return None

    return {
        "title": f"⚠️ {scenario_name}: sweep looks broken",
        "description": (
            f"{reason}. Silence would otherwise look identical to "
            "'no cheap flights today'."
        ),
        "color": COLOR_ALERT,
        "timestamp": datetime.now(UTC).isoformat(),
        "footer": {"text": "Flight scenario watcher — health check"},
    }


def post(webhook_url: str, embeds: list[dict]) -> bool:
    if not embeds:
        return False
    try:
        response = requests.post(
            webhook_url,
            json={"username": "Flight Tracker", "embeds": embeds},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as exc:
        print(f"[Discord] failed to send: {exc}")
        return False


def notify_watch(
    scenario,
    drops: list[dict],
    webhook_url: str | None = None,
    leg_drops: list[dict] | None = None,
) -> bool:
    """Post the watched days and legs that fell, if any.

    True when something was sent.

    Separate from `notify_sweep` because a watch has nothing to say most of the
    time, and that is the desired behaviour rather than a failure: at six runs
    a day, a message per run would be forty a week, most of them "still 23,485".

    Days and legs go in one post as two embeds, not two posts. They come from
    one run and are read in one sitting, and Discord shows them as two labelled
    cards - which is exactly the distinction that matters, since a ticket price
    beside a trip total would otherwise read as an impossibly cheap trip.
    """
    leg_drops = leg_drops or []
    if not drops and not leg_drops:
        return False
    # The same lookup as `notify_sweep`, so a webhook pasted into the Sources
    # tab reaches a local `python -m src.cli watch` as well. In Actions nothing
    # changes: the environment variable still wins.
    if not webhook_url:
        webhook_url, origin = load_webhook(SECRETS_DIR)
        if webhook_url:
            print(f"[Discord] using the webhook from the {origin}")
    if not webhook_url:
        print(
            "[Discord] no webhook configured — set DISCORD_WEBHOOK_URL, or paste "
            "one into the Sources tab, and nothing else has to change"
        )
        return False

    embeds = [
        embed
        for embed in (
            build_watch_embed(scenario.name, drops),
            build_leg_watch_embed(scenario.name, leg_drops),
        )
        if embed
    ]
    return post(webhook_url, embeds) if embeds else False


def notify_sweep(scenario, result, webhook_url: str | None = None) -> bool:
    """Evaluate a finished sweep and post whatever it warrants."""
    from .alerts import select_alerts
    from .combine import combine_all

    # Through the store rather than straight off the environment, so a webhook
    # saved in the Sources tab reaches a local `python -m src.cli sweep`. In
    # Actions nothing changes: the env var still wins.
    if not webhook_url:
        webhook_url, origin = load_webhook(SECRETS_DIR)
        if webhook_url:
            print(f"[Discord] using the webhook from the {origin}")
    if not webhook_url:
        print(
            "[Discord] no webhook configured — set DISCORD_WEBHOOK_URL, or paste "
            "one into the Sources tab, and nothing else has to change"
        )
        return False

    health = build_health_alert(
        scenario.name,
        len(result.legs),
        len(result.errors),
        result.total,
        getattr(result, "routes_with_no_results", None),
    )
    if health is not None:
        return post(webhook_url, [health])

    combined = combine_all(result.legs, scenario)
    if not combined.top:
        return post(
            webhook_url,
            [
                build_health_alert(
                    scenario.name,
                    len(result.legs),
                    len(result.errors),
                    result.total,
                    itineraries=0,
                )
            ],
        )

    picks = select_alerts(result.legs, scenario)
    if not picks:
        print("[Discord] nothing selected to report")
        return False

    bag = float(scenario.bag_estimate)
    state_dir = result.directory.parent

    # Judged per pick, not on one shared figure. A tier-1 trip dropping 3,000
    # is news on a day the outright cheapest did not move, and a single
    # threshold silenced exactly that.
    reportable = [
        pick for pick in picks
        if not scenario.notify_quiet
        or should_alert(
            pick.itinerary.total_with_bags(bag),
            load_best(state_dir, pick.name),
            scenario.alert_threshold,
        )
    ]
    if not reportable:
        print("[Discord] no pick improved on its recorded best; staying quiet")
        return False

    embed = build_price_embed(
        scenario.name,
        reportable,
        load_best(state_dir, "cheapest"),
        bag,
        getattr(result, "coverage", None),
        load_last(state_dir, "cheapest"),
    )
    sent = post(webhook_url, [embed])
    if sent:
        for pick in reportable:
            save_best(
                state_dir,
                pick.itinerary.total_with_bags(bag),
                pick.itinerary.currency,
                pick.name,
            )
    return sent


if __name__ == "__main__":
    # Legacy entry point kept so the old workflow step does not hard-fail.
    if not os.getenv("DISCORD_WEBHOOK_URL"):
        print("[Discord] DISCORD_WEBHOOK_URL not set, skipping notification")
        sys.exit(0)
    print("[Discord] use `python -m src.cli sweep` — it notifies as part of the run")
