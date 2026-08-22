"""What the cloud sweep is doing, has done, and is being held to do.

None of this was answerable from the app. `run_in_cloud` shelled `gh workflow
run`, returned `{"dispatched": true}` and forgot the run existed, so a sweep
that swept nothing looked exactly like one that worked. On 22 Aug that mattered
twice over: three dispatches planned no searches and reported success in twelve
seconds, and two more were cancelled while pending without ever starting a job.
The page said "Dispatched to GitHub Actions" five times.

Everything here is read through the `gh` CLI, which is already how the app
dispatches. It follows the same discipline as `_git` in `app.py`: no call raises
past the boundary. `gh` missing, not logged in, offline, or answering something
that is not JSON all mean *cannot say*, and a panel that cannot say must say that
rather than draw an empty list, which reads as "nothing has ever run".
"""
from __future__ import annotations

import json
import subprocess
import threading
from datetime import UTC, datetime

WORKFLOW = "scrape.yml"

# What `gh run list` is asked for. `databaseId` is the run number the Actions UI
# and `gh run view` both take.
FIELDS = "databaseId,status,conclusion,createdAt,updatedAt,event,url"

# A run that is in none of these states is finished, whatever it concluded.
LIVE = ("queued", "in_progress", "requested", "waiting", "pending")

# A finished run shorter than this that still reports success is the failure
# this module was written for: plan skipped the sweep, nothing was searched, and
# the tick was green. Twelve seconds was the real figure; a minute is a generous
# floor, since even a cache-warm sweep job spends longer than that installing
# Chromium.
NOTHING_HAPPENED_SECONDS = 60

# How often the holding queue looks to see whether the lane has cleared.
# Seconds, deliberately unhurried: what it waits for takes minutes at best, and
# every tick is a subprocess and an API call.
POLL_SECONDS = 30


class CloudError(Exception):
    """`gh` could not answer. Carries the sentence to show, not a traceback."""


