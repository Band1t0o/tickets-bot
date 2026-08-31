"""The probe's own airport list, and what it is for.

The probe exists to say which airports are worth flying from. Acting on its
answer means taking the losers out of the trip - and doing that used to delete
the answer: the verdict table filtered to the trip's own pools, so a dropped
airport lost its row, and the next probe never asked about it again either. The
one number you narrowed *by* was gone the moment you narrowed.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.scenario import Scenario, probing
from src.sweep.planner import PLANS


def _trip(**over) -> Scenario:
    data = {
        "id": "t",
        "name": "T",
        "origins": ["VIE", "PRG"],
        "stops": [{"label": "Japan", "airports": ["HND", "KIX"], "stay_days": [10, 14]}],
        "window_start": "2027-01-01",
        "window_end": "2027-02-01",
        "depth": "quick",
    }
    data.update(over)
    return Scenario.from_dict(data)


def test_a_pool_is_named_by_its_role_not_its_position():
    """`probe_extra` is keyed by these. Position renumbers when a stop is added,
    and reading one trip's pool 2 as another's is the failure this module has
    already had twice."""
    trip = _trip()
    assert trip.pool_keys == ["origins", "stop:0", "origins"]

    # A stop added in front does not renumber the origins, which is the whole
    # reason the key is not the index.
    grown = _trip(
        stops=[
            {"label": "Dubai", "airports": ["DXB"], "stay_days": [2, 3]},
            {"label": "Japan", "airports": ["HND", "KIX"], "stay_days": [10, 14]},
        ]
    )
    assert grown.pool_keys[0] == "origins"
    assert grown.pool_keys.index("stop:1") == 2


def test_the_way_home_shares_the_key_of_the_way_out_when_it_is_the_same_list():
    """A round trip's last pool *is* its origins. Probing "the way home" of such
    a trip means probing the airports you leave from, and one key says so."""
    trip = _trip()
    assert trip.pool_keys[0] == trip.pool_keys[-1] == "origins"

    widened = probing(_trip(probe_extra={"origins": ["BTS"]}))
    assert widened.airport_pools[0] == ["VIE", "PRG", "BTS"]
    assert widened.airport_pools[-1] == ["VIE", "PRG", "BTS"]


def test_a_separate_return_list_has_its_own_key():
    trip = _trip(return_to=["BUD"])
    assert trip.pool_keys == ["origins", "stop:0", "return_to"]
    widened = probing(_trip(return_to=["BUD"], probe_extra={"return_to": ["BTS"]}))
    assert widened.return_to == ["BUD", "BTS"]
    assert widened.origins == ["VIE", "PRG"]


def test_the_probe_asks_about_airports_the_trip_no_longer_searches():
    """The whole point. The sweep prices two Japanese airports; the probe keeps
    pricing the third, so the comparison that retired it stays current."""
    trip = _trip(probe_extra={"stop:0": ["NRT"]})
    assert trip.airport_pools[1] == ["HND", "KIX"]
    assert trip.probe_pools[1] == ["HND", "KIX", "NRT"]

    probe = PLANS["explore"](trip)
    assert any(s.destination == "NRT" for s in probe), "the probe never asked about NRT"

    # And the sweep does not, which is what makes the list worth having: the
    # evidence stays current without the 340-search plan growing.
    assert not any(s.destination == "NRT" for s in PLANS["sweep"](trip))


def test_an_airport_in_both_lists_is_searched_once():
    """Put an airport back into the trip while it is still on the probe list and
    nothing should be asked twice. A duplicate here is a real search against a
    site that answers about 120 of them per runner."""
    trip = _trip(probe_extra={"stop:0": ["HND"]})
    assert trip.probe_pools[1] == ["HND", "KIX"]

    searches = PLANS["explore"](trip)
    keys = [(s.origin, s.destination, s.depart_date) for s in searches]
    assert len(keys) == len(set(keys))


def test_a_key_naming_no_current_pool_is_kept_and_not_probed():
    """A stop removed for an afternoon must not cost a year of probe list, and a
    list being kept and not used must not also be invisible."""
    trip = _trip(probe_extra={"stop:7": ["NRT"], "stop:0": ["CTS"]})
    trip.validate()

    assert trip.probe_extra_unused == {"stop:7": ["NRT"]}
    assert not any(s.destination == "NRT" for s in PLANS["explore"](trip))
    assert any(s.destination == "CTS" for s in PLANS["explore"](trip))

    # Still on disk after a round trip, which is what "kept" means.
    assert Scenario.from_dict(trip.to_dict()).probe_extra["stop:7"] == ["NRT"]


def test_nothing_lands_on_the_probe_list_by_editing_the_route():
    """Deliberate. A list that grew on its own would walk a 51-search probe up
    toward the cost of the sweep it exists to avoid."""
    trip = _trip(probe_extra={})
    narrowed = replace(
        trip, stops=[replace(trip.stops[0], airports=["HND"])]
    )
    assert narrowed.probe_extra == {}
    assert narrowed.probe_pools[1] == ["HND"]


def test_a_trip_with_no_probe_list_is_not_copied():
    """`probing` is called on every estimate. The common case must be free."""
    trip = _trip()
    assert probing(trip) is trip


def test_a_lower_case_code_typed_into_the_panel_is_the_same_airport():
    trip = Scenario.from_dict({**_trip().to_dict(), "probe_extra": {"origins": ["ktw"]}})
    trip.validate()
    assert trip.probe_extra["origins"] == ["KTW"]


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"origins": ["Brno"]}, "not a 3-letter IATA code"),
        ({"origins": ["KTW", "KTW"]}, "twice"),
        ({"origins": "KTW"}, "must be a list"),
    ],
)
def test_the_probe_list_refuses_what_it_cannot_mean(extra, message):
    with pytest.raises(ValueError, match=message):
        _trip(probe_extra=extra).validate()


def test_the_probe_costs_more_when_it_is_asked_to_watch_more():
    """The number on the button has to move, or the trade is invisible."""
    plain = len(PLANS["explore"](_trip()))
    watched = len(PLANS["explore"](_trip(probe_extra={"stop:0": ["NRT", "CTS"]})))
    assert watched > plain


def test_the_snapshot_records_the_widened_trip(tmp_path):
    """`_sweep_scenario` takes a run's shape from this file. A snapshot narrower
    than the plan would make every extra airport's answer look like it belonged
    to another trip - the exact failure `_write_scenario` exists to stop."""
    from src.sweep.runner import run_sweep

    trip = _trip(id="snap", probe_extra={"stop:0": ["NRT"]})

    class Nothing:
        def search(self, *args, **kwargs):
            return []

    result = run_sweep(
        trip, provider=Nothing(), mode="explore", data_dir=tmp_path, delay_s=0
    )
    written = json.loads((result.directory / "scenario.json").read_text(encoding="utf-8"))
    assert written["stops"][0]["airports"] == ["HND", "KIX", "NRT"]


def test_a_sweep_snapshot_is_not_widened(tmp_path):
    """Only the probe searches the wider list, so only the probe may record it."""
    from src.sweep.runner import run_sweep

    trip = _trip(id="snap2", probe_extra={"stop:0": ["NRT"]})

    class Nothing:
        def search(self, *args, **kwargs):
            return []

    result = run_sweep(
        trip, provider=Nothing(), mode="sweep", data_dir=tmp_path, delay_s=0
    )
    written = json.loads((result.directory / "scenario.json").read_text(encoding="utf-8"))
    assert written["stops"][0]["airports"] == ["HND", "KIX"]


def test_a_hand_edited_probe_list_cannot_be_planned_as_airports():
    """Loading a trip does not validate it - `load_scenario` parses and returns
    - so a string where a list belongs would be iterated as three characters and
    planned as three airports. The read path repairs and the write path refuses,
    which is the rule `home_airports` already follows."""
    trip = Scenario.from_dict({**_trip().to_dict(), "probe_extra": {"origins": "KTW"}})

    assert trip.probe_pools[0] == ["VIE", "PRG"], "no phantom airports"
    assert not any(len(s.origin) != 3 for s in PLANS["explore"](trip))
    with pytest.raises(ValueError, match="must be a list"):
        trip.validate()
