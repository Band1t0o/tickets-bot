"""Tests for the concurrent sweep runner.

A fake provider stands in for Playwright: these tests must never launch a
browser or touch the network.
"""
from __future__ import annotations

import json
from datetime import date

from src.models import Leg
from src.scenario import Scenario
from src.sweep.runner import run_sweep


def scenario(**overrides) -> Scenario:
    defaults = dict(
        id="test-scenario",
        name="Test",
        trip_type="multi_city",
        origins=["PRG"],
        japan_airports=["NRT"],
        ph_airports=["MNL"],
        window_start=date(2027, 1, 5),
        window_end=date(2027, 2, 8),
        japan_stay_days=(9, 11),
        ph_stay_days=(9, 11),
        depth="quick",
    )
    defaults.update(overrides)
    return Scenario(**defaults)


class FakeProvider:
    """Returns one leg per search and records what it was asked for."""

    NAME = "FAKE"

    def __init__(self, fail_on: set[str] | None = None):
        self.calls: list[tuple[str, str, date]] = []
        self.fail_on = fail_on or set()

    def search_leg(self, page, origin, destination, depart, ret=None, adults=1):
        self.calls.append((origin, destination, depart))
        if destination in self.fail_on:
            raise RuntimeError(f"boom on {destination}")
        return [
            Leg(
                provider=self.NAME,
                origin=origin,
                destination=destination,
                depart_date=depart,
                airline="XX",
                flight_number=None,
                stops=1,
                price_currency="CZK",
                price_amount=10000.0,
                url="https://example.test/leg",
            )
        ]


def test_runs_every_planned_search(tmp_path):
    provider = FakeProvider()
    result = run_sweep(scenario(), provider=provider, data_dir=tmp_path, workers=2, delay_s=0)
    assert len(provider.calls) == result.total
    assert result.total > 0


def test_collects_legs_from_all_searches(tmp_path):
    result = run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=2, delay_s=0)
    assert len(result.legs) == result.total


def test_one_failing_search_does_not_abort_the_sweep(tmp_path):
    provider = FakeProvider(fail_on={"MNL"})
    result = run_sweep(scenario(), provider=provider, data_dir=tmp_path, workers=2, delay_s=0)
    assert result.errors, "the failing searches should be recorded"
    assert result.legs, "the surviving searches should still produce legs"
    assert result.completed == result.total


def test_progress_callback_fires_once_per_search(tmp_path):
    seen = []
    run_sweep(
        scenario(),
        provider=FakeProvider(),
        data_dir=tmp_path,
        workers=2,
        on_progress=lambda done, total, label: seen.append(done),
    )
    assert len(seen) == max(seen)


def test_writes_legs_and_status_to_disk(tmp_path):
    result = run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=2, delay_s=0)
    legs_file = result.directory / "legs.jsonl"
    status_file = result.directory / "status.json"
    assert legs_file.exists() and status_file.exists()

    rows = [json.loads(line) for line in legs_file.read_text().splitlines() if line.strip()]
    assert len(rows) == len(result.legs)

    status = json.loads(status_file.read_text())
    assert status["state"] == "done"
    assert status["completed"] == status["total"]
    assert status["scenario_id"] == "test-scenario"


def test_status_records_finished_timestamp(tmp_path):
    result = run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=2, delay_s=0)
    status = json.loads((result.directory / "status.json").read_text())
    assert status["started_at"] and status["finished_at"]


def test_sweep_with_every_search_failing_is_marked_unhealthy(tmp_path):
    provider = FakeProvider(fail_on={"NRT", "MNL", "PRG"})
    result = run_sweep(scenario(), provider=provider, data_dir=tmp_path, workers=2, delay_s=0)
    assert result.legs == []
    assert not result.is_healthy
