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

# Wall-clock seconds one worker spends per search, including the politeness
# delay. Measured, not reasoned: the cloud sweep of 11 Aug 03:24 ran 350
# searches in 5,156s on two workers with zero timeouts, which is 29.5s each.
#
# It was 19.0, from "~15s to first results plus the 4s delay" - a figure taken
# from a single fast search and never checked against a whole run. That made
# every estimate 55% optimistic: a deep sweep was advertised at ~97 minutes and
# takes ~146. The cost was not cosmetic. `timeout-minutes: 90` in the sweep
# workflow was set from it, so five consecutive nightly runs swept cleanly for
# 90 minutes and were killed before they could commit a single leg.
#
# A sweep that is being throttled is far slower still - a timed-out search costs
# 120s - but that is a broken sweep, and the estimate should describe a working
# one. `is_comparable` and the circuit breaker are what handle the other case.
SECONDS_PER_SEARCH = 29.5

# Searches per minute this client may run *in total*, across every machine at
# once, before pelikan.cz stops answering.
#
# Measured twice, and the second measurement is the one that matters:
#
# - 11 Aug, one cloud runner: 350 searches over 86 min = 4.1/min, zero timeouts.
# - 20 Aug, three cloud runners in parallel: 360 searches over 21 min = 17/min.
#   All three were cut off at search 120, within 90 seconds of one another, on
#   three different runner addresses.
#
# Three separate addresses hitting the same wall at the same moment is not a
# per-address limit, which is what sharding was introduced on the assumption of.
# It buys wall clock only up to this ceiling - and one runner at two workers with
# a four-second delay already sits at it. So the default shard count is 1, and
# raising it means lowering the rate per shard by the same factor for no net
# gain. The machinery is kept because it is the right tool if the site's
# behaviour changes, and because a lost shard thins the date grid evenly rather
# than truncating the sweep.
SAFE_SEARCHES_PER_MINUTE = 4.1

# The site substitutes nearby dates, so the final leg is searched past the end
# of the window - otherwise the latest valid itineraries are never seen.
RETURN_SLACK_DAYS = 4

# Dates sampled per leg by the exploration pass. Three is the smallest number
# that can distinguish a bad route from a bad date: one reading is an anecdote,
# two cannot outvote each other. It is deliberately not tunable from the UI -
# the probe's whole value is being cheap enough to run without deciding
# anything first.
EXPLORE_DATES_PER_LEG = 3


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


def _spread(start: date, end: date, count: int) -> list[date]:
    """`count` dates spanning [start, end], both ends included.

    Both ends on purpose: three dates a day apart would price one week and say
    nothing about the other nine, which is the opposite of what a probe is for.
    """
    if end < start:
        return []
    days = (end - start).days
    if count <= 1 or days == 0:
        return [start]
    offsets = {round(index * days / (count - 1)) for index in range(count)}
    return sorted(start + timedelta(days=offset) for offset in offsets)


def _leg_window(scenario: Scenario, leg_index: int) -> tuple[date, date]:
    """First and last date leg `leg_index` may depart.

    Shared by both planners for the same reason `airport_pools` is shared by the
    planner and the combiner: two copies of this arithmetic could disagree about
    when a leg is allowed to leave, and a probe that searched dates the real
    sweep never would is measuring a different trip.

    Every leg is bounded by the same horizon - the last date the trip may fly at
    all - minus the minimum stays that still have to happen after it. Only the
    final leg used to get that treatment, so the earlier legs were searched right
    up to `window_end` on dates from which no searched final leg was reachable.
    On the real trip that was 132 of 615 searches, and two of the exploration
    probe's three first-leg samples: it priced 22 January and 8 February, and
    neither could complete a trip, so the probe judged three airports on one
    reading instead of three.

    `max(start, end)` keeps a degenerate window - one shorter than the stays it
    declares - planning its first date rather than nothing at all. A window that
    tight is rejected by `validate()`, but the planners are also called directly.
    """
    horizon = scenario.window_end + timedelta(days=RETURN_SLACK_DAYS)
    start = _focus_start(scenario) + timedelta(days=scenario.earliest_departure(leg_index))
    end = horizon - timedelta(days=scenario.remaining_min_stay(leg_index))
    if scenario.focus_end is not None:
        # What a focused first leg can still reach, taking the longest stays.
        # Tighter than the horizon inside a focus and looser outside it, so the
        # binding bound is always the smaller of the two.
        end = min(end, scenario.focus_end + timedelta(days=scenario.max_stay_before(leg_index)))
    return start, max(start, end)


