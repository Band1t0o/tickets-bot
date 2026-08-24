"""Local control panel for the flight scenario watcher.

The UI is a viewer and launcher, never a dependency for searching — the
scheduled GitHub Actions sweep runs whether or not this server is up, and
commits its results back to the repo. This app reads those committed files.
"""
from __future__ import annotations

import json
import os
import re
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
from ..providers.pelikan_url import build_search_url
from ..scenario import LegWatch, Scenario, Watch, load_scenario, read_scenarios, save_scenario
from ..sources import DEFAULTS as DEFAULT_SOURCES
from ..sources import (
    Source,
    load_checks,
    load_source,
    load_sources,
    save_check,
    save_sources,
)
from ..sweep.explore import explore_report
from ..sweep.planner import (
    PLANS,
    SEARCHES_PER_RUNNER,
    SECONDS_PER_SEARCH,
    estimate_minutes,
    plan_searches,
    plan_watch,
    planned_routes,
    shards_for,
)
from ..sweep.runner import (
    DEFAULT_WORKERS,
    MODES,
    answered_searches,
    is_comparable,
    legs_per_search_of,
    load_legs,
    narrowing_of,
    run_sweep,
)
from ..viability import report as viability_report
from ..webhook_store import clear_webhook, load_webhook, save_webhook
from ..webhook_store import mask as mask_webhook
from . import branch_sync, cloud_runs

