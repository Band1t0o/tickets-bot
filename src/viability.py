"""What sweep history says about a route or an airport.

The old catalogue answered "is this airport worth using" with a hand-written
note per airport, measured once by hand. That was genuinely useful and it does
not generalise: nobody is going to hand-measure every airport on earth.

Everything here is derived from sweeps already on disk, so it grows as the tool
is used and it is right by construction. `routes_with_no_results` from the sweep
runner is the signal that matters - a route searched on many dates that never
returned a single offer is not a quiet market, it is a route with no inventory,
which is exactly the Brno finding.

Hand-measured facts that no sweep can reproduce - airports that were checked and
then never swept - live in `data/airport_notes.json` and are merged on top.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .airports import load_notes
from .models import Leg

# Below this many attempts, "never returned an offer" is not yet evidence of
# anything - one bad afternoon on a third-party site looks identical.
MIN_ATTEMPTS_FOR_A_VERDICT = 3


@dataclass
class RouteStats:
    route: str
    origin: str
    destination: str
    searches: int = 0
    legs: int = 0
    min_price: float | None = None
    currency: str = ""

    @property
    def dead(self) -> bool:
        """Searched enough times to be confident nothing is being sold."""
        return self.searches >= MIN_ATTEMPTS_FOR_A_VERDICT and self.legs == 0

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "origin": self.origin,
            "destination": self.destination,
            "searches": self.searches,
            "legs": self.legs,
            "min_price": self.min_price,
            "currency": self.currency,
            "dead": self.dead,
        }


@dataclass
class AirportStats:
    iata: str
    legs: int = 0
    min_price: float | None = None
    currency: str = ""
    dead_routes: list[str] = field(default_factory=list)
    note: str = ""
    verdict: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "iata": self.iata,
            "legs": self.legs,
            "min_price": self.min_price,
            "currency": self.currency,
            "dead_routes": self.dead_routes,
            "note": self.note,
            "verdict": self.verdict,
        }


def _sweep_dirs(data_dir: Path) -> list[Path]:
    root = Path(data_dir) / "sweeps"
    if not root.exists():
        return []
    return sorted(
        (stamp for scenario in root.iterdir() if scenario.is_dir() for stamp in scenario.iterdir()),
        key=lambda p: p.name,
    )


def route_stats(data_dir: Path = Path("data")) -> dict[str, RouteStats]:
    """Attempts, offers found and cheapest price per "ORIGIN->DEST"."""
    stats: dict[str, RouteStats] = {}

    def entry(origin: str, destination: str) -> RouteStats:
        key = f"{origin}->{destination}"
        if key not in stats:
            stats[key] = RouteStats(route=key, origin=origin, destination=destination)
        return stats[key]

    for directory in _sweep_dirs(data_dir):
        status_path = directory / "status.json"
        if status_path.exists():
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                status = {}
            # A sweep from before route accounting existed has neither key, and
            # is counted through its legs alone rather than skipped.
            for route, count in (status.get("route_searches") or {}).items():
                origin, _, destination = route.partition("->")
                entry(origin, destination).searches += count

        legs_path = directory / "legs.jsonl"
        if not legs_path.exists():
            continue
        for line in legs_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                leg = Leg.from_dict(json.loads(line))
            except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                continue
            record = entry(leg.origin, leg.destination)
            record.legs += 1
            if record.min_price is None or leg.price_amount < record.min_price:
                record.min_price = leg.price_amount
                record.currency = leg.price_currency

    return stats


def airport_stats(data_dir: Path = Path("data")) -> dict[str, AirportStats]:
    """Per-airport view of the same history, with hand-measured notes merged in."""
    routes = route_stats(data_dir)
    airports: dict[str, AirportStats] = {}
    dead_by_airport: dict[str, list[str]] = defaultdict(list)

    for record in routes.values():
        for code in (record.origin, record.destination):
            if not code:
                continue
            stats = airports.setdefault(code, AirportStats(iata=code))
            stats.legs += record.legs
            if record.min_price is not None and (
                stats.min_price is None or record.min_price < stats.min_price
            ):
                stats.min_price = record.min_price
                stats.currency = record.currency
        if record.dead:
            dead_by_airport[record.origin].append(record.route)
            dead_by_airport[record.destination].append(record.route)

    for code, stats in airports.items():
        stats.dead_routes = sorted(dead_by_airport.get(code, []))
        if stats.legs:
            stats.verdict = "ok"
        elif stats.dead_routes:
            stats.verdict = "no_inventory"

    # Hand-measured notes win on the note text and fill in a verdict for
    # airports no sweep has touched, but never overwrite a verdict that live
    # data supports.
    for code, note in load_notes(data_dir).items():
        stats = airports.setdefault(code, AirportStats(iata=code))
        stats.note = note.get("note", "")
        if stats.verdict == "unknown":
            stats.verdict = note.get("verdict", "unknown")

    return airports


def report(data_dir: Path = Path("data")) -> dict:
    """Everything the UI needs to badge an airport picker."""
    routes = route_stats(data_dir)
    return {
        "airports": {code: stats.to_dict() for code, stats in sorted(airport_stats(data_dir).items())},
        "dead_routes": sorted(r.route for r in routes.values() if r.dead),
        "routes_searched": len(routes),
    }
