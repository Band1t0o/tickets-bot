"""Tests for chaining cached legs into valid itineraries.

These encode the trip rules: continuity between legs, both stay windows, and
that stay lengths are computed from the dates actually returned by the site -
never from the dates that were requested.
"""
from __future__ import annotations

from datetime import date

from src.combine import best_open_jaw, best_same_airport, combine
from src.models import Leg
from src.scenario import Scenario, Stop
from tests.conftest import make_scenario


def scenario(**overrides) -> Scenario:
    defaults = dict(
        id="jp-ph",
        name="Test",
        origins=["PRG", "VIE"],
        stops=[
            Stop(airports=["NRT", "KIX"], stay_days=(9, 11), label="Japan"),
            Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
        ],
    )
    defaults.update(overrides)
    return make_scenario(**defaults)


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
    rt = scenario(stops=[Stop(airports=["NRT"], stay_days=(18, 22), label="Japan")])
    out = leg("PRG", "NRT", date(2027, 1, 10), 12000)
    back = leg("NRT", "PRG", date(2027, 1, 30), 13000)  # 20 days later
    result = combine([out, back], rt)
    assert len(result) == 1
    assert result[0].total_price == 25000


def test_round_trip_rejects_lengths_outside_the_configured_range():
    rt = scenario(stops=[Stop(airports=["NRT"], stay_days=(18, 22), label="Japan")])
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


def _bag_leg(origin, destination, depart, price, checked_bag):
    out = leg(origin, destination, depart, price=price)
    out.checked_bag = checked_bag
    return out


def test_ranking_prefers_a_bag_inclusive_fare_over_a_cheaper_bagless_one():
    # 23,000 with a bag beats 22,000 where one leg's bag costs ~1,500 extra.
    # Ranking on the headline fare alone systematically flatters low-cost
    # carriers, which is exactly the leg the cheapest itinerary rides on.
    a1 = date(2027, 1, 5)
    b1 = date(2027, 1, 15)
    c1 = date(2027, 1, 25)
    bagged = [
        _bag_leg("PRG", "NRT", a1, 10000, True),
        _bag_leg("NRT", "MNL", b1, 4000, True),
        _bag_leg("MNL", "PRG", c1, 9000, True),
    ]
    bagless = [
        _bag_leg("VIE", "KIX", a1, 10000, True),
        _bag_leg("KIX", "MNL", b1, 3000, None),  # bag not confirmed
        _bag_leg("MNL", "VIE", c1, 9000, True),
    ]
    out = combine(bagged + bagless, scenario(bag_estimate=1500))
    assert out[0].total_price == 23000
    assert out[0].legs[0].origin == "PRG"


def test_total_with_bags_only_charges_unconfirmed_legs():
    it = combine(
        [
            _bag_leg("PRG", "NRT", date(2027, 1, 5), 10000, True),
            _bag_leg("NRT", "MNL", date(2027, 1, 15), 4000, None),
            _bag_leg("MNL", "PRG", date(2027, 1, 25), 9000, True),
        ],
        scenario(),
    )[0]
    assert it.total_price == 23000
    assert it.total_with_bags(1500) == 24500


# ---------------------------------------------------------------- overland
#
# Fly into Haneda, cross Japan on the ground, fly out of Kansai. Porto in,
# Lisbon out. Everywhere else a stop's airports are alternatives *because* of
# the chain rule - leave from the airport you landed at - and an overland stop
# suspends exactly that rule, for exactly one stop.


def overland_scenario(**overrides) -> Scenario:
    defaults = dict(
        stops=[
            Stop(airports=["NRT", "KIX"], stay_days=(9, 11), label="Japan", overland=True),
            Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
        ]
    )
    defaults.update(overrides)
    return scenario(**defaults)


def test_overland_stop_may_be_left_from_a_different_airport():
    from_kix = leg("KIX", "MNL", date(2027, 1, 20), 4000)
    result = combine([LEG_A, from_kix, LEG_C], overland_scenario())
    assert len(result) == 1
    assert result[0].legs[0].destination == "NRT"
    assert result[0].legs[1].origin == "KIX"


def test_overland_stop_still_accepts_leaving_from_the_airport_it_landed_at():
    # Suspending the chain rule must widen the search, never replace it.
    result = combine([LEG_A, LEG_B, LEG_C], overland_scenario())
    assert len(result) == 1
    assert result[0].legs[1].origin == "NRT"


def test_overland_does_not_leak_into_a_stop_that_did_not_ask_for_it():
    # Japan is overland, the Philippines is not: landing MNL and leaving CEB
    # is still not a chain.
    trip = overland_scenario(
        stops=[
            Stop(airports=["NRT", "KIX"], stay_days=(9, 11), label="Japan", overland=True),
            Stop(airports=["MNL", "CEB"], stay_days=(9, 11), label="Philippines"),
        ]
    )
    from_kix = leg("KIX", "MNL", date(2027, 1, 20), 4000)
    home_from_ceb = leg("CEB", "PRG", date(2027, 1, 30), 14000)
    assert combine([LEG_A, from_kix, home_from_ceb], trip) == []


def test_overland_still_enforces_the_stay_window_across_the_gap():
    # Days in Japan are counted from landing at NRT to leaving KIX, so an
    # overland stop is not a way to smuggle a 15-day stay past a [9, 11] rule.
    too_late = leg("KIX", "MNL", date(2027, 1, 25), 4000)
    assert combine([LEG_A, too_late, LEG_C], overland_scenario()) == []


def test_overland_finds_the_cheapest_when_the_winner_leaves_the_other_airport():
    # The prune that makes this traversal affordable breaks out of the candidate
    # loop at the first leg too expensive to help, which is only sound while
    # candidates arrive cost-sorted. Two airports means two sorted lists, and
    # concatenating them is not sorted: the cheap KIX departure sits behind an
    # expensive NRT one and would be pruned away unheard.
    dear_from_nrt = leg("NRT", "MNL", date(2027, 1, 20), 9000, airline="ZZ")
    cheap_from_kix = leg("KIX", "MNL", date(2027, 1, 20), 2000, airline="YY")
    result = combine([LEG_A, dear_from_nrt, cheap_from_kix, LEG_C], overland_scenario())
    assert result[0].total_price == 28000
    assert result[0].legs[1].origin == "KIX"