SCENARIO_DIR = Path(os.getenv("SCENARIO_DIR", "scenarios"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
# Outside DATA_DIR on purpose: the scheduled workflow commits that directory.
SECRETS_DIR = Path(os.getenv("SECRETS_DIR", ".secrets"))
STATIC_DIR = Path(__file__).parent / "static"

# Both are interpolated straight into filesystem paths, so neither may contain
# a separator or a parent reference.
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")
SAFE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Searches a trip's whole watch plan may reach before another day is refused.
#
# The binding limit is the site, not taste: pelikan.cz answers about 120
# searches from one runner and then stops answering at all, with no slowdown
# leading up to it. The watch runs on a single runner by design, so a plan over
# that cliff does not fail - it silently prices half of what it planned and
# reports the cheapest of the half.
#
# Checked here rather than in `Scenario.validate` because the count depends on
# the planner, and `scenario.py` cannot import it without a cycle. `MAX_WATCHES`
# is the cheap guard that needs no planner; this is the one that actually binds.
WATCH_SEARCH_CAP = 110

# The branch the scheduled sweep runs from, and commits its results to. Defined
# in `branch_sync` because that module both reads it and fast-forwards onto it;
# two copies of a ref name is exactly the drift this app keeps being bitten by.
CLOUD_REF = branch_sync.CLOUD_REF

# The workflow is the authority on its own schedule, so the panel reads it there
# instead of restating it. `parents[2]` is the repo root: this file is
# `src/web/app.py`.
SWEEP_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "scrape.yml"
DAILY_CRON = re.compile(r"cron:\s*'(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*'")
# The afternoon slots, which re-price what the trip has been narrowed to rather
# than sweeping the window again. Matched as the literals the workflow itself
# compares `github.event.schedule` against, so the panel and the run agree about
# which slot does which - the whole reason the crons are read out of the file.
FINAL_CRONS = ("0 13 * * *", "0 20 * * *")
# The depth a *scheduled* run uses, which is a workflow default rather than a
# trip setting: the plan step reads `${INPUT_DEPTH:-deep}`, and a schedule
# supplies no input. Read from the workflow for the same reason as the crons -
# a trip saved as `quick` is still swept deep every night, and a panel restating
# the file's depth would report a plan seven times smaller than the real one.
FORCED_DEPTH = re.compile(r"INPUT_DEPTH:-(\w+)")

# Bumped whenever this file and `static/app.js` must be deployed together: a new
# endpoint the page relies on, or a changed response shape.
#
# It exists because static files are read from disk on every request while the
# Python is frozen at import time, so a `uvicorn` left running from an older
# commit serves the *newest* page against an old API. The page then gets 404s
# and 400s for things it needs, and renders them as emptiness - which is
# indistinguishable from "you have no saved trips". `static/app.js` carries the
# same number and refuses to render until they match.
API_CONTRACT = 14

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


# ----------------------------------------------------------------- night sweep
#
# The scheduled cloud sweep is the only place a sweep of the real trip has ever
# finished whole: 483/483 in 19 minutes on 21 Aug, against three throttled local
# runs the same morning. None of it was visible from this page - not which trips
# it would run, not how many runners they would be split across, not when it
# fires, and not the one that actually bit: **it sweeps the trip on the branch,
# not the trip on this screen.**


# One git boundary for the whole app, in `branch_sync`. It was written twice -
# once here to compare trips, once there to bring results across - and a second
# copy of "never raises, cannot-say is an answer" is a second copy to get wrong.
_git = branch_sync.git


def _cloud_scenario(scenario_id: str) -> Scenario | None:
    """This trip as the branch the nightly sweep runs from has it.

    Read from the last fetched `origin/main` rather than over the network, so
    opening the page never blocks on a remote. That makes the answer as old as
    the last fetch, which is why `cloud_seen_at` is reported beside it.
    """
    path = (SCENARIO_DIR / f"{scenario_id}.json").as_posix()
    raw = _git("show", f"{CLOUD_REF}:{path}")
    if raw is None:
        return None
    try:
        return Scenario.from_dict(json.loads(raw))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _at_depth(scenario: Scenario, depth: str) -> Scenario:
    return replace(scenario, depth=depth) if depth else scenario


def _night_depth() -> str:
    """The depth a scheduled sweep forces, or "" if it uses each trip's own."""
    try:
        found = FORCED_DEPTH.findall(SWEEP_WORKFLOW.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return found[0] if found else ""


def _cloud_state(live: Scenario, forced_depth: str) -> dict:
    """How the nightly sweep's copy of this trip differs from this one.

    Named differences rather than a diff, because the question being asked is
    "will tonight's results be about the trip I am looking at" and the answer
    has to be readable without opening git.

    Depth counts only when the workflow is not forcing one. While it is, a trip
    saved `quick` here and `deep` on the branch is swept identically by the
    night sweep, and reporting that as a difference would be a false alarm on
    the one panel whose job is to be trusted.
    """
    cloud = _cloud_scenario(live.id)
    if cloud is None:
        return {"known": False, "differs": [], "included": None, "searches": None}

    differs = []
    if cloud.enabled != live.enabled:
        differs.append("whether it runs at all")
    if cloud.leg_pools != live.leg_pools:
        differs.append("the airports it searches")
    if (cloud.window_start, cloud.window_end) != (live.window_start, live.window_end):
        differs.append("the date window")
    if [s.stay_days for s in cloud.stops] != [s.stay_days for s in live.stops]:
        differs.append("how long you stay")
    if not forced_depth and cloud.depth != live.depth:
        differs.append("how finely it prices")
    return {
        "known": True,
        "differs": differs,
        "included": cloud.enabled,
        # What the cloud will really spend the night doing. On 21 Aug that was
        # 483 searches of a three-origin trip while this screen showed 66.
        "searches": len(plan_searches(_at_depth(cloud, forced_depth))),
    }


def _night_schedule(now: datetime | None = None) -> list[dict]:
    """When the sweep workflow fires next, read out of the workflow itself.

    Two copies of a cron drift, and the one on screen is the one nobody can
    check against Actions. Only plain daily crons are understood; anything else
    is reported as its raw expression rather than as a wrong time.
    """
    now = now or datetime.now(UTC)
    try:
        text = SWEEP_WORKFLOW.read_text(encoding="utf-8")
    except OSError:
        return []
    slots = []
    for minute, hour in DAILY_CRON.findall(text):
        cron = f"{int(minute)} {int(hour)} * * *"
        at = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        slots.append({
            "cron": cron,
            # UTC, formatted into Prague by the page like every other moment it
            # shows. A time-of-day string would have to pick a zone here.
            "next": at.isoformat(),
            # Which of the two questions this slot answers. `sweep` prices the
            # whole window; `final` prices only the narrowing, so it is skipped
            # entirely for a trip that has not been narrowed to anything.
            "mode": "final" if cron in FINAL_CRONS else "sweep",
        })
    return sorted(slots, key=lambda slot: slot["next"])


@app.get("/api/night-sweep")
def night_sweep() -> dict:
    """What the scheduled cloud sweep will do tonight, and to which trip."""
    scenarios, _ = read_scenarios(SCENARIO_DIR)
    forced = _night_depth()
    trips = []
    for scenario in scenarios:
        at_depth = _at_depth(scenario, forced)
        searches = plan_searches(at_depth)
        # What the two afternoon slots will run, and nothing when the trip has
        # not been narrowed - which is a real answer rather than a zero: those
        # slots skip such a trip outright, and a panel quoting a cost for a run
        # that will not happen is the kind of number this app keeps removing.
        try:
            final = PLANS["final"](at_depth)
        except ValueError:
            final = None
        trips.append({
            "id": scenario.id,
            "name": scenario.name,
            "included": scenario.enabled,
            # What the night sweep will use, and what the trip is saved as.
            # They are usually different, and only the first one sizes the plan.
            "depth": forced or scenario.depth,
            "saved_depth": scenario.depth,
            "searches": len(searches),
            "runners": shards_for(len(searches)),
            "minutes": estimate_minutes(searches),
            "final_searches": len(final) if final is not None else None,
            "final_minutes": estimate_minutes(final) if final is not None else None,
            "cloud": _cloud_state(scenario, forced),
        })
    return {
        "schedule": _night_schedule(),
        "forced_depth": forced,
        "trips": trips,
        "cloud_ref": CLOUD_REF,
        "cloud_seen_at": (_git("log", "-1", "--format=%cI", CLOUD_REF) or "").strip(),
        "searches_per_runner": SEARCHES_PER_RUNNER,
    }


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
    searches = PLANS[mode](scenario)
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


def _narrowing_wanted(scenario: Scenario) -> dict:
    """A trip's three narrowing constraints, shaped like a status's record of them.

    The live half of the comparison `is_comparable` makes: what the trip asks for
    now, against what each run on disk actually searched under.
    """
    return {
        "focus": _focus_of(scenario),
        "return_focus": (
            [scenario.return_focus_start.isoformat(), scenario.return_focus_end.isoformat()]
            if scenario.return_focus_start and scenario.return_focus_end
            else None
        ),
        "total_days": list(scenario.total_days) if scenario.total_days else None,
    }


def _leg_labels(scenario: Scenario) -> list[str]:
    """"PRG/VIE → NRT/HND" per leg, for a UI that no longer knows the countries.

    From `leg_pools`, so a pinned crossing reads as the single airport it is
    going to be flown through rather than as every airport the stop still has.
    """
    return [
        f"{'/'.join(origins)} → {'/'.join(destinations)}"
        for origins, destinations in scenario.leg_pools
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

    # Planned here rather than left to the thread. `plan_final` refuses a trip
    # with nothing narrowed, and a refusal raised inside the worker lands in
    # `_failures` minutes later, reaching the strip as "Sweep failed —
    # ValueError: …" in the voice of a crash. It is not a crash; it is a button
    # that should not have been pressable.
    try:
        PLANS[mode](replace(scenario, depth=depth) if depth else scenario)
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


@app.post("/api/scenarios/{scenario_id}/resume")
def resume_run(scenario_id: str, stamp: str) -> dict:
    """Carry on a run that ended short, asking only for what it never answered.

    The site answers about 120 searches from one client, so the 80 answers a
    refused probe already has are worth more than the run that produced them:
    re-asking them to reach the remaining 46 spends the budget that ran out.

    Nothing here waits for the throttle to clear. How long that takes has never
    been measured, and a cooldown invented to look careful would refuse runs
    that would have worked. The page says when the site last refused and leaves
    the decision where it belongs.
    """
    scenario = _scenario_or_404(scenario_id)
    if _is_running(scenario_id):
        raise HTTPException(409, "A sweep is already running for this scenario")
    _, directory = _sweep_dir_or_404(scenario_id, stamp)

    # The resumed directory inherits that run's flights and is stamped with the
    # trip as it stands now. If those disagree it would claim to have searched
    # airports it never asked about - the reading that had two probes reporting
    # a trip nobody swept.
    # Airports only, though `_differs_from_live` now names three things. A
    # resumed run re-asks searches its own plan never got an answer for, so
    # different stays or a different window make it a run of an older plan -
    # which is what a sweep on disk always is. Different airports make it
    # incoherent: the report would name routes nothing in the file ever asked
    # about.
    if "airports" in _differs_from_live(directory, scenario):
        raise HTTPException(
            400,
            "That run searched a different set of airports than this trip has now, so "
            "carrying it on would mix answers for two trips into one set of results. "
            "Start a fresh run, or put the airports back as they were.",
        )

    left = _left_to_ask(directory)
    if not left:
        raise HTTPException(
            400,
            "That run answered everything it planned, so there is nothing left to ask for. "
            "Start a new run to price the trip again.",
        )

    _failures.pop(scenario_id, None)
    stop = threading.Event()
    mode = _read_status(directory).get("mode", "sweep")

    def work():
        try:
            run_sweep(
                scenario, data_dir=DATA_DIR, mode=mode, stop=stop, resume_from=directory
            )
        except Exception as exc:  # noqa: BLE001 - recorded and surfaced, not swallowed
            _failures[scenario_id] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    _running[scenario_id] = thread
    _stops[scenario_id] = stop
    return {"started": True, "scenario_id": scenario_id, "mode": mode, "searches": left}


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


def _fetch_cloud_ref() -> None:
    """Bring `origin/main` up to date before judging the trip against it.

    `_git` is deliberately non-network so rendering the page never blocks on a
    remote, which means the branch it reads is as old as the last fetch. That is
    fine for a panel and useless for a gate: this checkout was fifteen commits
    behind when the gate was written, and a refusal - or worse, a pass - decided
    against a fortnight-old branch is not an answer. Best effort; a failure here
    leaves `_cloud_state` reporting what it can, which is handled below.
    """
    branch_sync.fetch()


@app.post("/api/scenarios/{scenario_id}/run-cloud")
def run_in_cloud(
    scenario_id: str, depth: str | None = None, force: bool = False,
    mode: str = "sweep",
) -> dict:
    """Sweep this trip in the cloud, or hold it until the lane is free.

    Two things had to change here. It used to fire and forget - `gh workflow
    run`, `{"dispatched": true}`, and no idea afterwards whether anything ran -
    and it dispatched whatever the branch happened to hold, which on 22 Aug was
    a different trip than the one on screen.
    """
    scenario = _scenario_or_404(scenario_id)
    mode = _checked_mode(mode)
    if mode == "final":
        # Refused here rather than in the runner twenty minutes from now, on a
        # machine nobody is watching. `plan_final` raises the sentence to show.
        try:
            PLANS["final"](scenario)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    if not force:
        _fetch_cloud_ref()
        cloud = _cloud_state(scenario, _night_depth())
        if not cloud["known"]:
            raise HTTPException(
                400,
                "This app cannot read what is on the branch the cloud sweeps, so it "
                "cannot tell you whether a cloud run would be about this trip. Check "
                "the trip is committed and pushed, or run it anyway.",
            )
        if cloud["differs"]:
            raise HTTPException(
                400,
                "The cloud sweeps the trip committed to the branch, not the one on this "
                "screen, and the two differ in "
                + ", ".join(cloud["differs"])
                + ". Commit and push this trip first, or run it anyway and read the "
                "results as being about the branch's version.",
            )

    try:
        # Dispatching into a lane that is already busy leaves a run pending, and
        # the next dispatch cancels it. Hold it here instead and send it when the
        # lane clears - two runs were lost that way on 22 Aug, and nothing said
        # so.
        if cloud_runs.lane_is_busy():
            entry = cloud_runs.enqueue(scenario_id, depth, mode)
            return {"dispatched": False, "queued": True, "scenario_id": scenario_id, **entry}
        cloud_runs.dispatch(scenario_id, depth, mode)
    except cloud_runs.CloudError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"dispatched": True, "queued": False, "scenario_id": scenario_id, "mode": mode}


@app.get("/api/cloud-runs")
def cloud_run_listing(limit: int = 12) -> dict:
    """What the cloud is doing, has done, and is being held to do.

    `known: false` is a real answer here, not an error: without `gh` this app
    cannot see Actions at all, while the schedule carries on regardless. Drawing
    an empty list instead would read as "nothing has ever run", which is the
    kind of confident emptiness this whole panel exists to stop.
    """
    try:
        runs = cloud_runs.list_runs(limit)
    except cloud_runs.CloudError as exc:
        return {
            "known": False,
            "reason": str(exc),
            "runs": [],
            "queued": cloud_runs.queued(),
            "schedule": _night_schedule(),
        }
    return {
        "known": True,
        "reason": "",
        "runs": runs,
        "queued": cloud_runs.queued(),
        "schedule": _night_schedule(),
        "busy": any(run["live"] for run in runs),
    }


def _known_trips() -> list[str]:
    """Every trip this app knows about, for the branch to be asked about.

    Read from the scenario files rather than from the sweeps on disk: a trip
    whose runs are *all* still on the branch has no local directory at all, and
    listing from disk would leave it out of exactly the case this exists for.
    """
    scenarios, _ = read_scenarios(SCENARIO_DIR)
    return [scenario.id for scenario in scenarios]


@app.get("/api/cloud-sync")
def cloud_sync_state() -> dict:
    """Which cloud results are on the branch but not on this machine.

    The sweep commits to the branch and this app lists the working tree, and
    nothing joined the two: on 22 Aug a deep run of 64 searches and 638 flights
    finished, committed, and never appeared here, behind a picker that looked
    exactly like a picker with nothing to show. A count that can be wrong is
    worth having; an emptiness that cannot be questioned is not.

    Reads the last fetch and refreshes it on a background thread when it has
    gone stale, so drawing the page never waits on a remote - the rule `_git`
    has followed since it was written.
    """
    branch_sync.fetch_in_background()
    return branch_sync.state(DATA_DIR, _known_trips())


@app.post("/api/cloud-sync")
def cloud_sync_pull() -> dict:
    """Bring the branch's results onto this machine, by fast-forward only.

    409 rather than 500 when it refuses: a checkout with its own commits is not
    a broken app, it is a state only the person sitting here can resolve, and
    the reason is written to be shown to them verbatim.
    """
    result = branch_sync.pull(DATA_DIR, _known_trips())
    if not result["synced"] and result["reason"]:
        raise HTTPException(409, result["reason"])
    return result


@app.post("/api/cloud-sync/take")
def cloud_sync_take() -> dict:
    """Copy the missing run directories across without moving the branch.

    What `POST /api/cloud-sync` cannot do when the checkout has commits of its
    own. Refusing the merge is right; refusing the results with it never was,
    and the results are directories this machine does not have at all.

    409 on a refusal for the same reason as the sync: it is a state the person
    here resolves, not a fault, and git's sentence is the part worth reading.
    """
    result = branch_sync.take(DATA_DIR, _known_trips())
    if not result["took"] and result["reason"]:
        raise HTTPException(409, result["reason"])
    return result


@app.delete("/api/cloud-queue/{scenario_id}")
def drop_from_cloud_queue(scenario_id: str) -> dict:
    if not cloud_runs.drop(_safe_id(scenario_id)):
        raise HTTPException(404, f"{scenario_id!r} is not waiting to be dispatched")
    return {"dropped": scenario_id}


def _differs_from_live(directory: Path, live: Scenario) -> list[str]:
    """What this run searched that the trip no longer says, named.

    In the listing because the picker is where a run gets chosen, and a run of
    the wrong trip has to be recognisable before it is opened and believed.

    Airports used to be the whole of it, which left the two edits that drift
    fastest invisible. `japan-philippines` has twelve sweeps on disk spanning
    three stay settings and two windows, and the picker labelled them
    identically - so a run priced under 8-13 nights in Japan read exactly like
    a run of today's 10-13, and a table of trips the current trip would never
    search again read as a table of trips you could still book.

    Named rather than a boolean for the same reason the cloud queue names its
    own list: "different trip" tells you not to trust the row without telling
    you which part of it to distrust. The vocabulary is deliberately the short
    form of that list - an `<option>` is not the place for a sentence.
    """
    searched = _sweep_scenario(directory, live)
    differs = []
    if searched.airport_pools != live.airport_pools:
        differs.append("airports")
    if [s.stay_days for s in searched.stops] != [s.stay_days for s in live.stops]:
        differs.append("stays")
    if (searched.window_start, searched.window_end) != (
        live.window_start,
        live.window_end,
    ):
        differs.append("window")
    return differs


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
                "differs": _differs_from_live(d, live),
                **_read_status(d),
                # Whether carrying this one on would actually ask for anything.
                # Computed here rather than in the page so the button and the
                # endpoint agree about what "unfinished" means.
                "resumable": _resumable(d),
                # How many searches carrying on would actually make, so the
                # button can say what it costs before it is pressed. Its own key
                # rather than overwriting the run's own `unanswered`: the two are
                # computed the same way, and a silent disagreement between them
                # would be worth seeing rather than hiding.
                "left_to_ask": _left_to_ask(d),
            }
            for d in directories[:30]
        ],
    }


