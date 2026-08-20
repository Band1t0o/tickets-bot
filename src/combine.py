"""Chain cached legs into complete, valid itineraries.

Pure and fast: no browser, no network. Sweeping and combining are deliberately
separate, so stay lengths and airport preferences can be re-tuned and re-scored
against legs already on disk without re-scraping anything.

Stay lengths are always computed from the dates on the legs themselves.
pelikan.cz substitutes nearby dates - asking for 22 January can return the
23rd - so trusting the requested date would silently turn a 10-day stay into
an 11-day one.

Chaining is a depth-first walk over `scenario.airport_pools`, the same list the
planner walks, so the two cannot disagree about the shape of a trip. It handles
any number of stops, which the previous triple-nested loop could not.

Two things make an arbitrary-length chain affordable:

- **Pruning.** Candidate legs are held sorted by price, so once a partial total
  reaches the threshold every remaining candidate at that level is worse and the
  branch is abandoned. Prices are non-negative, so a partial total is a lower
  bound on any completion - nothing cheap is ever discarded.
- **One traversal.** Everything a caller needs - the cheapest N, the cheapest
  per departure date, the cheapest closed and open-jaw trips - is collected on
  the way. The old code built every itinerary, sorted them all, sliced 50, and
  then did the whole thing again for the chart.

The threshold is the *maximum* of every target still improvable, never just the
top-N cut. Pruning on the top-N alone would drop expensive departure dates out
of the chart entirely, implying they were never searched, and would hide the
cheapest open jaw whenever fifty closed trips happened to be cheaper.
"""
from __future__ import annotations

import heapq
import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from math import inf

from .models import Itinerary, Leg
from .scenario import Scenario

# Enough to explore alternatives without making the results table unusable.
MAX_RESULTS = 50

# Safety valve. Pruning handles realistic scenarios comfortably, but a wide
# enough trip could still explode, and a UI that hangs is worse than one that
# says it stopped early.
MAX_NODES = 2_000_000


@dataclass
class CombineResult:
    """Everything one traversal of the leg set produces."""

    top: list[Itinerary] = field(default_factory=list)
    best_by_date: dict[str, Itinerary] = field(default_factory=dict)
    best_same_airport: Itinerary | None = None
    best_open_jaw: Itinerary | None = None
    # How many legs went in, so a caller that holds only the result can still
    # report it without re-reading legs.jsonl.
    legs_in: int = 0
    considered: int = 0
    truncated: bool = False

    @property
    def best(self) -> Itinerary | None:
        return self.top[0] if self.top else None


def _stay_ok(earlier: Leg, later: Leg, span: tuple[int, int]) -> bool:
    days = (later.depart_date - earlier.depart_date).days
    return span[0] <= days <= span[1]


def _leg_cost(leg: Leg, bag_estimate: float) -> float:
    """What one leg contributes to the ranking total.

    Itineraries are ranked on the bag-inclusive total, because the cheapest
    headline fare is usually a low-cost carrier whose checked bag is extra, and
    comparing that against a bag-inclusive fare is not like-for-like.
    `total_price` stays intact so the headline fare is still visible.

    Summing this over an itinerary equals `Itinerary.total_with_bags`, and it is
    non-negative, which is what lets a partial sum prune: adding legs can only
    make a chain more expensive, so a partial total is a lower bound on every
    completion of it.
    """
    return leg.price_amount + (bag_estimate if leg.checked_bag is not True else 0.0)


def _departures(by_origin, arrived_at: str, stop, bag: float):
    """Legs the trip may leave `stop` on, cheapest first.

    Normally that is one airport's worth - you leave from the airport you
    landed at, which is what makes a stop's airports alternatives rather than a
    sequence. An overland stop lets any airport in the pool depart instead: fly
    into Haneda, cross Japan on the ground, fly out of Kansai.

    Merged by cost rather than concatenated, and that is not a detail.
    `descend` breaks out of this loop at the first candidate too expensive to
    help, which is only sound while candidates arrive cost-sorted. Chaining two
    sorted lists end to end is not sorted, so a cheap Kansai departure sitting
    behind an expensive Haneda one would be pruned away unheard - the traversal
    would still return an answer, just not the cheapest one, and nothing
    downstream could tell. `heapq.merge` is lazy, so a branch that breaks early
    still pays for nothing it never read.
    """
    if not stop.overland:
        return by_origin.get(arrived_at, ())
    return heapq.merge(
        *(by_origin[code] for code in stop.airports if code in by_origin),
        key=lambda leg: _leg_cost(leg, bag),
    )


