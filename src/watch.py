"""Track a handful of pinned candidate trips, and say when one really falls.

A sweep answers "what is this window worth?" once a day, at hundreds of
searches. Once it has shown which departure dates are cheap, the question
changes to "are *those* moving?" - and re-sweeping the window to answer it is
the load pelikan.cz throttles this client for.

So a watch prices the candidates instead: the exact leg dates of the trip that
won each chosen day, every airport pair still priced on them. That is 21
searches a candidate on the Japan/Philippines trip against 483 for a deep
sweep, which is what lets it run every four hours.

Deliberately modelled on `src/probe.py`, down to the append-only
`observations.jsonl`: it is a proven shape here, it survives a killed run, and
two workflows appending to different files never conflict on a rebase.

Pure apart from the file it appends to - nothing here launches a browser or
posts anything. `src/cli.py` runs the search, this records what it found, and
`src/notify_discord.py` decides how to say it.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .combine import combine_all
from .models import Leg
from .notify_discord import load_best, save_best
from .scenario import Scenario
from .sweep.runner import MIN_COMPARABLE_LEGS_PER_SEARCH, legs_per_search_of

DEFAULT_WATCH_DIR = Path("data/watch")

# How far a candidate must fall before it is worth a message.
#
# Not a taste setting. The probe established that this site moves fares by a
# few crowns at a time - six on a 3,850 fare - and that counting those made the
# steadiest of its three routes report the *highest* volatility of them all. A
# watch that ran every four hours and pinged on any change at all would send
# six messages a day about rounding, and the one that mattered would be lost in
# them. See `probe.MEANINGFUL_MOVE_PCT`, which exists for the same reason.
MEANINGFUL_DROP_PCT = 1.0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _comparable(status: dict) -> bool:
    """Whether this run looked at enough to be believed.

    Legs per search, not error count: the sweep that was failing most had
    `error_count: 0` and 2.9 legs per search where a healthy one returns ~10.
    A watch is small enough that one refused search is a large share of it, so
    coverage counts here too.
    """
    per_search = legs_per_search_of(status)
    if per_search is None or per_search < MIN_COMPARABLE_LEGS_PER_SEARCH:
        return False
    coverage = status.get("coverage")
    return coverage is None or coverage >= 1.0


def record_observations(
    legs: list[Leg],
    scenario: Scenario,
    status: dict,
    directory: Path | str = DEFAULT_WATCH_DIR,
) -> list[dict]:
    """Append one row per watched candidate and return them.

    The chain is rebuilt rather than read off the pinned dates, because pinning
    dates is not pinning airports: the whole point of still pricing every
    airport pair is that Frankfurt may have undercut Vienna overnight, or that
    arriving Haneda and leaving Kansai now beats both. `found_dates` and `route`
    therefore record what actually won, which can differ from what was pinned.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    combined = combine_all(legs, scenario, limit=None)
    bag = float(scenario.bag_estimate)
    comparable = _comparable(status)
    ts = _now()

    rows: list[dict] = []
    for watch in scenario.watches:
        best = combined.best_by_date.get(watch.key)
        rows.append(
            {
                "ts": ts,
                "scenario_id": scenario.id,
                "depart_date": watch.key,
                "pinned_dates": [d.isoformat() for d in watch.depart_dates],
                "found_dates": (
                    [leg.depart_date.isoformat() for leg in best.legs] if best else None
                ),
                "route": best.route if best else None,
                # None, never 0. A candidate that found nothing means the search
                # broke or the site refused - not a free flight - and a zero
                # averaged into the series would put the cheapest trip you ever
                # saw at the bottom of the chart.
                "total": best.total_price if best else None,
                "total_with_bags": best.total_with_bags(bag) if best else None,
                "currency": best.currency if best else scenario.currency,
                "has_overland": best.has_overland if best else False,
                # Travels with the price, so a starved run can be dimmed rather
                # than plotted as though it were a measurement.
                "coverage": status.get("coverage"),
                "legs_per_search": legs_per_search_of(status),
                "comparable": comparable,
            }
        )

    with (directory / "observations.jsonl").open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return rows


