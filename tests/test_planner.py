"""Tests for turning a Scenario into a concrete list of searches."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from src.scenario import Stop
from src.sweep.planner import (
    RETURN_SLACK_DAYS,
    SEARCHES_PER_RUNNER,
    estimate_minutes,
    plan_exploration,
    plan_final,
    plan_searches,
    planned_routes,
    shard_of,
    shards_for,
)
from tests.conftest import (
    WINDOW_END,
    WINDOW_START,
    make_round_trip,
    make_scenario,
    make_three_stop,
)


def two_stop(**overrides):
    """The historical shape, narrowed so the expected pairs stay readable."""
    defaults = dict(
        id="jp-ph",
        origins=["PRG", "VIE"],
        stops=[
            Stop(airports=["NRT", "KIX"], stay_days=(9, 11), label="Japan"),
            Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
        ],
        depth="standard",
    )
    defaults.update(overrides)
    return make_scenario(**defaults)


def pairs(scenario, leg_index):
    return {
        (s.origin, s.destination)
        for s in plan_searches(scenario)
        if s.leg_index == leg_index
    }


def test_a_two_stop_trip_plans_three_leg_groups():
    assert {s.leg_index for s in plan_searches(two_stop())} == {0, 1, 2}


def test_first_leg_covers_the_origins_and_the_first_stop():
    assert pairs(two_stop(), 0) == {
        ("PRG", "NRT"), ("PRG", "KIX"), ("VIE", "NRT"), ("VIE", "KIX"),
    }


def test_middle_leg_connects_consecutive_stops():
    assert pairs(two_stop(), 1) == {("NRT", "MNL"), ("KIX", "MNL")}


def test_final_leg_returns_to_the_origins():
    assert pairs(two_stop(), 2) == {("MNL", "PRG"), ("MNL", "VIE")}


def test_middle_leg_dates_start_after_the_first_minimum_stay():
    leg_b = [s for s in plan_searches(two_stop()) if s.leg_index == 1]
    assert min(s.depart_date for s in leg_b) >= WINDOW_START + timedelta(days=9)


def test_final_leg_dates_start_after_every_minimum_stay():
    leg_c = [s for s in plan_searches(two_stop()) if s.leg_index == 2]
    assert min(s.depart_date for s in leg_c) >= WINDOW_START + timedelta(days=18)


def test_final_leg_searches_past_the_window_end_for_slack():
    # The site substitutes nearby dates, and the last valid itineraries depart
    # after window_end. Without slack they are never found.
    leg_c = [s for s in plan_searches(two_stop()) if s.leg_index == 2]
    assert max(s.depart_date for s in leg_c) > WINDOW_END


def test_deep_searches_more_than_standard_which_searches_more_than_quick():
    quick = len(plan_searches(two_stop(depth="quick")))
    standard = len(plan_searches(two_stop(depth="standard")))
    deep = len(plan_searches(two_stop(depth="deep")))
    assert quick < standard < deep


# ------------------------------------------------------------- arbitrary shapes


def test_a_round_trip_plans_both_directions():
    """The bug the chain model removes.

    The old planner had a separate round-trip branch that emitted outbound
    searches only, so no leg ever departed the destination and the combiner
    could never close the trip.
    """
    scenario = make_round_trip()
    assert pairs(scenario, 0) == {("PRG", "NRT")}
    assert pairs(scenario, 1) == {("NRT", "PRG")}


def test_a_three_stop_trip_plans_four_legs():
    scenario = make_three_stop()
    assert {s.leg_index for s in plan_searches(scenario)} == {0, 1, 2, 3}
    assert pairs(scenario, 0) == {("PRG", "NRT")}
    assert pairs(scenario, 1) == {("NRT", "MNL")}
    assert pairs(scenario, 2) == {("MNL", "BKK")}
    assert pairs(scenario, 3) == {("BKK", "PRG")}


def test_each_leg_waits_out_every_preceding_stay():
    scenario = make_three_stop()
    starts = {
        index: min(s.depart_date for s in plan_searches(scenario) if s.leg_index == index)
        for index in range(4)
    }
    assert starts[0] == WINDOW_START
    assert starts[1] >= WINDOW_START + timedelta(days=7)
    assert starts[2] >= WINDOW_START + timedelta(days=14)
    assert starts[3] >= WINDOW_START + timedelta(days=19)


def test_a_one_way_trip_has_no_leg_home():
    scenario = make_three_stop(one_way=True)
    assert {s.leg_index for s in plan_searches(scenario)} == {0, 1, 2}
    assert not pairs(scenario, 2) & {("BKK", "PRG")}


def test_an_open_jaw_returns_to_the_configured_airports():
    scenario = make_round_trip(return_to=["BER", "MUC"])
    assert pairs(scenario, 1) == {("NRT", "BER"), ("NRT", "MUC")}


def test_never_searches_an_airport_against_itself():
    # Overlapping pools are possible now that any airport can appear anywhere.
    scenario = make_round_trip(origins=["PRG", "NRT"], return_to=["PRG", "NRT"])
    assert all(s.origin != s.destination for s in plan_searches(scenario))


# ------------------------------------------------------------------- mechanics


def test_all_searches_are_one_way():
    """Legs are reused across itineraries, so each is searched on its own."""
    assert all(s.ret_date is None for s in plan_searches(two_stop()))


def test_no_duplicate_searches():
    searches = plan_searches(two_stop(depth="deep"))
    assert len(searches) == len(set(searches))


def test_estimate_scales_with_search_count_and_workers():
    searches = plan_searches(two_stop())
    assert estimate_minutes(searches, workers=4) < estimate_minutes(searches, workers=1)
    assert estimate_minutes([], workers=4) == 0.0


def test_the_estimate_matches_a_sweep_that_actually_ran():
    """Measured against the cloud sweep of 11 Aug 03:24, which had no timeouts.

    350 searches in 5,156 s on 2 workers. The estimate used to model 19 s per
    worker per search and so promised ~97 min for a deep sweep that takes ~146 -
    which is where `timeout-minutes: 90` came from, and why five nightly runs in
    a row were killed before they could commit anything.
    """
    observed_minutes = 5156 / 60
    assert abs(estimate_minutes([None] * 350, workers=2) - observed_minutes) < observed_minutes * 0.1


def test_single_day_window_still_produces_searches():
    assert plan_searches(two_stop(window_start=WINDOW_START, window_end=WINDOW_START))


# ------------------------------------------------------------ planned routes
#
# The route set is what a sweep must cover to be worth comparing against
# another sweep of the same trip. Derived from airport_pools like everything
# else about a trip's shape, so it cannot disagree with plan_searches.


def test_planned_routes_are_the_distinct_pairs_plan_searches_visits():
    scenario = make_scenario()
    assert planned_routes(scenario) == {(s.origin, s.destination) for s in plan_searches(scenario)}


def test_planned_routes_do_not_depend_on_depth():
    """Depth changes how many dates are searched, never which routes."""
    assert planned_routes(make_scenario(depth="quick")) == planned_routes(
        make_scenario(depth="deep")
    )


def test_planned_routes_for_the_real_trip_number_21():
    # 3 origins x 3 Japan + 3 Japan x 2 Philippines + 2 Philippines x 3 origins.
    assert len(planned_routes(make_scenario())) == 21


def test_planned_routes_never_include_an_airport_flying_to_itself():
    # Pools overlap on a round trip, and no site will price PRG->PRG.
    for origin, destination in planned_routes(make_round_trip()):
        assert origin != destination


# ------------------------------------------------------------------- explore
#
# The reconnaissance pass: every route the trip implies, on a handful of dates,
# to find out which airports are plausible before spending an hour and a half
# pricing all of them.


def dates_of(searches, leg_index):
    return sorted({s.depart_date for s in searches if s.leg_index == leg_index})


def test_exploration_covers_every_route_the_trip_implies():
    """A probe that skipped routes could not rule any of them out."""
    scenario = make_scenario()
    searches = plan_exploration(scenario)
    assert {(s.origin, s.destination) for s in searches} == planned_routes(scenario)


def test_exploration_samples_a_few_dates_per_leg():
    searches = plan_exploration(make_scenario(), dates_per_leg=3)
    for leg_index in range(make_scenario().leg_count):
        assert len(dates_of(searches, leg_index)) == 3


def test_exploration_costs_the_routes_times_the_dates():
    scenario = make_scenario()
    assert len(plan_exploration(scenario, dates_per_leg=3)) == len(planned_routes(scenario)) * 3


def test_exploration_is_a_fraction_of_the_deep_sweep_it_saves_you_from():
    # The whole point: 63 searches to decide whether the 597 are worth running.
    assert len(plan_exploration(make_scenario())) < len(
        plan_searches(make_scenario(depth="deep"))
    ) / 5


def test_exploration_never_costs_more_than_the_shallowest_real_sweep():
    scenario = make_scenario(depth="quick")
    assert len(plan_exploration(scenario)) <= len(plan_searches(scenario))


def test_exploration_spans_the_whole_usable_window_rather_than_clustering():
    """Three dates a day apart would say nothing about the other two months.

    "Usable" is the correction. This asserted `== WINDOW_END`, which is what the
    probe used to do and why it was worth so much less than it looked: of its
    three first-leg samples, 22 January and 8 February could not complete a trip
    at all, so three airports were being ranked on one date's evidence. The span
    now ends at the last departure the trip can actually be flown from.
    """
    scenario = make_scenario()
    first_leg = dates_of(plan_exploration(scenario), 0)
    assert first_leg[0] == WINDOW_START
    assert first_leg[-1] == latest_departure(scenario, 0)


def latest_departure(scenario, leg_index):
    """The last date this leg may depart and still reach a searched final leg."""
    return (
        scenario.window_end
        + timedelta(days=RETURN_SLACK_DAYS)
        - timedelta(days=scenario.remaining_min_stay(leg_index))
    )


def test_every_leg_reserves_the_stays_that_must_follow_it():
    """The bound that was missing, and cost 132 of a deep sweep's 615 searches.

    A first leg departing after this cannot reach a final leg the same sweep
    searched, so every offer found on it is an orphan - proven on the committed
    data, where the quick sweep of 10 Aug searched 2 February and produced no
    itinerary from it.
    """
    scenario = two_stop(depth="deep")
    for leg_index in range(scenario.leg_count):
        latest = max(s.depart_date for s in plan_searches(scenario) if s.leg_index == leg_index)
        assert latest == latest_departure(scenario, leg_index)


def test_the_latest_first_leg_can_still_reach_a_searched_final_leg():
    """The property the reservation exists to guarantee, stated end to end."""
    scenario = two_stop(depth="deep")
    searches = plan_searches(scenario)
    last_out = max(s.depart_date for s in searches if s.leg_index == 0)
    last_home = max(s.depart_date for s in searches if s.leg_index == scenario.leg_count - 1)
    soonest_home = last_out + timedelta(days=scenario.min_trip_days)
    assert soonest_home <= last_home


def test_the_final_leg_keeps_its_slack_and_reserves_nothing():
    scenario = two_stop(depth="deep")
    assert scenario.remaining_min_stay(scenario.leg_count - 1) == 0
    assert latest_departure(scenario, scenario.leg_count - 1) > WINDOW_END


def test_a_one_way_final_leg_also_reserves_nothing():
    """It arrives where the trip ends; nothing has to happen after it."""
    scenario = make_three_stop(one_way=True)
    assert scenario.remaining_min_stay(scenario.leg_count - 1) == 0


def test_exploration_waits_out_the_minimum_stays_like_a_real_sweep():
    searches = plan_exploration(make_scenario())
    assert dates_of(searches, 1)[0] >= WINDOW_START + timedelta(days=9)
    assert dates_of(searches, 2)[0] >= WINDOW_START + timedelta(days=18)


def test_exploration_gives_the_final_leg_the_same_slack():
    assert dates_of(plan_exploration(make_scenario()), 2)[-1] > WINDOW_END


def test_exploration_does_not_depend_on_depth():
    """Depth is how thoroughly you price a trip, not how you scout it."""
    assert len(plan_exploration(make_scenario(depth="quick"))) == len(
        plan_exploration(make_scenario(depth="deep"))
    )


def test_exploration_never_repeats_a_search():
    # A short window has fewer distinct dates than samples asked for.
    scenario = make_scenario(window_start=WINDOW_START, window_end=WINDOW_START + timedelta(days=19))
    searches = plan_exploration(scenario, dates_per_leg=5)
    assert len(searches) == len(set(searches))


def test_exploration_of_the_tightest_valid_window_still_searches_every_route():
    """A window with exactly enough room for the minimum stays.

    The last leg then has a single valid departure date, and sampling three from
    one day must still ask about that leg rather than skip it - a route missing
    from the report reads as unmeasured, which is what the probe exists to avoid.
    """
    scenario = make_scenario(
        window_start=WINDOW_START, window_end=WINDOW_START + timedelta(days=18)
    )
    scenario.validate()
    searches = plan_exploration(scenario)
    assert {(s.origin, s.destination) for s in searches} == planned_routes(scenario)


def test_exploration_searches_one_way_like_everything_else():
    assert all(s.ret_date is None for s in plan_exploration(make_scenario()))


# ----------------------------------------------------------------- narrowing
#
# Two plans, one arithmetic. A broad sweep prices the window the trip declares
# and nothing on the trip may shrink it - that is what makes it broad, and what
# it was not until 24 Aug: `plan_searches` read the narrowing, so saving one
# quietly turned every sweep, including the 02:00 one, into a narrowed sweep.
# Measured on the committed japan-philippines trip that day: two nightly runs
# planned 48 searches where the window is 85, and their status recorded
# `focus: None`, because only the focus was ever written down.
#
# `plan_final` is that narrowed plan, asked for by name.


def focused(**overrides):
    return two_stop(
        depth="deep",
        focus_start=WINDOW_START + timedelta(days=7),
        focus_end=WINDOW_START + timedelta(days=11),
        **overrides,
    )


def narrowed(**overrides):
    """All three constraints at once - the shape a real trip ends up in."""
    return focused(
        return_focus_start=WINDOW_START + timedelta(days=30),
        return_focus_end=WINDOW_START + timedelta(days=36),
        total_days=(24, 28),
        **overrides,
    )


def test_a_broad_sweep_prices_the_window_whatever_the_trip_has_been_narrowed_to():
    """The bug this split exists to fix, stated as an equality.

    Not a count comparison: the two plans must be the *same searches*, so a
    narrowing cannot move a date rather than remove it.
    """
    assert plan_searches(narrowed()) == plan_searches(two_stop(depth="deep"))


def test_a_final_sweep_is_the_narrowed_plan_the_broad_one_used_to_be():
    assert len(plan_final(narrowed())) == 24
    assert len(plan_searches(narrowed())) == 168


def test_a_focus_bounds_a_final_sweeps_first_leg_to_the_chosen_dates():
    scenario = focused()
    assert dates_of(plan_final(scenario), 0) == [
        scenario.focus_start + timedelta(days=offset) for offset in range(5)
    ]


def test_a_focus_carries_through_to_the_later_legs():
    """A later leg spans the focus plus every legal stay, and no more.

    Not the whole window: that would price return dates unreachable from any
    focused departure, which is the same orphan-search waste the reservation
    bound exists to remove.
    """
    scenario = focused()
    for leg_index in (1, 2):
        dates = dates_of(plan_final(scenario), leg_index)
        assert dates[0] == scenario.focus_start + timedelta(
            days=scenario.earliest_departure(leg_index)
        )
        assert dates[-1] == min(
            latest_departure(scenario, leg_index),
            scenario.focus_end + timedelta(days=scenario.max_stay_before(leg_index)),
        )


def test_a_return_window_alone_narrows_a_final_sweep():
    """Each constraint bites on its own; they are intersected, never chosen between."""
    only_home = two_stop(
        depth="deep",
        return_focus_start=WINDOW_START + timedelta(days=30),
        return_focus_end=WINDOW_START + timedelta(days=36),
    )
    assert len(plan_final(only_home)) == 76
    assert len(plan_searches(only_home)) == 168


def test_a_nights_band_alone_narrows_a_final_sweep():
    """It bounds the last leg against the earliest the first one could go.

    Weakly - 140 against 168 - because with neither end pinned it can only say
    how soon the flight home may be. Most of its work happens in `combine.py`,
    on chains it can measure rather than on dates it can only guess at.
    """
    only_nights = two_stop(depth="deep", total_days=(24, 28))
    assert len(plan_final(only_nights)) == 140
    assert len(plan_searches(only_nights)) == 168


def test_every_search_a_final_sweep_plans_falls_inside_the_narrowing():
    """The property the whole feature is for, checked directly.

    A final sweep that priced one day outside the decision would be spending the
    site's answers on exactly what the broad sweep is already for.
    """
    scenario = narrowed()
    out = dates_of(plan_final(scenario), 0)
    home = dates_of(plan_final(scenario), scenario.leg_count - 1)

    assert min(out) >= scenario.focus_start and max(out) <= scenario.focus_end
    assert min(home) >= scenario.return_focus_start
    assert max(home) <= scenario.return_focus_end


def test_a_final_sweep_of_a_trip_with_nothing_narrowed_is_refused_by_name():
    """Otherwise the button spends an hour re-running the broad sweep.

    Refused rather than silently equal, because the two are different questions
    and a run filed as `final` that priced the whole window would join the wrong
    trend line for as long as it stayed on disk.
    """
    with pytest.raises(ValueError) as raised:
        plan_final(two_stop(depth="deep"))

    assert "narrow" in str(raised.value).lower()


def test_a_final_sweep_can_still_complete_a_trip():
    """The point of narrowing is fewer searches, never a sweep that finds nothing."""
    scenario = focused()
    searches = plan_final(scenario)
    last_out = max(s.depart_date for s in searches if s.leg_index == 0)
    last_home = max(s.depart_date for s in searches if s.leg_index == scenario.leg_count - 1)
    assert last_out + timedelta(days=scenario.min_trip_days) <= last_home


def test_a_probe_samples_the_whole_window_whatever_the_narrowing():
    """The probe judges airports, and it belongs to the broad half of the app.

    Sampled inside the narrowing it would rank airports on the handful of days
    already chosen, which is a verdict about those days wearing an airport's
    name. Asserted on the dates rather than the count: three dates a leg is
    three dates a leg either way, and only where they land says anything.
    """
    def sampled(scenario):
        return sorted({(s.origin, s.destination, s.depart_date) for s in plan_exploration(scenario)})

    assert sampled(narrowed()) == sampled(two_stop(depth="deep"))


# ---------------------------------------------------------------- watch plan
#
# A check prices a handful of preferences, not a window. Its whole reason to
# exist is that it fits inside what the site will answer, so the search count is
# the thing under test.


def watched(*starts, slack=0, **overrides):
    """A trip following one preference per start date, ten days a stop.

    `slack=0` by default, so the tests that are about *which dates a preference
    pins* are not also tests of how wide the slack happens to be. The slack has
    its own tests below.
    """
    from datetime import date

    from src.scenario import Preference

    def candidate(start):
        first = date.fromisoformat(start)
        return Preference(
            depart_dates=[first, first + timedelta(days=10), first + timedelta(days=20)],
            slack_days=slack,
        )

    return two_stop(preferences=[candidate(s) for s in starts], **overrides)


def test_a_watch_prices_every_airport_pair_on_the_pinned_dates():
    from datetime import date

    from src.sweep.planner import plan_watch

    searches = plan_watch(watched("2027-01-10"))
    # 2 origins x 2 Japanese airports, then 2 x 1, then 1 x 2 = 4 + 2 + 2.
    assert len(searches) == 8
    assert {s.depart_date for s in searches} == {
        date(2027, 1, 10), date(2027, 1, 20), date(2027, 1, 30)
    }
    assert ("PRG", "NRT", date(2027, 1, 10)) in {
        (s.origin, s.destination, s.depart_date) for s in searches
    }


def test_a_watch_never_searches_a_date_the_candidate_did_not_pin():
    """With no slack the saving over a focused sweep is exactly this."""
    from datetime import date

    from src.sweep.planner import plan_watch

    dates = {s.depart_date for s in plan_watch(watched("2027-01-10"))}
    assert date(2027, 1, 11) not in dates
    assert date(2027, 1, 21) not in dates


def test_slack_prices_the_days_either_side_of_every_pinned_leg():
    """The whole cost of a preference, and the whole reason to pay it.

    Without this a preference can say "your Tuesday has not moved" and nothing
    else; the neighbouring days are what let it say that leaving two days later
    is two thousand cheaper.
    """
    from datetime import date

    from src.sweep.planner import plan_watch

    dates = {s.depart_date for s in plan_watch(watched("2027-01-10", slack=2))}
    assert dates == {
        date(2027, 1, day) for day in (8, 9, 10, 11, 12, 18, 19, 20, 21, 22, 28, 29, 30, 31)
    } | {date(2027, 2, 1)}
    # Five dates a leg, on the same 8 pairs-per-pinned-date shape as above.
    assert len(plan_watch(watched("2027-01-10", slack=2))) == 8 * 5


def test_overlapping_slack_windows_are_searched_once():
    """Two preferences a few days apart cost far less than twice one.

    The dedup is on (origin, destination, date), so the days their windows
    share are planned once - which is what makes a second preference affordable
    rather than a doubling of the run.
    """
    from datetime import date

    from src.scenario import Preference
    from src.sweep.planner import plan_watch

    def candidate(day):
        first = date(2027, 1, day)
        return Preference(
            depart_dates=[first, first + timedelta(days=10), first + timedelta(days=20)],
            slack_days=2,
        )

    alone = len(plan_watch(two_stop(preferences=[candidate(10)])))
    together = len(plan_watch(two_stop(preferences=[candidate(10), candidate(12)])))
    assert together < alone * 2


def test_two_candidates_whose_legs_land_on_one_day_share_its_searches():
    """Sharing is per leg, not per date.

    Two preferences a day apart with stays that differ by a day fly their second
    and third legs on the very same days, and those searches are run once. A
    date shared across *different* legs is not a saving at all - leg 0 on 20
    January is PRG->NRT and leg 1 on 20 January is NRT->MNL, which have no
    search in common.
    """
    from datetime import date

    from src.scenario import Preference
    from src.sweep.planner import plan_watch

    def candidate(*days):
        return Preference(depart_dates=[date(2027, 1, d) for d in days], slack_days=0)

    alone = len(plan_watch(two_stop(preferences=[candidate(10, 20, 30)])))
    # 11 Jan + 9 days in Japan lands on the same 20th, and home the same 30th.
    together = len(
        plan_watch(two_stop(preferences=[candidate(10, 20, 30), candidate(11, 20, 30)]))
    )
    assert together < alone * 2


def test_watching_nothing_plans_nothing():
    from src.sweep.planner import plan_watch

    assert plan_watch(two_stop()) == []


def test_a_watch_of_a_settled_trip_fits_inside_what_the_site_answers():
    """Three preferences at the default slack on a trip whose airports are decided.

    This is the cadence the four-hourly check rests on. Once a crossing is
    pinned each leg is one pair, so a preference is five searches a leg and
    three of them still sit well under the ~120 the site answers from one
    runner.
    """
    from datetime import date
    from datetime import timedelta as td

    from src.scenario import DEFAULT_SLACK_DAYS, Preference
    from src.sweep.planner import plan_watch

    def candidate(day):
        first = date(2027, 1, day)
        return Preference(
            depart_dates=[first, first + td(days=10), first + td(days=20)],
            slack_days=DEFAULT_SLACK_DAYS,
        )

    trip = make_scenario(
        origins=["VIE"],
        stops=[
            Stop(airports=["HND"], stay_days=(9, 11), label="Japan"),
            Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
        ],
        preferences=[candidate(6), candidate(13), candidate(20)],
    )
    assert len(plan_watch(trip)) <= 110


def test_slack_on_an_undecided_trip_is_what_the_cap_is_for():
    """Three preferences at the default slack can absolutely blow the budget.

    Measured on the fixture trip - three departure airports, two in Japan, so
    21 routes - three preferences at plus-or-minus two days plan 315 searches
    against the ~120 the site answers. Nothing about the *count* of preferences
    says so, which is exactly why `web.app` refuses on the planned figure and
    `MAX_PREFERENCES` is only the cheap guard in front of it.
    """
    from datetime import date
    from datetime import timedelta as td

    from src.scenario import DEFAULT_SLACK_DAYS, Preference
    from src.sweep.planner import plan_watch

    def candidate(day):
        first = date(2027, 1, day)
        return Preference(
            depart_dates=[first, first + td(days=10), first + td(days=20)],
            slack_days=DEFAULT_SLACK_DAYS,
        )

    trip = make_scenario(preferences=[candidate(6), candidate(13), candidate(20)])
    assert len(plan_watch(trip)) > 110


# ------------------------------------------------ pinning an overland stop
#
# The point of pinning is that it costs fewer searches. These numbers are the
# feature: if a pin ever stops shrinking the plan, it is doing nothing.


def _japan_ph(**pins):
    return make_scenario(
        origins=["VIE", "FRA"],
        stops=[
            Stop(airports=["HND", "NRT", "KIX"], stay_days=(8, 13), label="Japan",
                 overland=True, **pins),
            Stop(airports=["MNL", "CEB"], stay_days=(8, 13), label="Philippines"),
        ],
        depth="deep",
    )


def test_an_unpinned_overland_trip_still_costs_what_it_always_did():
    """Overland alone changes what can be chained, never what is searched."""
    assert planned_routes(_japan_ph()) == planned_routes(
        make_scenario(
            origins=["VIE", "FRA"],
            stops=[
                Stop(airports=["HND", "NRT", "KIX"], stay_days=(8, 13), label="Japan"),
                Stop(airports=["MNL", "CEB"], stay_days=(8, 13), label="Philippines"),
            ],
            depth="deep",
        )
    )


def test_pinning_the_crossing_halves_the_routes():
    """2 origins x 3 Japanese airports x 2 Philippine ones is 16 route pairs.

    Pinned to arrive Haneda and leave Kansai it is 8: two ways in, one hop on,
    and four ways home.
    """
    assert len(planned_routes(_japan_ph())) == 16
    pinned = _japan_ph(arrive_via="HND", depart_via="KIX")
    assert len(planned_routes(pinned)) == 8


def test_a_pinned_trip_never_searches_the_airports_it_ruled_out():
    pinned = _japan_ph(arrive_via="HND", depart_via="KIX")
    routes = planned_routes(pinned)
    assert ("VIE", "NRT") not in routes, "NRT was ruled out as a way in"
    assert ("HND", "MNL") not in routes, "the way out is Kansai"
    assert ("VIE", "HND") in routes
    assert ("KIX", "MNL") in routes


def test_pinning_shrinks_the_sweep_it_plans():
    before = len(plan_searches(_japan_ph()))
    after = len(plan_searches(_japan_ph(arrive_via="HND", depart_via="KIX")))
    assert after < before / 1.8, f"{before} -> {after} is not worth a control"


def test_pinning_shrinks_the_probe_and_the_watch_too():
    """All three planners walk the same pools, so none of them can disagree."""
    for plan in (plan_exploration, plan_searches):
        before = len(plan(_japan_ph()))
        after = len(plan(_japan_ph(arrive_via="HND", depart_via="KIX")))
        assert after < before


# --------------------------------------------- probing the stops both ways
#
# Only the probe. A deep sweep of both orders is twice a 480-search plan; three
# dates a leg is cheap enough to answer "is Philippines first even viable"
# before committing to it, which is the question actually being asked.


def _two_ways(**overrides):
    return make_scenario(
        origins=["VIE"],
        stops=[
            Stop(airports=["HND"], stay_days=(8, 13), label="Japan"),
            Stop(airports=["MNL"], stay_days=(8, 13), label="Philippines"),
        ],
        **overrides,
    )


def test_a_trip_probes_one_order_unless_it_says_otherwise():
    routes = {(s.origin, s.destination) for s in plan_exploration(_two_ways())}
    assert ("VIE", "HND") in routes
    assert ("VIE", "MNL") not in routes


def test_probing_both_orders_adds_the_reverse_chain():
    routes = {
        (s.origin, s.destination)
        for s in plan_exploration(_two_ways(probe_both_orders=True))
    }
    # Japan first.
    assert {("VIE", "HND"), ("HND", "MNL"), ("MNL", "VIE")} <= routes
    # Philippines first.
    assert {("VIE", "MNL"), ("MNL", "HND"), ("HND", "VIE")} <= routes


def test_the_reverse_order_never_reaches_the_sweep():
    """Depth answers how finely to price one order, not which order to fly."""
    routes = {
        (s.origin, s.destination)
        for s in plan_searches(_two_ways(probe_both_orders=True))
    }
    assert ("VIE", "MNL") not in routes


def test_one_route_on_one_date_is_never_searched_twice():
    """The two orders overlap, and `LegSearch` carries the leg it came from.

    Deduplicating on the whole record would run the same search twice under two
    different leg indices, which is a real search against a site that answers
    about 120 of them.
    """
    searches = plan_exploration(_two_ways(probe_both_orders=True))
    keys = [(s.origin, s.destination, s.depart_date) for s in searches]
    assert len(keys) == len(set(keys))


def test_probing_both_orders_of_a_single_stop_trip_changes_nothing():
    """There is no other order of one stop."""
    trip = make_round_trip()
    assert len(plan_exploration(replace(trip, probe_both_orders=True))) == len(
        plan_exploration(trip)
    )


def test_both_orders_stays_affordable():
    """The point of confining this to the probe.

    Measured on the real trip's shape rather than the two-airport fixture: two
    origins, three Japanese airports and two Philippine ones. Sweeping both
    orders would be two ~370-search plans against a site that answers about 120
    per runner; probing both is a few dozen.
    """
    trip = _japan_ph()
    both = replace(trip, probe_both_orders=True)
    one_order, two_orders = len(plan_exploration(trip)), len(plan_exploration(both))

    # 48 -> 96. It really did cost something, or the flag does nothing.
    assert two_orders > one_order
    # Against 368 for a deep sweep of one order alone.
    assert two_orders < len(plan_searches(trip)) / 3
    # The bound that actually binds: pelikan.cz answers about 120 searches from
    # one runner and then stops answering at all, and the probe runs unsharded.
    # A both-orders probe that crossed this would report half a trip as though
    # it were the whole one.
    assert two_orders <= 120


# ------------------------------------------------------- surviving a short run
#
# A sweep that is cut short is not the same thing as a sweep of half a trip. A
# local run on 21 Aug answered 37 of 66 searches and found 357 real flights, and
# `combine_all` returned **nothing**: emitted leg by leg, the truncation left
# VIE->HND on 5-23 Jan, KIX->MNL on 25 Jan - 4 Feb and MNL->VIE on 22-28 Jan, so
# a return 8-13 days after 25 Jan had never been priced. 56% of the searches was
# 0% of the answer.
#
# `shard_of` already deals per route for exactly this reason. These say the same
# rule holds for the order the searches are made in.


def chains(scenario, searches) -> bool:
    """Whether some trip can be built from the dates these searches cover."""
    dates = {}
    for search in searches:
        dates.setdefault(search.leg_index, set()).add(search.depart_date)
    if set(dates) != set(range(scenario.leg_count)):
        return False

    reachable = dates[0]
    for leg_index in range(1, scenario.leg_count):
        low, high = scenario.stops[leg_index - 1].stay_days
        reachable = {
            later
            for later in dates[leg_index]
            for earlier in reachable
            if low <= (later - earlier).days <= high
        }
        if not reachable:
            return False
    return True


def test_a_whole_plan_chains():
    """The control: if this ever fails the trip shape is wrong, not the order."""
    assert chains(two_stop(), plan_searches(two_stop()))


def test_a_plan_cut_short_still_chains():
    trip = two_stop(depth="deep")
    plan = plan_searches(trip)
    # The share the 21 Aug run managed before the site refused it.
    assert chains(trip, plan[: len(plan) * 37 // 66])


def test_even_a_tenth_of_a_plan_chains():
    trip = two_stop(depth="deep")
    plan = plan_searches(trip)
    assert chains(trip, plan[: len(plan) // 10])


def test_a_prefix_of_the_plan_holds_every_route():
    """Not just every leg: a run cut short must thin the grid, not delete
    routes from it. A route missing outright reads downstream as a dead
    airport, which is a verdict rather than a gap."""
    plan = plan_searches(two_stop(depth="deep"))
    routes = {(s.leg_index, s.origin, s.destination) for s in plan}
    prefix = {(s.leg_index, s.origin, s.destination) for s in plan[: len(plan) // 4]}
    assert prefix == routes


def test_dealing_the_plan_changes_the_order_and_nothing_else():
    """The set of searches is the trip; the order is only what survives a
    refusal. Changing one must not change the other."""
    plan = plan_searches(two_stop(depth="deep"))
    assert len(plan) == len(set(plan))
    assert {(s.leg_index, s.origin, s.destination, s.depart_date) for s in plan} == {
        (s.leg_index, s.origin, s.destination, s.depart_date)
        for s in plan_searches(two_stop(depth="deep"))
    }


# ---------------------------------------------------------- sizing the runners
#
# `DEFAULT_SHARDS: 5` sat in the workflow as a bare number. It was right - 483
# planned over ~100 a runner - but only for one trip on one day, and pinning the
# Japan crossing took that trip to 66 searches, at which point five runners were
# splitting thirteen apiece. One number also cannot be right for two trips at
# once, and the matrix applied it to every trip in the run.


def test_the_shard_count_reproduces_the_number_that_worked():
    """483 searches over 5 runners is the configuration that has finished whole
    every night since 20 Aug. The rule has to give back that answer, not a new
    one."""
    assert shards_for(483) == 5


def test_a_small_trip_gets_one_runner():
    assert shards_for(66) == 1
    assert shards_for(SEARCHES_PER_RUNNER) == 1


def test_a_plan_one_search_over_the_cap_gets_another_runner():
    assert shards_for(SEARCHES_PER_RUNNER + 1) == 2


def test_no_shard_of_a_real_trip_is_given_more_than_the_site_answers():
    """The whole point, checked on plans rather than on arithmetic.

    `shard_of` deals within each route, so a shard is the sum of a rounding-up
    per route and runs a little over `planned / count` - the 483-search sweep
    of 20 Aug planned 96.6 a runner and handed out 105. That overshoot is what
    the gap between the 100 target and the measured 120 wall is *for*, so the
    assertion here is against the wall.
    """
    answered_by_one_client = 120
    for depth in ("quick", "standard", "deep"):
        plan = plan_searches(make_scenario(depth=depth))
        count = shards_for(len(plan))
        biggest = max(len(shard_of(plan, index, count)) for index in range(count))
        assert biggest <= answered_by_one_client, (depth, len(plan), count, biggest)


def test_an_empty_plan_still_asks_for_one_runner():
    """Zero runners is a matrix with no jobs, which reads as a successful night
    that swept nothing - the failure thirteen cancelled runs already made."""
    assert shards_for(0) == 1
