"""Volatility probe: does the cheapest price actually move between sweeps?

The daily sweep cadence rests on one observation made during planning — a
PRG→NRT round trip reading 19,223 Kč and then 19,850 Kč about 20 minutes later
(+3.3%). That shows intraday movement exists but says nothing about how often
or how far prices move.

This samples three fixed routes every couple of hours so the cadence can be set
from data. It is deliberately tiny (~2 min per run) and **temporary**: seven
days costs ~168 Actions minutes, but left running it costs ~720/month and would
break the budget alongside the daily deep sweep.

Routes are fixed rather than derived from a scenario, so observations stay
comparable across the whole collection period.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .models import Leg

PROBE_ROUTES: list[tuple[str, str, date]] = [
    ("PRG", "NRT", date(2027, 1, 12)),
    ("FRA", "NRT", date(2027, 1, 12)),
    ("NRT", "MNL", date(2027, 1, 22)),
]

DEFAULT_PROBE_DIR = Path("data/probe")

# Thresholds for the recommendation, stated in the plan: below these, daily
# sweeping loses nothing worth chasing.
STABLE_CHANGE_RATE = 0.20
STABLE_MOVE_PCT = 2.0


@dataclass
class RouteStats:
    route: str
    n_observations: int = 0
    n_changes: int = 0
    median_change: float = 0.0
    max_change: float = 0.0
    max_change_pct: float = 0.0
    largest_drop: float = 0.0

    @property
    def change_rate(self) -> float:
        """Share of consecutive observations where the price moved."""
        if self.n_observations < 2:
            return 0.0
        return round(self.n_changes / (self.n_observations - 1), 3)


@dataclass
class ProbeStats:
    routes: dict[str, RouteStats] = field(default_factory=dict)

    @property
    def recommendation(self) -> str:
        if not self.routes:
            return "No observations yet — let the probe run for a few days."
        rates = [r.change_rate for r in self.routes.values() if r.n_observations >= 2]
        moves = [r.max_change_pct for r in self.routes.values()]
        if not rates:
            return "Not enough observations yet — let the probe run for a few days."
        if max(rates) < STABLE_CHANGE_RATE and max(moves) < STABLE_MOVE_PCT:
            return (
                "Prices are stable at this horizon: a daily sweep is the right cadence. "
                "Disable the probe workflow."
            )
        return (
            "Prices move often enough to be worth sampling more often than daily. "
            "Consider a second sweep per day, weighing it against the Actions budget."
        )


def record_observation(
    legs: list[Leg],
    origin: str,
    destination: str,
    depart: date,
    directory: Path | str = DEFAULT_PROBE_DIR,
) -> dict:
    """Append one observation and return it."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    prices = [leg.price_amount for leg in legs if leg.price_amount]
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origin": origin,
        "destination": destination,
        "depart_date": depart.isoformat(),
        # None, never 0: a run that returned nothing is scraper breakage, not a
        # free flight, and averaging a 0 into the series would be nonsense.
        "min_price": min(prices) if prices else None,
        "n_offers": len(legs),
        "currency": legs[0].price_currency if legs else "CZK",
    }
    with (directory / "observations.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return record


def probe_report(directory: Path | str = DEFAULT_PROBE_DIR) -> ProbeStats:
    """Summarise how much prices moved, per route."""
    path = Path(directory) / "observations.jsonl"
    if not path.exists():
        return ProbeStats()

    series: dict[str, list[float]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("min_price") is None:
            continue  # breakage, not a price
        key = f"{row['origin']}→{row['destination']}"
        series.setdefault(key, []).append(float(row["min_price"]))

    stats = ProbeStats()
    for route, prices in series.items():
        entry = RouteStats(route=route, n_observations=len(prices))
        deltas = [b - a for a, b in zip(prices, prices[1:])]
        changes = [d for d in deltas if d != 0]
        entry.n_changes = len(changes)
        if changes:
            entry.median_change = round(statistics.median(abs(d) for d in changes), 1)
            entry.max_change = round(max(abs(d) for d in changes), 1)
            entry.largest_drop = round(abs(min(deltas)), 1) if min(deltas) < 0 else 0.0
            base = prices[0] or 1
            entry.max_change_pct = round(entry.max_change / base * 100, 2)
        stats.routes[route] = entry
    return stats


def format_report(stats: ProbeStats) -> str:
    lines = [f"{'route':12} {'obs':>5} {'changed':>8} {'median':>9} {'max':>9} {'max %':>7} {'biggest drop':>13}"]
    for route in sorted(stats.routes):
        r = stats.routes[route]
        lines.append(
            f"{route:12} {r.n_observations:>5} {r.change_rate * 100:>7.0f}% "
            f"{r.median_change:>9,.0f} {r.max_change:>9,.0f} {r.max_change_pct:>6.1f}% {r.largest_drop:>13,.0f}"
        )
    lines.append("")
    lines.append(stats.recommendation)
    return "\n".join(lines)


def run_probe(directory: Path | str = DEFAULT_PROBE_DIR, provider=None) -> list[dict]:
    """Sample every probe route once."""
    if provider is None:
        from .providers.pelikan import PelikanProvider

        provider = PelikanProvider()

    from .sweep.runner import _browser_page

    records = []
    with _browser_page(provider) as page:
        for origin, destination, depart in PROBE_ROUTES:
            try:
                legs = provider.search_leg(page, origin, destination, depart)
            except Exception as exc:
                print(f"[probe] {origin}→{destination} failed: {exc}")
                legs = []
            record = record_observation(legs, origin, destination, depart, directory)
            price = record["min_price"]
            summary = "no results" if price is None else f"{price:,.0f} {record['currency']}"
            print(f"[probe] {origin}→{destination} {depart}: {summary}")
            records.append(record)
    return records
