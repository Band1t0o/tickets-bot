"""Volatility probe: does the cheapest price actually move between sweeps?

The daily sweep cadence rests on one observation made during planning — a
PRG→NRT round trip reading 19,223 Kč and then 19,850 Kč about 20 minutes later
(+3.3%). That shows intraday movement exists but says nothing about how often
or how far prices move.

This samples three fixed routes every couple of hours so the cadence can be set
from data. It is deliberately tiny (~2 min per run).

It was scheduled for deletion - ~720 Actions minutes a month would have broken
the 2,000-minute private tier alongside the daily deep sweep - and then the repo
went public and those minutes became free. It has earned its place twice since:
it is what established that fares move day-over-day rather than intraday, and
what caught FRA->NRT climbing 25% in four days. `.github/workflows/probe.yml` is
the authority on whether it runs, and says the same.

Routes are fixed rather than derived from a scenario, so observations stay
comparable across the whole collection period.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
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

# A move smaller than this is the site rounding, not the market. Counting every
# non-zero delta made NRT->MNL - by far the steadiest of the three routes,
# living inside a 2% band all week - report the *highest* change rate of them
# all, at 56%, because its 6 Kc twitches on a 3,850 Kc fare each counted once.
MEANINGFUL_MOVE_PCT = 1.0

# Net movement past this, in either direction, is worth saying out loud. FRA
# ->NRT climbed 25% in four days while the panel reported "biggest drop 20".
NOTABLE_TREND_PCT = 5.0


@dataclass
class RouteStats:
    route: str
    n_observations: int = 0
    n_changes: int = 0
    median_change: float = 0.0
    max_change: float = 0.0
    max_change_pct: float = 0.0
    largest_drop: float = 0.0
    # First observation to last, signed. The single most useful number here and
    # the one that was missing: it is what says a fare is running away from you.
    net_change_pct: float = 0.0
    # Cheapest to dearest seen, regardless of order - how much was on the table
    # across the period, which a net of zero can completely conceal.
    range_pct: float = 0.0
    n_meaningful_changes: int = 0

    @property
    def change_rate(self) -> float:
        """Share of consecutive observations where the price moved at all."""
        if self.n_observations < 2:
            return 0.0
        return round(self.n_changes / (self.n_observations - 1), 3)

    @property
    def meaningful_change_rate(self) -> float:
        """Share of steps that moved by more than rounding noise."""
        if self.n_observations < 2:
            return 0.0
        return round(self.n_meaningful_changes / (self.n_observations - 1), 3)


@dataclass
class ProbeStats:
    routes: dict[str, RouteStats] = field(default_factory=dict)

    @property
    def recommendation(self) -> str:
        """What the numbers actually argue for.

        Weighed on magnitude, not on counting. The old rule fired whenever more
        than 20% of steps were non-zero, so three routes sitting inside a 2%
        band - one of them moving by six crowns at a time - read as "sample
        more often than daily" and would have doubled the sweep budget to chase
        rounding.
        """
        if not self.routes:
            return "No observations yet — let the probe run for a few days."
        measured = [r for r in self.routes.values() if r.n_observations >= 2]
        if not measured:
            return "Not enough observations yet — let the probe run for a few days."

        # Direction first: whether the fare is running away from you matters
        # more than how often it twitches, and nothing here used to report it.
        trending = [r for r in measured if abs(r.net_change_pct) >= NOTABLE_TREND_PCT]
        lines = []
        if trending:
            worst = max(trending, key=lambda r: abs(r.net_change_pct))
            direction = "risen" if worst.net_change_pct > 0 else "fallen"
            others = f" ({len(trending)} of {len(measured)} routes are moving)" if len(trending) > 1 else ""
            lines.append(
                f"{worst.route} has {direction} {abs(worst.net_change_pct):.1f}% "
                f"over the period measured{others}."
            )

        rates = [r.meaningful_change_rate for r in measured]
        moves = [r.range_pct for r in measured]
        if max(rates) < STABLE_CHANGE_RATE or max(moves) < STABLE_MOVE_PCT:
            lines.append(
                "Within a day prices barely move: a daily sweep is the right cadence, "
                "and a second one would mostly re-measure the same number."
            )
        else:
            lines.append(
                "Prices move often and by enough to be worth sampling more often than daily. "
                "Consider a second sweep per day."
            )
        return " ".join(lines)


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
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
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
        pairs = list(zip(prices, prices[1:], strict=False))
        deltas = [b - a for a, b in pairs]
        changes = [d for d in deltas if d != 0]
        entry.n_changes = len(changes)
        # Judged against the price it moved *from*, so the same absolute step is
        # not "large" on a 3,850 fare and "small" on a 13,556 one.
        entry.n_meaningful_changes = sum(
            1 for before, after in pairs if before and abs(after - before) / before * 100 >= MEANINGFUL_MOVE_PCT
        )
        if prices:
            first = prices[0] or 1
            entry.net_change_pct = round((prices[-1] - prices[0]) / first * 100, 1)
            low = min(prices) or 1
            entry.range_pct = round((max(prices) - min(prices)) / low * 100, 1)
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
