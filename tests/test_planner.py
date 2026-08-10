"""Tests for turning a Scenario into a concrete list of searches."""
from __future__ import annotations

from datetime import timedelta

from src.scenario import Stop
from src.sweep.planner import estimate_minutes, plan_searches
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


def test_single_day_window_still_produces_searches():
    assert plan_searches(two_stop(window_start=WINDOW_START, window_end=WINDOW_START))
