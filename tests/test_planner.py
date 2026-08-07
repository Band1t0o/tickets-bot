"""Tests for turning a Scenario into a concrete list of searches."""
from __future__ import annotations

from datetime import date, timedelta

from src.scenario import Scenario
from src.sweep.planner import estimate_minutes, plan_searches

WINDOW_START = date(2027, 1, 5)
WINDOW_END = date(2027, 2, 8)


def multi_city(**overrides) -> Scenario:
    defaults = dict(
        id="jp-ph",
        name="Japan then Philippines",
        trip_type="multi_city",
        origins=["PRG", "VIE"],
        japan_airports=["NRT", "KIX"],
        ph_airports=["MNL"],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        japan_stay_days=(9, 11),
        ph_stay_days=(9, 11),
        depth="standard",
    )
    defaults.update(overrides)
    return Scenario(**defaults)


def round_trip(**overrides) -> Scenario:
    defaults = dict(
        id="tokyo",
        name="Tokyo return",
        trip_type="round_trip",
        origins=["PRG"],
        japan_airports=["NRT"],
        ph_airports=[],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        trip_length_days=(18, 20),
        depth="quick",
    )
    defaults.update(overrides)
    return Scenario(**defaults)


def test_multi_city_plans_three_leg_groups():
    assert {s.leg_index for s in plan_searches(multi_city())} == {0, 1, 2}


def test_leg_a_covers_the_configured_origins_and_japan_airports():
    leg_a = [s for s in plan_searches(multi_city()) if s.leg_index == 0]
    assert {(s.origin, s.destination) for s in leg_a} == {
        ("PRG", "NRT"), ("PRG", "KIX"), ("VIE", "NRT"), ("VIE", "KIX"),
    }


def test_leg_b_flies_japan_to_philippines():
    leg_b = [s for s in plan_searches(multi_city()) if s.leg_index == 1]
    assert {(s.origin, s.destination) for s in leg_b} == {("NRT", "MNL"), ("KIX", "MNL")}


def test_leg_c_returns_from_philippines_to_the_origins():
    leg_c = [s for s in plan_searches(multi_city()) if s.leg_index == 2]
    assert {(s.origin, s.destination) for s in leg_c} == {("MNL", "PRG"), ("MNL", "VIE")}


def test_leg_b_dates_start_after_minimum_japan_stay():
    leg_b = [s for s in plan_searches(multi_city()) if s.leg_index == 1]
    assert min(s.depart_date for s in leg_b) >= WINDOW_START + timedelta(days=9)


def test_leg_c_dates_start_after_both_minimum_stays():
    leg_c = [s for s in plan_searches(multi_city()) if s.leg_index == 2]
    assert min(s.depart_date for s in leg_c) >= WINDOW_START + timedelta(days=18)


def test_leg_c_searches_past_the_window_end_for_slack():
    # The site substitutes nearby dates, and the last valid itineraries depart
    # after window_end. Without slack they are never found.
    leg_c = [s for s in plan_searches(multi_city()) if s.leg_index == 2]
    assert max(s.depart_date for s in leg_c) > WINDOW_END


def test_deep_searches_more_than_standard_which_searches_more_than_quick():
    quick = len(plan_searches(multi_city(depth="quick")))
    standard = len(plan_searches(multi_city(depth="standard")))
    deep = len(plan_searches(multi_city(depth="deep")))
    assert quick < standard < deep


def test_round_trip_scenario_produces_only_round_trip_searches():
    searches = plan_searches(round_trip())
    assert searches
    assert all(s.ret_date is not None for s in searches)


def test_round_trip_return_dates_honour_trip_length():
    for s in plan_searches(round_trip()):
        assert 18 <= (s.ret_date - s.depart_date).days <= 20


def test_multi_city_searches_are_all_one_way():
    assert all(s.ret_date is None for s in plan_searches(multi_city()))


def test_no_duplicate_searches():
    searches = plan_searches(multi_city(depth="deep"))
    assert len(searches) == len(set(searches))


def test_estimate_scales_with_search_count_and_workers():
    searches = plan_searches(multi_city())
    assert estimate_minutes(searches, workers=4) < estimate_minutes(searches, workers=1)
    assert estimate_minutes([], workers=4) == 0.0


def test_single_day_window_still_produces_searches():
    scenario = multi_city(window_start=WINDOW_START, window_end=WINDOW_START)
    assert plan_searches(scenario)
