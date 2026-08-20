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
COLOR_ALERT = 0xE12D39  # palette red500


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


def load_best(directory: Path | str, name: str = "cheapest") -> float | None:
    """Best total previously recorded for one pick, if any."""
    entry = _read_state(directory).get(name)
    if not entry:
        return None
    try:
        return float(entry["best_total"])
    except (ValueError, KeyError, TypeError):
        return None


def save_best(
    directory: Path | str, best_total: float, currency: str, name: str = "cheapest"
) -> None:
    """Record a new best for one pick, but only when it really is one.

    This used to be called unconditionally whenever an alert was sent. With
    `alert_threshold` set, an alert fires on any total under the threshold -
    including one *worse* than the recorded best - and the recorded best then
    walked upward, quietly destroying the "only alert on genuine improvement"
    guarantee for every later run.

    Picks are recorded separately because they improve separately: a tier-1
    trip dropping 3,000 CZK is news even on a day Frankfurt did not move, and
    one shared figure hid exactly that.
    """
    directory = Path(directory)
    state = _read_state(directory)
    previous = load_best(directory, name)
    if previous is not None and best_total >= previous:
        return
    state[name] = {
        "best_total": best_total,
        "currency": currency,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
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


def build_price_embed(
    scenario_name: str,
    picks: list,
    previous_best: float | None = None,
    bag_estimate: float = 0.0,
    coverage: float | None = None,
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

    description = f"Best total: **{headline:,.0f}**"
    if previous_best is not None:
        delta = previous_best - headline
        if delta > 0:
            description += f" — down {delta:,.0f} from {previous_best:,.0f}"
    if coverage is not None and coverage < 1.0:
        description += (
            f"\n⚠️ Only **{coverage:.0%}** of the planned searches were answered, "
            "so a cheaper trip may simply not have been seen."
        )

    return {
        "title": f"✈️ {scenario_name}",
        "description": description,
        "color": COLOR_GOOD,
        "fields": fields,
        "timestamp": datetime.now(UTC).isoformat(),
        "footer": {"text": "Flight scenario watcher"},
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

    previous_best = load_best(state_dir, "cheapest")
    embed = build_price_embed(
        scenario.name, reportable, previous_best, bag, getattr(result, "coverage", None)
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
