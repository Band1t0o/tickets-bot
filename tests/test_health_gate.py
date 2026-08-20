"""The guard on the second sweep of the day.

Two deep sweeps a day is ~1,230 searches against a site that has already
throttled this client into 58 of 93 timeouts once. The most likely way to make
the data worse is to keep hammering a site that is already refusing, so the
afternoon run asks the morning run how it went first.
"""
from __future__ import annotations

import json

import pytest

from src.cli import health_gate_command


def sweep(tmp_path, stamp="2026-08-10T11-57-06Z", **status):
    directory = tmp_path / "sweeps" / "jp-ph" / stamp
    directory.mkdir(parents=True)
    payload = {"state": "done", "total": 93, "legs_found": 903, "legs_per_search": 9.71}
    payload.update(status)
    (directory / "status.json").write_text(json.dumps(payload), encoding="utf-8")
    return directory


def test_a_healthy_last_sweep_opens_the_gate(tmp_path, capsys):
    sweep(tmp_path)
    assert health_gate_command("jp-ph", 6.0, data_dir=tmp_path) == 0


def test_a_starved_last_sweep_closes_the_gate(tmp_path):
    sweep(tmp_path, legs_per_search=2.93)
    assert health_gate_command("jp-ph", 6.0, data_dir=tmp_path) == 1


def test_the_gate_reads_the_most_recent_sweep_not_the_best_one(tmp_path):
    sweep(tmp_path, "2026-08-06T02-00-00Z", legs_per_search=9.71)
    sweep(tmp_path, "2026-08-10T02-00-00Z", legs_per_search=2.93)
    assert health_gate_command("jp-ph", 6.0, data_dir=tmp_path) == 1


def test_a_sweep_predating_the_metric_is_judged_on_what_it_does_record(tmp_path):
    directory = sweep(tmp_path)
    (directory / "status.json").write_text(
        json.dumps({"state": "done", "total": 93, "legs_found": 903}), encoding="utf-8"
    )
    assert health_gate_command("jp-ph", 6.0, data_dir=tmp_path) == 0


def test_no_sweeps_at_all_opens_the_gate(tmp_path):
    """Nothing to be suspicious of yet. Refusing to start would be a deadlock:
    the gate would never open, because only a sweep can open it."""
    (tmp_path / "sweeps").mkdir()
    assert health_gate_command("jp-ph", 6.0, data_dir=tmp_path) == 0


def test_an_unreadable_status_closes_the_gate(tmp_path):
    directory = sweep(tmp_path)
    (directory / "status.json").write_text("{not json", encoding="utf-8")
    assert health_gate_command("jp-ph", 6.0, data_dir=tmp_path) == 1


def test_the_gate_says_why_it_closed(tmp_path, capsys):
    sweep(tmp_path, legs_per_search=2.93)
    health_gate_command("jp-ph", 6.0, data_dir=tmp_path)
    printed = capsys.readouterr().out
    assert "2.93" in printed and "6.0" in printed


@pytest.mark.parametrize("state", ["running", "unhealthy"])
def test_a_sweep_that_did_not_finish_closes_the_gate(tmp_path, state):
    sweep(tmp_path, state=state)
    assert health_gate_command("jp-ph", 6.0, data_dir=tmp_path) == 1
