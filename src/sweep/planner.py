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

from dataclasses import dataclass, replace
from datetime import date, timedelta
from itertools import zip_longest

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

# What actually stops a sweep, measured across three cloud runs and twice read
# wrongly before the third one settled it.
#
#   11 Aug, 1 runner, 2 workers:  350 searches, zero timeouts, 86 min
#   20 Aug, 3 runners, 2 workers: 360 answered - 120 per runner
#   20 Aug, 1 runner,  2 workers: 120 answered
#
# The first two readings looked like a per-address quota, then like an aggregate
# rate limit. Both were wrong, and the third run is what shows it: one runner and
# three runners both answered 120 per runner, at a steady ten seconds a search
# with no slowdown at all before a hard cliff. Divide by the workers and it is
# **60 searches per browser session**, every time - each worker holds its own
# browser, so three runners simply meant six sessions rather than two.
#
# Nothing here is therefore a rate to stay under. `runner.PAGE_RECYCLE_EVERY` is
# the constant that matters: replace the browser before a session is spent.

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


def _leg_window(scenario: Scenario, leg_index: int, narrowed: bool = False) -> tuple[date, date]:
    """First and last date leg `leg_index` may depart.

    `narrowed` decides whether the trip's narrowing is one of the bounds. It is
    the difference between the two sweeps this app runs, and it used not to
    exist: the narrowing was always applied, so the moment one was saved every
    sweep became a narrowed sweep - including the nightly one whose whole job is
    to keep saying what the rest of the window costs. Measured on the committed
    japan-philippines trip on 24 Aug, the two nightly runs planned 48 searches
    against a window of 85, and nothing on the page said so.

    Off is the broad sweep and the exploration probe: the window, the stays, and
    nothing a decision has added since. On is `plan_final`, which prices the
    decision itself.

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

    Four things can narrow a leg, and this is the one place they meet:

      window        the horizon above, always
      focus         when you leave, propagated forward through the stay ranges
      return focus  when you fly home, propagated *backward* through them
      total_days    how long you are away, which bounds the two against each other

    They are intersected, never chosen between. That is what stops them
    contradicting: a leg's window can be narrowed to nothing by a combination
    that makes no sense, and `Scenario.validate` refuses those by name before a
    sweep is ever planned. Propagating backward needs `max_stay_after` for the
    early bound and `remaining_min_stay` for the late one - the longest and
    shortest a leg can be from the final one - and getting those two the wrong
    way round widens the plan silently instead of narrowing it, which is why
    they are named rather than inlined.

    `max(start, end)` keeps a degenerate window - one shorter than the stays it
    declares - planning its first date rather than nothing at all. A window that
    tight is rejected by `validate()`, but the planners are also called directly.
    """
    horizon = scenario.window_end + timedelta(days=RETURN_SLACK_DAYS)
    start = _focus_start(scenario, narrowed) + timedelta(
        days=scenario.earliest_departure(leg_index)
    )
    end = horizon - timedelta(days=scenario.remaining_min_stay(leg_index))

    if not narrowed:
        # The window and the stays are the whole of a broad plan. Returning here
        # rather than guarding each bound below keeps the three narrowing rules
        # in one block, where they can go on being read as the one place they
        # meet - which is what the rest of this docstring is about.
        return start, max(start, end)

    if scenario.focus_end is not None:
        # What a focused first leg can still reach, taking the longest stays.
        # Tighter than the horizon inside a focus and looser outside it, so the
        # binding bound is always the smaller of the two.
        end = min(end, scenario.focus_end + timedelta(days=scenario.max_stay_before(leg_index)))

    if scenario.return_focus_start is not None:
        # Backward from the day you fly home. The final leg is clamped to the
        # return window itself; both expressions collapse to exactly that for
        # it, because `max_stay_after` and `remaining_min_stay` are each zero
        # there - nothing has to happen after the last leg departs.
        start = max(
            start,
            scenario.return_focus_start - timedelta(days=scenario.max_stay_after(leg_index)),
        )
        end = min(
            end,
            scenario.return_focus_end - timedelta(days=scenario.remaining_min_stay(leg_index)),
        )

    if scenario.total_days is not None:
        low, high = scenario.total_days
        # A nights band is a statement about the first and last legs together,
        # so it only bounds a middle leg through whichever end is pinned. With a
        # focus set it says how late this leg can be and still leave room to get
        # home inside the band; with a return window set, how early.
        if scenario.focus_end is not None:
            end = min(
                end,
                scenario.focus_end
                + timedelta(days=high - scenario.remaining_min_stay(leg_index)),
            )
        if scenario.return_focus_start is not None:
            start = max(
                start,
                scenario.return_focus_start
                - timedelta(days=high - scenario.earliest_departure(leg_index)),
            )
        # The floor holds against the window too, not only against a focus: a
        # leg cannot be earlier than the soonest the first leg could go plus
        # whatever the band still requires after it.
        start = max(
            start,
            _focus_start(scenario, narrowed)
            + timedelta(days=max(0, low - scenario.max_stay_after(leg_index))),
        )

    return start, max(start, end)


