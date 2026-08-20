"""Which deals a finished sweep is worth reporting.

Pure: these run against hand-built legs, never a browser. The point of the
module under test is that "the cheapest" and "the cheapest I would actually
enjoy flying" are different answers, and reporting only the first is how a
watcher ends up recommending Frankfurt every single day.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.alerts import select_alerts
from tests.conftest import make_scenario

TIERS = [["PRG", "VIE"], ["BER", "KRK"], ["FRA"]]


def trip(make_leg, origin, home, *, price, via="NRT", then="MNL"):
    """One complete three-leg itinerary at a known total."""
    return [
        make_leg(origin=origin, destination=via, depart_date=date(2027, 1, 12),
                 price_amount=price - 6000, checked_bag=True),
        make_leg(origin=via, destination=then, depart_date=date(2027, 1, 22),
                 price_amount=3000, checked_bag=True),
        make_leg(origin=then, destination=home, depart_date=date(2027, 2, 1),
                 price_amount=3000, checked_bag=True),
    ]


def picks(legs, **overrides):
    scenario = make_scenario(
        origins=["PRG", "VIE", "FRA", "BER", "KRK"],
        stops=make_scenario().stops,
        **overrides,
    )
    return {p.name: p for p in select_alerts(legs, scenario)}


# --------------------------------------------------------------- cheapest


def test_the_cheapest_pick_is_the_best_bag_inclusive_total(make_leg):
    legs = trip(make_leg, "FRA", "FRA", price=21000) + trip(make_leg, "PRG", "PRG", price=30000)
    result = picks(legs, preferred_origins=TIERS)
    assert result["cheapest"].itinerary.total_with_bags(1500) == 21000
    assert result["cheapest"].itinerary.legs[0].origin == "FRA"


def test_a_trip_with_no_itineraries_yields_no_picks(make_leg):
    assert picks([], preferred_origins=TIERS) == {}


# -------------------------------------------------------------- preferred


def test_the_preferred_pick_comes_from_the_best_tier_that_has_anything(make_leg):
    # FRA is cheapest but sits in tier 3; PRG is tier 1 and therefore preferred
    # even though it costs more.
    legs = trip(make_leg, "FRA", "FRA", price=21000) + trip(make_leg, "PRG", "PRG", price=30000)
    result = picks(legs, preferred_origins=TIERS)
    assert result["preferred"].itinerary.legs[0].origin == "PRG"
    assert result["preferred"].tier == 1


def test_the_preferred_pick_falls_to_a_lower_tier_when_the_top_is_empty(make_leg):
    legs = trip(make_leg, "FRA", "FRA", price=21000) + trip(make_leg, "BER", "BER", price=25000)
    result = picks(legs, preferred_origins=TIERS)
    assert result["preferred"].itinerary.legs[0].origin == "BER"
    assert result["preferred"].tier == 2


def test_the_cheapest_within_a_tier_wins(make_leg):
    # The FRA trip is there to keep the preferred pick distinct from the
    # cheapest one; without it the two collapse into a single card and there is
    # no separate "preferred" to inspect.
    legs = (
        trip(make_leg, "FRA", "FRA", price=21000)
        + trip(make_leg, "PRG", "PRG", price=30000)
        + trip(make_leg, "VIE", "VIE", price=27000)
    )
    assert picks(legs, preferred_origins=TIERS)["preferred"].itinerary.legs[0].origin == "VIE"


def test_an_open_jaw_is_ranked_by_its_worse_end(make_leg):
    """Flying out of Prague and home into Frankfurt is a tier-3 trip.

    Both ends have to be acceptable, or the "preferred" pick recommends a trip
    that strands you at the airport you were trying to avoid.
    """
    legs = trip(make_leg, "PRG", "FRA", price=21000) + trip(make_leg, "BER", "BER", price=25000)
    result = picks(legs, preferred_origins=TIERS)
    assert result["preferred"].itinerary.legs[0].origin == "BER"
    assert result["preferred"].tier == 2


def test_an_airport_outside_every_tier_is_never_preferred(make_leg):
    legs = trip(make_leg, "KRK", "KRK", price=25000)
    result = picks(legs, preferred_origins=[["PRG", "VIE"]])
    assert "preferred" not in result


def test_no_tiers_configured_means_no_preferred_pick(make_leg):
    legs = trip(make_leg, "FRA", "FRA", price=21000)
    assert "preferred" not in picks(legs, preferred_origins=[])


# ------------------------------------------------------------ the overlap


def test_one_pick_when_the_cheapest_is_already_top_tier(make_leg):
    """Two identical Discord cards for one flight is noise, not thoroughness."""
    legs = trip(make_leg, "PRG", "PRG", price=21000) + trip(make_leg, "FRA", "FRA", price=30000)
    result = picks(legs, preferred_origins=TIERS)
    assert len(result) == 1
    assert result["cheapest"].also_preferred is True
    assert result["cheapest"].tier == 1


def test_the_preferred_pick_reports_what_the_preference_costs(make_leg):
    legs = trip(make_leg, "FRA", "FRA", price=21000) + trip(make_leg, "PRG", "PRG", price=30000)
    result = picks(legs, preferred_origins=TIERS)
    assert result["preferred"].premium == 9000
    assert result["cheapest"].premium == 0


# ------------------------------------------------------------- the filter


@pytest.mark.parametrize("wanted", [["cheapest"], ["preferred"]])
def test_notify_selects_which_picks_are_reported(make_leg, wanted):
    legs = trip(make_leg, "FRA", "FRA", price=21000) + trip(make_leg, "PRG", "PRG", price=30000)
    assert list(picks(legs, preferred_origins=TIERS, notify=wanted)) == wanted


def test_notifying_on_nothing_yields_nothing(make_leg):
    legs = trip(make_leg, "FRA", "FRA", price=21000)
    assert picks(legs, preferred_origins=TIERS, notify=[]) == {}
