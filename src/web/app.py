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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..airports import describe, frequent_airports, lookup
from ..airports import search_with_meta as search_airports
from ..combine import combine_all, series_from_result
from ..notify_discord import COLOR_INFO, post
from ..scenario import Scenario, load_scenario, read_scenarios, save_scenario
from ..sources import DEFAULTS as DEFAULT_SOURCES
from ..sources import Source, load_source, load_sources, save_sources
from ..sweep.explore import explore_report
from ..sweep.planner import (
    SECONDS_PER_SEARCH,
    estimate_minutes,
    plan_exploration,
    plan_searches,
    planned_routes,
)
from ..sweep.runner import (
    DEFAULT_WORKERS,
    MODES,
    is_comparable,
    legs_per_search_of,
    load_legs,
    run_sweep,
)
from ..viability import report as viability_report
from ..webhook_store import clear_webhook, load_webhook, save_webhook
from ..webhook_store import mask as mask_webhook

SCENARIO_DIR = Path(os.getenv("SCENARIO_DIR", "scenarios"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
# Outside DATA_DIR on purpose: the scheduled workflow commits that directory.
SECRETS_DIR = Path(os.getenv("SECRETS_DIR", ".secrets"))
STATIC_DIR = Path(__file__).parent / "static"

# Both are interpolated straight into filesystem paths, so neither may contain
# a separator or a parent reference.
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")

# Bumped whenever this file and `static/app.js` must be deployed together: a new
# endpoint the page relies on, or a changed response shape.
#
# It exists because static files are read from disk on every request while the
# Python is frozen at import time, so a `uvicorn` left running from an older
# commit serves the *newest* page against an old API. The page then gets 404s
# and 400s for things it needs, and renders them as emptiness - which is
# indistinguishable from "you have no saved trips". `static/app.js` carries the
# same number and refuses to render until they match.
API_CONTRACT = 5

app = FastAPI(title="Flight scenario watcher")

# One local sweep at a time; the UI reflects this state in its status strip.
_running: dict[str, threading.Thread] = {}
# Set to ask the sweep in `_running` to stop. Kept beside it rather than inside
# the runner because the thread that asks is never the thread that runs.
_stops: dict[str, threading.Event] = {}
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


def _read_json(path: Path):
    """Parsed JSON, or None when absent or unreadable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_status(directory: Path) -> dict:
    path = directory / "status.json"
    if not path.exists():
        return {"state": "unknown"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"state": "unreadable"}


# -------------------------------------------------------------------- version


@app.get("/api/version")
def version() -> dict:
    """What generation of the API this process is.

    The page asks this before anything else, so it deliberately reads no file
    and loads no scenario: the whole point is to still answer when the things
    it would have read are the broken ones.
    """
    return {"contract": API_CONTRACT}


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
def list_scenarios_endpoint() -> dict:
    """The trips that load, and what went wrong with any that did not.

    Deliberately never an error: one file with a typo used to 400 the whole
    listing, and the page drew that as an empty trip picker - the same thing it
    draws when nothing is saved at all.
    """
    scenarios, problems = read_scenarios(SCENARIO_DIR)
    return {"trips": [s.to_dict() for s in scenarios], "problems": problems}


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


def _checked_mode(mode: str | None) -> str:
    if mode is None:
        return "sweep"
    if mode not in MODES:
        raise HTTPException(400, f"mode must be one of {', '.join(MODES)}, got {mode!r}")
    return mode


@app.post("/api/scenarios/{scenario_id}/estimate")
def estimate(
    scenario_id: str,
    depth: str | None = None,
    mode: str | None = None,
    trip: dict | None = Body(default=None),
) -> dict:
    """What a run of this trip would cost, in searches and minutes.

    `trip` prices an edited trip that has not been saved, without saving it.
    The badge sits beside the run buttons, so reading the cost off the file
    while the screen showed something else made the number quietly wrong.
    """
    saved = _scenario_or_404(scenario_id)
    mode = _checked_mode(mode)
    if trip:
        try:
            scenario = Scenario.from_dict(trip)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, f"That trip cannot be read: {exc}") from exc
    else:
        scenario = saved
    if depth:
        scenario = replace(scenario, depth=depth)
    try:
        scenario.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    searches = plan_exploration(scenario) if mode == "explore" else plan_searches(scenario)
    return {
        "searches": len(searches),
        "minutes": estimate_minutes(searches),
        "mode": mode,
        "depth": scenario.depth,
        "leg_count": scenario.leg_count,
        "per_leg": {
            str(i): sum(1 for s in searches if s.leg_index == i)
            for i in sorted({s.leg_index for s in searches})
        },
        "leg_labels": _leg_labels(scenario),
    }


def _focus_of(scenario: Scenario) -> list | None:
    """A trip's focus as the two strings a sweep records, or None."""
    if scenario.focus_start and scenario.focus_end:
        return [scenario.focus_start.isoformat(), scenario.focus_end.isoformat()]
    return None


def _leg_labels(scenario: Scenario) -> list[str]:
    """"PRG/VIE → NRT/HND" per leg, for a UI that no longer knows the countries."""
    pools = scenario.airport_pools
    return [
        f"{'/'.join(pools[i])} → {'/'.join(pools[i + 1])}" for i in range(len(pools) - 1)
    ]


# ----------------------------------------------------------------- running


def _is_running(scenario_id: str) -> bool:
    thread = _running.get(scenario_id)
    return thread is not None and thread.is_alive()


@app.post("/api/scenarios/{scenario_id}/run")
def run_locally(scenario_id: str, depth: str | None = None, mode: str | None = None) -> dict:
    scenario = _scenario_or_404(scenario_id)
    mode = _checked_mode(mode)
    if _is_running(scenario_id):
        raise HTTPException(409, "A sweep is already running for this scenario")

    if depth:
        try:
            replace(scenario, depth=depth).validate()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    _failures.pop(scenario_id, None)
    stop = threading.Event()

    def work():
        try:
            run_sweep(scenario, data_dir=DATA_DIR, depth=depth, mode=mode, stop=stop)
        except Exception as exc:  # noqa: BLE001 - recorded and surfaced, not swallowed
            _failures[scenario_id] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    _running[scenario_id] = thread
    _stops[scenario_id] = stop
    return {
        "started": True,
        "scenario_id": scenario_id,
        "mode": mode,
        "depth": depth or scenario.depth,
    }


@app.post("/api/scenarios/{scenario_id}/stop")
def stop_run(scenario_id: str) -> dict:
    """Ask the running sweep to stop after the searches already in flight.

    Not instant, and deliberately not pretending to be: a search can sit on the
    site's 120s timeout, and killing it mid-page would lose the offers it is
    part-way through reading. Everything found so far is already on disk.
    """
    _scenario_or_404(scenario_id)
    if not _is_running(scenario_id):
        raise HTTPException(409, "No sweep is running for this scenario")
    _stops[scenario_id].set()
    return {"stopping": True, "scenario_id": scenario_id}


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


def _searched_another_trip(directory: Path, live: Scenario) -> bool:
    """Whether this run swept a different set of airports than the trip now.

    In the listing because the picker is where a run gets chosen, and a run of
    the wrong trip has to be recognisable before it is opened and believed.
    """
    return _sweep_scenario(directory, live).airport_pools != live.airport_pools


@app.get("/api/sweeps/{scenario_id}")
def list_sweeps(scenario_id: str) -> dict:
    directories = _sweep_dirs(scenario_id)
    stop = _stops.get(scenario_id)
    live = _scenario_or_404(scenario_id)
    return {
        "scenario_id": scenario_id,
        "running": _is_running(scenario_id),
        # A stop that has been asked for but not yet reached. The strip says
        # "stopping" for as long as this is true, because the alternative is a
        # button that looks broken for two minutes.
        "stopping": bool(stop is not None and stop.is_set() and _is_running(scenario_id)),
        "error": _failures.get(scenario_id),
        # The countdown used to recompute this from its own copies of the pace
        # constants. They were halved and re-measured without the UI following,
        # leaving "minutes left" reading roughly half the real wait.
        "seconds_per_search": SECONDS_PER_SEARCH,
        "workers": DEFAULT_WORKERS,
        "sweeps": [
            {
                "stamp": d.name,
                "has_legs": _has_legs(d),
                "searched_another_trip": _searched_another_trip(d, live),
                **_read_status(d),
            }
            for d in directories[:30]
        ],
    }


def _has_legs(directory: Path) -> bool:
    """Whether this sweep's legs are still on disk to be read.

    `legs_found` in status.json says what a run found, which is not the same
    thing. The 11 Aug local sweep reports 1,167 flights and has no legs.jsonl
    at all: it predated incremental writing, so they lived only in the process
    that was killed. Anything picking a sweep to work from has to know the
    difference, or it offers the richest-looking run and reads nothing.

    A stat, not a read - this is called for every sweep on every status poll.
    """
    try:
        return (directory / "legs.jsonl").stat().st_size > 0
    except OSError:
        return False


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


def _sweep_scenario(directory: Path, live: Scenario) -> Scenario:
    """The trip this sweep searched, read with today's preferences.

    Shape - airports, stops, window, stays - comes from the snapshot the run
    wrote, because a sweep is a record of searching one particular trip. Read
    against a trip that has since been edited it silently stops making sense:
    the Explore tab lists airports the run never asked about, and the
    itineraries vanish because they no longer chain through the pools.

    Bag estimate, preferred origins and the alert threshold are taken from the
    live trip instead. Those are how a result is *read*, not what was searched,
    and they have to stay adjustable on runs already on disk.
    """
    data = _read_json(directory / "scenario.json")
    if data is None:
        return live  # sweeps from before the snapshot existed
    try:
        searched = Scenario.from_dict(data)
    except (ValueError, KeyError, TypeError):
        return live
    return replace(
        searched,
        bag_estimate=live.bag_estimate,
        preferred_origins=live.preferred_origins,
        alert_threshold=live.alert_threshold,
    )


def _sweep_dir_or_404(scenario_id: str, stamp: str) -> tuple[Scenario, Path]:
    """The sweep's own trip and its directory. Callers wanting the trip as it
    stands now - to compare against, or to edit - must load that separately."""
    live = _scenario_or_404(scenario_id)
    directory = DATA_DIR / "sweeps" / scenario_id / _safe_stamp(stamp)
    if not directory.exists():
        raise HTTPException(404, f"No sweep {stamp!r} for {scenario_id!r}")
    return _sweep_scenario(directory, live), directory


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
        # How much of the plan this price was found by looking at. A sweep with
        # holes in its date grid reports a cheapest total in the same words as a
        # complete one, and the difference is whether a cheaper trip was ever
        # looked at. None on sweeps committed before the figure was recorded,
        # which must not read as "complete".
        "coverage": _read_status(directory).get("coverage"),
        "focus": _read_status(directory).get("focus"),
        # Absent until `python -m src.cli verify` has been run for this sweep.
        # None means "not checked", which must not read as "checked and fine".
        "verification": _read_json(directory / "verify.json"),
        "currency": scenario.currency,
        "truncated": result.truncated,
        "best_same_airport": (
            result.best_same_airport.to_dict(bag) if result.best_same_airport else None
        ),
        "best_open_jaw": result.best_open_jaw.to_dict(bag) if result.best_open_jaw else None,
        "itineraries": [i.to_dict(bag) for i in itineraries[: min(max(limit, 1), 200)]],
    }


@app.get("/api/sweeps/{scenario_id}/{stamp}/explore")
def sweep_explore(scenario_id: str, stamp: str) -> dict:
    """Which airports of this trip are worth pricing properly.

    Served for any sweep, not only an exploration one - a stopped deep sweep is
    a perfectly good source of the same judgement, and refusing to summarise it
    would throw away the hour it spent.
    """
    scenario, directory = _sweep_dir_or_404(scenario_id, stamp)
    status = _read_status(directory)
    return {
        "stamp": stamp,
        "mode": status.get("mode", "sweep"),
        "state": status.get("state"),
        # The trip as it stands now goes in only to be compared against, never
        # to filter: the tab has to be able to say "this run never priced KTW".
        **explore_report(
            load_legs(directory), scenario, status, current=_scenario_or_404(scenario_id)
        ),
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
    """Best total per sweep over time — 'book now or wait'.

    Every point carries the quality of the sweep behind it. Without that the
    chart drew one line through sweeps running 2.9 to 9.7 legs per search, so
    its ups and downs tracked how well the scraper was working rather than what
    flights cost. `comparable` is the flag the UI dims on.
    """
    scenario = _scenario_or_404(scenario_id)
    # What the trip requires *now*, so a sweep taken under a narrower shape is
    # retired rather than plotted beside sweeps of the current one.
    required = planned_routes(scenario)
    # Same idea along the date axis. A focused sweep prices a handful of
    # departure dates, so its cheapest is the cheapest of those dates and not of
    # the trip; charting it beside a broad sweep draws a step no fare made.
    focus = _focus_of(scenario)
    series = []
    for directory in reversed(_sweep_dirs(scenario_id)):
        legs = load_legs(directory)
        best = _combination(scenario, directory).best
        if best is None:
            continue
        status = _read_status(directory)
        covered = len(required & {(leg.origin, leg.destination) for leg in legs})
        series.append(
            {
                "swept_at": directory.name,
                "best_total": best.total_price,
                "best_total_with_bags": best.total_with_bags(scenario.bag_estimate),
                "currency": best.currency,
                "depth": status.get("depth"),
                "searches": status.get("total"),
                "legs_per_search": legs_per_search_of(status),
                "routes_covered": covered,
                "routes_planned": len(required),
                "comparable": is_comparable(status, covered, len(required), focus),
                "focus": status.get("focus"),
                "coverage": status.get("coverage"),
            }
        )
    return series


@app.get("/api/probe")
def probe_stats() -> dict:
    from ..probe import probe_report

    stats = probe_report(DATA_DIR / "probe")
    # `asdict` serialises fields only, so every derived rate has to be named
    # here. Missing one is silent: the UI reads undefined, prints 0% and looks
    # like a measurement rather than an omission.
    return {
        "routes": {
            name: asdict(route)
            | {
                "change_rate": route.change_rate,
                "meaningful_change_rate": route.meaningful_change_rate,
            }
            for name, route in stats.routes.items()
        },
        "recommendation": stats.recommendation,
    }


@app.get("/api/describe/{code}")
def describe_airport(code: str) -> dict:
    return {"code": code.upper(), "label": describe(code, data_dir=DATA_DIR)}


# ------------------------------------------------------------- notify target
#
# A webhook URL is a bearer token: whoever holds it can post to the channel. So
# it goes out of the repo entirely (see src/webhook_store.py) and it never
# leaves this process whole — every response carries the masked form only.


@app.get("/api/notify")
def get_notify() -> dict:
    url, origin = load_webhook(SECRETS_DIR)
    return {
        "configured": url is not None,
        "origin": origin,
        "masked": mask_webhook(url),
        # Named so the page can say which file to look in, or that the
        # environment is overriding whatever is in it.
        "path": str(SECRETS_DIR / "discord.json"),
    }


@app.put("/api/notify")
def put_notify(payload: dict = Body(...)) -> dict:
    # `save_webhook` raises ValueError with the advice; the handler below turns
    # that into a 400 the page shows verbatim.
    save_webhook(str(payload.get("url", "")), SECRETS_DIR)
    return get_notify()


@app.delete("/api/notify")
def delete_notify() -> dict:
    removed = clear_webhook(SECRETS_DIR)
    return {**get_notify(), "removed": removed}


@app.post("/api/notify/test")
def test_notify() -> dict:
    """Post a real message, so "saved" and "works" are not the same claim."""
    url, origin = load_webhook(SECRETS_DIR)
    if url is None:
        raise HTTPException(400, "There is no webhook saved yet, so there is nowhere to send.")

    embed = {
        "title": "✅ Test message from the flight watcher",
        "description": (
            "This is the channel sweep results will land in. Each sweep posts the "
            "picks ticked under *What gets sent to Discord*."
        ),
        "color": COLOR_INFO,
        "timestamp": datetime.now(UTC).isoformat(),
        "footer": {"text": f"Flight scenario watcher — webhook from the {origin}"},
    }
    if post(url, [embed]):
        return {"sent": True, "message": "Sent. Check the channel."}
    return {
        "sent": False,
        "message": (
            "Discord refused the message. The usual cause is a webhook that was "
            "deleted or regenerated in the channel settings — copy the URL again. "
            "The exact error is in the server's console output."
        ),
    }


# ------------------------------------------------------------------- sources
#
# When a site renames a CSS class, the sweep goes quiet and the fix is a new
# string, not a code change. These endpoints let that string be typed, saved
# and — the part that matters — proven against a real page before 02:00.

# The parser reads all six. A half-filled map would parse nothing at all, and
# nothing at all looks exactly like a quiet market until someone checks.
REQUIRED_SELECTORS = ("card", "price", "date", "time", "baggage_icon", "baggage_label")

# A route with dependable inventory, so a zero result means the selectors are
# wrong rather than the route being empty. Deliberately near-term: a date the
# site will not sell is not a test of anything.
TEST_ROUTE = ("PRG", "NRT")


@app.get("/api/sources")
def get_sources() -> dict:
    return {name: source.to_dict() for name, source in load_sources(DATA_DIR).items()}


@app.put("/api/sources")
def put_sources(payload: dict = Body(...)) -> dict:
    unknown = set(payload) - set(DEFAULT_SOURCES)
    if unknown:
        raise HTTPException(
            400,
            f"unknown source(s): {', '.join(sorted(unknown))}. "
            f"Known sources are {', '.join(sorted(DEFAULT_SOURCES))}",
        )

    sources = {}
    for name, body in payload.items():
        selectors = body.get("selectors") or {}
        missing = [key for key in REQUIRED_SELECTORS if not str(selectors.get(key, "")).strip()]
        if missing:
            raise HTTPException(
                400, f"{name}: selector(s) {', '.join(missing)} must not be empty"
            )
        if not str(body.get("base_url", "")).strip():
            raise HTTPException(400, f"{name}: base_url must not be empty")
        sources[name] = Source(
            name=name,
            base_url=body["base_url"],
            url_template=body.get("url_template", DEFAULT_SOURCES[name].url_template),
            selectors={key: str(selectors[key]).strip() for key in REQUIRED_SELECTORS},
            no_results_marker=body.get("no_results_marker", ""),
            result_timeout_s=int(body.get("result_timeout_s", 120)),
            enabled=bool(body.get("enabled", True)),
        )

    save_sources(sources, DATA_DIR)
    return {name: source.to_dict() for name, source in sources.items()}


def _fetch_for_test(source, url: str) -> tuple[int, str, str]:
    """Render one search page and return its status, final URL and HTML.

    All three are needed to tell a wrong address from renamed markup, which is
    the one distinction this endpoint exists to make:

    - **Status** catches an honest 404.
    - **Final URL** catches a dishonest one. Measured live: pelikan.cz answers
      200 for a path that does not exist and quietly bounces to
      `https://www.pelikan.cz/cs`, dropping the search. Status clean, page real,
      no offer cards - identical to a renamed class unless you notice you were
      moved. A working search redirects too (it appends ",LOAD"), so what
      matters is whether the address still begins with the one asked for.

    Separated so a test can supply a saved page instead of launching Chromium
    and hitting a third-party site.
    """
    import time

    from ..sweep.runner import _browser_page

    class _Real:
        NAME = "PELIKAN"

    with _browser_page(_Real()) as page:
        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        status = response.status if response else 0
        if status >= 400:
            return status, page.url, page.content()
        for _ in range(0, source.result_timeout_s, 5):
            time.sleep(5)
            if page.locator(source.selectors["card"]).count():
                break
            if source.no_results_marker and source.no_results_marker in page.inner_text("body"):
                break
        time.sleep(2)
        return status, page.url, page.content()


@app.post("/api/sources/{name}/test")
def test_source(name: str) -> dict:
    """Run one real search and report exactly what these selectors parsed.

    The single question this answers is "is it the URL or the markup?", which
    otherwise costs an afternoon. It reports rather than raises: a zero count
    *is* the finding.
    """
    if name not in DEFAULT_SOURCES:
        raise HTTPException(404, f"No source named {name!r}")

    from ..providers.pelikan import parse_results_html
    from ..providers.pelikan_url import build_search_url

    source = load_source(name, DATA_DIR)
    origin, destination = TEST_ROUTE
    depart = date.today() + timedelta(days=45)
    url = build_search_url(origin, destination, depart, source=source)

    def outcome(message: str, cards: int = 0, legs: list | None = None) -> dict:
        legs = legs or []
        return {
            "ok": bool(legs),
            "url": url,
            "route": f"{origin}→{destination} {depart.isoformat()}",
            "cards_found": cards,
            "legs_parsed": len(legs),
            "sample": legs[0].to_dict() if legs else None,
            "message": message,
        }

    try:
        status, final_url, html = _fetch_for_test(source, url)
    except Exception as exc:  # noqa: BLE001 - the failure is the answer here
        return outcome(
            f"Could not load the page at all: {type(exc).__name__}: {exc}. "
            "That points at base_url or url_template rather than at the selectors."
        )

    # Both checked before the selectors: either failure parses to zero cards and
    # would otherwise be reported as renamed markup, sending you to rewrite a
    # selector that was never wrong.
    if status >= 400:
        return outcome(
            f"The site answered {status} for this address, so nothing was searched. "
            "Fix base_url or url_template; the selectors are untested either way."
        )
    if not final_url.startswith(source.base_url):
        return outcome(
            f"The site accepted the request but redirected to {final_url}, dropping the "
            "search. A path it does not recognise is answered with a 200 and a bounce to "
            "the homepage, so this is a wrong base_url or url_template rather than a "
            "selector problem; the selectors are untested either way."
        )

    cards = len(BeautifulSoup(html, "lxml").select(source.selectors["card"]))
    legs = parse_results_html(html, origin, destination, source=source)

    if legs:
        return outcome(f"{cards} card(s) matched and {len(legs)} parsed cleanly.", cards, legs)
    if cards:
        return outcome(
            f"{cards} card(s) matched but none produced a price, so the `price` selector "
            "is the likely culprit.",
            cards,
        )
    # A route the site really has no inventory for renders its own message. That
    # is data, not breakage, and must not be reported as a broken selector.
    if source.no_results_marker and source.no_results_marker in html:
        return outcome(
            "The page loaded and the site says it has no flights on this route. The URL "
            "grammar is therefore fine; the selectors stay unproven until a route with "
            "inventory is tried."
        )
    return outcome(
        "The page loaded with a normal status but the `card` selector matched nothing, "
        "and the site did not say it had no flights either. That is the signature of "
        "renamed markup — open the URL below and read the class names off a real offer."
    )


@app.exception_handler(ValueError)
def value_error_handler(request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# Mounted last: mounting "/" before the API routes would shadow all of them.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
