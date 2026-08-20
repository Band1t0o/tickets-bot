"""A second opinion on the trip you are about to book.

Not on all 900 legs — on the handful the decision actually rests on.

letuska.cz was rejected as a sweep source and stays rejected: it has no
deep-link grammar, so one search means driving an Angular form through a cookie
banner, autocomplete typing and a Czech-month calendar behind two nested shadow
roots. That is ~60–90s against pelikan's ~14s, and a 615-search deep sweep is
not affordable at that rate.

Re-pricing a shortlist is a different question with a different answer. The top
few itineraries share most of their legs, so deduplicated they come to five or
six searches — a few minutes, once, after the sweep has already finished.

The checker is injected rather than imported, so this module is pure and
testable and the sweep never depends on a second site being up. Every failure
becomes a verdict rather than an exception: a second opinion that cannot be had
is a fact about the second opinion, not a reason to lose the sweep.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime

from .combine import combine_all
from .models import Leg
from .scenario import Scenario

# Below this the two sites are quoting the same fare through different fee
# rounding, not a different fare. Above it, one of them is worth a click.
MATERIAL_SAVING_PCT = 3.0

# How many itineraries to re-price. Three is what fits in the minutes available:
# they share most of their legs, so it is typically five or six searches.
DEFAULT_TOP = 3

# (origin, destination, depart) -> prices found. Empty means "could not price".
Checker = Callable[[str, str, date], Sequence[float]]


def verify_shortlist(
    legs: list[Leg],
    scenario: Scenario,
    check: Checker,
    top: int = DEFAULT_TOP,
) -> dict:
    """Re-price the shortlist's legs elsewhere and report any disagreement.

    Returns a JSON-serialisable dict; never raises.
    """
    report: dict = {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "top": top,
        "tolerance_pct": MATERIAL_SAVING_PCT,
        "legs_checked": 0,
        "unpriced": [],
        "comparisons": [],
        "cheapest_elsewhere": None,
        "verdict": "nothing_to_check",
    }

    itineraries = combine_all(legs, scenario, limit=top).top
    if not itineraries:
        return report

    # Deduplicated: the top itineraries overlap heavily, and re-pricing NRT→MNL
    # five times is five wasted minutes against a site that takes a minute a
    # search. Cheapest instance of each route wins, since that is the one the
    # shortlist is actually built from.
    ours: dict[tuple[str, str, date], float] = {}
    for itinerary in itineraries:
        for leg in itinerary.legs:
            if leg.depart_date is None:
                continue
            key = (leg.origin, leg.destination, leg.depart_date)
            if key not in ours or leg.price_amount < ours[key]:
                ours[key] = leg.price_amount

    unpriced: list[str] = []
    for (origin, destination, depart), our_price in sorted(ours.items()):
        route = f"{origin}->{destination}"
        try:
            quotes = check(origin, destination, depart)
        except Exception as exc:  # noqa: BLE001 - a dead second source is a verdict
            report["verdict"] = "unavailable"
            report["error"] = f"{type(exc).__name__}: {exc}"
            return report

        report["legs_checked"] += 1
        prices = [float(price) for price in quotes if price]
        if not prices:
            # Recorded, not skipped: silence from the second source is not
            # confirmation from it.
            unpriced.append(route)
            continue

        theirs = min(prices)
        saving_pct = round((our_price - theirs) / our_price * 100, 1) if our_price else 0.0
        comparison = {
            "route": route,
            "depart_date": depart.isoformat(),
            "ours": our_price,
            "theirs": theirs,
            "saving_pct": saving_pct,
        }
        report["comparisons"].append(comparison)
        if saving_pct >= MATERIAL_SAVING_PCT and (
            report["cheapest_elsewhere"] is None
            or saving_pct > report["cheapest_elsewhere"]["saving_pct"]
        ):
            report["cheapest_elsewhere"] = comparison

    report["unpriced"] = sorted(unpriced)
    if report["cheapest_elsewhere"] is not None:
        report["verdict"] = "cheaper_elsewhere"
    elif unpriced:
        report["verdict"] = "partial"
    else:
        report["verdict"] = "agrees"
    return report


def letuska_checker(adults: int = 1) -> Checker:
    """The real second opinion. Import-time free so tests never touch a browser.

    Two things this has to get right, both of which it got wrong first:

    - **One-way, not a same-day return.** Passing no return date used to make
      the provider search `ret or depart`, quoting a round trip against our
      one-way legs. Every route read ~2.2x dearer and the report said the two
      sites agreed.
    - **The date asked for, not the cheapest nearby.** letuska offers
      neighbouring days ("Lety i v okolních dnech"), so the cheapest quote on
      the page is frequently for a different departure than the one in the
      itinerary. A leg reported as unpriced is honest; a leg compared against a
      different day is not.
    """

    def _check(origin: str, destination: str, depart: date) -> list[float]:
        from .providers.letuska import LetuskaProvider, LetuskaSearchFailed

        try:
            legs = LetuskaProvider().check_price(
                origin, destination, depart, ret=None, adults=adults
            )
        except LetuskaSearchFailed as exc:
            # One route the other site cannot price is not a reason to abandon
            # the rest of the shortlist.
            print(f"[verify] {origin}->{destination} {depart}: {exc}")
            return []
        return [
            leg.price_amount
            for leg in legs
            if leg.price_amount and leg.depart_date == depart
        ]

    return _check
