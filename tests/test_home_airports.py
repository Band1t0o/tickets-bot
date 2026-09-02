"""Tests for the convenience ranking of the airports near home.

The axis nothing else measures. `frequent_airports` counts what the saved trips
use, and `preferred_origins` ranks what Discord should report - neither can say
that Brno is a tram ride and Vienna is a coach, and neither could learn it,
because the usual reason a convenient airport goes unused is that it has no
long-haul inventory at all.

So the things under test are the ones that keep it from getting in the way: that
a missing or broken file costs you the ordering and never the page, and that a
typo typed into the form is refused by name rather than silently dropped.
"""
from __future__ import annotations

import json

import pytest

from src.home_airports import load_ranking, load_tiers, save_tiers


def test_a_ranking_round_trips_in_the_order_it_was_given(tmp_path):
    """Position is rank, so the order is the entire content of the file."""
    save_tiers([["BRQ"], ["PRG"], ["VIE"]], tmp_path)
    assert load_ranking(tmp_path) == ["BRQ", "PRG", "VIE"]


def test_codes_are_taken_however_they_are_typed(tmp_path):
    save_tiers([[" brq "], ["Prg"]], tmp_path)
    assert load_ranking(tmp_path) == ["BRQ", "PRG"]


def test_no_file_is_no_ranking_rather_than_an_error(tmp_path):
    """Empty means "no ranking", and every caller falls back to what it did
    before. It is the normal state of a fresh checkout."""
    assert load_ranking(tmp_path) == []


def test_a_corrupt_file_costs_the_ordering_and_not_the_page(tmp_path):
    """The same rule as a corrupt `sources.json`: degrade to the default.

    A hand-edited file with a trailing comma should cost you the order of some
    chips, not the ability to build a trip at all.
    """
    (tmp_path / "home_airports.json").write_text("{ nonsense,", encoding="utf-8")
    assert load_ranking(tmp_path) == []


def test_a_bad_code_already_on_disk_is_skipped_rather_than_fatal(tmp_path):
    (tmp_path / "home_airports.json").write_text(
        json.dumps({"airports": ["BRQ", "nonsense", "PRG"]}), encoding="utf-8"
    )
    assert load_ranking(tmp_path) == ["BRQ", "PRG"]


def test_a_repeated_code_on_disk_keeps_its_first_place(tmp_path):
    """Rank is position, so one airport twice would have two ranks and the
    reading would depend on which loop found it first."""
    (tmp_path / "home_airports.json").write_text(
        json.dumps({"airports": ["BRQ", "PRG", "BRQ"]}), encoding="utf-8"
    )
    assert load_ranking(tmp_path) == ["BRQ", "PRG"]


def test_a_typo_typed_into_the_form_is_refused_by_name(tmp_path):
    """Unlike the read path, which repairs. Nobody typed what is on disk; they
    did type this, and it is a mistake worth being told about."""
    with pytest.raises(ValueError, match="Brno"):
        save_tiers([["BRQ"], ["Brno"]], tmp_path)


def test_the_same_airport_twice_in_one_save_is_refused(tmp_path):
    with pytest.raises(ValueError, match="twice"):
        save_tiers([["BRQ"], ["PRG"], ["BRQ"]], tmp_path)


def test_two_airports_can_share_a_rank(tmp_path):
    """Prague and Vienna are both a morning. Forcing an order on them invents a
    preference nobody holds."""
    save_tiers([["BRQ"], ["PRG", "VIE"]], tmp_path)
    assert load_tiers(tmp_path) == [["BRQ"], ["PRG", "VIE"]]


def test_a_file_written_before_tiers_existed_still_reads(tmp_path):
    """One airport per tier is exactly what a flat list meant, so there is
    nothing to migrate and an older checkout keeps working."""
    (tmp_path / "home_airports.json").write_text(
        json.dumps({"airports": ["BRQ", "PRG", "VIE"]}), encoding="utf-8"
    )
    assert load_tiers(tmp_path) == [["BRQ"], ["PRG"], ["VIE"]]
    assert load_ranking(tmp_path) == ["BRQ", "PRG", "VIE"]


def test_the_chips_still_get_one_flat_order(tmp_path):
    """A row of buttons has no way to show two airports at the same rank."""
    save_tiers([["BRQ"], ["PRG", "VIE"]], tmp_path)
    assert load_ranking(tmp_path) == ["BRQ", "PRG", "VIE"]


def test_an_empty_tier_is_dropped_rather_than_kept_as_a_hole(tmp_path):
    assert save_tiers([["BRQ"], [], ["PRG"]], tmp_path) == [["BRQ"], ["PRG"]]


def test_a_trip_that_says_nothing_is_reported_from_your_own_airports(tmp_path):
    """The dedupe. Two lists that had to be kept in step are now one, and a trip
    inherits the tiers verbatim - they are already the same shape."""
    from src.scenario import Scenario

    save_tiers([["BRQ"], ["PRG", "VIE"]], tmp_path)
    trip = Scenario.from_dict(
        {
            "id": "t",
            "name": "T",
            "origins": ["PRG"],
            "stops": [{"label": "Japan", "airports": ["HND"], "stay_days": [10, 14]}],
            "window_start": "2027-01-01",
            "window_end": "2027-02-01",
        }
    )
    assert trip.preferred_origins == []
    assert trip.reporting_tiers(tmp_path) == [["BRQ"], ["PRG", "VIE"]]


def test_a_trip_with_its_own_ranking_keeps_it(tmp_path):
    """Inheritance is a fallback, never an override."""
    from src.scenario import Scenario

    save_tiers([["BRQ"], ["PRG", "VIE"]], tmp_path)
    trip = Scenario.from_dict(
        {
            "id": "t",
            "name": "T",
            "origins": ["PRG"],
            "stops": [{"label": "Japan", "airports": ["HND"], "stay_days": [10, 14]}],
            "window_start": "2027-01-01",
            "window_end": "2027-02-01",
            "preferred_origins": [["VIE"]],
        }
    )
    assert trip.reporting_tiers(tmp_path) == [["VIE"]]


def test_the_ranking_is_not_written_into_the_trip(tmp_path):
    """Inherited at read time. Writing today's answer into the file would freeze
    it, and would make every trip claim a preference nobody expressed for it."""
    from src.scenario import Scenario

    save_tiers([["BRQ"]], tmp_path)
    trip = Scenario.from_dict(
        {
            "id": "t",
            "name": "T",
            "origins": ["PRG"],
            "stops": [{"label": "Japan", "airports": ["HND"], "stay_days": [10, 14]}],
            "window_start": "2027-01-01",
            "window_end": "2027-02-01",
        }
    )
    trip.reporting_tiers(tmp_path)
    assert trip.to_dict()["preferred_origins"] == []

    # And it follows the global list as the global list changes.
    save_tiers([["OSR"]], tmp_path)
    assert trip.reporting_tiers(tmp_path) == [["OSR"]]