def _left_to_ask(directory: Path) -> int:
    """Searches this run planned and never got an answer for."""
    status = _read_status(directory)
    planned = status.get("planned") or status.get("total") or 0
    return max(0, planned - (status.get("answered") or 0))


def _resumable(directory: Path) -> bool:
    """A finished run with holes counts too.

    Not gated on `state`: a run can answer 460 of 483 and still record itself
    `done`, and those 23 are exactly the dates that might have been the cheap
    ones. What matters is whether anything is still unasked.
    """
    return _left_to_ask(directory) > 0


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


def _combination(
    scenario: Scenario, directory: Path, narrowing: tuple = (), in_window: bool = True
):
    """This sweep's traversal, memoised per legs file and per narrowing.

    `narrowing` is part of the key rather than applied to a shared result,
    because a filtered traversal answers a different question and prunes
    differently - see `combine.combine_all`. Two filters therefore hold two
    entries, which is the point; the alternative is one of them being wrong.

    `in_window` is in the key for exactly the same reason, and it is not a
    cosmetic one. Measured on the 21 August sweep: with the return window set to
    4-8 February, the unnarrowed traversal kept a 3 February trip and pruned an
    identically priced 6 February one that sits inside the window. Filtering the
    unnarrowed result afterwards would have reported nothing available at all.
    """
    legs_file = directory / "legs.jsonl"
    # Everything `_sweep_scenario` takes from the *live* trip belongs in the key,
    # not only the legs on disk. Editing a trip does not touch its sweeps, so a
    # key made of the legs file alone hands back the previous answer: setting a
    # nights band and then widening it returned the narrower result forever, and
    # read exactly like the sweep having found nothing.
    reading = (
        scenario.bag_estimate,
        scenario.focus_start,
        scenario.focus_end,
        scenario.return_focus_start,
        scenario.return_focus_end,
        scenario.total_days,
        in_window,
    )
    try:
        key = (str(directory), legs_file.stat().st_mtime_ns, narrowing, reading)
    except OSError:
        key = (str(directory), 0, narrowing, reading)
    cached = _combined_cache.get(key)
    if cached is None:
        from_airport, to_airport, bags = narrowing or (None, None, False)
        legs = load_legs(directory)
        # "Every leg confirms a checked bag" is a per-leg condition wearing a
        # whole-trip disguise, so it is applied to the legs: the chain can then
        # never be built at all, which prunes earlier than any test on a
        # finished itinerary could. `is not True` and not `is False` - the site
        # says nothing at all for about half its fares, and unknown is not free.
        if bags:
            legs = [leg for leg in legs if leg.checked_bag is True]
        cached = combine_all(
            legs,
            scenario,
            limit=None,
            starts={from_airport} if from_airport else None,
            ends={to_airport} if to_airport else None,
            narrowed=in_window,
        )
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

    The focus, the return window and the nights band come from the live trip
    for the same reason, and it is the more important case. Narrowing is a decision made
    *after* a broad sweep has been read - that is the whole point of it - so the
    sweeps worth narrowing are precisely the ones that ran before the narrowing
    existed. Taking it from the snapshot would apply it only to sweeps that no
    longer needed it.

    The focus is read from the live trip too, and that is a change: it used to
    stay with the snapshot on the grounds that it decided which searches ran.
    It did, but it is also two thirds of a sentence whose other third is here,
    and splitting them made "the cheapest trip that fits your narrowing" mean a
    trip leaving outside your departure window. What a run actually searched
    under is recorded in its `status.json`, which is what `is_comparable` reads
    to decide what may be charted beside what - so nothing that needed the
    snapshot's copy loses it.
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
        focus_start=live.focus_start,
        focus_end=live.focus_end,
        return_focus_start=live.return_focus_start,
        return_focus_end=live.return_focus_end,
        total_days=live.total_days,
    )


