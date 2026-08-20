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


# --------------------------------------------------------------- observed_at
#
# A deep sweep runs ~97 minutes and the probe caught FRA->NRT moving 21% inside
# a single two-hour window, so a leg read at minute 3 and one read at minute 95
# are not the same measurement. Every price therefore carries the moment it was
# read off the page.


def test_observed_at_survives_a_round_trip(make_leg):
    leg = make_leg(observed_at="2026-08-10T11:59:04+00:00")
    from src.models import Leg

    assert Leg.from_dict(leg.to_dict()).observed_at == "2026-08-10T11:59:04+00:00"


def test_a_leg_written_before_observed_at_existed_still_loads(make_leg):
    """The four committed sweeps predate the field; they must not fail to load."""
    from src.models import Leg

    payload = make_leg().to_dict()
    del payload["observed_at"]
    assert Leg.from_dict(payload).observed_at is None


def test_observed_at_does_not_change_the_content_hash(make_leg):
    """Hashing it would give one flight two hashes and make _dedupe a no-op.

    The same reasoning already keeps checked_bag out: the timestamp belongs to
    the observation, not to the flight.
    """
    early = make_leg(observed_at="2026-08-10T11:59:04+00:00")
    late = make_leg(observed_at="2026-08-10T13:22:47+00:00")
    assert early.content_hash() == late.content_hash()


def test_itinerary_observed_at_is_its_stalest_leg(make_leg):
    """A trip is only as fresh as the oldest price in it."""
    it = Itinerary(
        legs=[
            make_leg(observed_at="2026-08-10T12:40:00+00:00"),
            make_leg(observed_at="2026-08-10T11:15:00+00:00"),
            make_leg(observed_at="2026-08-10T12:05:00+00:00"),
        ]
    )
    assert it.observed_at == "2026-08-10T11:15:00+00:00"


def test_itinerary_observed_span_reports_how_far_apart_the_prices_were(make_leg):
    it = Itinerary(
        legs=[
            make_leg(observed_at="2026-08-10T11:15:00+00:00"),
            make_leg(observed_at="2026-08-10T12:45:00+00:00"),
        ]
    )
    assert it.observed_span_minutes == 90


def test_itinerary_without_timestamps_reports_neither(make_leg):
    it = Itinerary(legs=[make_leg(), make_leg()])
    assert it.observed_at is None
    assert it.observed_span_minutes is None


def test_itinerary_to_dict_carries_the_observation_window(make_leg):
    it = Itinerary(
        legs=[
            make_leg(observed_at="2026-08-10T11:15:00+00:00"),
            make_leg(observed_at="2026-08-10T12:45:00+00:00"),
        ]
    )
    payload = it.to_dict()
    assert payload["observed_at"] == "2026-08-10T11:15:00+00:00"
    assert payload["observed_span_minutes"] == 90


# ------------------------------------------------------- overland in a route


def test_route_marks_where_you_travel_overland(make_leg):
    # Fly into Haneda, cross Japan on the ground, fly out of Kansai. Joining the
    # leg endpoints blindly renders "VIE → HND → MNL" and hides the fact that
    # you are getting yourself 500 km down the country - the same class of lie
    # as a sweep reporting error_count: 0.
    it = Itinerary(
        legs=[
            make_leg(origin="VIE", destination="HND"),
            make_leg(origin="KIX", destination="MNL", depart_date=date(2027, 1, 21)),
            make_leg(origin="MNL", destination="VIE", depart_date=date(2027, 1, 31)),
        ]
    )
    assert it.route == "VIE → HND ⇢ KIX → MNL → VIE"
    assert it.has_overland is True


def test_a_route_with_no_ground_hop_reads_exactly_as_before(make_leg):
    it = Itinerary(
        legs=[
            make_leg(origin="VIE", destination="HND"),
            make_leg(origin="HND", destination="MNL", depart_date=date(2027, 1, 21)),
        ]
    )
    assert it.route == "VIE → HND → MNL"
    assert it.has_overland is False


def test_to_dict_reports_the_ground_hop(make_leg):
    it = Itinerary(
        legs=[
            make_leg(origin="VIE", destination="HND"),
            make_leg(origin="KIX", destination="MNL", depart_date=date(2027, 1, 21)),
        ]
    )
    assert it.to_dict()["has_overland"] is True
