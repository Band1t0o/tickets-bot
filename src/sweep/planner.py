"""Turn a Scenario into the concrete list of searches a sweep must run.

Pure and browser-free, so the combinatorics can be checked in tests and the UI
can show a cost estimate before anyone commits to a 45-minute sweep.

Legs are planned as independent groups rather than as whole itineraries. That
keeps cost additive instead of multiplicative - each leg is searched once and
then reused across every itinerary the combiner builds from it. A three-leg
trip is ~200 searches this way and tens of thousands the other.

Every leg, including the one home, comes out of the same loop over
`scenario.airport_pools`. The previous version had a hand-written block per leg
plus a separate round-trip branch that emitted outbound searches only, which no
combiner could ever close into a trip.
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
    # Always None today: every leg is searched as a one-way so it can be reused
    # across itineraries. Kept because a round-trip search prices a through-fare
    # the leg chain cannot express - measured 33% cheaper on PRG<->NRT but 19%
    # worse on FRA<->NRT + NRT<->MNL, so it belongs as an extra candidate rather
    # than as a replacement.
    ret_date: date | None
    # Position in the chain: 0 is the first hop out, the last is the way home.
    leg_index: int


def _date_range(start: date, end: date, step_days: int) -> list[date]:
    if end < start:
        return []
    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(0, days + 1, step_days)]


def plan_searches(scenario: Scenario) -> list[LegSearch]:
    """Every search needed to evaluate `scenario`, deduplicated."""
    pools = scenario.airport_pools
    step = scenario.step_days
    final_leg = len(pools) - 2

    searches: list[LegSearch] = []
    for leg_index in range(len(pools) - 1):
        start = scenario.window_start + timedelta(days=scenario.earliest_departure(leg_index))
        end = scenario.window_end
        if leg_index == final_leg:
            end += timedelta(days=RETURN_SLACK_DAYS)

        for depart in _date_range(start, end, step):
            for origin in pools[leg_index]:
                for destination in pools[leg_index + 1]:
                    # Pools may overlap once any airport can appear anywhere - a
                    # trip returning to its own departure list would otherwise
                    # generate PRG->PRG, which no site will price.
                    if origin == destination:
                        continue
                    searches.append(LegSearch(origin, destination, depart, None, leg_index))

    # Different date arithmetic can land on the same search; run each once.
    return list(dict.fromkeys(searches))


def estimate_minutes(searches: list[LegSearch], workers: int = 2) -> float:
    """Wall-clock estimate for running `searches` across `workers` browsers."""
    if not searches:
        return 0.0
    return round(len(searches) * SECONDS_PER_SEARCH / max(1, workers) / 60, 1)
