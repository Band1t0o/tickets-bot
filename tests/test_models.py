"""Tests for Leg and Itinerary.

The hash tests encode a real bug: the old Offer model hardcoded airline and
flight_number to "Unknown", so ten genuinely different flights collapsed into a
single content hash. A live run wrote 10 rows that reduced to 1 distinct hash,
and Discord duly announced "10 New Flight Offers" for one flight.
"""
from __future__ import annotations

from datetime import date

from src.models import Itinerary


def test_legs_differing_only_by_flight_number_have_different_hashes(make_leg):
    a = make_leg(flight_number="LH1400")
    b = make_leg(flight_number="QR8100")
    assert a.content_hash() != b.content_hash()


def test_legs_differing_only_by_airline_have_different_hashes(make_leg):
    assert make_leg(airline="Lufthansa").content_hash() != make_leg(airline="Qatar Airways").content_hash()


def test_legs_differing_only_by_stops_have_different_hashes(make_leg):
    assert make_leg(stops=0).content_hash() != make_leg(stops=1).content_hash()


def test_identical_legs_share_a_hash(make_leg):
    assert make_leg().content_hash() == make_leg().content_hash()


def test_itinerary_totals_three_legs(make_leg):
    it = Itinerary(
        legs=[
            make_leg(price_amount=10000),
            make_leg(price_amount=4000),
            make_leg(price_amount=12000),
        ]
    )
    assert it.total_price == 26000


def test_same_airport_flag_detects_open_jaw(make_leg):
    it = Itinerary(
        legs=[
            make_leg(origin="PRG", destination="NRT"),
            make_leg(origin="NRT", destination="MNL"),
            make_leg(origin="MNL", destination="VIE"),
        ]
    )
    assert it.same_airport is False


def test_same_airport_flag_detects_closed_loop(make_leg):
    it = Itinerary(
        legs=[
            make_leg(origin="PRG", destination="NRT"),
            make_leg(origin="NRT", destination="MNL"),
            make_leg(origin="MNL", destination="PRG"),
        ]
    )
    assert it.same_airport is True


def test_total_for_party_multiplies_per_person_price(make_leg):
    # Card prices on pelikan.cz are per person, verified live against
    # P:1000E_0_0 / P:2000E_0_0 / P:3000E_0_0 returning identical prices.
    it = Itinerary(legs=[make_leg(price_amount=10000), make_leg(price_amount=5000)])
    assert it.total_for_party(2) == 30000


def test_itinerary_exposes_departure_and_return_dates(make_leg):
    it = Itinerary(
        legs=[
            make_leg(depart_date=date(2027, 1, 12)),
            make_leg(depart_date=date(2027, 1, 22)),
            make_leg(depart_date=date(2027, 2, 1)),
        ]
    )
    assert it.departure_date == date(2027, 1, 12)
    assert it.return_date == date(2027, 2, 1)
