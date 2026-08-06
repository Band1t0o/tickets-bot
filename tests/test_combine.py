"""Tests for chaining cached legs into valid itineraries.

These encode the trip rules: continuity between legs, both stay windows, and
that stay lengths are computed from the dates actually returned by the site -
never from the dates that were requested.
"""
from __future__ import annotations

from datetime import date

from src.combine import best_open_jaw, best_same_airport, combine
from src.models import Leg
from src.scenario import Scenario


def scenario(**overrides) -> Scenario:
    defaults = dict(
        id="jp-ph",
        name="Test",
        trip_type="multi_city",
        origins=["PRG", "VIE"],
        japan_airports=["NRT", "KIX"],
        ph_airports=["MNL"],
        window_start=date(2027, 1, 5),
        window_end=date(2027, 2, 8),
        japan_stay_days=(9, 11),
        ph_stay_days=(9, 11),
    )
    defaults.update(overrides)
    return Scenario(**defaults)


def leg(origin, destination, depart, price=10000.0, airline="XX") -> Leg:
    return Leg(
        provider="TEST",
        origin=origin,
        destination=destination,
        depart_date=depart,
        airline=airline,
        flight_number=None,
        stops=1,
        price_currency="CZK",
        price_amount=price,
        url="https://example.test/leg",
    )


# A valid chain: PRG->NRT 10 Jan, NRT->MNL 20 Jan (10 days), MNL->PRG 30 Jan (10 days)
LEG_A = leg("PRG", "NRT", date(2027, 1, 10), 12000)
LEG_B = leg("NRT", "MNL", date(2027, 1, 20), 4000)
LEG_C = leg("MNL", "PRG", date(2027, 1, 30), 14000)


def test_builds_a_valid_three_leg_itinerary():
    result = combine([LEG_A, LEG_B, LEG_C], scenario())
    assert len(result) == 1
    assert result[0].total_price == 30000
    assert result[0].route == "PRG → NRT → MNL → PRG"


def test_rejects_combination_violating_japan_stay_window():
    # Arrives 10 Jan, leaves Japan 25 Jan = 15 days, outside [9, 11].
    too_late = leg("NRT", "MNL", date(2027, 1, 25), 4000)
    assert combine([LEG_A, too_late, LEG_C], scenario()) == []


def test_rejects_combination_violating_philippines_stay_window():
    too_soon = leg("MNL", "PRG", date(2027, 1, 22), 14000)  # only 2 days
    assert combine([LEG_A, LEG_B, too_soon], scenario()) == []


def test_requires_leg_continuity():
    # Leg A lands at NRT but leg B departs KIX: not a chain.
    from_kix = leg("KIX", "MNL", date(2027, 1, 20), 4000)
    assert combine([LEG_A, from_kix, LEG_C], scenario()) == []


def test_rejects_return_to_an_airport_outside_the_configured_origins():
    to_lhr = leg("MNL", "LHR", date(2027, 1, 30), 9000)
    assert combine([LEG_A, LEG_B, to_lhr], scenario()) == []


def test_uses_actual_dates_not_requested_dates():
    # The site substitutes nearby dates: this leg was requested for 20 Jan but
    # came back as 22 Jan, making the Japan stay 12 days - outside [9, 11].
    substituted = leg("NRT", "MNL", date(2027, 1, 22), 4000)
    assert combine([LEG_A, substituted, LEG_C], scenario()) == []


def test_returns_cheapest_first():
    cheaper_b = leg("NRT", "MNL", date(2027, 1, 20), 3000, airline="YY")
    result = combine([LEG_A, LEG_B, cheaper_b, LEG_C], scenario())
    assert [i.total_price for i in result] == sorted(i.total_price for i in result)
    assert result[0].total_price == 29000


def test_open_jaw_itineraries_are_allowed_and_flagged():
    to_vie = leg("MNL", "VIE", date(2027, 1, 30), 11000)
    result = combine([LEG_A, LEG_B, to_vie], scenario())
    assert len(result) == 1
    assert result[0].same_airport is False


def test_best_same_airport_and_best_open_jaw_are_reported_separately():
    to_vie = leg("MNL", "VIE", date(2027, 1, 30), 11000)  # cheaper, open jaw
    result = combine([LEG_A, LEG_B, LEG_C, to_vie], scenario())
    assert best_open_jaw(result).total_price == 27000
    assert best_same_airport(result).total_price == 30000


def test_best_helpers_return_none_when_nothing_qualifies():
    assert best_same_airport([]) is None
    assert best_open_jaw([]) is None


def test_round_trip_scenario_pairs_outbound_with_return():
    rt = scenario(trip_type="round_trip", trip_length_days=(18, 22), ph_airports=[])
    out = leg("PRG", "NRT", date(2027, 1, 10), 12000)
    back = leg("NRT", "PRG", date(2027, 1, 30), 13000)  # 20 days later
    result = combine([out, back], rt)
    assert len(result) == 1
    assert result[0].total_price == 25000


def test_round_trip_rejects_lengths_outside_the_configured_range():
    rt = scenario(trip_type="round_trip", trip_length_days=(18, 22), ph_airports=[])
    out = leg("PRG", "NRT", date(2027, 1, 10), 12000)
    back = leg("NRT", "PRG", date(2027, 1, 15), 13000)  # only 5 days
    assert combine([out, back], rt) == []


def test_result_count_is_capped():
    legs = [LEG_A, LEG_B]
    # 80 distinct return legs, all valid - output must be capped for the UI.
    legs += [leg("MNL", "PRG", date(2027, 1, 30), 14000 + i, airline=f"A{i}") for i in range(80)]
    assert len(combine(legs, scenario())) <= 50


def test_limit_none_returns_every_itinerary():
    legs = [LEG_A, LEG_B]
    legs += [leg("MNL", "PRG", date(2027, 1, 30), 14000 + i, airline=f"A{i}") for i in range(80)]
    assert len(combine(legs, scenario(), limit=None)) == 80


def test_by_date_series_keeps_the_cheapest_per_departure_date():
    from src.combine import cheapest_by_departure_date

    # Two departure dates; the 12th has a cheaper option available.
    second_a = leg("PRG", "NRT", date(2027, 1, 12), 9000)
    second_b = leg("NRT", "MNL", date(2027, 1, 22), 4000)
    second_c = leg("MNL", "PRG", date(2027, 2, 1), 14000)
    itineraries = combine([LEG_A, LEG_B, LEG_C, second_a, second_b, second_c], scenario(), limit=None)

    series = cheapest_by_departure_date(itineraries)
    assert [row["depart_date"] for row in series] == ["2027-01-10", "2027-01-12"]
    assert series[1]["cheapest_total"] == 27000


def test_empty_input_yields_nothing():
    assert combine([], scenario()) == []
