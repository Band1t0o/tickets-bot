"""Tests for scenario definition, validation and on-disk storage."""
from __future__ import annotations

from datetime import date

import pytest

from src.scenario import Scenario, Stop, load_scenario, load_scenarios, save_scenario
from tests.conftest import make_round_trip, make_scenario, make_three_stop


def test_round_trips_through_json(tmp_path):
    original = make_scenario()
    save_scenario(original, tmp_path)
    assert load_scenario(tmp_path / "japan-philippines.json") == original


def test_round_trips_an_arbitrary_number_of_stops(tmp_path):
    original = make_three_stop()
    save_scenario(original, tmp_path)
    assert load_scenario(tmp_path / "grand-tour.json") == original


def test_load_scenarios_reads_a_directory(tmp_path):
    save_scenario(make_scenario(), tmp_path)
    save_scenario(make_scenario(id="second", name="Second"), tmp_path)
    assert {s.id for s in load_scenarios(tmp_path)} == {"japan-philippines", "second"}


# ------------------------------------------------------------------- the shape


def test_airport_pools_describe_the_whole_chain():
    scenario = make_scenario()
    assert scenario.airport_pools == [
        ["PRG", "VIE", "FRA"],
        ["NRT", "HND", "KIX"],
        ["MNL", "CEB"],
        ["PRG", "VIE", "FRA"],
    ]
    assert scenario.leg_count == 3


def test_a_single_stop_is_a_round_trip():
    scenario = make_round_trip()
    assert scenario.airport_pools == [["PRG"], ["NRT"], ["PRG"]]
    assert scenario.leg_count == 2


def test_return_to_a_different_airport_is_an_open_jaw():
    scenario = make_scenario(return_to=["BER"])
    assert scenario.airport_pools[-1] == ["BER"]


def test_one_way_drops_the_leg_home():
    there = make_scenario()
    one_way = make_scenario(one_way=True)
    assert one_way.leg_count == there.leg_count - 1
    assert one_way.airport_pools[-1] == ["MNL", "CEB"]


def test_each_leg_waits_out_the_preceding_stays():
    scenario = make_scenario()
    assert scenario.earliest_departure(0) == 0
    assert scenario.earliest_departure(1) == 9
    assert scenario.earliest_departure(2) == 18
    assert scenario.min_trip_days == 18


# -------------------------------------------------------------------- validity


def test_accepts_a_valid_scenario():
    make_scenario().validate()  # must not raise


def test_accepts_any_number_of_stops():
    make_three_stop().validate()  # must not raise


def test_rejects_window_end_before_start():
    with pytest.raises(ValueError, match="window_end"):
        make_scenario(window_start=date(2027, 2, 8), window_end=date(2027, 1, 5)).validate()


def test_rejects_empty_origins():
    with pytest.raises(ValueError, match="origins"):
        make_scenario(origins=[]).validate()


def test_rejects_a_trip_with_no_destination():
    with pytest.raises(ValueError, match="at least one destination"):
        make_scenario(stops=[]).validate()


def test_rejects_a_stop_with_no_airports():
    with pytest.raises(ValueError, match="Philippines"):
        make_scenario(
            stops=[
                Stop(airports=["NRT"], stay_days=(9, 11), label="Japan"),
                Stop(airports=[], stay_days=(9, 11), label="Philippines"),
            ]
        ).validate()


def test_rejects_inverted_stay_range():
    with pytest.raises(ValueError, match="Japan"):
        make_scenario(
            stops=[
                Stop(airports=["NRT"], stay_days=(11, 9), label="Japan"),
                Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
            ]
        ).validate()


def test_unlabelled_stops_are_named_by_position():
    with pytest.raises(ValueError, match="stop 2"):
        make_scenario(
            stops=[
                Stop(airports=["NRT"], stay_days=(9, 11)),
                Stop(airports=["MNL"], stay_days=(11, 9)),
            ]
        ).validate()


@pytest.mark.parametrize("code", ["prg", "PRAGUE", "PR", "P1G ", ""])
def test_rejects_airport_codes_that_are_not_iata(code):
    """Typos become searches that quietly find nothing, so catch them here."""
    with pytest.raises(ValueError, match="IATA"):
        make_scenario(origins=[code]).validate()


def test_accepts_multi_passenger_search():
    # Card prices are per person - confirmed from the site's own label, which
    # reads "Celková cena pro všechny osoby" for 1 passenger and "Průměrná cena
    # na osobu" for 2. Itinerary.total_for_party multiplies accordingly.
    make_scenario(adults=2).validate()  # must not raise


def test_rejects_absurd_party_size():
    with pytest.raises(ValueError, match="adults"):
        make_scenario(adults=0).validate()