def _sweep_dir_or_404(scenario_id: str, stamp: str) -> tuple[Scenario, Path]:
    """The sweep's own trip and its directory. Callers wanting the trip as it
    stands now - to compare against, or to edit - must load that separately."""
    live = _scenario_or_404(scenario_id)
    directory = DATA_DIR / "sweeps" / scenario_id / _safe_stamp(stamp)
    if not directory.exists():
        raise HTTPException(404, f"No sweep {stamp!r} for {scenario_id!r}")
    return _sweep_scenario(directory, live), directory


def _cheapest(itineraries: list, bag: float, closed: bool):
    """Cheapest bag-inclusive trip that does or does not end where it started."""
    matching = [i for i in itineraries if i.same_airport is closed]
    return min(matching, key=lambda i: i.total_with_bags(bag)) if matching else None


def _narrowing_of(scenario: Scenario, in_window: bool) -> dict:
    """What the narrowing is doing to this reading, for the page to caption.

    Reported whether or not it is on. A view that only mentions a constraint
    while it is applied leaves the reader to infer, from silence, whether they
    are seeing everything - and silence is what an unset narrowing and a
    switched-off one look like alike.
    """
    return {
        "applied": bool(
            in_window
            and (scenario.focus_start or scenario.return_focus_start or scenario.total_days)
        ),
        "focus": _focus_of(scenario),
        "return_focus": (
            [scenario.return_focus_start.isoformat(), scenario.return_focus_end.isoformat()]
            if scenario.return_focus_start
            else None
        ),
        "total_days": list(scenario.total_days) if scenario.total_days else None,
        "span_days": [scenario.min_span_days, scenario.max_span_days],
    }


@app.get("/api/sweeps/{scenario_id}/{stamp}/results")
def sweep_results(
    scenario_id: str,
    stamp: str,
    mode: str = "all",
    limit: int = 50,
    from_airport: str | None = None,
    to_airport: str | None = None,
    bags: bool = False,
    window: str = "narrow",
) -> dict:
    """This sweep's itineraries, optionally narrowed to how you would fly.

    The narrowing happens here rather than in the browser, and that is the whole
    design. `_combination` combines with `limit=None`, so `result.top` holds
    every itinerary this sweep can build while only the slice at the bottom of
    this function cuts it to fifty. Filtering in the page would therefore narrow
    the fifty cheapest, while the headline cards above the table went on
    describing all of them - a summary and a table reporting two different
    populations, with nothing on screen to say which.

    The cards are recomputed from the filtered list for the same reason, on one
    code path whether or not a filter is set, so the two cannot drift apart.
    """
    scenario, directory = _sweep_dir_or_404(scenario_id, stamp)
    bag = scenario.bag_estimate

    # Typed into a URL as often as picked from the dropdowns, and an airport code
    # is upper-case everywhere else in this app.
    from_airport = (from_airport or "").strip().upper() or None
    to_airport = (to_airport or "").strip().upper() or None

    # `narrow` is the default because the narrowing is a decision already made
    # about this trip, and a page that has to be told to respect it will be read
    # once without. `all` exists so that what the narrowing costs stays visible:
    # it is a lens over legs already on disk and never runs a search.
    in_window = window != "all"
    everything = _combination(scenario, directory, in_window=in_window)
    narrowing = (from_airport, to_airport, bool(bags))
    result = (
        _combination(scenario, directory, narrowing, in_window)
        if any(narrowing)
        else everything
    )

    itineraries = result.top
    matched = len(itineraries)

    if mode == "same":
        itineraries = [i for i in itineraries if i.same_airport]
    elif mode == "open":
        itineraries = [i for i in itineraries if not i.same_airport]

    same_airport = _cheapest(itineraries, bag, closed=True)
    open_jaw = _cheapest(itineraries, bag, closed=False)

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
        # The other narrowing, and not the same thing as `focus`: that one is a
        # record of what this run searched, this one is what is being asked of
        # the legs right now and can be turned off without re-sweeping.
        "window": _narrowing_of(scenario, in_window),
        # Absent until `python -m src.cli verify` has been run for this sweep.
        # None means "not checked", which must not read as "checked and fine".
        "verification": _read_json(directory / "verify.json"),
        "currency": scenario.currency,
        "truncated": result.truncated,
        # How many trips the narrowing kept, out of how many this sweep can
        # build. Both, because "34 trips" alone cannot tell you whether the
        # filter is doing anything or the sweep simply found little.
        "matched": matched,
        # Deliberately not "matched out of N". Pruning means the unfiltered
        # traversal is not a superset of a filtered one - it discards a Vienna
        # trip because a cheaper Prague trip shares its departure date, while a
        # Vienna-only traversal keeps it - so an unfiltered count can be
        # *smaller* than a filtered one. A ratio built from the two would read
        # as "34 of 812" while being neither a fraction nor true. What the
        # reader actually needs is whether anything is being hidden right now.
        "narrowed": bool(any(narrowing)),
        # What the unfiltered view costs, so a filter can say what preferring it
        # is worth rather than only that it is on. None when nothing chains.
        "cheapest_unfiltered": (
            everything.best.total_with_bags(bag) if everything.best else None
        ),
        # From the trip's own pools rather than from the itineraries found.
        # Pruning means an airport can be absent from an unfiltered traversal and
        # still have trips of its own - the Vienna case in `combine_all` - so
        # deriving the options from the results would hide exactly the choices
        # worth making. An option that turns out to have nothing says "0 of 812".
        "start_airports": sorted(set(scenario.airport_pools[0])),
        "end_airports": sorted(set(scenario.airport_pools[-1])),
        "best_same_airport": same_airport.to_dict(bag) if same_airport else None,
        "best_open_jaw": open_jaw.to_dict(bag) if open_jaw else None,
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
        # This tab draws conclusions - it calls an airport dear, or not worth
        # pricing - so how much of the plan those conclusions rest on belongs
        # beside them. A probe the site refused after 31 of 123 searches ranks
        # airports in exactly the words a complete one uses. `None` on probes
        # from before the figure was recorded, and it must not read as "all
        # answered": the caller distinguishes the two.
        "coverage": status.get("coverage"),
        "answered": status.get("answered"),
        "planned": status.get("planned"),
        # The trip as it stands now goes in only to be compared against, never
        # to filter: the tab has to be able to say "this run never priced KTW".
        **explore_report(
            load_legs(directory), scenario, status, current=_scenario_or_404(scenario_id)
        ),
    }