def _focus_start(scenario: Scenario, narrowed: bool = False) -> date:
    """Where the first leg may start departing: the focus, or the window."""
    if narrowed and scenario.focus_start is not None:
        return scenario.focus_start
    return scenario.window_start


def _deal(searches: list[LegSearch]) -> list[LegSearch]:
    """Round-robin the plan across routes: one date from each, then the next.

    The order a plan is emitted in only matters when the plan does not finish -
    and then it decides whether what did finish is worth anything. Emitted leg
    by leg, a run cut short leaves each route's *tail* unasked, and the legs end
    up on date bands that cannot reach each other. Measured 21 Aug: a run that
    answered 37 of 66 searches found 357 real flights and `combine_all` returned
    **no complete trip at all**, because the return leg was priced 22-28 January
    and the trip it had to complete needed 2-7 February.

    Dealt this way, a run that answers half its plan holds half of *every*
    route's dates and still chains. It is the rule `shard_of` already applies
    across runners, for the same reason and in the same words: thin the date
    grid evenly rather than delete part of the trip from it.

    A prefix is each route's *earlier* dates rather than a spread of its window,
    which is honest but worth knowing - the legs' windows are offset by the
    minimum stays, so early dates of every leg do reach each other. `coverage`
    is what reports how much of the window was really seen.
    """
    by_route: dict[tuple[int, str, str], list[LegSearch]] = {}
    for search in searches:
        key = (search.leg_index, search.origin, search.destination)
        by_route.setdefault(key, []).append(search)

    return [
        search
        for row in zip_longest(*by_route.values())
        for search in row
        if search is not None
    ]


def _searches_for(scenario: Scenario, dates_by_leg: dict[int, list[date]]) -> list[LegSearch]:
    """Every route pair of each leg, on that leg's dates, deduplicated and dealt.

    Walks `leg_pools` rather than consecutive `airport_pools`, so a stop pinned
    to arrive at one airport and leave from another is searched only the way it
    is actually going to be flown. The two are identical on any trip that has
    pinned nothing, which is every trip until a probe has settled the crossing.
    """
    pools = scenario.leg_pools
    searches: list[LegSearch] = []
    for leg_index, dates in dates_by_leg.items():
        origins, destinations = pools[leg_index]
        for depart in dates:
            for origin in origins:
                for destination in destinations:
                    # Pools may overlap once any airport can appear anywhere - a
                    # trip returning to its own departure list would otherwise
                    # generate PRG->PRG, which no site will price.
                    if origin == destination:
                        continue
                    searches.append(LegSearch(origin, destination, depart, None, leg_index))

    # Different date arithmetic can land on the same search; run each once.
    return _deal(list(dict.fromkeys(searches)))


def plan_searches(scenario: Scenario) -> list[LegSearch]:
    """Every search needed to price `scenario`'s whole window, deduplicated.

    The broad sweep, and broad whatever the trip has been narrowed to since.
    That independence is the point: the narrowed sweep answers "is the trip I
    have chosen getting cheaper", and only this one can still answer "is there a
    better week out there" - which is the question a narrowing was decided
    against in the first place, and the one that goes stale fastest.
    """
    step = scenario.step_days
    return _searches_for(
        scenario,
        {
            leg_index: _date_range(*_leg_window(scenario, leg_index), step)
            for leg_index in range(scenario.leg_count)
        },
    )


