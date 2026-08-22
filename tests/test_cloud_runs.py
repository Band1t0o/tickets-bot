"""Reading the cloud's run history, and holding a run back until it can go.

Both halves of this exist because of 22 Aug. Three dispatches went green in
twelve seconds having swept nothing, and two more were cancelled while pending
without ever starting a job - and from the app all five looked identical, because
all five had produced the words "Dispatched to GitHub Actions" and nothing else.

Nothing here shells out. `gh` is monkeypatched at `_gh`, the one boundary.
"""
from __future__ import annotations

import json

import pytest

from src.web import cloud_runs

# Captured before `no_real_gh` in conftest replaces it, for the one test below
# that is about `_gh` itself rather than about a caller of it.
REAL_GH = cloud_runs._gh


@pytest.fixture(autouse=True)
def empty_queue():
    """The queue is module state, and `test_api` reloads `app` but not this."""
    cloud_runs._queue.clear()
    yield
    cloud_runs._queue.clear()


def run(**overrides) -> dict:
    payload = {
        "databaseId": 1,
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "createdAt": "2026-08-22T14:00:00Z",
        "updatedAt": "2026-08-22T14:20:00Z",
        "url": "https://example.test/1",
    }
    payload.update(overrides)
    return payload


def answering(monkeypatch, runs):
    monkeypatch.setattr(cloud_runs, "_gh", lambda *a, **k: json.dumps(runs))


# ------------------------------------------------------------ reading a run


def test_a_twelve_second_success_is_reported_as_having_swept_nothing(monkeypatch):
    """The exact shape of run 32578318120: green, and it never searched."""
    answering(monkeypatch, [run(updatedAt="2026-08-22T14:00:12Z")])
    assert cloud_runs.list_runs()[0]["swept_nothing"] is True


def test_a_real_sweep_is_not_flagged(monkeypatch):
    answering(monkeypatch, [run()])
    assert cloud_runs.list_runs()[0]["swept_nothing"] is False


def test_a_run_still_going_is_never_flagged(monkeypatch):
    """It has been going twelve seconds because it started twelve seconds ago."""
    answering(monkeypatch, [
        run(status="in_progress", conclusion=None, updatedAt="2026-08-22T14:00:12Z")
    ])
    read = cloud_runs.list_runs()[0]
    assert read["live"] is True
    assert read["swept_nothing"] is False


def test_an_unreadable_timestamp_is_not_read_as_suspiciously_fast(monkeypatch):
    """None, not zero: 'cannot tell' must not become an accusation."""
    answering(monkeypatch, [run(updatedAt="not a time")])
    read = cloud_runs.list_runs()[0]
    assert read["seconds"] is None
    assert read["swept_nothing"] is False


def test_a_run_cancelled_before_it_started_is_told_apart_from_one_stopped_late(monkeypatch):
    """Runs 32577251585 and 32577483180: killed while pending, zero jobs run."""
    answering(monkeypatch, [
        run(conclusion="cancelled", updatedAt="2026-08-22T14:04:35Z"),
        run(databaseId=2, conclusion="cancelled", updatedAt="2026-08-22T15:00:00Z"),
    ])
    waiting, stopped = cloud_runs.list_runs()
    assert waiting["cancelled_while_waiting"] is True
    assert stopped["cancelled_while_waiting"] is False


def test_gh_answering_nonsense_is_a_cloud_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(cloud_runs, "_gh", lambda *a, **k: "<!DOCTYPE html>")
    with pytest.raises(cloud_runs.CloudError):
        cloud_runs.list_runs()


def test_a_missing_gh_says_the_schedule_is_unaffected(monkeypatch):
    """The distinction that matters when the panel goes blank: this app cannot
    see Actions, which is not the same as Actions having stopped."""
    def missing(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(cloud_runs, "_gh", REAL_GH)
    monkeypatch.setattr(cloud_runs.subprocess, "run", missing)
    with pytest.raises(cloud_runs.CloudError, match="schedule in GitHub Actions is unaffected"):
        cloud_runs.list_runs()


# ------------------------------------------------------------- the lane


def test_a_live_run_makes_the_lane_busy(monkeypatch):
    answering(monkeypatch, [run(status="in_progress", conclusion=None)])
    assert cloud_runs.lane_is_busy() is True


def test_a_finished_history_leaves_the_lane_free(monkeypatch):
    answering(monkeypatch, [run(), run(databaseId=2)])
    assert cloud_runs.lane_is_busy() is False


# ------------------------------------------------------------- the queue


def test_asking_twice_for_one_trip_queues_it_once(monkeypatch):
    """A second click is the first being doubted. Two dispatches of one trip is
    the shape that gets a run cancelled, which is what this is here to avoid."""
    cloud_runs.enqueue("jp-ph", "deep")
    cloud_runs.enqueue("jp-ph", "deep")
    assert [e["scenario_id"] for e in cloud_runs.queued()] == ["jp-ph"]


def test_a_held_run_is_dispatched_once_the_lane_clears(monkeypatch):
    sent = []
    states = [True, False]
    monkeypatch.setattr(cloud_runs, "lane_is_busy", lambda *a: states.pop(0))
    monkeypatch.setattr(cloud_runs, "dispatch", lambda *a: sent.append(a))

    cloud_runs._queue.append({"scenario_id": "jp-ph", "depth": "deep", "error": ""})
    cloud_runs.drain(wait=lambda _seconds: None)

    assert sent == [("jp-ph", "deep")]
    assert cloud_runs.queued() == []


def test_a_gh_that_cannot_answer_keeps_the_run_held_and_says_why(monkeypatch):
    """A laptop that is briefly offline must not silently drop the run."""
    tries = []

    def refusing():
        tries.append(1)
        if len(tries) < 2:
            raise cloud_runs.CloudError("gh failed: offline")
        return False

    monkeypatch.setattr(cloud_runs, "lane_is_busy", refusing)
    monkeypatch.setattr(cloud_runs, "dispatch", lambda *a: None)

    cloud_runs._queue.append({"scenario_id": "jp-ph", "depth": "", "error": ""})
    cloud_runs.drain(wait=lambda _seconds: None)

    assert len(tries) == 2
    assert cloud_runs.queued() == []


def test_dropping_a_held_run_reports_whether_there_was_one(monkeypatch):
    cloud_runs.enqueue("jp-ph")
    assert cloud_runs.drop("jp-ph") is True
    assert cloud_runs.drop("jp-ph") is False