@app.get("/api/scenarios/{scenario_id}/airport-verdicts")
def airport_verdicts(scenario_id: str) -> dict:
    """Every airport of the trip, judged by the best run that measured it.

    The Explore tab used to open on a picker of runs by date and time: you chose
    "23 Aug, 10:46 · probe · 48 searches" and only then found out what it said
    about Vienna. That is backwards - the question is about an airport, and the
    run is an implementation detail of the answer - and it also threw work away.
    A probe the site refused after 31 of 123 searches has nothing to say about
    the airports it never reached, and yesterday's complete deep sweep, sitting
    right there on disk, does.

    **Chosen per pool, not per airport.** The verdicts in a pool are relative -
    `_rank` scores each airport against the cheapest of its own pool - so rows
    taken from different runs would be percentages against different baselines,
    printed in one table as though they were comparable. So a whole pool comes
    from one run: the newest run that priced the most of that pool's airports.
    Different pools may come from different runs, which is safe, because pools
    are ranked independently in the first place.

    Runs of a differently shaped trip are skipped outright. Pools are positional,
    and lining up pool 2 of a two-stop trip with pool 2 of a three-stop one is
    how a probe of Prague and Vienna came to be presented as the verdict for a
    trip flying out of Katowice.
    """
    live = _scenario_or_404(scenario_id)
    pools = live.airport_pools
    roles = live.pool_roles

    # Best run per pool, decided as the runs are read rather than after all of
    # them are: `best[index]` is (how many of that pool it priced, stamp, block,
    # status). Walking newest-first with a strict `>` means recency breaks a tie.
    best: dict[int, tuple] = {}
    runs_read = 0

    for directory in _sweep_dirs(scenario_id):
        # Nothing more to learn: every pool has a run that priced all of it, and
        # no older run can beat "all of it" or be newer. Without this the tab
        # re-reads every sweep ever committed - 22 of them and ~140 ms today,
        # and it only grows.
        if len(best) == len(pools) and all(
            best[i][0] == len(pools[i]) for i in range(len(pools))
        ):
            break

        status = _read_status(directory)
        if not _has_legs(directory):
            continue
        searched = _sweep_scenario(directory, live)
        if len(searched.airport_pools) != len(pools):
            continue
        try:
            report = explore_report(load_legs(directory), searched, status, current=live)
        except (ValueError, KeyError, TypeError):
            # One unreadable run must not cost the tab every other run's answer.
            continue
        runs_read += 1

        for index, airports in enumerate(pools):
            wanted = set(airports)
            block = next((b for b in report["pools"] if b["index"] == index), None)
            if block is None:
                continue
            priced = sum(
                1
                for row in block["airports"]
                if row["iata"] in wanted and row["total_min"] is not None
            )
            if priced and (index not in best or priced > best[index][0]):
                best[index] = (priced, directory.name, block, status)

    blocks = []
    for index, (airports, role) in enumerate(zip(pools, roles, strict=True)):
        wanted = set(airports)
        if index not in best:
            blocks.append(
                {
                    "index": index,
                    **role,
                    "measured_by": None,
                    "airports": [],
                    "not_searched": sorted(airports),
                }
            )
            continue

        _, stamp, block, status = best[index]
        rows = [row for row in block["airports"] if row["iata"] in wanted]
        seen = {row["iata"] for row in rows}
        blocks.append(
            {
                "index": index,
                **role,
                # Named, because a verdict is only as good as the run behind it
                # and this table mixes runs by design.
                "measured_by": {
                    "stamp": stamp,
                    "mode": status.get("mode", "sweep"),
                    "depth": status.get("depth"),
                    "state": status.get("state"),
                    "coverage": status.get("coverage"),
                },
                "airports": rows,
                # Airports of this pool the chosen run has no row for at all,
                # which happens when the trip has gained an airport since it
                # ran. Deliberately *not* "nothing has ever priced this": an
                # airport the run asked about and got nothing for already has a
                # row, saying `unproven`, and listing it twice would read as two
                # different facts. Same name as the single-run report's field,
                # because it means the same thing.
                "not_searched": sorted(wanted - seen),
            }
        )

    return {
        "scenario_id": scenario_id,
        "currency": live.currency,
        "runs_read": runs_read,
        "pools": blocks,
    }


@app.get("/api/sweeps/{scenario_id}/{stamp}/by-date")
def sweep_by_date(scenario_id: str, stamp: str, window: str = "narrow") -> list[dict]:
    scenario, directory = _sweep_dir_or_404(scenario_id, stamp)
    # Computed in the same traversal as the results, and never from a capped
    # list: capping first would drop expensive dates from the series and make
    # the chart imply they were never searched.
    #
    # `window` must agree with whatever the results table is showing. Two panels
    # on one screen, one narrowed and one not, would put a departure date on the
    # chart at a price the table below it does not contain.
    return series_from_result(
        _combination(scenario, directory, in_window=window != "all"), scenario.bag_estimate
    )