def _focus_start(scenario: Scenario) -> date:
    """Where the first leg may start departing: the focus, or the window."""
    return scenario.focus_start or scenario.window_start


def _searches_for(scenario: Scenario, dates_by_leg: dict[int, list[date]]) -> list[LegSearch]:
    """Every route pair of each leg, on that leg's dates, deduplicated."""
    pools = scenario.airport_pools
    searches: list[LegSearch] = []
    for leg_index, dates in dates_by_leg.items():
        for depart in dates:
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


def plan_searches(scenario: Scenario) -> list[LegSearch]:
    """Every search needed to evaluate `scenario`, deduplicated."""
    step = scenario.step_days
    return _searches_for(
        scenario,
        {
            leg_index: _date_range(*_leg_window(scenario, leg_index), step)
            for leg_index in range(scenario.leg_count)
        },
    )


def plan_exploration(
    scenario: Scenario, dates_per_leg: int = EXPLORE_DATES_PER_LEG
) -> list[LegSearch]:
    """A reconnaissance pass: every route, on a handful of spread-out dates.

    The point is to find out which airports are worth pricing properly before
    committing to an hour and a half of doing it. A trip with three origins,
    three Japanese airports and two Philippine ones is 21 routes: 63 searches
    here against 615 for a deep sweep of the same trip.

    Depth is deliberately ignored. Depth decides how finely a trip is priced;
    this decides which airports deserve to be priced at all, and sampling that
    question more finely on a `deep` trip would make the cheap pass expensive.
    """
    return _searches_for(
        scenario,
        {
            leg_index: _spread(*_leg_window(scenario, leg_index), dates_per_leg)
            for leg_index in range(scenario.leg_count)
        },
    )


def planned_routes(scenario: Scenario) -> set[tuple[str, str]]:
    """The distinct origin-destination pairs this trip requires, ignoring dates.

    What a sweep has to cover before its result may be compared with another
    sweep of the same trip. Walks `airport_pools` exactly as `plan_searches`
    does - including the self-pair skip - so the two cannot disagree about
    which routes a trip implies.
    """
    pools = scenario.airport_pools
    return {
        (origin, destination)
        for index in range(len(pools) - 1)
        for origin in pools[index]
        for destination in pools[index + 1]
        if origin != destination
    }


def shard_of(searches: list[LegSearch], index: int, count: int) -> list[LegSearch]:
    """This runner's share of the plan: every `count`-th date of every route.

    **Read `SAFE_SEARCHES_PER_MINUTE` before raising the shard count.** Three
    shards on three separate runner addresses were each cut off at search 120,
    within 90 seconds of one another, so the site limits this client as a whole
    and not per address. Shards buy wall clock only while their combined rate
    stays under what one runner has already proven.

    Dealt per route rather than off the top of the list. `searches[index::count]`
    looks equivalent and is not: `_searches_for` emits routes as its innermost
    loop, so with nine routes on the first leg and three shards, every shard got
    the *same* three routes on every date. Each runner would then own a few
    routes outright - and a runner that gets throttled would delete those routes
    from the merged sweep entirely, which reads downstream as a dead route
    rather than as a lost shard.

    Counting within each route instead gives every shard roughly `1/count` of
    every route's dates. A shard lost to throttling then thins the date grid
    evenly, the merged sweep still chains complete itineraries, and `coverage`
    reports the shortfall for what it is.

    The shards partition the plan exactly - no search dropped, none run twice -
    which is what lets the merge report coverage against the whole plan.
    """
    if count < 1:
        raise ValueError(f"a plan cannot be split into {count} shards")
    if not 0 <= index < count:
        raise ValueError(f"shard {index} is not one of the {count} shards 0..{count - 1}")

    dealt: dict[tuple[int, str, str], int] = {}
    mine: list[LegSearch] = []
    for search in searches:
        key = (search.leg_index, search.origin, search.destination)
        position = dealt.get(key, 0)
        dealt[key] = position + 1
        if position % count == index:
            mine.append(search)
    return mine


def estimate_minutes(searches: list[LegSearch], workers: int = 2) -> float:
    """Wall-clock estimate for running `searches` across `workers` browsers."""
    if not searches:
        return 0.0
    return round(len(searches) * SECONDS_PER_SEARCH / max(1, workers) / 60, 1)
