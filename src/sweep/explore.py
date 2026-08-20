"""Read an exploration sweep and say which airports are worth pricing properly.

A deep sweep of a trip with three origins, three Japanese airports and two
Philippine ones is 597 searches and an hour and a half against a site that
throttles this client. Most of that is spent on airports that were never going
to win. This turns a 63-search reconnaissance pass into the one thing needed to
narrow the trip: for each airport, what it costs against the alternatives
standing in the same place.

Nothing here decides anything. It ranks and it labels, and the removing is done
by a person looking at the numbers - three sampled dates is enough to see that
Katowice is 82% dearer with an extra transfer, and nowhere near enough to
justify a tool quietly dropping it.

The one distinction it must never get wrong is **measured** against
**unmeasured**. The sweep running when this was written managed 1.9 legs per
search, so an empty result was far more often the site refusing to answer than
a route with nothing on it. Saying "no flights" there would retire a perfectly
good airport on the strength of four timeouts, which is the exact failure this
project keeps having to design against.
"""
from __future__ import annotations

from ..models import Leg
from ..scenario import Scenario
from ..viability import MIN_ATTEMPTS_FOR_A_VERDICT

# Distance above the cheapest airport in the same pool, in percent. Sampled on
# three dates, so they are deliberately coarse: the report is meant to separate
# "obviously not worth it" from "worth pricing properly", never to rank two
# plausible airports against each other.
CLOSE_PCT = 15.0
WORSE_PCT = 50.0


def _route_key(origin: str, destination: str) -> str:
    return f"{origin}->{destination}"


def _cheapest(legs: list[Leg]) -> Leg | None:
    return min(legs, key=lambda leg: leg.price_amount) if legs else None


def _side(
    routes: dict[str, dict], keys: list[str]
) -> tuple[float | None, int | None, int, int, int]:
    """Cheapest offer across `keys`, and what was asked to find it.

    Returns (min price, stops of that offer, searches, errors, legs).
    """
    searches = sum(routes[key]["searches"] for key in keys if key in routes)
    errors = sum(routes[key]["errors"] for key in keys if key in routes)
    found = sum(routes[key]["legs"] for key in keys if key in routes)
    priced = [routes[key] for key in keys if key in routes and routes[key]["min_price"] is not None]
    if not priced:
        return None, None, searches, errors, found
    best = min(priced, key=lambda row: row["min_price"])
    return best["min_price"], best["min_stops"], searches, errors, found


def _unmeasured_verdict(searches: int, errors: int) -> str:
    """Why an airport has no price: never asked, never answered, or nothing sold.

    `no_offers` is a claim about the market and is only made when the site
    actually answered often enough to support it. Everything else is
    `unproven`, which reads as "come back when the site is behaving" rather than
    as a verdict on the airport.
    """
    answered = searches - errors
    if answered >= MIN_ATTEMPTS_FOR_A_VERDICT:
        return "no_offers"
    return "unproven"


def _price_verdict(total: float, best: float) -> tuple[str, float]:
    if best <= 0:
        return "best", 0.0
    excess = round((total - best) / best * 100, 1)
    if excess <= 0:
        return "best", 0.0
    if excess <= CLOSE_PCT:
        return "close", excess
    if excess <= WORSE_PCT:
        return "worse", excess
    return "poor", excess


