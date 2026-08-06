"""Tests for scenario definition, validation and on-disk storage."""
from __future__ import annotations

from datetime import date

import pytest

from src.scenario import Scenario, load_scenario, load_scenarios, save_scenario


def multi_city(**overrides) -> Scenario:
    defaults = dict(
        id="japan-philippines",
        name="Japan then Philippines",
        trip_type="multi_city",
        origins=["PRG", "VIE", "FRA"],
        japan_airports=["NRT", "HND", "KIX"],
        ph_airports=["MNL", "CEB"],
        window_start=date(2027, 1, 5),
        window_end=date(2027, 2, 8),
        japan_stay_days=(9, 11),
        ph_stay_days=(9, 11),
    )
    defaults.update(overrides)
    return Scenario(**defaults)


def test_round_trips_through_json(tmp_path):
    original = multi_city()
    save_scenario(original, tmp_path)
    assert load_scenario(tmp_path / "japan-philippines.json") == original


def test_load_scenarios_reads_a_directory(tmp_path):
    save_scenario(multi_city(), tmp_path)
    save_scenario(multi_city(id="second", name="Second"), tmp_path)
    assert {s.id for s in load_scenarios(tmp_path)} == {"japan-philippines", "second"}


def test_rejects_window_end_before_start():
    with pytest.raises(ValueError, match="window_end"):
        multi_city(window_start=date(2027, 2, 8), window_end=date(2027, 1, 5)).validate()


def test_rejects_empty_origins():
    with pytest.raises(ValueError, match="origins"):
        multi_city(origins=[]).validate()


def test_multi_city_requires_philippines_airports():
    with pytest.raises(ValueError, match="ph_airports"):
        multi_city(ph_airports=[]).validate()


def test_rejects_unknown_trip_type():
    with pytest.raises(ValueError, match="trip_type"):
        multi_city(trip_type="teleport").validate()


def test_rejects_inverted_stay_range():
    with pytest.raises(ValueError, match="japan_stay_days"):
        multi_city(japan_stay_days=(11, 9)).validate()


def test_rejects_unverified_multi_passenger_search():
    # pelikan.cz honours P:{n}000E_0_0 in the URL (the results page reports the
    # right passenger count) but returned byte-identical prices for 1, 2 and 3
    # passengers, so whether the price is per-person or a party total is
    # unresolved. Blocking is safer than reporting a total that may be wrong by
    # a factor of the party size.
    with pytest.raises(ValueError, match="adults"):
        multi_city(adults=2).validate()


def test_accepts_a_valid_scenario():
    multi_city().validate()  # must not raise


def test_round_trip_scenario_does_not_need_philippines_airports():
    Scenario(
        id="tokyo",
        name="Tokyo return",
        trip_type="round_trip",
        origins=["PRG"],
        japan_airports=["NRT"],
        ph_airports=[],
        window_start=date(2027, 1, 5),
        window_end=date(2027, 2, 8),
        trip_length_days=(18, 22),
    ).validate()


def test_unknown_depth_is_rejected():
    with pytest.raises(ValueError, match="depth"):
        multi_city(depth="exhaustive").validate()
