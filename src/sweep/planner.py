"""Turn a Scenario into the concrete list of searches a sweep must run.

Pure and browser-free, so the combinatorics can be checked in tests and the UI
can show a cost estimate before anyone commits to a 45-minute sweep.

The multi-city trip is planned as three independent leg groups rather than as
whole itineraries. That keeps cost additive (~700 searches) instead of
multiplicative (every date combination), because each leg is searched once and
then reused across every itinerary the combiner builds from it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..scenario import Scenario

# Wall-clock seconds for one search: ~15s to first results plus the 4s
# politeness delay. Under sweep conditions a sizeable share instead run to the
# 120s timeout and are retried once, so treat this as a floor, not an average.
SECONDS_PER_SEARCH = 19.0

# The site substitutes nearby dates, so the final leg is searched past the end
# of the window - otherwise the latest valid itineraries are never seen.
RETURN_SLACK_DAYS = 4


@dataclass(frozen=True)
class LegSearch:
    origin: str
    destination: str
    depart_date: date
    ret_date: date | None
    leg_index: int  # 0 = Europe->Japan, 1 = Japan->Philippines, 2 = ->Europe


def _date_range(start: date, end: date, step_days: int) -> list[date]:
    if end < start:
        return []
    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(0, days + 1, step_days)]


def plan_searches(scenario: Scenario) -> list[LegSearch]:
    """Every search needed to evaluate `scenario`, deduplicated."""
    if scenario.trip_type == "round_trip":
        searches = _plan_round_trip(scenario)
    else:
        searches = _plan_multi_city(scenario)

    # Different date arithmetic can land on the same search; run each once.
    return list(dict.fromkeys(searches))


def _plan_round_trip(scenario: Scenario) -> list[LegSearch]:
    step = scenario.step_days
    length_min, length_max = scenario.trip_length_days
    # Step through trip lengths too, so "quick" does not explode on a wide range.
    lengths = list(range(length_min, length_max + 1, max(1, step)))
    if length_max not in lengths:
        lengths.append(length_max)

    searches = []
    for depart in _date_range(scenario.window_start, scenario.window_end, step):
        for length in lengths:
            for origin in scenario.origins:
                for destination in scenario.japan_airports:
                    searches.append(
                        LegSearch(origin, destination, depart, depart + timedelta(days=length), 0)
                    )
    return searches


def _plan_multi_city(scenario: Scenario) -> list[LegSearch]:
    step = scenario.step_days
    japan_min = scenario.japan_stay_days[0]
    ph_min = scenario.ph_stay_days[0]

    searches: list[LegSearch] = []

    # Leg A: Europe -> Japan, anywhere in the window.
    for depart in _date_range(scenario.window_start, scenario.window_end, step):
        for origin in scenario.origins:
            for destination in scenario.japan_airports:
                searches.append(LegSearch(origin, destination, depart, None, 0))

    # Leg B: Japan -> Philippines, no earlier than the shortest Japan stay.
    leg_b_start = scenario.window_start + timedelta(days=japan_min)
    for depart in _date_range(leg_b_start, scenario.window_end, step):
        for origin in scenario.japan_airports:
            for destination in scenario.ph_airports:
                searches.append(LegSearch(origin, destination, depart, None, 1))

    # Leg C: Philippines -> Europe, after both minimum stays, with slack past
    # the window end so the latest itineraries are still reachable.
    leg_c_start = scenario.window_start + timedelta(days=japan_min + ph_min)
    leg_c_end = scenario.window_end + timedelta(days=RETURN_SLACK_DAYS)
    for depart in _date_range(leg_c_start, leg_c_end, step):
        for origin in scenario.ph_airports:
            for destination in scenario.origins:
                searches.append(LegSearch(origin, destination, depart, None, 2))

    return searches


def estimate_minutes(searches: list[LegSearch], workers: int = 2) -> float:
    """Wall-clock estimate for running `searches` across `workers` browsers."""
    if not searches:
        return 0.0
    return round(len(searches) * SECONDS_PER_SEARCH / max(1, workers) / 60, 1)