def watch_report(directory: Path | str = DEFAULT_WATCH_DIR) -> dict:
    """Each candidate's series and how far it has moved.

    Points with no price are dropped - there is nothing to draw - but points
    from a starved run are kept and flagged, because the gap in the record is
    itself worth seeing. Only trustworthy points are counted into the summary
    figures: averaging a refused run into the move charts scraper health, which
    is exactly what the history chart did across four sweeps running 2.9 to 9.7
    legs per search.
    """
    path = Path(directory) / "observations.jsonl"
    if not path.exists():
        return {"candidates": {}}

    series: dict[str, list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("total") is None:
            continue
        series.setdefault(row["depart_date"], []).append(row)

    candidates: dict[str, dict] = {}
    for key, rows in series.items():
        trusted = [row for row in rows if row.get("comparable")]
        summary = {
            "depart_date": key,
            "series": [
                {
                    "ts": row["ts"],
                    "total": row["total"],
                    "total_with_bags": row["total_with_bags"],
                    "comparable": bool(row.get("comparable")),
                }
                for row in rows
            ],
            "currency": rows[-1]["currency"],
            "route": (trusted or rows)[-1]["route"],
            "has_overland": bool((trusted or rows)[-1].get("has_overland")),
            "observations": len(rows),
            "first": None,
            "latest": None,
            "latest_with_bags": None,
            "net_change": 0,
            "net_change_pct": 0.0,
            "low": None,
            "high": None,
        }
        if trusted:
            prices = [row["total"] for row in trusted]
            summary["first"] = prices[0]
            summary["latest"] = prices[-1]
            summary["latest_with_bags"] = trusted[-1]["total_with_bags"]
            summary["low"] = min(prices)
            summary["high"] = max(prices)
            summary["net_change"] = round(prices[-1] - prices[0], 2)
            if prices[0]:
                summary["net_change_pct"] = round(
                    (prices[-1] - prices[0]) / prices[0] * 100, 1
                )
        candidates[key] = summary

    return {"candidates": candidates}


def _alert_name(key: str) -> str:
    """Namespaced, so a watch never overwrites the sweep's recorded best.

    They live in different files today, but they are the same shape and the
    same helpers read them, and a collision would silence the sweep rather than
    fail loudly.
    """
    return f"watch:{key}"


def drops(
    report: dict,
    scenario: Scenario,
    directory: Path | str = DEFAULT_WATCH_DIR,
    min_drop_pct: float = MEANINGFUL_DROP_PCT,
) -> list[dict]:
    """Candidates that have genuinely fallen since anything was last said.

    Measured against the last level actually *reported*, not against the last
    observation. That distinction is what stops a slow slide staying silent
    forever: five falls of 0.4% each never trip a per-step threshold, but the
    fifth one is 2% below the level you were last told about, so it is news.

    Recording the level only on a report is also what makes this idempotent -
    running it twice on the same observation reports once.

    Starved runs are skipped entirely rather than compared. A run the site
    refused most of reports a cheapest total that is an artefact, and an
    artefact that happens to be low would fire an alert about a trip nobody can
    buy.
    """
    directory = Path(directory)
    added = {watch.key: watch.added_price for watch in scenario.watches}
    threshold = scenario.alert_threshold

    found: list[dict] = []
    for key, candidate in sorted(report.get("candidates", {}).items()):
        total = candidate.get("latest")
        if total is None:
            continue
        # The price it was picked at seeds the level, so the very first run can
        # be news: you add a candidate at 30,000 and the watch finds 26,000 an
        # hour later, which is precisely the message worth having.
        previous = load_best(directory, _alert_name(key))
        if previous is None:
            previous = added.get(key)
        if previous is None:
            # Nothing to compare against. Record where it started so the next
            # run has a level, and say nothing.
            save_best(directory, total, candidate["currency"], _alert_name(key))
            continue

        under_threshold = threshold is not None and total <= threshold
        fallen = previous - total
        far_enough = previous > 0 and (fallen / previous * 100) >= min_drop_pct
        if not (under_threshold or far_enough):
            continue

        found.append(
            {
                "depart_date": key,
                "route": candidate["route"],
                "total": total,
                "total_with_bags": candidate["latest_with_bags"],
                "currency": candidate["currency"],
                "previous_best": previous,
                "drop": round(fallen, 2),
                "drop_pct": round(fallen / previous * 100, 1) if previous else 0.0,
                "has_overland": candidate["has_overland"],
            }
        )
        # `save_best` refuses anything that is not an improvement, so an alert
        # fired by `alert_threshold` on a total worse than the recorded best
        # cannot walk the level upward - the bug that quietly destroyed the
        # "only on genuine improvement" guarantee for the sweep alerts once.
        save_best(directory, total, candidate["currency"], _alert_name(key))

    return found
