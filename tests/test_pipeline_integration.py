"""The planner and the combiner must agree about what a trip is.

Every other test exercises one side or the other. `test_combine.py` hand-builds
the legs it wants to chain, and `test_planner.py` only counts and dates the
searches it emits - so a planner that never searches a leg the combiner requires
passes both files and produces nothing at all in production.

That is not hypothetical: it is exactly the state of `round_trip` today. These
tests close the loop by feeding the planner's own output through a provider that
cannot fail, so anything that comes out empty is the pipeline's fault and not
the scraper's.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.combine import combine
from src.models import Leg
from src.scenario import Scenario
from src.sweep.planner import LegSearch, plan_searches

WINDOW_START = date(2027, 1, 5)
WINDOW_END = date(2027, 2, 8)


def multi_city(**overrides) -> Scenario:
    defaults = dict(
        id="jp-ph",
        name="Japan then Philippines",
        trip_type="multi_city",
        origins=["PRG", "VIE"],
        japan_airports=["NRT", "KIX"],
        ph_airports=["MNL"],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        japan_stay_days=(9, 11),
        ph_stay_days=(9, 11),
        depth="standard",
    )
    defaults.update(overrides)
    return Scenario(**defaults)


def round_trip(**overrides) -> Scenario:
    defaults = dict(
        id="tokyo",
        name="Tokyo return",
        trip_type="round_trip",
        origins=["PRG"],
        japan_airports=["NRT"],
        ph_airports=[],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        trip_length_days=(18, 20),
        depth="quick",
    )
    defaults.update(overrides)
    return Scenario(**defaults)


def _leg_for(search: LegSearch) -> Leg:
    """The leg a flawless provider would return for `search`.

    Faithful to what the real one does, and the fidelity is the whole point:
    `parse_results_html(html, origin, destination)` stamps every card with the
    direction that was *searched*. A round-trip search therefore yields outbound
    legs only - the return half of the fare is priced into the card but is not a
    leg anyone can chain. Simulating it any other way would invent a return leg
    that production never sees, and the test would pass while the sweep found
    nothing.
    """
    # Deterministic, distinct, and cheap to reason about: a stable price per
    # route-and-date so ordering assertions do not depend on dict iteration.
    price = 10000 + (hash((search.origin, search.destination)) % 50) * 100
    price += (search.depart_date - WINDOW_START).days * 10
    return Leg(
        provider="FAKE",
        origin=search.origin,
        destination=search.destination,
        depart_date=search.depart_date,
        airline="XX",
        flight_number=None,
        stops=0,
        price_currency="CZK",
        price_amount=float(price),
        url="https://example.invalid/",
    )


def legs_from_plan(scenario: Scenario) -> list[Leg]:
    """Run the scenario through the planner and a provider that never fails."""
    return [_leg_for(s) for s in plan_searches(scenario)]


# --------------------------------------------------------------- the loop closes


def test_multi_city_plan_produces_itineraries():
    scenario = multi_city()
    itineraries = combine(legs_from_plan(scenario), scenario)
    assert itineraries, "planner and combiner disagree: no itinerary from a full plan"


def test_round_trip_plan_produces_itineraries():
    """The bug this file exists for.

    The planner emits only outbound searches, so no leg ever departs the
    destination; the combiner requires exactly such a leg to close the trip.
    Every scenario of this shape yields zero itineraries, and only
    `"enabled": false` on tokyo-round-trip.json keeps it from running daily.
    """
    scenario = round_trip()
    itineraries = combine(legs_from_plan(scenario), scenario)
    assert itineraries, "planner and combiner disagree: no itinerary from a full plan"


# ------------------------------------------------------------------- invariants


@pytest.mark.parametrize("scenario", [multi_city(), round_trip()], ids=["multi_city", "round_trip"])
def test_every_itinerary_chains_end_to_end(scenario):
    """Leg i must land where leg i+1 departs - no teleporting between airports."""
    itineraries = combine(legs_from_plan(scenario), scenario)
    assert itineraries
    for itinerary in itineraries:
        for earlier, later in zip(itinerary.legs, itinerary.legs[1:], strict=False):
            assert earlier.destination == later.origin


@pytest.mark.parametrize("scenario", [multi_city(), round_trip()], ids=["multi_city", "round_trip"])
def test_every_itinerary_travels_forward_in_time(scenario):
    itineraries = combine(legs_from_plan(scenario), scenario)
    assert itineraries
    for itinerary in itineraries:
        dates = [leg.depart_date for leg in itinerary.legs]
        assert dates == sorted(dates)


def test_multi_city_itineraries_respect_the_configured_stays():
    scenario = multi_city()
    for itinerary in combine(legs_from_plan(scenario), scenario):
        leg_a, leg_b, leg_c = itinerary.legs
        assert (
            scenario.japan_stay_days[0]
            <= (leg_b.depart_date - leg_a.depart_date).days
            <= scenario.japan_stay_days[1]
        )
        assert (
            scenario.ph_stay_days[0]
            <= (leg_c.depart_date - leg_b.depart_date).days
            <= scenario.ph_stay_days[1]
        )


def test_itineraries_only_use_legs_the_planner_actually_searched():
    """Guards against the combiner inventing a route no sweep would ever price."""
    scenario = multi_city()
    planned = {(s.origin, s.destination, s.depart_date) for s in plan_searches(scenario)}
    for itinerary in combine(legs_from_plan(scenario), scenario):
        for leg in itinerary.legs:
            assert (leg.origin, leg.destination, leg.depart_date) in planned


def test_itineraries_start_at_an_origin_and_end_at_one():
    scenario = multi_city()
    for itinerary in combine(legs_from_plan(scenario), scenario):
        assert itinerary.legs[0].origin in scenario.origins
        assert itinerary.legs[-1].destination in scenario.origins


def test_results_are_sorted_cheapest_first():
    scenario = multi_city()
    totals = [i.total_price for i in combine(legs_from_plan(scenario), scenario)]
    assert totals == sorted(totals)
