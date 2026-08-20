"""Tests for scenario definition, validation and on-disk storage."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from src.scenario import (
    Scenario,
    Stop,
    load_scenario,
    load_scenarios,
    read_scenarios,
    save_scenario,
)
from tests.conftest import (
    WINDOW_END,
    WINDOW_START,
    make_round_trip,
    make_scenario,
    make_three_stop,
)


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


# ----------------------------------------------------- one bad file, many good
#
# `load_scenarios` builds its list inside `sorted(...)`, so a single unreadable
# file used to raise and take every other trip down with it. Through the API
# that surfaced as an empty trip picker, which is indistinguishable from having
# no saved trips at all - the app said "your data is gone" when one file had a
# typo in it.


def test_read_scenarios_returns_the_files_it_could_read(tmp_path):
    save_scenario(make_scenario(id="good-one"), tmp_path)
    save_scenario(make_scenario(id="good-two"), tmp_path)
    (tmp_path / "broken.json").write_text("{ this is not json", encoding="utf-8")

    scenarios, problems = read_scenarios(tmp_path)

    assert [s.id for s in scenarios] == ["good-one", "good-two"]
    assert [p["file"] for p in problems] == ["broken.json"]


def test_read_scenarios_names_the_file_and_the_reason(tmp_path):
    """The message is shown verbatim, so it has to identify what to go and fix."""
    save_scenario(make_scenario(id="fine"), tmp_path)
    (tmp_path / "half-migrated.json").write_text(
        json.dumps({"id": "x", "name": "X", "origins": ["PRG"], "stops": [],
                    "window_start": "2027-01-05", "window_end": "2027-02-08",
                    "moon_phase": "waxing"}),
        encoding="utf-8",
    )

    _, problems = read_scenarios(tmp_path)

    assert problems[0]["file"] == "half-migrated.json"
    assert "moon_phase" in problems[0]["error"]


def test_read_scenarios_on_a_missing_directory_is_empty_not_an_error(tmp_path):
    assert read_scenarios(tmp_path / "nope") == ([], [])


# --------------------------------------------------------------------- focus


def test_a_focus_needs_both_ends():
    with pytest.raises(ValueError, match="both a first and a last"):
        make_scenario(focus_start=WINDOW_START).validate()
    with pytest.raises(ValueError, match="both a first and a last"):
        make_scenario(focus_end=WINDOW_END).validate()


def test_a_focus_may_not_run_backwards():
    with pytest.raises(ValueError, match="must not precede"):
        make_scenario(
            focus_start=WINDOW_START + timedelta(days=5), focus_end=WINDOW_START
        ).validate()


def test_a_focus_must_sit_inside_the_window():
    """Outside it, it is not a narrowing but a different trip - and its sweeps
    would be charted against sweeps of a window it was never part of."""
    with pytest.raises(ValueError, match="falls outside the window"):
        make_scenario(
            focus_start=WINDOW_START - timedelta(days=1), focus_end=WINDOW_END
        ).validate()
    with pytest.raises(ValueError, match="falls outside the window"):
        make_scenario(
            focus_start=WINDOW_START, focus_end=WINDOW_END + timedelta(days=1)
        ).validate()


def test_no_focus_is_valid_and_is_the_default():
    scenario = make_scenario()
    assert scenario.focus_start is None and scenario.focus_end is None
    scenario.validate()


def test_a_focus_survives_a_round_trip_through_json():
    scenario = make_scenario(
        focus_start=WINDOW_START + timedelta(days=7),
        focus_end=WINDOW_START + timedelta(days=11),
    )
    restored = Scenario.from_dict(scenario.to_dict())
    assert (restored.focus_start, restored.focus_end) == (
        scenario.focus_start,
        scenario.focus_end,
    )


def test_a_trip_saved_before_focus_existed_still_loads():
    """Every scenario file on disk predates the field."""
    payload = make_scenario().to_dict()
    del payload["focus_start"], payload["focus_end"]
    assert Scenario.from_dict(payload).focus_start is None


# ------------------------------------------------------------- overland stops


def _overland_trip(**overrides):
    return make_scenario(
        stops=[
            Stop(airports=["NRT", "KIX"], stay_days=(9, 11), label="Japan", overland=True),
            Stop(airports=["MNL", "CEB"], stay_days=(9, 11), label="Philippines"),
        ],
        **overrides,
    )


def test_a_stop_is_not_overland_unless_it_says_so():
    assert make_scenario().stops[0].overland is False


def test_overland_survives_a_save_and_load(tmp_path):
    original = _overland_trip()
    save_scenario(original, tmp_path)
    loaded = load_scenario(tmp_path / "japan-philippines.json")
    # `to_dict` lists a stop's fields by hand, so a new one is dropped silently
    # unless it is named there: the trip would come back off disk chaining
    # Haneda to Haneda and nothing would say why.
    assert loaded.stops[0].overland is True
    assert loaded.stops[1].overland is False
    assert loaded == original


def test_a_stop_with_one_airport_may_not_be_overland():
    trip = make_scenario(
        stops=[
            Stop(airports=["NRT"], stay_days=(9, 11), label="Japan", overland=True),
            Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
        ]
    )
    with pytest.raises(ValueError, match="Japan"):
        trip.validate()


def test_an_overland_trip_is_valid():
    _overland_trip().validate()


def test_a_file_without_overland_still_loads(tmp_path):
    path = tmp_path / "old.json"
    payload = make_scenario().to_dict()
    for stop in payload["stops"]:
        stop.pop("overland", None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_scenario(path).stops[0].overland is False


# ------------------------------------------------------------------- watches
#
# A watch is one candidate trip being tracked on its exact dates: the departure
# date, plus the leg dates of the trip that won it. Pinning the later legs is
# what makes a watch affordable enough to run every few hours - deriving them
# through the stay ranges instead costs 75 searches a candidate against 21.


def _watch(dates=("2027-01-10", "2027-01-20", "2027-01-30"), **overrides):
    from src.scenario import Watch

    return Watch(depart_dates=[date.fromisoformat(d) for d in dates], **overrides)


def test_a_trip_watches_nothing_by_default():
    assert make_scenario().watches == []


def test_a_watch_is_keyed_by_the_day_you_leave():
    assert _watch().key == "2027-01-10"


def test_watches_survive_a_save_and_load(tmp_path):
    original = make_scenario(watches=[_watch(added_price=21000.0)])
    save_scenario(original, tmp_path)
    loaded = load_scenario(tmp_path / "japan-philippines.json")
    assert loaded.watches[0].depart_dates[0] == date(2027, 1, 10)
    assert loaded.watches[0].added_price == 21000.0
    assert loaded == original


def test_a_file_without_watches_still_loads(tmp_path):
    payload = make_scenario().to_dict()
    payload.pop("watches", None)
    path = tmp_path / "old.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_scenario(path).watches == []


def test_a_watch_needs_one_date_per_leg():
    trip = make_scenario(watches=[_watch(dates=("2027-01-10", "2027-01-20"))])
    with pytest.raises(ValueError, match="one date per leg"):
        trip.validate()


def test_a_watch_may_not_run_backwards():
    trip = make_scenario(watches=[_watch(dates=("2027-01-10", "2027-01-09", "2027-01-30"))])
    with pytest.raises(ValueError, match="order"):
        trip.validate()


def test_a_watch_must_respect_the_stay_windows():
    # 4 days in Japan, against a [9, 11] stay: this trip was never possible, so
    # watching it would price a chain the combiner can never close.
    trip = make_scenario(watches=[_watch(dates=("2027-01-10", "2027-01-14", "2027-01-24"))])
    with pytest.raises(ValueError, match="Japan"):
        trip.validate()


def test_a_watch_must_start_inside_the_window():
    trip = make_scenario(watches=[_watch(dates=("2026-12-10", "2026-12-20", "2026-12-30"))])
    with pytest.raises(ValueError, match="window"):
        trip.validate()


def test_the_same_day_may_not_be_watched_twice():
    trip = make_scenario(watches=[_watch(), _watch()])
    with pytest.raises(ValueError, match="twice|already"):
        trip.validate()


def test_there_is_a_ceiling_on_how_many_days_may_be_watched():
    from src.scenario import MAX_WATCHES

    starts = [date(2027, 1, 6) + timedelta(days=i) for i in range(MAX_WATCHES + 1)]
    trip = make_scenario(
        watches=[
            _watch(dates=(s.isoformat(), (s + timedelta(days=10)).isoformat(),
                          (s + timedelta(days=20)).isoformat()))
            for s in starts
        ]
    )
    with pytest.raises(ValueError, match="at once|too many"):
        trip.validate()


def test_a_valid_set_of_watches_passes():
    make_scenario(
        watches=[
            _watch(),
            _watch(dates=("2027-01-12", "2027-01-22", "2027-02-01")),
        ]
    ).validate()


def test_a_watch_on_a_one_way_trip_has_no_stay_after_the_last_leg():
    """The final stop is where the trip ends, so nothing follows it.

    Walking every stop looking for the leg that leaves it runs off the end of
    the dates on a one-way chain - and it does so inside validation, so the
    trip cannot be saved at all and the error names an index rather than
    anything a person could act on.
    """
    trip = make_scenario(one_way=True, watches=[_watch(dates=("2027-01-10", "2027-01-20"))])
    trip.validate()
