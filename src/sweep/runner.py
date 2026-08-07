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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from ..models import Leg
from ..scenario import Scenario
from .planner import LegSearch, plan_searches

# Politeness delay between searches on the same worker.
SEARCH_DELAY_S = 1.5
DEFAULT_WORKERS = 4

ProgressFn = Callable[[int, int, str], None]


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
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
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "depth": scenario.depth,
        }

    _write_status(directory, status_payload("running"))

    # Each chunk is handled start-to-finish by one worker, so one browser is
    # launched per worker rather than per search.
    chunks: list[list[LegSearch]] = [searches[i::workers] for i in range(workers)]

    def record(search: LegSearch, legs: list[Leg], error: str | None) -> None:
        label = f"{search.origin}→{search.destination} {search.depart_date}"
        with lock:
            result.completed += 1
            if error:
                result.errors.append(f"{label}: {error}")
            else:
                result.legs.extend(legs)
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
                    legs = provider.search_leg(
                        page,
                        search.origin,
                        search.destination,
                        search.depart_date,
                        search.ret_date,
                        scenario.adults,
                    )
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


def load_legs(directory: Path) -> list[Leg]:
    path = Path(directory) / "legs.jsonl"
    if not path.exists():
        return []
    return [
        Leg.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _NullPage:
    """Stand-in page for providers that do not need a browser (tests, demo)."""


class _browser_page:
    """Context manager yielding a Playwright page, or a null page for fakes."""

    def __init__(self, provider: LegProvider):
        self.provider = provider
        self._pw = None
        self._browser = None

    def __enter__(self):
        # Providers that never touch `page` (the fake in tests, DemoStatic) do
        # not justify launching Chromium.
        if getattr(self.provider, "NAME", "") in {"FAKE", "DEMO_STATIC"}:
            return _NullPage()
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        context = self._browser.new_context(
            locale="cs-CZ", viewport={"width": 1600, "height": 1000}
        )
        return context.new_page()

    def __exit__(self, *exc_info):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        return False