def _gh(*args: str, timeout: int = 30) -> str:
    try:
        done = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise CloudError(
            "The gh CLI is not installed, so this app cannot see or start cloud "
            "runs. The schedule in GitHub Actions is unaffected."
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise CloudError(f"gh did not answer: {exc}") from exc
    if done.returncode != 0:
        raise CloudError(f"gh failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def list_runs(limit: int = 12) -> list[dict]:
    """The most recent sweep runs, newest first. Raises CloudError if it cannot."""
    raw = _gh(
        "run", "list", "--workflow", WORKFLOW, "--limit", str(limit), "--json", FIELDS
    )
    try:
        found = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CloudError("gh answered something that was not JSON") from exc
    if not isinstance(found, list):
        raise CloudError("gh answered something that was not a list of runs")
    return [_describe(run) for run in found if isinstance(run, dict)]


def _describe(run: dict) -> dict:
    """One run, plus the reading of it the panel actually needs.

    `swept_nothing` is the state this module came from: a run that went green
    without ever starting a sweep job. It is worth naming, because "success" on
    its own is what let three of them pass unnoticed.
    """
    status = run.get("status") or ""
    conclusion = run.get("conclusion") or ""
    live = status in LIVE
    seconds = _seconds(run.get("createdAt"), run.get("updatedAt"))
    return {
        "id": run.get("databaseId"),
        "status": status,
        "conclusion": conclusion,
        "event": run.get("event") or "",
        "created_at": run.get("createdAt") or "",
        "url": run.get("url") or "",
        "live": live,
        "seconds": None if seconds is None else round(seconds),
        "swept_nothing": (
            not live
            and conclusion == "success"
            and seconds is not None
            and seconds < NOTHING_HAPPENED_SECONDS
        ),
        # A run cancelled before it started a single job is the pending-cancel
        # that per-trip lanes now prevent. Worth distinguishing from a run
        # someone stopped on purpose half an hour in.
        "cancelled_while_waiting": (
            conclusion == "cancelled"
            and seconds is not None
            and seconds < 15 * 60
        ),
    }


def _seconds(started: str | None, ended: str | None) -> float | None:
    """How long a run took, or None when it cannot be worked out.

    None rather than zero: an unreadable timestamp must not read as "finished
    suspiciously fast" and get flagged as a run that swept nothing.
    """
    try:
        return (
            datetime.fromisoformat(ended.replace("Z", "+00:00"))
            - datetime.fromisoformat(started.replace("Z", "+00:00"))
        ).total_seconds()
    except (AttributeError, ValueError):
        return None


def dispatch(scenario_id: str, depth: str | None = None) -> None:
    """Ask Actions to sweep one trip now."""
    command = ["workflow", "run", WORKFLOW, "-f", f"scenario={scenario_id}"]
    if depth:
        command += ["-f", f"depth={depth}"]
    _gh(*command)


def lane_is_busy(runs: list[dict] | None = None) -> bool:
    """Whether a sweep run is already queued or going.

    Dispatching into a busy lane leaves a run pending, and a *third* dispatch
    cancels it outright - the shape that lost two runs on 22 Aug.

    `gh run list` does not report the inputs a run was dispatched with, so this
    cannot tell which trip a live run is about and treats any live run as busy.
    That errs towards holding a run back, which is the cheap mistake; the
    expensive one is the cancellation this exists to prevent.
    """
    runs = list_runs() if runs is None else runs
    return any(run["live"] for run in runs)


# --------------------------------------------------------------- the queue
#
# In this process, like `_running` and `_stops` in `app.py`, and deliberately not
# in `DATA_DIR`: the workflow commits that directory, and a local intention is
# not a measurement. A queued run is minutes of intent, so the panel says
# outright that closing the app forgets it rather than implying it survives.

_queue: list[dict] = []
_lock = threading.Lock()
_worker: threading.Thread | None = None


def queued() -> list[dict]:
    with _lock:
        return [dict(entry) for entry in _queue]


def enqueue(scenario_id: str, depth: str | None = None) -> dict:
    entry = {
        "scenario_id": scenario_id,
        "depth": depth or "",
        "queued_at": datetime.now(UTC).isoformat(),
        "error": "",
    }
    with _lock:
        # Asking twice for the same trip is one run, not two. A second click is
        # nearly always the first being doubted, and two dispatches of one trip
        # is exactly the shape that gets a run cancelled.
        for existing in _queue:
            if existing["scenario_id"] == scenario_id:
                return dict(existing)
        _queue.append(entry)
    _start_worker()
    return dict(entry)


def drop(scenario_id: str) -> bool:
    with _lock:
        before = len(_queue)
        _queue[:] = [e for e in _queue if e["scenario_id"] != scenario_id]
        return len(_queue) != before


def _start_worker() -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=drain, daemon=True)
        _worker.start()


def drain(wait=None) -> None:
    """Dispatch held runs one at a time, as the lane clears.

    Never raises: this runs on a daemon thread nobody is waiting on, and a
    thread that dies takes its traceback with it - the exact failure the
    `_failures` dict in `app.py` exists to stop happening to sweeps. A `gh` that
    cannot answer is recorded against the held run and retried, because the
    usual cause is a laptop that is briefly offline.

    `wait` is the sleep, injectable so a test can drive this without waiting
    half a minute per turn.
    """
    sleeper = wait or threading.Event().wait
    while True:
        with _lock:
            if not _queue:
                return
            entry = dict(_queue[0])
        try:
            if not lane_is_busy():
                dispatch(entry["scenario_id"], entry["depth"] or None)
                drop(entry["scenario_id"])
                continue
        except CloudError as exc:
            with _lock:
                for held in _queue:
                    if held["scenario_id"] == entry["scenario_id"]:
                        held["error"] = str(exc)
        sleeper(POLL_SECONDS)
