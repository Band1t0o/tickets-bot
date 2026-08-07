"""Chain cached legs into complete, valid itineraries.

Pure and fast: no browser, no network. Sweeping and combining are deliberately
separate, so stay lengths and airport preferences can be re-tuned and re-scored
against legs already on disk without re-scraping anything.

Stay lengths are always computed from the dates on the legs themselves.
pelikan.cz substitutes nearby dates - asking for 22 January can return the
23rd - so trusting the requested date would silently turn a 10-day stay into
an 11-day one.
"""
from __future__ import annotations

from collections import defaultdict

from .models import Itinerary, Leg
from .scenario import Scenario

# Enough to explore alternatives without making the results table unusable.
MAX_RESULTS = 50


def _by_origin(legs: list[Leg]) -> dict[str, list[Leg]]:
    grouped: dict[str, list[Leg]] = defaultdict(list)
    for leg in legs:
        if leg.depart_date is not None:
            grouped[leg.origin].append(leg)
    return grouped


def _stay_ok(earlier: Leg, later: Leg, span: tuple[int, int]) -> bool:
    days = (later.depart_date - earlier.depart_date).days
    return span[0] <= days <= span[1]


def combine(
    legs: list[Leg], scenario: Scenario, limit: int | None = MAX_RESULTS
) -> list[Itinerary]:
    """Every valid itinerary in `legs`, cheapest first.

    `limit` caps the result for display. Pass None when the full set is needed -
    the price-by-date chart must see every itinerary, or expensive dates drop
    out of the series entirely and the chart implies they were never searched.
    """
    if not legs:
        return []
    if scenario.trip_type == "round_trip":
        itineraries = _combine_round_trip(legs, scenario)
    else:
        itineraries = _combine_multi_city(legs, scenario)

    # Rank on the bag-inclusive total: the cheapest headline fare is often a
    # low-cost carrier whose checked bag is extra, and comparing it against a
    # bag-inclusive fare is not a like-for-like comparison. total_price is kept
    # intact so the headline fare is still visible alongside it.
    bag = scenario.bag_estimate_czk
    itineraries.sort(key=lambda i: (i.total_with_bags(bag), i.total_price))
    return itineraries if limit is None else itineraries[:limit]


def _combine_multi_city(legs: list[Leg], scenario: Scenario) -> list[Itinerary]:
    origins = set(scenario.origins)
    japan = set(scenario.japan_airports)
    philippines = set(scenario.ph_airports)
    by_origin = _by_origin(legs)

    results: list[Itinerary] = []
    for leg_a in legs:
        if leg_a.origin not in origins or leg_a.destination not in japan:
            continue
        if leg_a.depart_date is None:
            continue

        # Leg B must depart the airport leg A landed at - a Tokyo arrival
        # cannot be followed by an Osaka departure.
        for leg_b in by_origin.get(leg_a.destination, ()):
            if leg_b.destination not in philippines:
                continue
            if not _stay_ok(leg_a, leg_b, scenario.japan_stay_days):
                continue

            for leg_c in by_origin.get(leg_b.destination, ()):
                if leg_c.destination not in origins:
                    continue
                if not _stay_ok(leg_b, leg_c, scenario.ph_stay_days):
                    continue
                results.append(Itinerary(legs=[leg_a, leg_b, leg_c]))
    return results


def _combine_round_trip(legs: list[Leg], scenario: Scenario) -> list[Itinerary]:
    origins = set(scenario.origins)
    japan = set(scenario.japan_airports)
    by_origin = _by_origin(legs)

    results: list[Itinerary] = []
    for outbound in legs:
        if outbound.origin not in origins or outbound.destination not in japan:
            continue
        if outbound.depart_date is None:
            continue
        for inbound in by_origin.get(outbound.destination, ()):
            if inbound.destination not in origins:
                continue
            if not _stay_ok(outbound, inbound, scenario.trip_length_days):
                continue
            results.append(Itinerary(legs=[outbound, inbound]))
    return results


def best_same_airport(itineraries: list[Itinerary]) -> Itinerary | None:
    """Cheapest itinerary that ends where it started."""
    closed = [i for i in itineraries if i.same_airport]
    return min(closed, key=lambda i: i.total_price) if closed else None


def best_open_jaw(itineraries: list[Itinerary]) -> Itinerary | None:
    """Cheapest itinerary that returns to a different airport."""
    open_jaw = [i for i in itineraries if not i.same_airport]
    return min(open_jaw, key=lambda i: i.total_price) if open_jaw else None


def cheapest_by_departure_date(itineraries: list[Itinerary]) -> list[dict]:
    """Cheapest total per departure date - the series behind the price chart."""
    best: dict[str, Itinerary] = {}
    for itinerary in itineraries:
        if itinerary.departure_date is None:
            continue
        key = itinerary.departure_date.isoformat()
        if key not in best or itinerary.total_price < best[key].total_price:
            best[key] = itinerary
    return [
        {
            "depart_date": key,
            "cheapest_total": itinerary.total_price,
            "currency": itinerary.currency,
            "route": itinerary.route,
        }
        for key, itinerary in sorted(best.items())
    ]
