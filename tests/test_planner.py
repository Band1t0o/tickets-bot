"""Tests for turning a Scenario into a concrete list of searches."""
from __future__ import annotations

from datetime import timedelta

from src.scenario import Stop
from src.sweep.planner import (
    RETURN_SLACK_DAYS,
    estimate_minutes,
    plan_exploration,
    plan_searches,
    planned_routes,
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


# --------------------------------------------------------------------- focus
#
# Once a broad sweep has shown which departure dates are cheap, a focus narrows
# the next sweep onto them. It bounds the *first* leg; the later legs follow
# through the stay ranges, so the three can never contradict each other.


def focused(**overrides):
    return two_stop(
        depth="deep",
        focus_start=WINDOW_START + timedelta(days=7),
        focus_end=WINDOW_START + timedelta(days=11),
        **overrides,
    )


def test_no_focus_plans_exactly_what_the_whole_window_plans():
    """The unfocused case must not be a special case. It is the same code path
    with the focus bound absent, so it has to reduce to it exactly."""
    assert plan_searches(two_stop(depth="deep")) == plan_searches(
        two_stop(depth="deep", focus_start=None, focus_end=None)
    )


def test_a_focus_bounds_the_first_leg_to_the_chosen_dates():
    scenario = focused()
    assert dates_of(plan_searches(scenario), 0) == [
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
        dates = dates_of(plan_searches(scenario), leg_index)
        assert dates[0] == scenario.focus_start + timedelta(
            days=scenario.earliest_departure(leg_index)
        )
        assert dates[-1] == min(
            latest_departure(scenario, leg_index),
            scenario.focus_end + timedelta(days=scenario.max_stay_before(leg_index)),
        )


def test_a_focus_is_much_cheaper_than_the_window_it_narrows():
    assert len(plan_searches(focused())) < len(plan_searches(two_stop(depth="deep"))) / 2


def test_a_focused_sweep_can_still_complete_a_trip():
    """The point of narrowing is fewer searches, never a sweep that finds nothing."""
    scenario = focused()
    searches = plan_searches(scenario)
    last_out = max(s.depart_date for s in searches if s.leg_index == 0)
    last_home = max(s.depart_date for s in searches if s.leg_index == scenario.leg_count - 1)
    assert last_out + timedelta(days=scenario.min_trip_days) <= last_home


# ---------------------------------------------------------------- watch plan
#
# A watch prices a handful of pinned candidate trips, not a window. Its whole
# reason to exist is that it fits inside what the site will answer, so the
# search count is the thing under test.


def watched(*starts, **overrides):
    """A trip watching one candidate per start date, ten days a stop."""
    from datetime import date

    from src.scenario import Watch

    def candidate(start):
        first = date.fromisoformat(start)
        return Watch(depart_dates=[first, first + timedelta(days=10), first + timedelta(days=20)])

    return two_stop(watches=[candidate(s) for s in starts], **overrides)


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
    """The saving over a focused sweep is exactly this: no derived dates."""
    from datetime import date

    from src.sweep.planner import plan_watch

    dates = {s.depart_date for s in plan_watch(watched("2027-01-10"))}
    assert date(2027, 1, 11) not in dates
    assert date(2027, 1, 21) not in dates


def test_two_candidates_whose_legs_land_on_one_day_share_its_searches():
    """Sharing is per leg, not per date.

    Two candidates a day apart with stays that differ by a day fly their second
    and third legs on the very same days, and those searches are run once. A
    date shared across *different* legs is not a saving at all - leg 0 on 20
    January is PRG->NRT and leg 1 on 20 January is NRT->MNL, which have no
    search in common.
    """
    from datetime import date

    from src.scenario import Watch
    from src.sweep.planner import plan_watch

    def candidate(*days):
        return Watch(depart_dates=[date(2027, 1, d) for d in days])

    alone = len(plan_watch(two_stop(watches=[candidate(10, 20, 30)])))
    # 11 Jan + 9 days in Japan lands on the same 20th, and home the same 30th.
    together = len(plan_watch(two_stop(watches=[candidate(10, 20, 30), candidate(11, 20, 30)])))
    assert together < alone * 2


def test_watching_nothing_plans_nothing():
    from src.sweep.planner import plan_watch

    assert plan_watch(two_stop()) == []


def test_a_watch_of_the_real_trip_fits_inside_what_the_site_answers():
    """Three candidates on the full trip: the number the cadence rests on.

    21 routes at one date each per leg is 21 searches a candidate. If this ever
    exceeds ~120 the four-hourly watch stops being possible from one runner,
    and it will do so silently - the sweep just stops being answered part way.
    """
    from datetime import date
    from datetime import timedelta as td

    from src.scenario import Watch

    def candidate(day):
        first = date(2027, 1, day)
        return Watch(depart_dates=[first, first + td(days=10), first + td(days=20)])

    from src.sweep.planner import plan_watch

    trip = make_scenario(watches=[candidate(6), candidate(13), candidate(20)])
    assert len(plan_watch(trip)) <= 110
