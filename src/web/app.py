"""Local control panel for the flight scenario watcher.

The UI is a viewer and launcher, never a dependency for searching — the
scheduled GitHub Actions sweep runs whether or not this server is up, and
commits its results back to the repo. This app reads those committed files.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import asdict, replace
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..airports import describe, frequent_airports, lookup
from ..airports import search_with_meta as search_airports
from ..combine import combine_all, series_from_result
from ..scenario import Scenario, load_scenario, load_scenarios, save_scenario
from ..sweep.planner import SECONDS_PER_SEARCH, estimate_minutes, plan_searches
from ..sweep.runner import DEFAULT_WORKERS, load_legs, run_sweep
from ..viability import report as viability_report

SCENARIO_DIR = Path(os.getenv("SCENARIO_DIR", "scenarios"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STATIC_DIR = Path(__file__).parent / "static"

# Both are interpolated straight into filesystem paths, so neither may contain
# a separator or a parent reference.
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")

app = FastAPI(title="Flight scenario watcher")

# One local sweep at a time; the UI reflects this state in its status strip.
_running: dict[str, threading.Thread] = {}
# A sweep thread that dies takes its traceback with it. Without this the
# endpoint has already returned {"started": true}, no status.json is ever
# written, and the UI polls forever showing "No sweeps yet".
_failures: dict[str, str] = {}


def _safe_id(scenario_id: str) -> str:
    if not SAFE_ID.match(scenario_id):
        raise HTTPException(400, f"{scenario_id!r} is not a valid scenario id")
    return scenario_id


def _safe_stamp(stamp: str) -> str:
    if not SAFE_STAMP.match(stamp):
        raise HTTPException(400, f"{stamp!r} is not a valid sweep timestamp")
    return stamp


def _scenario_or_404(scenario_id: str) -> Scenario:
    path = SCENARIO_DIR / f"{_safe_id(scenario_id)}.json"
    if not path.exists():
        raise HTTPException(404, f"No scenario named {scenario_id!r}")
    return load_scenario(path)


def _sweep_dirs(scenario_id: str) -> list[Path]:
    root = DATA_DIR / "sweeps" / _safe_id(scenario_id)
    if not root.exists():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)


def _read_status(directory: Path) -> dict:
    path = directory / "status.json"
    if not path.exists():
        return {"state": "unknown"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"state": "unreadable"}


# ------------------------------------------------------------------- airports


@app.get("/api/airports/search")
def airport_search(q: str = "", limit: int = 20) -> dict:
    """Matches, plus `total` and `country` so the UI can say what it cut."""
    return search_airports(q, limit=min(max(limit, 1), 50), data_dir=DATA_DIR)


@app.get("/api/airports/frequent")
def airport_frequent() -> dict:
    """Airports you already use, for one-click chips beside the typeahead."""
    return frequent_airports(SCENARIO_DIR, data_dir=DATA_DIR)


@app.get("/api/airports/{code}")
def airport_detail(code: str) -> dict:
    airport = lookup(code, data_dir=DATA_DIR)
    if not airport:
        raise HTTPException(404, f"No airport with IATA code {code!r}")
    return airport


@app.get("/api/viability")
def viability() -> dict:
    """What sweep history says about each airport and route."""
    return viability_report(DATA_DIR)


# ------------------------------------------------------------------ scenarios


@app.get("/api/scenarios")
def list_scenarios_endpoint() -> list[dict]:
    return [s.to_dict() for s in load_scenarios(SCENARIO_DIR)]


@app.get("/api/scenarios/{scenario_id}")
def get_scenario(scenario_id: str) -> dict:
    return _scenario_or_404(scenario_id).to_dict()


def _scenario_from_payload(payload: dict) -> Scenario:
    """Build and validate a Scenario from a request body.

    There is no Pydantic mirror of the schema. One used to exist and it restated
    all fourteen fields and their defaults, so every schema change was a
    four-file edit and the two definitions drifted. `Scenario.from_dict` already
    rejects unknown fields and `validate()` already produces messages written to
    be shown verbatim, which is exactly what an API needs.
    """
    try:
        scenario = Scenario.from_dict(payload)
        scenario.validate()
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return scenario


@app.post("/api/scenarios", status_code=201)
def create_scenario(payload: dict = Body(...)) -> dict:
    scenario = _scenario_from_payload(payload)
    _safe_id(scenario.id)
    # `save_scenario` writes unconditionally, so POST used to silently overwrite
    # an existing trip - and the UI now generates ids from the trip name, which
    # makes a collision something a person can hit by naming two trips alike.
    if (SCENARIO_DIR / f"{scenario.id}.json").exists():
        raise HTTPException(409, f"a trip with the id {scenario.id!r} already exists")
    save_scenario(scenario, SCENARIO_DIR)
    return scenario.to_dict()


@app.delete("/api/scenarios/{scenario_id}")
def delete_scenario(scenario_id: str) -> dict:
    path = SCENARIO_DIR / f"{_safe_id(scenario_id)}.json"
    if not path.exists():
        raise HTTPException(404, f"No scenario {scenario_id!r}")
    path.unlink()
    # `data/sweeps/<id>/` stays. It is committed history that took real Actions
    # minutes to gather, and deleting a plan is not a request to burn the
    # measurements taken under it.
    return {"deleted": scenario_id}


@app.put("/api/scenarios/{scenario_id}")
def update_scenario(scenario_id: str, payload: dict = Body(...)) -> dict:
    _safe_id(scenario_id)
    scenario = _scenario_from_payload(payload)
    # The path used to be ignored entirely, so PUT /api/scenarios/foo with a
    # body naming "bar" wrote bar.json and left foo.json stale behind it.
    if scenario.id != scenario_id:
        raise HTTPException(
            400, f"body id {scenario.id!r} does not match the path id {scenario_id!r}"
        )
    save_scenario(scenario, SCENARIO_DIR)
    return scenario.to_dict()


@app.post("/api/scenarios/{scenario_id}/estimate")
def estimate(scenario_id: str, depth: str | None = None) -> dict:
    scenario = _scenario_or_404(scenario_id)
    if depth:
        scenario = replace(scenario, depth=depth)
        try:
            scenario.validate()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    searches = plan_searches(scenario)
    return {
        "searches": len(searches),
        "minutes": estimate_minutes(searches),
        "depth": scenario.depth,
        "leg_count": scenario.leg_count,
        "per_leg": {
            str(i): sum(1 for s in searches if s.leg_index == i)
            for i in sorted({s.leg_index for s in searches})
        },
        "leg_labels": _leg_labels(scenario),
    }


def _leg_labels(scenario: Scenario) -> list[str]:
    """"PRG/VIE → NRT/HND" per leg, for a UI that no longer knows the countries."""
    pools = scenario.airport_pools
    return [
        f"{'/'.join(pools[i])} → {'/'.join(pools[i + 1])}" for i in range(len(pools) - 1)
    ]


# ----------------------------------------------------------------- running


@app.post("/api/scenarios/{scenario_id}/run")
def run_locally(scenario_id: str, depth: str | None = None) -> dict:
    scenario = _scenario_or_404(scenario_id)
    if scenario_id in _running and _running[scenario_id].is_alive():
        raise HTTPException(409, "A sweep is already running for this scenario")

    if depth:
        try:
            replace(scenario, depth=depth).validate()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    _failures.pop(scenario_id, None)

    def work():
        try:
            run_sweep(scenario, data_dir=DATA_DIR, depth=depth)
        except Exception as exc:  # noqa: BLE001 - recorded and surfaced, not swallowed
            _failures[scenario_id] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    _running[scenario_id] = thread
    return {"started": True, "scenario_id": scenario_id, "depth": depth or scenario.depth}


@app.post("/api/scenarios/{scenario_id}/run-cloud")
def run_in_cloud(scenario_id: str, depth: str | None = None) -> dict:
    _scenario_or_404(scenario_id)
    command = ["gh", "workflow", "run", "scrape.yml", "-f", f"scenario={scenario_id}"]
    if depth:
        command += ["-f", f"depth={depth}"]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise HTTPException(500, "The gh CLI is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(500, f"gh failed: {exc.stderr.strip()}") from exc
    return {"dispatched": True, "scenario_id": scenario_id}


@app.get("/api/sweeps/{scenario_id}")
def list_sweeps(scenario_id: str) -> dict:
    directories = _sweep_dirs(scenario_id)
    return {
        "scenario_id": scenario_id,
        "running": scenario_id in _running and _running[scenario_id].is_alive(),
        "error": _failures.get(scenario_id),
        # The countdown used to recompute this from its own copies of the pace
        # constants. They were halved and re-measured without the UI following,
        # leaving "minutes left" reading roughly half the real wait.
        "seconds_per_search": SECONDS_PER_SEARCH,
        "workers": DEFAULT_WORKERS,
        "sweeps": [{"stamp": d.name, **_read_status(d)} for d in directories[:30]],
    }


# ------------------------------------------------------------------- results

# One sweep's combination, keyed by directory and the mtime of its legs file.
# The Prices tab used to trigger a full re-combine of every sweep ever
# committed, on every render.
_combined_cache: dict[tuple[str, int], object] = {}


def _combination(scenario: Scenario, directory: Path):
    legs_file = directory / "legs.jsonl"
    try:
        key = (str(directory), legs_file.stat().st_mtime_ns)
    except OSError:
        key = (str(directory), 0)
    cached = _combined_cache.get(key)
    if cached is None:
        cached = combine_all(load_legs(directory), scenario, limit=None)
        _combined_cache[key] = cached
    return cached


def _sweep_dir_or_404(scenario_id: str, stamp: str) -> tuple[Scenario, Path]:
    scenario = _scenario_or_404(scenario_id)
    directory = DATA_DIR / "sweeps" / scenario_id / _safe_stamp(stamp)
    if not directory.exists():
        raise HTTPException(404, f"No sweep {stamp!r} for {scenario_id!r}")
    return scenario, directory


@app.get("/api/sweeps/{scenario_id}/{stamp}/results")
def sweep_results(scenario_id: str, stamp: str, mode: str = "all", limit: int = 50) -> dict:
    scenario, directory = _sweep_dir_or_404(scenario_id, stamp)
    result = _combination(scenario, directory)
    bag = scenario.bag_estimate

    itineraries = result.top
    if mode == "same":
        itineraries = [i for i in itineraries if i.same_airport]
    elif mode == "open":
        itineraries = [i for i in itineraries if not i.same_airport]

    return {
        "scenario_id": scenario_id,
        "stamp": stamp,
        "legs_found": result.legs_in,
        "bag_estimate": bag,
        "currency": scenario.currency,
        "truncated": result.truncated,
        "best_same_airport": (
            result.best_same_airport.to_dict(bag) if result.best_same_airport else None
        ),
        "best_open_jaw": result.best_open_jaw.to_dict(bag) if result.best_open_jaw else None,
        "itineraries": [i.to_dict(bag) for i in itineraries[: min(max(limit, 1), 200)]],
    }


@app.get("/api/sweeps/{scenario_id}/{stamp}/by-date")
def sweep_by_date(scenario_id: str, stamp: str) -> list[dict]:
    scenario, directory = _sweep_dir_or_404(scenario_id, stamp)
    # Computed in the same traversal as the results, and never from a capped
    # list: capping first would drop expensive dates from the series and make
    # the chart imply they were never searched.
    return series_from_result(_combination(scenario, directory), scenario.bag_estimate)


@app.get("/api/history/{scenario_id}")
def history(scenario_id: str) -> list[dict]:
    """Best total per sweep over time — 'book now or wait'."""
    scenario = _scenario_or_404(scenario_id)
    series = []
    for directory in reversed(_sweep_dirs(scenario_id)):
        best = _combination(scenario, directory).best
        if best is None:
            continue
        series.append(
            {
                "swept_at": directory.name,
                "best_total": best.total_price,
                "best_total_with_bags": best.total_with_bags(scenario.bag_estimate),
                "currency": best.currency,
            }
        )
    return series


@app.get("/api/probe")
def probe_stats() -> dict:
    from ..probe import probe_report

    stats = probe_report(DATA_DIR / "probe")
    return {
        "routes": {
            name: asdict(route) | {"change_rate": route.change_rate}
            for name, route in stats.routes.items()
        },
        "recommendation": stats.recommendation,
    }


@app.get("/api/describe/{code}")
def describe_airport(code: str) -> dict:
    return {"code": code.upper(), "label": describe(code, data_dir=DATA_DIR)}


@app.exception_handler(ValueError)
def value_error_handler(request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# Mounted last: mounting "/" before the API routes would shadow all of them.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