def explore_report(
    legs: list[Leg],
    scenario: Scenario,
    status: dict,
    current: Scenario | None = None,
) -> dict:
    """Per-airport and per-route summary of one exploration sweep.

    `scenario` is the trip this run *searched* - the snapshot written into the
    sweep directory, not whatever the trip has since become. `current` is the
    trip as it stands now, and is used only to say how the two differ; it never
    filters a row. Reading a run against the wrong trip is how a probe of
    Prague, Vienna and Frankfurt came to be presented as the answer for a trip
    flying out of Katowice.

    `status` supplies attempts and failures per route; they are not derivable
    from `legs`, and without them a route that was never searched looks exactly
    like one that was searched and came back empty.
    """
    pools = scenario.airport_pools
    roles = scenario.pool_roles
    wanted = {
        _route_key(origin, destination)
        for index in range(len(pools) - 1)
        for origin in pools[index]
        for destination in pools[index + 1]
        if origin != destination
    }

    searched = status.get("route_searches") or {}
    failed = status.get("route_errors") or {}

    # Legs from a route the trip no longer contains are dropped rather than
    # reported: a trip edited between the probe and this report would otherwise
    # grow rows for airports it does not have.
    by_route: dict[str, list[Leg]] = {}
    for leg in legs:
        key = _route_key(leg.origin, leg.destination)
        if key in wanted:
            by_route.setdefault(key, []).append(leg)

    routes: dict[str, dict] = {}
    for key in sorted(wanted | set(searched) & wanted):
        origin, _, destination = key.partition("->")
        found = by_route.get(key, [])
        cheapest = _cheapest(found)
        routes[key] = {
            "route": key,
            "origin": origin,
            "destination": destination,
            "searches": int(searched.get(key, 0)),
            "errors": int(failed.get(key, 0)),
            "legs": len(found),
            "min_price": cheapest.price_amount if cheapest else None,
            "min_stops": cheapest.stops if cheapest else None,
            "min_duration_minutes": cheapest.duration_minutes if cheapest else None,
        }

    now = current.airport_pools if current is not None else pools
    # Pools are positional, so a trip that has gained or lost a stop cannot be
    # lined up against this run at all. Saying the shape changed is honest;
    # comparing pool 2 of one trip with pool 2 of the other would accuse Manila
    # of never being searched because it moved along one.
    shape_changed = len(now) != len(pools)

    pool_blocks = []
    for index, (airports, role) in enumerate(zip(pools, roles, strict=True)):
        rows = [
            _airport_row(code, index, pools, routes)
            for code in airports
        ]
        _rank(rows)
        missing = (
            [] if shape_changed else [code for code in now[index] if code not in airports]
        )
        pool_blocks.append(
            {"index": index, **role, "airports": rows, "not_searched": missing}
        )

    return {
        "scenario_id": scenario.id,
        "currency": scenario.currency,
        "shape_changed": shape_changed,
        # False whenever a row here is about an airport you no longer fly, or an
        # airport you do fly is absent. Either way the report answers a question
        # you did not ask, and the tab has to say so before drawing a table.
        "matches_current_trip": not shape_changed
        and all(set(searched) == set(now[index]) for index, searched in enumerate(pools)),
        "pools": pool_blocks,
        "routes": list(routes.values()),
    }


def _airport_row(code: str, index: int, pools: list[list[str]], routes: dict[str, dict]) -> dict:
    """One airport, judged on every side of it this trip actually flies.

    A middle stop is scored on arriving *and* leaving, added together. Cheap to
    fly into and dear to fly out of is not a cheap airport, and reporting only
    one side would recommend exactly that mistake.
    """
    inbound = (
        [_route_key(previous, code) for previous in pools[index - 1] if previous != code]
        if index > 0
        else []
    )
    outbound = (
        [_route_key(code, following) for following in pools[index + 1] if following != code]
        if index < len(pools) - 1
        else []
    )

    in_price, in_stops, in_searches, in_errors, in_legs = _side(routes, inbound)
    out_price, out_stops, out_searches, out_errors, out_legs = _side(routes, outbound)

    sides = [(inbound, in_price), (outbound, out_price)]
    needed = [price for keys, price in sides if keys]
    total = sum(needed) if needed and all(price is not None for price in needed) else None

    row = {
        "iata": code,
        "searches": in_searches + out_searches,
        "errors": in_errors + out_errors,
        "legs": in_legs + out_legs,
        "in_min_price": in_price,
        "in_min_stops": in_stops,
        "out_min_price": out_price,
        "out_min_stops": out_stops,
        "total_min": total,
        "vs_best_pct": None,
        "verdict": "unproven",
        "routes": inbound + outbound,
    }

    if total is None:
        # Judged on the side that failed, and on the harsher reading when both
        # did: an airport is only called dead when the site answered enough
        # times to mean it.
        missing = [
            (keys, searches, errors)
            for (keys, price), searches, errors in zip(
                sides, (in_searches, out_searches), (in_errors, out_errors), strict=True
            )
            if keys and price is None
        ]
        verdicts = {_unmeasured_verdict(searches, errors) for _, searches, errors in missing}
        row["verdict"] = "unproven" if "unproven" in verdicts else "no_offers"
    return row


def _rank(rows: list[dict]) -> None:
    """Score every priced airport in a pool against the cheapest of them."""
    priced = [row for row in rows if row["total_min"] is not None]
    if not priced:
        return
    best = min(row["total_min"] for row in priced)
    for row in priced:
        row["verdict"], row["vs_best_pct"] = _price_verdict(row["total_min"], best)