@app.get("/api/sweeps/{scenario_id}/{stamp}/by-leg")
def sweep_by_leg(scenario_id: str, stamp: str) -> dict:
    """The cheapest fare per date for each leg on its own, plus what was asked.

    Every other chart in this app is about the *total*, which is the right shape
    for choosing a departure date and the wrong one for choosing a trip. A total
    cannot tell you that the flight out is flat all January while the one home
    has a single cheap Thursday - and that reading is what lets a person pick a
    combination the ranking would never surface, because the ranking may only
    offer combinations that obey the stay ranges.

    Two series per leg. `points` is the cheapest offer per date across the whole
    pool, naming the pair that won it; `routes` breaks the same dates out per
    airport pair, for when the question is whether one origin is dragging the
    leg. Derived from `leg_pools`, the property the planner and the combiner
    both walk, rather than from whatever happens to be in `legs.jsonl` - an
    airport that returned nothing must still appear, as nothing.

    `searched` comes from `searches.jsonl` and is not decoration. A date with no
    fare and a date never asked about draw identically otherwise, and they are
    opposite facts: one is the site having nothing, the other is a hole in the
    sweep. The chart draws them differently, so the endpoint has to tell them
    apart.
    """
    scenario, directory = _sweep_dir_or_404(scenario_id, stamp)
    legs = load_legs(directory)
    asked = answered_searches(directory)
    bag = float(scenario.bag_estimate)
    labels = _leg_labels(scenario)

    def cheapest_by_date(pool_legs: list) -> dict[str, dict]:
        best: dict[str, dict] = {}
        for leg in pool_legs:
            if leg.depart_date is None or leg.price_amount is None:
                continue
            key = leg.depart_date.isoformat()
            # Ranked bag-inclusive, like every other total in this app: the
            # cheapest headline fare is usually a carrier whose bag costs extra.
            cost = leg.price_amount + (bag if leg.checked_bag is not True else 0.0)
            if key not in best or cost < best[key]["_cost"]:
                best[key] = {
                    "_cost": cost,
                    "depart_date": key,
                    "price": leg.price_amount,
                    "with_bags": cost,
                    "currency": leg.price_currency,
                    "origin": leg.origin,
                    "destination": leg.destination,
                    "airline": leg.airline,
                    "stops": leg.stops,
                    "url": leg.url,
                    "observed_at": leg.observed_at,
                }
        for row in best.values():
            del row["_cost"]
        return best

    def series(pool_legs: list, pairs: list[tuple[str, str]]) -> list[dict]:
        """Cheapest per date, plus a `searched: false` row for every hole."""
        rows = cheapest_by_date(pool_legs)
        for origin, destination, when in asked:
            if (origin, destination) in pairs and when not in rows:
                rows[when] = {"depart_date": when, "price": None, "searched": True}
        for row in rows.values():
            row.setdefault("searched", True)
        return sorted(rows.values(), key=lambda row: row["depart_date"])

    out = []
    for index, (origins, destinations) in enumerate(scenario.leg_pools):
        pairs = [(o, d) for o in origins for d in destinations]
        allowed = set(pairs)
        pool_legs = [leg for leg in legs if (leg.origin, leg.destination) in allowed]
        out.append(
            {
                "index": index,
                "label": labels[index],
                "origins": list(origins),
                "destinations": list(destinations),
                "points": series(pool_legs, pairs),
                "routes": [
                    {
                        "route": f"{origin}→{destination}",
                        "origin": origin,
                        "destination": destination,
                        "points": series(
                            [
                                leg
                                for leg in pool_legs
                                if leg.origin == origin and leg.destination == destination
                            ],
                            [(origin, destination)],
                        ),
                    }
                    for origin, destination in pairs
                ],
            }
        )

    return {
        "scenario_id": scenario_id,
        "stamp": stamp,
        "currency": scenario.currency,
        "bag_estimate": bag,
        "coverage": _read_status(directory).get("coverage"),
        # So the page can draw the bands it is picking inside, and say which
        # rules a hand-picked combination breaks without asking again.
        "stay_days": [list(stop.stay_days) for stop in scenario.stops],
        "stop_labels": [stop.describe(i) for i, stop in enumerate(scenario.stops)],
        "window": _narrowing_of(scenario, True),
        "focus": (
            [scenario.focus_start.isoformat(), scenario.focus_end.isoformat()]
            if scenario.focus_start
            else None
        ),
        "legs": out,
    }


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
    # Same idea along the date axis, and the reason there are two lines rather
    # than one. A narrowed sweep prices a handful of departure dates, so its
    # cheapest is the cheapest of those dates and not of the trip; charting it
    # beside a broad sweep draws a step no fare made.
    wanted = _narrowing_wanted(scenario)
    series = []
    for directory in reversed(_sweep_dirs(scenario_id)):
        status = _read_status(directory)
        # Which question this run answered, taken from what it recorded rather
        # than from what the trip says today. A run is broad or narrowed for
        # good the moment it finishes; nothing edited afterwards can change it.
        narrowed = any(narrowing_of(status).values())
        # And read on its own terms. A broad run's best total is the cheapest of
        # the whole window - reading it through today's narrowing would turn the
        # broad line into a second copy of the narrowed one, drawn from older
        # data. That was the state of it: every point on the chart was narrowed.
        best = _combination(scenario, directory, in_window=narrowed).best
        if best is None:
            continue
        legs = load_legs(directory)
        covered = len(required & {(leg.origin, leg.destination) for leg in legs})
        series.append(
            {
                "swept_at": directory.name,
                "best_total": best.total_price,
                "best_total_with_bags": best.total_with_bags(scenario.bag_estimate),
                "currency": best.currency,
                "depth": status.get("depth"),
                "mode": status.get("mode", "sweep"),
                # The line this point belongs on. Two runs on different lines are
                # never joined, whatever else they have in common.
                "series": "final" if narrowed else "broad",
                "searches": status.get("total"),
                "legs_per_search": legs_per_search_of(status),
                "routes_covered": covered,
                "routes_planned": len(required),
                # Judged against its own line: a broad run against no narrowing,
                # a narrowed one against the narrowing the trip has now. A trip
                # gaining a focus no longer retires the broad sweeps behind it -
                # they priced the window, and the window has not moved.
                "comparable": is_comparable(
                    status, covered, len(required), wanted if narrowed else None
                ),
                "focus": status.get("focus"),
                "narrowing": narrowing_of(status),
                "coverage": status.get("coverage"),
            }
        )
    return series


# --------------------------------------------------------------------- watch
#
# A sweep prices a window; a watch follows a handful of candidate trips on their
# exact days, every few hours. These endpoints are how days get picked off the
# last sweep, and how the series they have traced since is read back.


def _watch_dir(scenario_id: str) -> Path:
    return DATA_DIR / "watch" / _safe_id(scenario_id)


def _watch_key(scenario_id: str) -> str:
    """Namespaced so a local watch and a local sweep of one trip do not share a
    slot - they are different runs and either may be in flight alone."""
    return f"watch:{scenario_id}"


@app.get("/api/sweeps/{scenario_id}/{stamp}/candidates")
def sweep_candidates(
    scenario_id: str, stamp: str, limit: int = 30, window: str = "narrow"
) -> dict:
    """The cheapest trip on each departure date this sweep priced, cheapest first.

    The Watch tab's source list. It differs from `by-date`, which draws the
    chart, in carrying every leg's date: pinning a candidate means pinning the
    trip that won the day, not merely the day.
    """
    scenario, directory = _sweep_dir_or_404(scenario_id, stamp)
    bag = scenario.bag_estimate
    in_window = window != "all"
    result = _combination(scenario, directory, in_window=in_window)

    candidates = [
        {
            "depart_date": key,
            "depart_dates": [leg.depart_date.isoformat() for leg in itinerary.legs],
            "total": itinerary.total_price,
            "total_with_bags": itinerary.total_with_bags(bag),
            "currency": itinerary.currency,
            "route": itinerary.route,
            "has_overland": itinerary.has_overland,
        }
        for key, itinerary in result.best_by_date.items()
    ]
    candidates.sort(key=lambda row: row["total_with_bags"])
    return {
        "scenario_id": scenario_id,
        "stamp": stamp,
        # So the tab can say which sweep these came from, and flag a partial
        # one: a day that looks cheap because its rivals went unpriced is not a
        # day worth watching.
        "coverage": _read_status(directory).get("coverage"),
        "window": _narrowing_of(scenario, in_window),
        "candidates": candidates[: min(max(limit, 1), 200)],
    }