def combine_all(
    legs: list[Leg], scenario: Scenario, limit: int | None = MAX_RESULTS
) -> CombineResult:
    """Walk every valid itinerary in `legs`, keeping what callers need."""
    result = CombineResult(legs_in=len(legs))
    pools = scenario.airport_pools
    leg_count = len(pools) - 1
    if not legs or leg_count < 1:
        return result

    allowed = [set(pool) for pool in pools]
    stops = scenario.stops
    bag = float(scenario.bag_estimate)

    # Sorted by the ranking cost - not the headline price - so a branch can stop
    # at the first candidate that is too expensive instead of testing all of
    # them. Sorting by price while pruning on cost would break early on a leg
    # that a later, pricier-but-bag-inclusive one beats.
    by_origin: dict[str, list[Leg]] = defaultdict(list)
    for leg in legs:
        if leg.depart_date is None:
            continue
        by_origin[leg.origin].append(leg)
    for candidates in by_origin.values():
        candidates.sort(key=lambda leg: _leg_cost(leg, bag))

    # A target that can never be hit must not sit in the threshold as `inf`,
    # which would disable pruning for the whole traversal. A trip that leaves
    # from and returns to the same single airport has no open jaw to find, and
    # would otherwise explore every branch to the end looking for one.
    starts, ends = set(pools[0]), set(pools[-1])
    same_airport_possible = bool(starts & ends)
    open_jaw_possible = any(start != end for start in starts for end in ends)

    heap: list[tuple[float, int, Itinerary]] = []  # max-heap on cost, via negation
    tiebreak = itertools.count()
    nodes = 0

    def rank(itinerary: Itinerary) -> float:
        return itinerary.total_with_bags(bag)

    def threshold(date_key: str) -> float:
        """Cheapest total this branch must beat to be worth finishing.

        The maximum of every target still open: the top-N cut, this departure
        date's best, and the best closed and open-jaw trips so far. A partial
        total at or above all of them cannot improve any of them.
        """
        limits = [rank(c) if (c := result.best_by_date.get(date_key)) else inf]
        if limit is not None and len(heap) >= limit:
            limits.append(-heap[0][0])
        if same_airport_possible:
            best = result.best_same_airport
            limits.append(rank(best) if best else inf)
        if open_jaw_possible:
            best = result.best_open_jaw
            limits.append(rank(best) if best else inf)
        return max(limits)

    def keep(itinerary: Itinerary, date_key: str) -> None:
        result.considered += 1
        total = rank(itinerary)

        current = result.best_by_date.get(date_key)
        if current is None or total < rank(current):
            result.best_by_date[date_key] = itinerary

        if itinerary.same_airport:
            if result.best_same_airport is None or total < rank(result.best_same_airport):
                result.best_same_airport = itinerary
        elif result.best_open_jaw is None or total < rank(result.best_open_jaw):
            result.best_open_jaw = itinerary

        entry = (-total, next(tiebreak), itinerary)
        if limit is None or len(heap) < limit:
            heapq.heappush(heap, entry)
        elif total < -heap[0][0]:
            heapq.heapreplace(heap, entry)

    def descend(chain: list[Leg], running: float, currency: str, date_key: str) -> None:
        nonlocal nodes
        level = len(chain)
        if level == leg_count:
            keep(Itinerary(legs=list(chain)), date_key)
            return

        previous = chain[-1]
        # The stop this chain has just arrived at, and is now leaving.
        stop = stops[level - 1]
        span = stop.stay_days
        for leg in _departures(by_origin, previous.destination, stop, bag):
            nodes += 1
            if nodes > MAX_NODES:
                result.truncated = True
                return
            total = running + _leg_cost(leg, bag)
            # Candidates are cost-sorted, so everything after this is worse.
            if total >= threshold(date_key):
                break
            if leg.destination not in allowed[level + 1]:
                continue
            # Totals across currencies are meaningless, and there is no FX rate
            # here to make them meaningful.
            if leg.price_currency != currency:
                continue
            if not _stay_ok(previous, leg, span):
                continue
            chain.append(leg)
            descend(chain, total, currency, date_key)
            chain.pop()

    for leg in legs:
        if leg.depart_date is None:
            continue
        if leg.origin not in allowed[0] or leg.destination not in allowed[1]:
            continue
        date_key = leg.depart_date.isoformat()
        cost = _leg_cost(leg, bag)
        if cost >= threshold(date_key):
            continue
        descend([leg], cost, leg.price_currency, date_key)
        if result.truncated:
            break

    result.top = [entry[2] for entry in sorted(heap, key=lambda e: -e[0])]
    return result


def combine(
    legs: list[Leg], scenario: Scenario, limit: int | None = MAX_RESULTS
) -> list[Itinerary]:
    """Every valid itinerary in `legs`, cheapest first, capped at `limit`.

    Prefer `combine_all` when the per-date or open-jaw figures are also wanted;
    it produces them from the same traversal.
    """
    return combine_all(legs, scenario, limit=limit).top


def best_same_airport(itineraries: list[Itinerary]) -> Itinerary | None:
    """Cheapest itinerary that ends where it started."""
    closed = [i for i in itineraries if i.same_airport]
    return min(closed, key=lambda i: i.total_price) if closed else None


def best_open_jaw(itineraries: list[Itinerary]) -> Itinerary | None:
    """Cheapest itinerary that returns to a different airport."""
    open_jaw = [i for i in itineraries if not i.same_airport]
    return min(open_jaw, key=lambda i: i.total_price) if open_jaw else None


def cheapest_by_departure_date(
    itineraries: list[Itinerary], bag_estimate: float = 0
) -> list[dict]:
    """Cheapest total per departure date - the series behind the price chart."""
    best: dict[str, Itinerary] = {}
    for itinerary in itineraries:
        if itinerary.departure_date is None:
            continue
        key = itinerary.departure_date.isoformat()
        current = best.get(key)
        if current is None or itinerary.total_with_bags(bag_estimate) < current.total_with_bags(
            bag_estimate
        ):
            best[key] = itinerary
    return _series(best, bag_estimate)


def series_from_result(result: CombineResult, bag_estimate: float = 0) -> list[dict]:
    """The price-by-date series, from a traversal that already computed it."""
    return _series(result.best_by_date, bag_estimate)


def _series(best: dict[str, Itinerary], bag_estimate: float) -> list[dict]:
    return [
        {
            "depart_date": key,
            # The headline fare, which is what the chart has always plotted, and
            # the bag-inclusive total that actually decided the ranking.
            "cheapest_total": itinerary.total_price,
            "cheapest_total_with_bags": itinerary.total_with_bags(bag_estimate),
            "currency": itinerary.currency,
            "route": itinerary.route,
        }
        for key, itinerary in sorted(best.items())
    ]
