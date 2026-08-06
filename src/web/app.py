"""Local control panel for the flight scenario watcher.

The UI is a viewer and launcher, never a dependency for searching — the
scheduled GitHub Actions sweep runs whether or not this server is up, and
commits its results back to the repo. This app reads those committed files.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..combine import (
    best_open_jaw,
    best_same_airport,
    cheapest_by_departure_date,
    combine,
)
from ..scenario import Scenario, load_scenario, load_scenarios, save_scenario
from ..sweep.planner import estimate_minutes, plan_searches
from ..sweep.runner import load_legs, run_sweep
from .airports import catalogue

SCENARIO_DIR = Path(os.getenv("SCENARIO_DIR", "scenarios"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Flight scenario watcher")

# One local sweep at a time; the UI reflects this state in its status strip.
_running: dict[str, threading.Thread] = {}


class ScenarioPayload(BaseModel):
    id: str
    name: str
    trip_type: str
    origins: list[str]
    japan_airports: list[str]
    ph_airports: list[str] = []
    window_start: date
    window_end: date
    japan_stay_days: tuple[int, int] = (9, 11)
    ph_stay_days: tuple[int, int] = (9, 11)
    trip_length_days: tuple[int, int] = (18, 22)
    adults: int = 1
    depth: str = "standard"
    alert_threshold_czk: int | None = None
    enabled: bool = True
    notes: str = ""

    def to_scenario(self) -> Scenario:
        return Scenario(**self.model_dump())


def _scenario_or_404(scenario_id: str) -> Scenario:
    path = SCENARIO_DIR / f"{scenario_id}.json"
    if not path.exists():
        raise HTTPException(404, f"No scenario named {scenario_id!r}")
    return load_scenario(path)


def _sweep_dirs(scenario_id: str) -> list[Path]:
    root = DATA_DIR / "sweeps" / scenario_id
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


@app.get("/api/airports")
def get_airports() -> dict:
    return catalogue()


@app.get("/api/scenarios")
def list_scenarios() -> list[dict]:
    return [s.to_dict() for s in load_scenarios(SCENARIO_DIR)]


@app.get("/api/scenarios/{scenario_id}")
def get_scenario(scenario_id: str) -> dict:
    return _scenario_or_404(scenario_id).to_dict()


@app.put("/api/scenarios/{scenario_id}")
@app.post("/api/scenarios")
def upsert_scenario(payload: ScenarioPayload, scenario_id: str | None = None) -> dict:
    scenario = payload.to_scenario()
    try:
        scenario.validate()
    except ValueError as exc:
        # Surfaced verbatim in the UI, so the message must read well.
        raise HTTPException(400, str(exc)) from exc
    save_scenario(scenario, SCENARIO_DIR)
    return scenario.to_dict()


@app.post("/api/scenarios/{scenario_id}/estimate")
def estimate(scenario_id: str, depth: str | None = None) -> dict:
    scenario = _scenario_or_404(scenario_id)
    if depth:
        scenario = replace(scenario, depth=depth)
    searches = plan_searches(scenario)
    return {
        "searches": len(searches),
        "minutes": estimate_minutes(searches),
        "depth": scenario.depth,
        "per_leg": {
            str(i): sum(1 for s in searches if s.leg_index == i)
            for i in sorted({s.leg_index for s in searches})
        },
    }


@app.post("/api/scenarios/{scenario_id}/run")
def run_locally(scenario_id: str, depth: str | None = None) -> dict:
    scenario = _scenario_or_404(scenario_id)
    if scenario_id in _running and _running[scenario_id].is_alive():
        raise HTTPException(409, "A sweep is already running for this scenario")

    def work():
        run_sweep(scenario, data_dir=DATA_DIR, depth=depth)

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
        "sweeps": [
            {"stamp": d.name, **_read_status(d)} for d in directories[:30]
        ],
    }


def _load_results(scenario_id: str, stamp: str):
    scenario = _scenario_or_404(scenario_id)
    directory = DATA_DIR / "sweeps" / scenario_id / stamp
    if not directory.exists():
        raise HTTPException(404, f"No sweep {stamp!r} for {scenario_id!r}")
    return scenario, load_legs(directory)


@app.get("/api/sweeps/{scenario_id}/{stamp}/results")
def sweep_results(scenario_id: str, stamp: str, mode: str = "all") -> dict:
    scenario, legs = _load_results(scenario_id, stamp)
    itineraries = combine(legs, scenario)
    same, jaw = best_same_airport(itineraries), best_open_jaw(itineraries)

    if mode == "same":
        itineraries = [i for i in itineraries if i.same_airport]
    elif mode == "open":
        itineraries = [i for i in itineraries if not i.same_airport]

    return {
        "scenario_id": scenario_id,
        "stamp": stamp,
        "legs_found": len(legs),
        "best_same_airport": same.to_dict() if same else None,
        "best_open_jaw": jaw.to_dict() if jaw else None,
        "itineraries": [i.to_dict() for i in itineraries],
    }


@app.get("/api/sweeps/{scenario_id}/{stamp}/by-date")
def sweep_by_date(scenario_id: str, stamp: str) -> list[dict]:
    scenario, legs = _load_results(scenario_id, stamp)
    # Uncapped: capping first would drop expensive dates from the series and
    # make the chart imply they were never searched.
    return cheapest_by_departure_date(combine(legs, scenario, limit=None))


@app.get("/api/history/{scenario_id}")
def history(scenario_id: str) -> list[dict]:
    """Best total per sweep over time — 'book now or wait'."""
    scenario = _scenario_or_404(scenario_id)
    series = []
    for directory in reversed(_sweep_dirs(scenario_id)):
        legs = load_legs(directory)
        if not legs:
            continue
        itineraries = combine(legs, scenario, limit=1)
        if not itineraries:
            continue
        series.append({
            "swept_at": directory.name,
            "best_total": itineraries[0].total_price,
            "currency": itineraries[0].currency,
        })
    return series


@app.get("/api/probe")
def probe_stats() -> dict:
    from ..probe import probe_report

    stats = probe_report(DATA_DIR / "probe")
    return {
        "routes": {name: asdict(route) | {"change_rate": route.change_rate}
                   for name, route in stats.routes.items()},
        "recommendation": stats.recommendation,
    }


@app.exception_handler(ValueError)
def value_error_handler(request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# Mounted last: mounting "/" before the API routes would shadow all of them.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
