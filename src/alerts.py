"""Choose which of a sweep's itineraries are worth reporting.

Pure and browser-free, like `combine.py` and `viability.py`: it reads legs and
returns picks. Nothing here posts anything.

The reason it exists: "the cheapest trip" and "the cheapest trip I would
actually enjoy flying" are different answers, and a watcher that only ever
reports the first recommends Frankfurt every single day. Measured on real
sweeps FRA genuinely is cheapest — but a 9,000 CZK saving against a coach to
Frankfurt is a judgement the traveller makes, not the tool. So both are
reported, with the difference between them stated.

Preference is ranked rather than flat: PRG/VIE are not equivalent to KTW just
because both beat FRA. A trip belongs to a tier when *both* its ends are in
that tier — flying out of Prague and home into Frankfurt is a Frankfurt trip,
since it strands you at exactly the airport you were avoiding.

**The preferred pick cannot be filtered out of an existing result.** The
combiner prunes: once a cheap trip is found, any branch whose partial total
already exceeds it is abandoned, so a dearer trip from a preferred airport is
never built in the first place. Measured on a real leg set, a 21,000 FRA trip
pruned away a 30,000 PRG trip entirely. Each tier therefore gets its own
traversal of the same legs, with the scenario narrowed to that tier's airports
at both ends — "the cheapest trip within this tier" is exactly "the cheapest
trip for a trip that may only start and end there". Traversal is pure and
in-memory, and tiers number a handful, so the cost is small and paid once per
sweep.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .combine import combine_all
from .models import Itinerary, Leg
from .scenario import Scenario


@dataclass(frozen=True)
class Pick:
    """One reportable itinerary, and why it was chosen."""

    name: str
    itinerary: Itinerary
    # 1-based rank of the worse end's tier; None when the trip touches an
    # airport you never ranked.
    tier: int | None = None
    # What choosing this over the outright cheapest costs. 0 for the cheapest
    # itself, so a card can print it without a special case.
    premium: float = 0.0
    # True when this pick is simultaneously the cheapest and top-tier, so the
    # caller reports one card rather than two identical ones.
    also_preferred: bool = False

    @property
    def label(self) -> str:
        if self.also_preferred:
            return "Cheapest — and from an airport you prefer"
        if self.name == "preferred":
            return f"Cheapest from your tier-{self.tier} airports"
        return "Cheapest overall"


def _tier_of(itinerary: Itinerary, tiers: list[list[str]]) -> int | None:
    """Rank of the worse end of this trip, or None if either end is unranked.

    Both ends must be ranked: an itinerary half in your preferred set is not a
    preferred itinerary, it is a preferred outbound and a problem on the way
    home.
    """
    if not itinerary.legs:
        return None
    ranks = []
    for code in (itinerary.legs[0].origin, itinerary.legs[-1].destination):
        rank = next((i for i, tier in enumerate(tiers, start=1) if code in tier), None)
        if rank is None:
            return None
        ranks.append(rank)
    return max(ranks)


def _identity(itinerary: Itinerary) -> tuple[str, ...]:
    """Value identity for a trip, since each traversal builds its own objects."""
    return tuple(leg.content_hash() for leg in itinerary.legs)


def _best_in_tier(legs: list[Leg], scenario: Scenario, tier: list[str]) -> Itinerary | None:
    """Cheapest trip that both starts and ends within `tier`."""
    narrowed = replace(
        scenario,
        origins=list(tier),
        # None would mean "back where you started", which is already this tier.
        # Stated explicitly so the intent survives a later change to that
        # default. One-way trips ignore it: they have no leg home.
        return_to=None if scenario.one_way else list(tier),
    )
    return combine_all(legs, narrowed, limit=1).best


def select_alerts(legs: list[Leg], scenario: Scenario) -> list[Pick]:
    """The picks this sweep warrants, in reporting order.

    Ranking is on the bag-inclusive total throughout, for the same reason
    `combine` ranks on it: a low-cost carrier's bagless fare is not comparable
    with a bag-inclusive one.
    """
    bag = float(scenario.bag_estimate)

    def cost(itinerary: Itinerary) -> float:
        return itinerary.total_with_bags(bag)

    cheapest = combine_all(legs, scenario, limit=1).best
    if cheapest is None:
        return []

    # First tier with anything wins outright; a better airport beats a cheaper
    # one, which is the whole point of ranking them.
    preferred: Itinerary | None = None
    preferred_tier: int | None = None
    tiers = scenario.reporting_tiers()
    for rank, tier in enumerate(tiers, start=1):
        found = _best_in_tier(legs, scenario, tier)
        if found is not None:
            preferred, preferred_tier = found, rank
            break

    # The cheapest trip is often one you would have chosen anyway. Reporting it
    # twice is noise, so it is reported once and says so.
    collapses = preferred is not None and _identity(preferred) == _identity(cheapest)

    picks: list[Pick] = []
    if "cheapest" in scenario.notify:
        picks.append(
            Pick(
                "cheapest",
                cheapest,
                tier=preferred_tier if collapses else _tier_of(cheapest, tiers),
                also_preferred=collapses,
            )
        )
    if "preferred" in scenario.notify and preferred is not None and not collapses:
        picks.append(
            Pick(
                "preferred",
                preferred,
                tier=preferred_tier,
                premium=cost(preferred) - cost(cheapest),
            )
        )
    return picks