def plan_final(scenario: Scenario) -> list[LegSearch]:
    """Every search needed to price the decision, rather than the window.

    The same arithmetic as `plan_searches` with the trip's narrowing added to
    the bounds, which is what `plan_searches` itself did until the two were
    separated. On the real trip it is 24 searches against 168 - cheap enough to
    run three times a day against a site that answers about 120 per address,
    which is exactly what makes it worth having as its own mode.

    Refuses a trip with nothing narrowed rather than quietly returning the broad
    plan. A run filed as `final` that had in fact priced the whole window would
    sit in the narrowed trend line, on the wrong axis, for as long as it stayed
    on disk - and it would have cost an hour to produce the answer the 02:00
    sweep already had.
    """
    if not _has_narrowing(scenario):
        raise ValueError(
            "There is nothing to narrow to yet. Set a departure window, a return "
            "window or a nights band under 'Narrow it down' first - without one, "
            "a final sweep would price the whole window the broad sweep already does."
        )
    step = scenario.step_days
    return _searches_for(
        scenario,
        {
            leg_index: _date_range(*_leg_window(scenario, leg_index, narrowed=True), step)
            for leg_index in range(scenario.leg_count)
        },
    )


def _has_narrowing(scenario: Scenario) -> bool:
    """Whether anything on the trip says less than its whole window.

    Any one of the three is enough. They are independent constraints - a trip
    can pin when it flies home without having chosen when it leaves - and
    requiring a departure window would refuse exactly that case.
    """
    return bool(
        (scenario.focus_start and scenario.focus_end)
        or (scenario.return_focus_start and scenario.return_focus_end)
        or scenario.total_days
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

    With `probe_both_orders` it also samples the stops the other way round, so
    "would Philippines first be cheaper?" can be answered for tens of searches
    instead of the several hundred a second sweep would cost. Only here: the
    sweep prices the order the stops are actually listed in.
    """
    searches = _explore_pass(scenario, dates_per_leg)
    for other in reordered(scenario):
        searches += _explore_pass(other, dates_per_leg)

    # Deduplicated on the search itself rather than on the whole `LegSearch`.
    # The two orders overlap, and a record carries the leg index it came from -
    # VIE->MNL is leg 0 one way round and MNL->VIE is leg 2 the other - so
    # `dict.fromkeys` over the frozen dataclass would let one route on one date
    # through twice under two indices. That is a real search against a site
    # that answers about 120 of them per runner.
    unique: dict[tuple[str, str, date], LegSearch] = {}
    for search in searches:
        unique.setdefault((search.origin, search.destination, search.depart_date), search)
    return list(unique.values())


def _explore_pass(scenario: Scenario, dates_per_leg: int) -> list[LegSearch]:
    """One order's worth of reconnaissance: every route on a few spread dates."""
    return _searches_for(
        scenario,
        {
            leg_index: _spread(*_leg_window(scenario, leg_index), dates_per_leg)
            for leg_index in range(scenario.leg_count)
        },
    )


def reordered(scenario: Scenario) -> list[Scenario]:
    """The other stop orders worth sampling, or nothing.

    Only the full reverse today, and only for a trip that asked. Reversing is
    enough for the two-stop trip this exists for; every permutation of four
    stops is 24 orders and a probe that costs more than the sweep it was meant
    to make unnecessary.

    Reversing carries each stop's stay range with it, which is the point: a
    Philippines-first trip spends the Philippines stay first, so its later legs
    open on different dates.
    """
    if not scenario.probe_both_orders or len(scenario.stops) < 2:
        return []
    return [replace(scenario, stops=list(reversed(scenario.stops)))]


def plan_watch(scenario: Scenario) -> list[LegSearch]:
    """Every airport pair of every leg, on the days each preference pinned.

    The cheap counterpart to `plan_searches`. A preference already knows which
    trip it is following, so it prices that trip's own days rather than deriving
    later legs from the stay ranges: on the Japan/Philippines trip that is 15
    searches a preference against 75, which is the whole difference between a
    check that can run every four hours and one that cannot run at all.
    pelikan.cz answers about 120 searches per runner before it stops answering.

    Each pinned date brings `slack_days` either side of it. That is what lets a
    follow answer "the same trip two days later is two thousand cheaper" instead
    of only "your Tuesday has not moved" - the question a decision is actually
    waiting on. It is also the whole cost of the mode: the slack multiplies
    across legs and airport pairs, which is why `Scenario` bounds it and
    `web.app` refuses on the planned count.

    Airports are *not* pinned, only dates. The preference says which days to
    look at; which airports win on those days is exactly what is being followed,
    so a check can still find that Frankfurt undercut Vienna overnight, or that
    arriving Haneda and leaving Kansai beat both.

    Dates are pooled across preferences before the pairs are built, so two
    preferences a week apart share every search their slack windows overlap on,
    and one ten days apart shares the search where one's second leg lands on the
    other's first. `_searches_for` deduplicates the rest.

    Leg watches are added on top, one search each: they name a route and a date
    outright rather than a trip, so there is nothing to expand. A route the trip
    does not fly is still planned - picking freely is the point of them - and
    the deduplication below means a leg a preference already covers costs
    nothing extra, which is what makes following a preference's own legs free.
    """
    dates_by_leg: dict[int, list[date]] = {}
    for preference in scenario.preferences:
        for leg_index, depart in enumerate(preference.depart_dates):
            for offset in range(-preference.slack_days, preference.slack_days + 1):
                dates_by_leg.setdefault(leg_index, []).append(depart + timedelta(days=offset))
    searches = _searches_for(scenario, dates_by_leg)

    seen = {(s.origin, s.destination, s.depart_date) for s in searches}
    for leg in scenario.leg_watches:
        key = (leg.origin, leg.destination, leg.depart_date)
        if key in seen:
            continue
        seen.add(key)
        searches.append(
            LegSearch(
                leg.origin,
                leg.destination,
                leg.depart_date,
                None,
                # Reporting only - it groups the dry-run breakdown and the
                # per-leg estimate. A route the trip does not fly has no
                # position in the chain, and 0 is a label rather than a claim.
                _chain_position(scenario, leg.origin, leg.destination),
            )
        )
    return searches


def _chain_position(scenario: Scenario, origin: str, destination: str) -> int:
    """Which leg of the trip this route belongs to, or 0 when it belongs to none."""
    for index, (origins, destinations) in enumerate(scenario.leg_pools):
        if origin in origins and destination in destinations:
            return index
    return 0


# Which planner each mode runs. One table, because there used to be three
# identical copies of it - in `web/app.py`, in `sweep/runner.py` and in `cli.py`
# - and a mode added to two of them is a mode the estimate prices one way and
# the sweep runs another. `runner.MODES` names them; this decides what they do.
PLANS = {
    "sweep": plan_searches,
    "explore": plan_exploration,
    "watch": plan_watch,
    "final": plan_final,
}


def planned_routes(scenario: Scenario) -> set[tuple[str, str]]:
    """The distinct origin-destination pairs this trip requires, ignoring dates.

    What a sweep has to cover before its result may be compared with another
    sweep of the same trip. Walks `leg_pools` exactly as `plan_searches` does -
    including the self-pair skip - so the two cannot disagree about which routes
    a trip implies.
    """
    return {
        (origin, destination)
        for origins, destinations in scenario.leg_pools
        for origin in origins
        for destination in destinations
        if origin != destination
    }


# How many searches one runner may be handed.
#
# Measured four ways on 20 Aug and wrong three times before that: pelikan.cz
# answers about **120** searches from one client and then stops answering at
# all - a steady ten seconds a search right through [120/483], then half an
# hour of silence. Replacing the whole browser changes nothing, so it is not a
# cookie and not a session; what is constant within a runner and differs
# between runners is the address.
#
# 100 rather than 120 is the margin. A shard that goes over does not fail
# loudly - it simply stops answering, and the merged sweep has a hole in its
# date grid that only `coverage` can see.
SEARCHES_PER_RUNNER = 100


def shards_for(planned: int) -> int:
    """How many runners a plan of this size has to be split across.

    Replaces a bare `DEFAULT_SHARDS: 5` in the workflow. The 5 was right, and
    was derived exactly this way - 483 planned over ~100 a runner - but it was
    written down as a number rather than as the rule, so it stayed 5 when
    pinning the Japan crossing took that trip to 66 searches, and it applied to
    every trip in the matrix whatever its size.

    Never zero: a matrix with no jobs is a night that reports success and
    sweeps nothing, which is the shape of failure this repo has already had
    thirteen times in a row.
    """
    return max(1, -(-planned // SEARCHES_PER_RUNNER))


def shard_of(searches: list[LegSearch], index: int, count: int) -> list[LegSearch]:
    """This runner's share of the plan: every `count`-th date of every route.

    Shards multiply *sessions*, which is why three runners answered three times
    as much as one before `runner.PAGE_RECYCLE_EVERY` existed - each runner holds
    two browsers, and the site stops answering a browser after about sixty
    searches. Recycling the browser gets the same thing from one runner, so the
    shard count is now a wall-clock choice rather than a way to buy searches.

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