def _watch_payload(scenario: Scenario) -> dict:
    """Every watched day and leg, the series each has traced, and what a check costs.

    One payload for both because they share one budget: the searches figure and
    the cap below are for the whole planned run, and reporting them separately
    would let two panels each look affordable while the run they add up to is
    refused.
    """
    from ..watch import leg_report, watch_report

    report = watch_report(_watch_dir(scenario.id))
    legs_recorded = leg_report(_watch_dir(scenario.id))["legs"]
    searches = plan_watch(scenario)
    candidates = []
    for watch in scenario.watches:
        recorded = report["candidates"].get(watch.key, {})
        candidates.append(
            {
                "depart_date": watch.key,
                "depart_dates": [d.isoformat() for d in watch.depart_dates],
                "added_at": watch.added_at,
                # What it cost when it was picked, so the very first observation
                # can already say which way it has gone.
                "added_price": watch.added_price,
                "currency": recorded.get("currency", watch.currency),
                "route": recorded.get("route"),
                "has_overland": recorded.get("has_overland", False),
                "series": recorded.get("series", []),
                "observations": recorded.get("observations", 0),
                "first": recorded.get("first"),
                "latest": recorded.get("latest"),
                "latest_with_bags": recorded.get("latest_with_bags"),
                "net_change": recorded.get("net_change", 0),
                "net_change_pct": recorded.get("net_change_pct", 0.0),
                "low": recorded.get("low"),
                "high": recorded.get("high"),
            }
        )
    # Routes the trip's own sweep would never search. Not refused - picking
    # freely is the point - but said, because a leg nothing else prices is one
    # this run alone keeps alive, and that is worth knowing before relying on it.
    trip_routes = planned_routes(scenario)
    watched_legs = [
        {
            "key": watch.key,
            "route": watch.route,
            "origin": watch.origin,
            "destination": watch.destination,
            "depart_date": watch.depart_date.isoformat(),
            "added_at": watch.added_at,
            "added_price": watch.added_price,
            "currency": recorded.get("currency", watch.currency),
            "airline": recorded.get("airline"),
            "stops": recorded.get("stops"),
            "checked_bag": recorded.get("checked_bag"),
            "series": recorded.get("series", []),
            "observations": recorded.get("observations", 0),
            "first": recorded.get("first"),
            "latest": recorded.get("latest"),
            "net_change": recorded.get("net_change", 0),
            "net_change_pct": recorded.get("net_change_pct", 0.0),
            "low": recorded.get("low"),
            "high": recorded.get("high"),
            # False when the site answered with a neighbouring day instead.
            "exact": recorded.get("exact", True),
            "found_date": recorded.get("found_date"),
            "off_trip": (watch.origin, watch.destination) not in trip_routes,
            # Where to go and buy it. The watch records a price and never the
            # offer's URL - `record_leg` keeps what it needs to compare, and a
            # deep link scraped four hours ago is not that - so the search is
            # rebuilt from the three things that define this watch. A search
            # page rather than a specific offer, which is the honest link:
            # what was cheapest at the last check need not still be.
            "book_url": build_search_url(
                watch.origin,
                watch.destination,
                watch.depart_date,
                adults=scenario.adults,
                source=load_source("PELIKAN", DATA_DIR),
            ),
        }
        for watch in scenario.leg_watches
        for recorded in [legs_recorded.get(watch.key, {})]
    ]

    return {
        "scenario_id": scenario.id,
        "candidates": candidates,
        "legs": watched_legs,
        "searches": len(searches),
        "minutes": estimate_minutes(searches),
        "cap": WATCH_SEARCH_CAP,
        "running": _is_running(_watch_key(scenario.id)),
        "error": _failures.get(_watch_key(scenario.id)),
    }


@app.get("/api/watch/{scenario_id}")
def get_watch(scenario_id: str) -> dict:
    return _watch_payload(_scenario_or_404(scenario_id))


@app.post("/api/watch/{scenario_id}", status_code=201)
def add_watch(scenario_id: str, payload: dict = Body(...)) -> dict:
    """Start watching one candidate trip.

    Refused on two counts, both in words the page shows verbatim: a candidate
    the combiner could never chain, and a plan that would run past what the
    site answers.
    """
    scenario = _scenario_or_404(scenario_id)
    raw = payload.get("depart_dates") or []
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "depart_dates must list one date per leg of the trip")
    try:
        dates = [date.fromisoformat(str(value)) for value in raw]
    except ValueError as exc:
        raise HTTPException(400, f"depart_dates must be YYYY-MM-DD: {exc}") from exc

    candidate = Watch(
        depart_dates=dates,
        added_at=datetime.now(UTC).isoformat(timespec="seconds"),
        added_price=payload.get("added_price"),
        currency=payload.get("currency") or scenario.currency,
    )
    proposed = replace(scenario, watches=[*scenario.watches, candidate])
    try:
        proposed.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    planned = len(plan_watch(proposed))
    if planned > WATCH_SEARCH_CAP:
        raise HTTPException(
            400,
            f"watching that day as well would be {planned} searches every few hours, "
            f"and pelikan.cz stops answering this client after about "
            f"{WATCH_SEARCH_CAP}. Stop watching a day first, or narrow the trip.",
        )

    save_scenario(proposed, SCENARIO_DIR)
    return _watch_payload(proposed)


@app.delete("/api/watch/{scenario_id}/{depart_date}")
def remove_watch(scenario_id: str, depart_date: str) -> dict:
    scenario = _scenario_or_404(scenario_id)
    if not SAFE_DATE.match(depart_date):
        raise HTTPException(400, f"{depart_date!r} is not a date")
    kept = [w for w in scenario.watches if w.key != depart_date]
    if len(kept) == len(scenario.watches):
        raise HTTPException(404, f"{depart_date} is not being watched")
    updated = replace(scenario, watches=kept)
    save_scenario(updated, SCENARIO_DIR)
    # The observations stay. They were real measurements that cost real
    # searches, and un-picking a day is not a request to forget what it did -
    # the same reason deleting a trip leaves its sweeps behind.
    return _watch_payload(updated)


@app.post("/api/watch/{scenario_id}/legs", status_code=201)
def add_leg_watch(scenario_id: str, payload: dict = Body(...)) -> dict:
    """Follow one route on one date.

    Deliberately permissive about *which* route. A leg watch exists because the
    decision is assembled by hand - Vienna to Haneda on the 10th and the 12th,
    Manila home on the 2nd - and refusing anything the trip's own sweep would
    not search would defeat that. The payload flags an off-trip route instead.

    The one refusal that matters is the budget, and it counts the whole planned
    run: pelikan.cz answers about 120 searches from one runner before it stops
    answering at all, and the watch runs unsharded by design.
    """
    scenario = _scenario_or_404(scenario_id)
    try:
        origin = str(payload.get("origin", "")).strip().upper()
        destination = str(payload.get("destination", "")).strip().upper()
        depart_date = date.fromisoformat(str(payload.get("depart_date", "")))
    except ValueError as exc:
        raise HTTPException(400, f"depart_date must be YYYY-MM-DD: {exc}") from exc

    candidate = LegWatch(
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        added_at=datetime.now(UTC).isoformat(timespec="seconds"),
        added_price=payload.get("added_price"),
        currency=payload.get("currency") or scenario.currency,
    )
    proposed = replace(scenario, leg_watches=[*scenario.leg_watches, candidate])
    try:
        proposed.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    planned = len(plan_watch(proposed))
    if planned > WATCH_SEARCH_CAP:
        raise HTTPException(
            400,
            f"following that flight as well would be {planned} searches every few "
            f"hours, and pelikan.cz stops answering this client after about "
            f"{WATCH_SEARCH_CAP}. That budget covers the watched days and the "
            f"watched flights together, so stop following one of either first.",
        )

    save_scenario(proposed, SCENARIO_DIR)
    return _watch_payload(proposed)


@app.delete("/api/watch/{scenario_id}/legs/{key}")
def remove_leg_watch(scenario_id: str, key: str) -> dict:
    scenario = _scenario_or_404(scenario_id)
    kept = [w for w in scenario.leg_watches if w.key != key]
    if len(kept) == len(scenario.leg_watches):
        raise HTTPException(404, f"{key} is not being followed")
    updated = replace(scenario, leg_watches=kept)
    save_scenario(updated, SCENARIO_DIR)
    # The observations stay, for the same reason un-picking a day leaves its
    # series behind: they were real measurements that cost real searches.
    return _watch_payload(updated)