def test_rejects_window_too_short_for_the_minimum_stays():
    # Otherwise the planner emits the first leg only - the later legs have no
    # valid dates - and the sweep silently produces no itineraries at all.
    with pytest.raises(ValueError, match="minimum stays"):
        make_scenario(window_start=date(2027, 1, 5), window_end=date(2027, 1, 12)).validate()


def test_the_too_short_window_message_names_each_stop():
    with pytest.raises(ValueError, match="9 at Japan"):
        make_scenario(window_end=date(2027, 1, 12)).validate()


def test_unknown_depth_is_rejected():
    with pytest.raises(ValueError, match="depth"):
        make_scenario(depth="exhaustive").validate()


# ------------------------------------------------------------------- migration


OLD_FORMAT = {
    "id": "japan-philippines",
    "name": "Europe → Japan → Philippines → Europe",
    "trip_type": "multi_city",
    "origins": ["PRG", "VIE", "FRA"],
    "japan_airports": ["NRT", "HND", "KIX"],
    "ph_airports": ["MNL", "CEB"],
    "window_start": "2027-01-05",
    "window_end": "2027-02-08",
    "japan_stay_days": [9, 11],
    "ph_stay_days": [9, 11],
    "trip_length_days": [18, 22],
    "adults": 1,
    "depth": "standard",
    "alert_threshold_czk": None,
    "bag_estimate_czk": 1500,
    "enabled": True,
    "notes": "Main trip.",
}


def test_a_pre_chain_file_still_loads():
    """Hand-edited copies of an old example must not fail with a schema error."""
    scenario = Scenario.from_dict(OLD_FORMAT)
    assert [stop.airports for stop in scenario.stops] == [["NRT", "HND", "KIX"], ["MNL", "CEB"]]
    assert [stop.stay_days for stop in scenario.stops] == [(9, 11), (9, 11)]
    assert scenario.leg_count == 3
    scenario.validate()


def test_migration_moves_currency_out_of_the_field_names():
    scenario = Scenario.from_dict({**OLD_FORMAT, "alert_threshold_czk": 28000})
    assert scenario.alert_threshold == 28000
    assert scenario.bag_estimate == 1500


def test_an_old_round_trip_keeps_its_trip_length_as_the_stay():
    old = {
        **OLD_FORMAT,
        "id": "tokyo",
        "trip_type": "round_trip",
        "ph_airports": [],
        "trip_length_days": [18, 22],
    }
    scenario = Scenario.from_dict(old)
    assert len(scenario.stops) == 1
    assert scenario.stops[0].stay_days == (18, 22)
    assert scenario.leg_count == 2


def test_unknown_fields_are_rejected_rather_than_ignored():
    with pytest.raises(ValueError, match="unknown scenario fields"):
        Scenario.from_dict({**make_scenario().to_dict(), "japan_stay_dayz": [1, 2]})


# ----------------------------------------------- notification preferences
#
# Which deals get sent, and from where. These live on the scenario beside
# alert_threshold and bag_estimate because the scenario file is what the cloud
# sweep reads - a preference held anywhere else would not reach the run that
# actually sends the message.


def test_a_trip_defaults_to_no_airport_preference():
    assert make_scenario().preferred_origins == []


def test_preferred_origins_round_trip_as_ranked_tiers(tmp_path):
    tiers = [["PRG", "VIE"], ["BER", "KRK", "KTW"], ["FRA", "MUC"]]
    save_scenario(make_scenario(preferred_origins=tiers), tmp_path)
    assert load_scenario(tmp_path / "japan-philippines.json").preferred_origins == tiers


def test_preferred_origins_reject_a_code_that_is_not_iata():
    with pytest.raises(ValueError, match="preferred_origins"):
        make_scenario(preferred_origins=[["PRG"], ["Berlin"]]).validate()


def test_preferred_origins_reject_an_empty_tier():
    """An empty tier is a ranking with a hole in it, not a preference."""
    with pytest.raises(ValueError, match="tier 2"):
        make_scenario(preferred_origins=[["PRG"], []]).validate()


def test_an_airport_may_not_sit_in_two_tiers():
    # Otherwise "best tier that has one" has two answers for the same airport.
    with pytest.raises(ValueError, match="VIE"):
        make_scenario(preferred_origins=[["PRG", "VIE"], ["VIE", "BER"]]).validate()


def test_notify_defaults_to_the_cheapest_and_the_preferred():
    assert make_scenario().notify == ["cheapest", "preferred"]


def test_unknown_notify_selection_is_rejected():
    with pytest.raises(ValueError, match="notify"):
        make_scenario(notify=["cheapest", "shiniest"]).validate()


def test_quiet_by_default_so_two_sweeps_a_day_do_not_become_sixty_pings():
    assert make_scenario().notify_quiet is True
