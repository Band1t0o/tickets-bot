"""Tests for the concurrent sweep runner.

A fake provider stands in for Playwright: these tests must never launch a
browser or touch the network.
"""
from __future__ import annotations

import json
from datetime import date

from src.models import Leg
from src.scenario import Scenario, Stop
from src.sweep.runner import run_sweep
from tests.conftest import make_scenario


def scenario(**overrides) -> Scenario:
    defaults = dict(
        id="test-scenario",
        name="Test",
        origins=["PRG"],
        stops=[
            Stop(airports=["NRT"], stay_days=(9, 11), label="Japan"),
            Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
        ],
        depth="quick",
    )
    defaults.update(overrides)
    return make_scenario(**defaults)


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


# ------------------------------------------------------------ sweep quality
#
# "0 errors" is not health. The 06 Aug standard sweep reported error_count 0
# while averaging 2.9 legs per search, where a working search returns ~10 -
# roughly 70% of it failed invisibly. These are the figures that tell the two
# apart, and they are recorded so a later sweep can be compared against them.


def test_legs_per_search_is_recorded(tmp_path):
    result = run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=2, delay_s=0)
    # The fake returns exactly one leg per search.
    assert result.legs_per_search == 1.0
    status = json.loads((result.directory / "status.json").read_text())
    assert status["legs_per_search"] == 1.0


def test_legs_per_search_is_zero_when_nothing_was_planned(tmp_path):
    """Never a ZeroDivisionError.

    `Scenario.validate` makes a zero-search sweep unreachable through
    `run_sweep`, so the property is exercised on the result directly - it is
    still what `status_payload` calls on the empty case.
    """
    from src.sweep.runner import SweepResult

    empty = SweepResult(scenario_id="x", directory=tmp_path, total=0)
    assert empty.legs_per_search == 0.0
    assert empty.route_coverage == 0.0


def test_route_coverage_falls_when_a_route_goes_dark(tmp_path):
    # MNL->PRG is the way home; losing it silently is exactly how a sweep keeps
    # reporting a healthy leg count while producing no complete trip.
    provider = FakeProvider(fail_on={"PRG"})
    result = run_sweep(scenario(), provider=provider, data_dir=tmp_path, workers=2, delay_s=0)
    assert 0.0 < result.route_coverage < 1.0
    status = json.loads((result.directory / "status.json").read_text())
    assert status["route_coverage"] == result.route_coverage
    assert status["routes_planned"] > status["routes_with_legs"]


def test_a_fully_covered_sweep_reports_complete_coverage(tmp_path):
    result = run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=2, delay_s=0)
    assert result.route_coverage == 1.0


# ------------------------------------------------------- observed_at back-fill


def test_loading_legs_written_before_timestamps_falls_back_to_the_sweep_stamp(tmp_path):
    """The four committed sweeps have no per-leg time; the directory name is
    the honest resolution, not None."""
    from src.sweep.runner import load_legs

    directory = tmp_path / "sweeps" / "old" / "2026-08-10T11-57-06Z"
    directory.mkdir(parents=True)
    payload = Leg(
        provider="PELIKAN", origin="PRG", destination="NRT", depart_date=date(2027, 1, 12),
        airline="QR", flight_number=None, stops=1, price_currency="CZK",
        price_amount=1.0, url="",
    ).to_dict()
    del payload["observed_at"]
    (directory / "legs.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert load_legs(directory)[0].observed_at == "2026-08-10T11:57:06+00:00"


def test_a_legs_own_timestamp_is_never_overwritten_by_the_fallback(tmp_path):
    from src.sweep.runner import load_legs

    directory = tmp_path / "sweeps" / "new" / "2026-08-10T11-57-06Z"
    directory.mkdir(parents=True)
    payload = Leg(
        provider="PELIKAN", origin="PRG", destination="NRT", depart_date=date(2027, 1, 12),
        airline="QR", flight_number=None, stops=1, price_currency="CZK",
        price_amount=1.0, url="", observed_at="2026-08-10T12:44:01+00:00",
    ).to_dict()
    (directory / "legs.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert load_legs(directory)[0].observed_at == "2026-08-10T12:44:01+00:00"
