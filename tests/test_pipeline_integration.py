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

import pytest

from src.combine import combine, combine_all
from src.models import Leg
from src.scenario import Scenario
from src.sweep.planner import LegSearch, plan_searches
from tests.conftest import WINDOW_START, make_round_trip, make_scenario, make_three_stop

SHAPES = {
    "round_trip": make_round_trip,
    "two_stop": make_scenario,
    "three_stop": make_three_stop,
}


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


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_a_full_plan_produces_itineraries(shape):
    """The bug this file exists for.

    The old planner had a round-trip branch that emitted outbound searches only,
    so no leg ever departed the destination while its combiner branch required
    exactly such a leg. Every scenario of that shape yielded zero itineraries,
    and only `"enabled": false` on tokyo-round-trip.json kept it off the daily
    schedule. Three stops could not be expressed at all.
    """
    scenario = SHAPES[shape]()
    itineraries = combine(legs_from_plan(scenario), scenario)
    assert itineraries, "planner and combiner disagree: no itinerary from a full plan"


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_itineraries_have_one_leg_per_hop(shape):
    scenario = SHAPES[shape]()
    for itinerary in combine(legs_from_plan(scenario), scenario):
        assert len(itinerary.legs) == scenario.leg_count


# ------------------------------------------------------------------- invariants


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_every_itinerary_chains_end_to_end(shape):
    """Leg i must land where leg i+1 departs - no teleporting between airports."""
    scenario = SHAPES[shape]()
    itineraries = combine(legs_from_plan(scenario), scenario)
    assert itineraries
    for itinerary in itineraries:
        for earlier, later in zip(itinerary.legs, itinerary.legs[1:], strict=False):
            assert earlier.destination == later.origin


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_every_itinerary_travels_forward_in_time(shape):
    scenario = SHAPES[shape]()
    itineraries = combine(legs_from_plan(scenario), scenario)
    assert itineraries
    for itinerary in itineraries:
        dates = [leg.depart_date for leg in itinerary.legs]
        assert dates == sorted(dates)


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_every_stay_falls_inside_its_configured_range(shape):
    """Checked against the dates on the legs, never the dates requested."""
    scenario = SHAPES[shape]()
    itineraries = combine(legs_from_plan(scenario), scenario)
    assert itineraries
    for itinerary in itineraries:
        for index, stop in enumerate(scenario.stops):
            arrived = itinerary.legs[index]
            departed = itinerary.legs[index + 1]
            stayed = (departed.depart_date - arrived.depart_date).days
            low, high = stop.stay_days
            assert low <= stayed <= high, f"{stop.describe(index)}: {stayed} days"


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_itineraries_only_use_legs_the_planner_actually_searched(shape):
    """Guards against the combiner inventing a route no sweep would ever price."""
    scenario = SHAPES[shape]()
    planned = {(s.origin, s.destination, s.depart_date) for s in plan_searches(scenario)}
    for itinerary in combine(legs_from_plan(scenario), scenario):
        for leg in itinerary.legs:
            assert (leg.origin, leg.destination, leg.depart_date) in planned


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_itineraries_start_and_end_where_the_scenario_says(shape):
    scenario = SHAPES[shape]()
    pools = scenario.airport_pools
    for itinerary in combine(legs_from_plan(scenario), scenario):
        assert itinerary.legs[0].origin in pools[0]
        assert itinerary.legs[-1].destination in pools[-1]


def test_a_one_way_trip_ends_at_the_last_stop():
    scenario = make_three_stop(one_way=True)
    itineraries = combine(legs_from_plan(scenario), scenario)
    assert itineraries
    for itinerary in itineraries:
        assert itinerary.legs[-1].destination == "BKK"


def test_an_open_jaw_returns_to_the_configured_airport():
    scenario = make_round_trip(return_to=["BER"])
    itineraries = combine(legs_from_plan(scenario), scenario)
    assert itineraries
    for itinerary in itineraries:
        assert itinerary.legs[-1].destination == "BER"
        assert itinerary.same_airport is False


# -------------------------------------------------------------------- ranking


def test_results_are_sorted_cheapest_first():
    scenario = make_scenario()
    totals = [i.total_price for i in combine(legs_from_plan(scenario), scenario)]
    assert totals == sorted(totals)


def test_pruning_finds_the_same_cheapest_as_an_exhaustive_search():
    """The prune bound must be admissible - never discarding a cheaper trip.

    Compared against the unbounded traversal, which explores everything.
    """
    scenario = make_scenario()
    legs = legs_from_plan(scenario)
    bounded = combine_all(legs, scenario, limit=5)
    exhaustive = combine_all(legs, scenario, limit=None)
    assert bounded.top[0].total_price == exhaustive.top[0].total_price
    assert len(bounded.top) == 5


