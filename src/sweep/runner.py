"""Execute a planned sweep across several browsers and write results to disk.

Concurrency note: `sync_playwright()` is not shareable across threads. Each
worker thread therefore creates its own Playwright instance and reuses a single
page for every search it handles, which also avoids paying browser startup per
search.

Everything lands under `data/sweeps/<scenario_id>/<timestamp>/` as plain files,
keeping the project database-free so the cloud run can commit results straight
back to the repo.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..models import Leg
from ..scenario import Scenario
from .planner import LegSearch, plan_searches

# Politeness delay between searches on the same worker.
#
# Raised from 1.5s/4 workers after the first sweep that could actually report
# failures: 58 of 93 searches timed out. The same rate was present before and
# invisible - the previous "0 errors" sweep averaged 2.9 legs per search where a
# healthy search returns ~10, so roughly 70% of it was already failing silently.
#
# Whether the cause is concurrency or IP-level throttling is not established:
# after a day of probing, even sequential single searches from this machine went
# from ~14s to over 6 minutes, which is consistent with the site throttling the
# client rather than with load per se. Both readings point the same way, so
# these settings are deliberately gentler than measured need, and the daily
# scheduled run is what should validate them - not more hammering.
SEARCH_DELAY_S = 4.0
DEFAULT_WORKERS = 2

ProgressFn = Callable[[int, int, str], None]

# What a sweep must clear before its best total may be plotted against another
# sweep's. Measured, not guessed: of the first four sweeps committed here, the
# `standard` one averaged 2.9 legs per search with error_count 0 and reported a
# best total 7% *worse* than a `quick` sweep at 9.7 that ran half as many
# searches. Comparing them as a price series charts scraper health.
#
# 6.0 sits between the two clusters (2.9/3.7 against 7.6/9.7) rather than on
# either, so neither a marginal pass nor a marginal fail turns on it.
MIN_COMPARABLE_LEGS_PER_SEARCH = 6.0


def legs_per_search_of(status: dict) -> float | None:
    """This sweep's legs per search, recorded or derived.

    Sweeps written before the field existed still carry `legs_found` and
    `total`, and the figure is exactly their quotient - so deriving it
    classifies committed history honestly instead of discarding all of it for
    lacking a field it could not have had.
    """
    recorded = status.get("legs_per_search")
    if recorded is not None:
        return float(recorded)
    total = status.get("total")
    found = status.get("legs_found")
    if not total or found is None:
        return None
    return round(found / total, 2)


def is_comparable(status: dict, routes_covered: int, routes_planned: int) -> bool:
    """Whether this sweep's best total may be plotted beside another's.

    Two independent ways for a sweep to be incomparable, and both have happened:

    - **Starved.** Enough searches failed or came back thin that the cheapest
      trip was simply never seen. `error_count` does not catch this; legs per
      search does.
    - **Narrower than the trip.** Either a route went dark, or the sweep
      predates a widening of the trip and searched a smaller one. Measuring
      coverage against what the trip plans *now* handles both, and correctly
      retires old sweeps when the trip is edited.
    """
    if status.get("state") != "done":
        return False
    if not routes_planned or routes_covered < routes_planned:
        return False
    legs_per_search = legs_per_search_of(status)
    return legs_per_search is not None and legs_per_search >= MIN_COMPARABLE_LEGS_PER_SEARCH


class LegProvider(Protocol):
    NAME: str

    def search_leg(self, page, origin: str, destination: str, depart, ret=None, adults: int = 1) -> list[Leg]:
        ...


@dataclass
class SweepResult:
    scenario_id: str
    directory: Path
    total: int
    completed: int = 0
    legs: list[Leg] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    # Searches attempted and legs found, per "ORIGIN->DEST". A route that was
    # searched on many dates and yielded nothing on all of them is breakage,
    # not a quiet market - see routes_with_no_results.
    route_searches: dict[str, int] = field(default_factory=dict)
    route_legs: dict[str, int] = field(default_factory=dict)

    @property
    def routes_with_no_results(self) -> list[str]:
        """Routes that were searched but never returned a single offer.

        This is the signal that was missing when MNL->VIE, CEB->PRG and
        CEB->FRA all came back empty while the sweep reported error_count: 0.
        """
        return sorted(
            route
            for route, attempts in self.route_searches.items()
            if attempts and not self.route_legs.get(route)
        )

    @property
    def legs_per_search(self) -> float:
        """The honest health metric: flights found per search run.

        ~10 is healthy; 2.9 was 70% silent failure. Recorded rather than
        derived on demand because it is what makes two sweeps comparable, and
        the one figure that separates a broken sweep from a quiet market -
        `error_count` cannot, having read 0 on the sweep that was failing most.
        """
        if not self.total:
            return 0.0
        return round(len(self.legs) / self.total, 2)

    @property
    def route_coverage(self) -> float:
        """Share of searched routes that returned at least one offer.

        A sweep missing the leg home still reports a healthy leg count while
        being unable to produce a single complete trip, so coverage is tracked
        separately from volume.
        """
        if not self.route_searches:
            return 0.0
        found = sum(1 for route in self.route_searches if self.route_legs.get(route))
        return round(found / len(self.route_searches), 3)

    @property
    def is_healthy(self) -> bool:
        """False when the sweep looks broken rather than merely unlucky.

        A sweep that finds nothing, or fails more than half its searches, is
        almost certainly scraper breakage. This distinction exists because the
        previous automation died silently and went unnoticed for 8 months:
        "no results" must never be indistinguishable from "no cheap flights".
        """
        if self.total == 0:
            return False
        if not self.legs:
            return False
        return len(self.errors) <= self.total / 2


def _search_with_retry(provider, page, search: LegSearch, adults: int, delay_s: float):
    """One retry on timeout before giving up on a search.

    Four workers hammering the site makes it noticeably slower to render, which
    is the most likely reason a search times out. A single retry recovers those
    without masking a genuinely broken selector, which fails both times.
    """
    from ..providers.pelikan import SearchTimeout

    for attempt in (1, 2):
        try:
            return provider.search_leg(
                page,
                search.origin,
                search.destination,
                search.depart_date,
                search.ret_date,
                adults,
            )
        except SearchTimeout:
            if attempt == 2:
                raise
            if delay_s:
                time.sleep(delay_s)
    return []


def _chunk(searches: list[LegSearch], workers: int) -> list[list[LegSearch]]:
    """Split into contiguous runs, one per worker.

    Two bugs lived in the one-liner this replaces, `searches[i::workers]`:

    - `workers=0` produced an empty list, so a sweep reported itself complete
      having run nothing at all.
    - Striding interleaves. The planner emits searches grouped by route, so
      every worker started on the *same* origin-destination pair at the same
      moment - the worst possible pattern against a per-route throttle, and a
      plausible contributor to the timeouts that made 58 of 93 searches fail.
      Contiguous runs spread the workers across different routes instead.
    """
    workers = max(1, workers)
    if not searches:
        return []
    size, extra = divmod(len(searches), workers)
    chunks: list[list[LegSearch]] = []
    start = 0
    for index in range(workers):
        end = start + size + (1 if index < extra else 0)
        if start < end:
            chunks.append(searches[start:end])
        start = end
    return chunks


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_status(directory: Path, payload: dict) -> None:
    (directory / "status.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_sweep(
    scenario: Scenario,
    provider: LegProvider | None = None,
    data_dir: Path | str = "data",
    workers: int = DEFAULT_WORKERS,
    on_progress: ProgressFn | None = None,
    depth: str | None = None,
    delay_s: float = SEARCH_DELAY_S,
) -> SweepResult:
    """Run every search `scenario` implies and persist the legs found."""
    if depth:
        from dataclasses import replace

        scenario = replace(scenario, depth=depth)
    scenario.validate()

    if provider is None:
        from ..providers.pelikan import PelikanProvider

        provider = PelikanProvider()

    searches = plan_searches(scenario)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    directory = Path(data_dir) / "sweeps" / scenario.id / stamp
    directory.mkdir(parents=True, exist_ok=True)

    result = SweepResult(
        scenario_id=scenario.id,
        directory=directory,
        total=len(searches),
        started_at=_now(),
    )

    lock = threading.Lock()

    def status_payload(state: str, current: str = "") -> dict:
        return {
            "scenario_id": scenario.id,
            "state": state,
            "total": result.total,
            "completed": result.completed,
            "current": current,
            "legs_found": len(result.legs),
            "errors": result.errors[-20:],
            "error_count": len(result.errors),
            # Written into every sweep so later ones can be compared against
            # earlier ones without re-reading and re-combining legs.jsonl.
            "legs_per_search": result.legs_per_search,
            "route_coverage": result.route_coverage,
            "routes_planned": len(result.route_searches),
            "routes_with_legs": sum(1 for r in result.route_searches if result.route_legs.get(r)),
            "routes_with_no_results": result.routes_with_no_results,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "depth": scenario.depth,
        }

    _write_status(directory, status_payload("running"))

    # Each chunk is handled start-to-finish by one worker, so one browser is
    # launched per worker rather than per search.
    chunks = _chunk(searches, workers)

    def record(search: LegSearch, legs: list[Leg], error: str | None) -> None:
        label = f"{search.origin}→{search.destination} {search.depart_date}"
        route = f"{search.origin}->{search.destination}"
        with lock:
            result.completed += 1
            result.route_searches[route] = result.route_searches.get(route, 0) + 1
            result.route_legs.setdefault(route, 0)
            if error:
                result.errors.append(f"{label}: {error}")
            else:
                result.legs.extend(legs)
                result.route_legs[route] += len(legs)
            if result.completed % 5 == 0 or result.completed == result.total:
                _write_status(directory, status_payload("running", label))
            if on_progress:
                on_progress(result.completed, result.total, label)

    def worker(chunk: list[LegSearch]) -> None:
        if not chunk:
            return
        with _browser_page(provider) as page:
            for search in chunk:
                try:
                    legs = _search_with_retry(provider, page, search, scenario.adults, delay_s)
                    record(search, legs, None)
                except Exception as exc:  # one bad search must not kill the sweep
                    record(search, [], str(exc))
                if delay_s:
                    time.sleep(delay_s)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(worker, chunks))

    result.finished_at = _now()
    _write_legs(directory, result.legs)
    _write_status(directory, status_payload("done" if result.is_healthy else "unhealthy"))
    return result


def _write_legs(directory: Path, legs: list[Leg]) -> None:
    with (directory / "legs.jsonl").open("w", encoding="utf-8") as handle:
        for leg in legs:
            handle.write(json.dumps(leg.to_dict(), ensure_ascii=False) + "\n")


def _stamp_to_iso(name: str) -> str | None:
    """"2026-08-10T11-57-06Z" -> "2026-08-10T11:57:06+00:00", or None."""
    try:
        return (
            datetime.strptime(name, "%Y-%m-%dT%H-%M-%SZ")
            .replace(tzinfo=UTC)
            .isoformat(timespec="seconds")
        )
    except ValueError:
        return None


def load_legs(directory: Path) -> list[Leg]:
    directory = Path(directory)
    path = directory / "legs.jsonl"
    if not path.exists():
        return []
    legs = [
        Leg.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    # Sweeps committed before legs carried their own timestamp still deserve an
    # answer to "when was this true", and the directory name is the honest one:
    # accurate to the start of the sweep rather than to the search. Never
    # applied over a leg that knows its own time.
    fallback = _stamp_to_iso(directory.name)
    if fallback:
        for leg in legs:
            if leg.observed_at is None:
                leg.observed_at = fallback
    return legs


class _NullPage:
    """Stand-in page for providers that do not need a browser (tests, demo)."""


class _browser_page:
    """Context manager yielding a Playwright page, or a null page for fakes."""

    def __init__(self, provider: LegProvider):
        self.provider = provider
        self._pw = None
        self._browser = None
        self._context = None

    def __enter__(self):
        # Providers that never touch `page` (the fake in tests, DemoStatic) do
        # not justify launching Chromium.
        if getattr(self.provider, "NAME", "") in {"FAKE", "DEMO_STATIC"}:
            return _NullPage()
        from playwright.sync_api import sync_playwright

        # If launch() raises after start() succeeds, __exit__ never runs and the
        # driver subprocess is left behind - once per worker, every sweep.
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                locale="cs-CZ", viewport={"width": 1600, "height": 1000}
            )
            return self._context.new_page()
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc_info):
        # Each step is independent: a context that refuses to close must not
        # strand the browser, and a browser that refuses to close must not
        # strand the driver process.
        for close in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if close is None:
                continue
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - teardown, nothing left to do
                print(f"[sweep] browser teardown: {type(exc).__name__}: {exc}")
        self._context = self._browser = self._pw = None
        return False