@app.post("/api/watch/{scenario_id}/run")
def run_watch_now(scenario_id: str) -> dict:
    """Check the watched days now, on this machine.

    The scheduled workflow is what keeps the series going; this is for when an
    answer is wanted before the next slot comes round. Threaded exactly like a
    local sweep, so the status strip reports it the same way.
    """
    scenario = _scenario_or_404(scenario_id)
    if not scenario.watches and not scenario.leg_watches:
        raise HTTPException(400, "nothing is being watched yet, so there is nothing to check")
    key = _watch_key(scenario_id)
    if _is_running(key):
        raise HTTPException(409, "a check is already running for this trip")

    _failures.pop(key, None)
    stop = threading.Event()

    def work():
        try:
            from ..sweep.runner import run_sweep
            from ..watch import (
                drops,
                leg_drops,
                leg_report,
                record_leg_observations,
                record_observations,
                watch_report,
            )

            result = run_sweep(scenario, data_dir=DATA_DIR, mode="watch", stop=stop)
            status = _read_status(result.directory)
            directory = _watch_dir(scenario_id)
            record_observations(result.legs, scenario, status, directory)
            record_leg_observations(result.legs, scenario, status, directory)
            drops(watch_report(directory), scenario, directory)
            leg_drops(leg_report(directory), scenario, directory)
        except Exception as exc:  # noqa: BLE001 - recorded and surfaced, not swallowed
            _failures[key] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    _running[key] = thread
    _stops[key] = stop
    return {"started": True, "scenario_id": scenario_id}


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
    """Every source, with whatever the last check of it found.

    The check travels with the source so the tab can show a state on load. A
    card that says "unknown until you press the button" is a card you have to
    press a button on to learn anything, which is most of why the old one was
    not worth opening.
    """
    checks = load_checks(DATA_DIR)
    return {
        name: {**source.to_dict(), "last_check": checks.get(name)}
        for name, source in load_sources(DATA_DIR).items()
    }


@app.put("/api/sources")
def put_sources(payload: dict = Body(...)) -> dict:
    unknown = set(payload) - set(DEFAULT_SOURCES)
    if unknown:
        raise HTTPException(
            400,
            f"unknown source(s): {', '.join(sorted(unknown))}. "
            f"Known sources are {', '.join(sorted(DEFAULT_SOURCES))}",
        )
    # A site driven through its form has its steps in code, so accepting
    # selectors for it would save a repair that cannot take effect - and then
    # report success. Refusing names the reason instead.
    for name in payload:
        if not DEFAULT_SOURCES[name].repairable:
            raise HTTPException(
                400,
                f"{name} has no selectors to edit: {DEFAULT_SOURCES[name].note}",
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
            role=DEFAULT_SOURCES[name].role,
            label=DEFAULT_SOURCES[name].label,
            note=DEFAULT_SOURCES[name].note,
            repairable=DEFAULT_SOURCES[name].repairable,
        )

    # The sources not being edited are written back as they stand, so saving one
    # never silently reverts another to its built-in defaults.
    save_sources({**load_sources(DATA_DIR), **sources}, DATA_DIR)
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


def _record_check(name: str, outcome: dict) -> dict:
    """Stamp a check with when it happened and write it down.

    Timed here rather than in each branch so no outcome can reach the page
    without a time on it - "working" with no date is a claim about now that may
    be a fortnight old.
    """
    stamped = {**outcome, "checked_at": datetime.now(UTC).isoformat(timespec="seconds")}
    save_check(name, stamped, DATA_DIR)
    return stamped


def _test_check_source(name: str) -> dict:
    """Price one leg on a form-driven source, and report what happened.

    Deliberately not a selector report. There is no selector map to be right or
    wrong about here - the steps live in the provider - so the only honest
    question is whether asking it for a price still works.
    """
    from ..providers.letuska import LetuskaProvider, LetuskaSearchFailed

    # One check source exists. Named rather than assumed, so adding a second one
    # fails here instead of quietly reporting letuska's health under its name.
    if name != "LETUSKA":
        raise HTTPException(500, f"no check is implemented for {name!r}")

    origin, destination = TEST_ROUTE
    depart = date.today() + timedelta(days=45)
    route = f"{origin}→{destination} {depart.isoformat()}"
    try:
        legs = LetuskaProvider().check_price(origin, destination, depart)
    except LetuskaSearchFailed as exc:
        return {
            "ok": False,
            "route": route,
            "legs_parsed": 0,
            "cards_found": 0,
            "sample": None,
            "message": (
                f"The search did not complete: {exc}. This site is driven through its "
                "form rather than a deep link, so the usual cause is the form itself "
                "changing - which is a code fix in src/providers/letuska.py, not a "
                "selector you can type here."
            ),
        }
    except Exception as exc:  # noqa: BLE001 - the failure is the answer here
        return {
            "ok": False,
            "route": route,
            "legs_parsed": 0,
            "cards_found": 0,
            "sample": None,
            "message": f"Could not run the search at all: {type(exc).__name__}: {exc}",
        }

    if not legs:
        return {
            "ok": False,
            "route": route,
            "legs_parsed": 0,
            "cards_found": 0,
            "sample": None,
            "message": (
                "The search completed and the site offered nothing on this route. That "
                "is data rather than breakage, but it leaves the check unproven - try "
                "again on a route it definitely sells."
            ),
        }
    cheapest = min(legs, key=lambda leg: leg.price_amount)
    return {
        "ok": True,
        "route": route,
        "legs_parsed": len(legs),
        "cards_found": len(legs),
        "sample": cheapest.to_dict(),
        "message": (
            f"Priced {len(legs)} offer(s); cheapest {cheapest.price_amount:,.0f} "
            f"{cheapest.price_currency}. The second opinion still works."
        ),
    }


@app.post("/api/sources/{name}/test")
def test_source(name: str) -> dict:
    """Run one real search against this source and record what came back.

    Three kinds of source, three kinds of answer:

    - A **sweep** source is checked by parsing a real results page, because the
      single question worth answering is "is it the URL or the markup?", which
      otherwise costs an afternoon.
    - A **check** source has no deep link and no selector map; it is checked by
      asking it to price one leg, which is the only thing it is ever asked to do.
    - A source that is **not connected** is not checked at all. Pretending to
      test one would be the same lie as a sweep reporting `error_count: 0`.

    Reports rather than raises: a zero count *is* the finding. The outcome is
    written to disk so the card shows it again on the next load.
    """
    if name not in DEFAULT_SOURCES:
        raise HTTPException(404, f"No source named {name!r}")

    role = DEFAULT_SOURCES[name].role
    if role == "none":
        raise HTTPException(
            400,
            f"{DEFAULT_SOURCES[name].label or name} is not connected, so there is "
            "nothing to check. Nothing in this app reads it.",
        )
    if role == "check":
        return _record_check(name, _test_check_source(name))

    from ..providers.pelikan import parse_results_html
    from ..providers.pelikan_url import build_search_url

    source = load_source(name, DATA_DIR)
    origin, destination = TEST_ROUTE
    depart = date.today() + timedelta(days=45)
    url = build_search_url(origin, destination, depart, source=source)

    def outcome(message: str, cards: int = 0, legs: list | None = None) -> dict:
        legs = legs or []
        return _record_check(name, {
            "ok": bool(legs),
            "url": url,
            "route": f"{origin}→{destination} {depart.isoformat()}",
            "cards_found": cards,
            "legs_parsed": len(legs),
            "sample": legs[0].to_dict() if legs else None,
            "message": message,
        })

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