def test_the_by_date_series_keeps_expensive_dates():
    """Pruning on the top-N alone would drop them, implying they were never searched."""
    scenario = make_scenario()
    legs = legs_from_plan(scenario)
    bounded = combine_all(legs, scenario, limit=5)
    exhaustive = combine_all(legs, scenario, limit=None)
    assert set(bounded.best_by_date) == set(exhaustive.best_by_date)
    for key, itinerary in exhaustive.best_by_date.items():
        assert bounded.best_by_date[key].total_price == itinerary.total_price


def test_the_cheapest_open_jaw_survives_pruning():
    """It can sit far below the top-N cut when closed trips are cheaper."""
    scenario = make_scenario(return_to=["BER", "PRG"])
    legs = legs_from_plan(scenario)
    bounded = combine_all(legs, scenario, limit=1)
    exhaustive = combine_all(legs, scenario, limit=None)
    assert bounded.best_open_jaw is not None
    assert bounded.best_open_jaw.total_price == exhaustive.best_open_jaw.total_price
    assert bounded.best_same_airport.total_price == exhaustive.best_same_airport.total_price


# ------------------------------------------- the two sweeps, end to end
#
# The planner and the runner must agree about which dates a mode searches, and
# the only record of that agreement is `searches.jsonl` - one row per search
# whatever the outcome, which is the file that can prove a hole in the grid.
#
# Counting is not enough here. The bug this feature exists to fix was a nightly
# sweep quietly pricing 48 dates out of a window of 85, and a count alone cannot
# tell "fewer searches" from "the wrong searches". So these read the dates back
# off disk and check where they landed.


def narrowed_two_stop():
    """The real shape: a departure window, a return window and a nights band."""
    from datetime import date

    return make_scenario(
        depth="deep",
        focus_start=date(2027, 1, 8),
        focus_end=date(2027, 1, 12),
        return_focus_start=date(2027, 1, 28),
        return_focus_end=date(2027, 2, 4),
        total_days=(18, 22),
    )


def searched_dates(directory, leg_index):
    """The dates a finished run actually asked about, from `searches.jsonl`."""
    import json
    from datetime import date

    rows = [
        json.loads(line)
        for line in (directory / "searches.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return sorted(
        date.fromisoformat(row["depart_date"])
        for row in rows
        if row["leg_index"] == leg_index
    )


def run(scenario, tmp_path, mode):
    from src.sweep.runner import run_sweep

    class Provider:
        NAME = "FAKE"

        def search_leg(self, page, origin, destination, depart, ret=None, adults=1):
            return [_leg_for(LegSearch(origin, destination, depart, None, 0))]

    return run_sweep(
        scenario, provider=Provider(), data_dir=tmp_path, workers=1, delay_s=0, mode=mode,
    )


def test_a_final_sweep_asks_only_about_dates_inside_the_narrowing(tmp_path):
    """Read back off disk, because that is the only record of what ran."""
    trip = narrowed_two_stop()
    result = run(trip, tmp_path, "final")

    out = searched_dates(result.directory, 0)
    home = searched_dates(result.directory, trip.leg_count - 1)

    assert out, "the final sweep asked about no first leg at all"
    assert min(out) >= trip.focus_start and max(out) <= trip.focus_end
    assert min(home) >= trip.return_focus_start
    assert max(home) <= trip.return_focus_end


def test_a_broad_sweep_of_the_same_trip_asks_outside_it(tmp_path):
    """The half that was missing. Both halves have to hold or the split has not
    happened: a narrowed plan proves nothing if the broad one is narrowed too."""
    trip = narrowed_two_stop()
    result = run(trip, tmp_path, "sweep")

    out = searched_dates(result.directory, 0)

    assert min(out) < trip.focus_start, "the broad sweep started at the focus"
    assert max(out) > trip.focus_end, "the broad sweep stopped at the focus"
    assert min(out) == trip.window_start


def test_the_two_sweeps_are_told_apart_by_what_they_recorded(tmp_path):
    """A run is broad or narrowed for good the moment it finishes.

    Both write into `data/sweeps/`, so the only thing separating them a week
    later is the status - and recording the focus alone was not enough, because
    this trip is also bounded by a return window and a nights band.
    """
    import json

    trip = narrowed_two_stop()
    narrowed = json.loads(
        (run(trip, tmp_path, "final").directory / "status.json").read_text(encoding="utf-8")
    )
    broad = json.loads(
        (run(trip, tmp_path, "sweep").directory / "status.json").read_text(encoding="utf-8")
    )

    assert narrowed["mode"] == "final"
    assert narrowed["narrowing"]["return_focus"] == ["2027-01-28", "2027-02-04"]
    assert narrowed["narrowing"]["total_days"] == [18, 22]
    # The broad run records three Nones on the very same trip, because that is
    # what it searched. The field says what happened, not what the trip said.
    assert broad["mode"] == "sweep"
    assert not any(broad["narrowing"].values())
    assert broad["total"] > narrowed["total"]
