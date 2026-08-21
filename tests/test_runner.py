"""Tests for the concurrent sweep runner.

A fake provider stands in for Playwright: these tests must never launch a
browser or touch the network.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date

import pytest

from src.models import Leg
from src.scenario import Scenario, Stop
from src.sweep.planner import plan_exploration, plan_searches, planned_routes, shard_of
from src.sweep.runner import (
    THROTTLE_STREAK,
    ShardMismatch,
    _chunk,
    is_comparable,
    load_legs,
    merge_shards,
    run_sweep,
)
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
        delay_s=0,
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


# ---------------------------------------------------------------- explore mode


def test_explore_mode_runs_the_exploration_plan(tmp_path):
    trip = scenario(depth="deep")
    result = run_sweep(
        trip, provider=FakeProvider(), data_dir=tmp_path, workers=2, delay_s=0, mode="explore"
    )
    assert result.total == len(plan_exploration(trip))
    assert result.total < len(plan_searches(trip))


def test_two_sweeps_started_in_the_same_second_do_not_share_a_directory(tmp_path):
    """The stamp names the directory and is only accurate to the second.

    Harmless when legs were written once at the end - the loser's results were
    simply replaced. Now that legs are appended as they are found, the second
    run opens the first run's file and truncates it mid-sweep.
    """
    first = run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=1, delay_s=0)
    second = run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=1, delay_s=0)
    assert first.directory != second.directory
    assert len(load_legs(first.directory)) == len(first.legs)


def test_the_mode_is_recorded_so_a_probe_is_never_mistaken_for_a_sweep(tmp_path):
    explore = run_sweep(
        scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=1, delay_s=0,
        mode="explore",
    )
    sweep = run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=1, delay_s=0)
    assert json.loads((explore.directory / "status.json").read_text())["mode"] == "explore"
    assert json.loads((sweep.directory / "status.json").read_text())["mode"] == "sweep"


# ------------------------------------------------- the trip a sweep searched


def test_a_sweep_records_the_trip_it_searched(tmp_path):
    """Without this a sweep is a pile of legs with no idea what was asked.

    A trip edited after the run is then read back over the old results: rows
    appear for airports that were never searched, and the ones that were get
    dropped. Two probes were spent on that before it was noticed.
    """
    trip = scenario()
    result = run_sweep(trip, provider=FakeProvider(), data_dir=tmp_path, workers=1, delay_s=0)
    snapshot = json.loads((result.directory / "scenario.json").read_text(encoding="utf-8"))
    assert Scenario.from_dict(snapshot) == trip


def test_the_recorded_trip_survives_a_run_that_never_finished(tmp_path):
    """Written before the first search, not after the last: a stopped or killed
    run is exactly the one whose contents most need explaining."""
    stop = threading.Event()
    result = run_sweep(
        scenario(), provider=StoppingProvider(stop, after=1), data_dir=tmp_path,
        workers=1, delay_s=0, stop=stop,
    )
    assert (result.directory / "scenario.json").exists()


def test_an_explore_run_is_never_plotted_beside_real_sweeps():
    """It covers every route and can look perfectly healthy while pricing three
    dates. Charting its best total against a deep sweep's is comparing a
    reconnaissance photo with a survey."""
    status = {"state": "done", "legs_per_search": 9.7, "mode": "explore"}
    assert not is_comparable(status, routes_covered=21, routes_planned=21)
    assert is_comparable({**status, "mode": "sweep"}, routes_covered=21, routes_planned=21)


# --------------------------------------------------------------------- stopping


class StoppingProvider(FakeProvider):
    """Asks for the sweep to stop once it has answered `after` searches."""

    def __init__(self, stop: threading.Event, after: int):
        super().__init__()
        self.stop = stop
        self.after = after

    def search_leg(self, page, origin, destination, depart, ret=None, adults=1):
        legs = super().search_leg(page, origin, destination, depart, ret, adults)
        if len(self.calls) >= self.after:
            self.stop.set()
        return legs


def test_stopping_halts_the_sweep_before_its_next_search(tmp_path):
    stop = threading.Event()
    provider = StoppingProvider(stop, after=3)
    result = run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0, stop=stop
    )
    assert result.completed == 3
    assert result.completed < result.total


def test_a_stopped_sweep_says_so_rather_than_claiming_to_be_done(tmp_path):
    stop = threading.Event()
    result = run_sweep(
        scenario(), provider=StoppingProvider(stop, after=3), data_dir=tmp_path,
        workers=1, delay_s=0, stop=stop,
    )
    assert json.loads((result.directory / "status.json").read_text())["state"] == "stopped"


def test_a_stopped_sweep_keeps_every_leg_it_had_already_found(tmp_path):
    stop = threading.Event()
    result = run_sweep(
        scenario(), provider=StoppingProvider(stop, after=3), data_dir=tmp_path,
        workers=1, delay_s=0, stop=stop,
    )
    rows = (result.directory / "legs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len([r for r in rows if r.strip()]) == 3


def test_a_stopped_sweep_is_not_comparable_with_a_finished_one():
    status = {"state": "stopped", "legs_per_search": 9.7, "mode": "sweep"}
    assert not is_comparable(status, routes_covered=21, routes_planned=21)


class WatchingProvider(FakeProvider):
    """Counts the legs already on disk each time it is asked for another."""

    def __init__(self, directory_holder: dict):
        super().__init__()
        self.holder = directory_holder
        self.rows_seen: list[int] = []

    def search_leg(self, page, origin, destination, depart, ret=None, adults=1):
        path = self.holder.get("directory")
        if path is not None:
            legs_file = path / "legs.jsonl"
            text = legs_file.read_text(encoding="utf-8") if legs_file.exists() else ""
            self.rows_seen.append(len([line for line in text.splitlines() if line.strip()]))
        return super().search_leg(page, origin, destination, depart, ret, adults)


def test_legs_reach_disk_while_the_sweep_is_still_running(tmp_path):
    """Otherwise stopping - or a crash, or a restart - costs the whole run.

    A deep sweep runs 97 minutes and used to write `legs.jsonl` once, at the
    very end.
    """
    holder: dict = {}
    provider = WatchingProvider(holder)

    def on_progress(done, total, label):
        holder["directory"] = next((tmp_path / "sweeps" / "test-scenario").iterdir())

    run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0,
        on_progress=on_progress,
    )
    assert max(provider.rows_seen) > 0, "legs.jsonl was empty for the whole run"


# ------------------------------------------------------- refusing to grind
#
# The 11 Aug local sweep spent 4.5 hours to complete 245 of 615 searches, 125 of
# them timeouts, and would have kept going. 93% of its worker time went on
# searches that returned nothing: a timeout waits 120s, is retried immediately
# into the same throttle for another 120s, and nothing ever concludes that the
# site is simply refusing this client.


class TimeoutProvider(FakeProvider):
    """Times out on the first `failures` searches, then answers normally."""

    def __init__(
        self, failures: int, fail_routes: set[str] | None = None, takes: float = 0.0
    ):
        super().__init__()
        self.failures = failures
        self.fail_routes = fail_routes
        # A real search costs seconds; this one costs less than the clock can
        # resolve on Windows, so a test asking "did anything happen during the
        # pause" got every event stamped at the same instant.
        self.takes = takes
        self.attempts: list[tuple[str, str, date]] = []
        # When each search *began*, which is the only way to ask whether the
        # client was quiet during a pause. `attempts` says what was searched;
        # a pause is a claim about when.
        self.started: list[float] = []

    def search_leg(self, page, origin, destination, depart, ret=None, adults=1):
        from src.providers.pelikan import SearchTimeout

        self.attempts.append((origin, destination, depart))
        self.started.append(time.monotonic())
        if self.takes:
            time.sleep(self.takes)
        route = f"{origin}->{destination}"
        wanted = self.fail_routes is None or route in self.fail_routes
        if wanted and self.failures > 0:
            self.failures -= 1
            raise SearchTimeout(f"{route} {depart}: no results within 120s")
        return super().search_leg(page, origin, destination, depart, ret, adults)


def test_a_timed_out_search_is_retried_after_the_others_not_immediately(tmp_path):
    """Retrying a timeout on the spot doubles the load at the worst moment.

    It also doubles what a failure costs, from ~124s to ~248s. The retry is
    still worth having - a genuinely transient timeout recovers - but only once
    the site has had the rest of the chunk to breathe.
    """
    provider = TimeoutProvider(failures=1, fail_routes={"PRG->NRT"})
    run_sweep(scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0)

    first = provider.attempts[0]
    assert first[:2] == ("PRG", "NRT")
    # The same search appears again, but not as the second thing attempted.
    assert provider.attempts[1] != first
    assert first in provider.attempts[1:], "the timed-out search was never retried"


def test_a_deferred_retry_that_succeeds_keeps_its_legs(tmp_path):
    provider = TimeoutProvider(failures=1, fail_routes={"PRG->NRT"})
    result = run_sweep(scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0)
    assert result.route_legs["PRG->NRT"] > 0
    assert result.errors == []


def test_a_wall_of_timeouts_ends_the_sweep_instead_of_grinding_on(tmp_path):
    """The behaviour the 4.5-hour run needed and did not have.

    A deep trip, so there is a long way left to grind when the site starts
    refusing - which is exactly the situation that cost 4.5 hours.
    """
    provider = TimeoutProvider(failures=10_000)
    result = run_sweep(
        scenario(depth="deep"), provider=provider, data_dir=tmp_path,
        workers=1, delay_s=0, backoff_s=[0, 0],
    )
    assert result.throttled
    assert result.total > 50, "the trip must be big enough to have something to abandon"
    # Three streaks of five is all it takes to reach a verdict, against a plan
    # of dozens. At the real pace that is ~25 minutes rather than 4.5 hours.
    assert len(provider.attempts) <= THROTTLE_STREAK * 3
    assert result.completed == 0


def test_a_throttled_sweep_says_the_site_refused_not_that_it_broke(tmp_path):
    """`unhealthy` means the scraper is broken and someone must fix a selector.

    A throttled sweep needs no fix at all - it needs to be run later, or from
    somewhere else - and reporting the two the same way sends you debugging code
    that is working.
    """
    provider = TimeoutProvider(failures=10_000)
    result = run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0, backoff_s=[0, 0],
    )
    status = json.loads((result.directory / "status.json").read_text())
    assert status["state"] == "throttled"


def test_the_breaker_pauses_before_it_gives_up(tmp_path):
    """Backing off is the cheap move: a 2-minute pause costs less than one
    timed-out search, and may be all the site wants."""
    waits: list[float] = []
    provider = TimeoutProvider(failures=10_000)
    run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0,
        backoff_s=[0, 0, 0], on_backoff=waits.append,
    )
    assert waits == [0, 0, 0], waits


def test_a_pause_holds_every_worker_not_only_the_one_that_tripped_it(tmp_path):
    """A pause one worker sleeps through while another searches is not a pause.

    This is what made the ladder useless in practice. The worker that recorded
    the fifth timeout was handed the sleep and took it alone; the other kept
    searching the whole time, so the client was never quiet and the site had
    nothing to forgive. Measured on 21 Aug: three local runs in a row reached
    the end of the ladder without the site ever having had two minutes off.
    """
    opened: list[float] = []
    provider = TimeoutProvider(failures=10_000, takes=0.02)
    run_sweep(
        scenario(depth="deep"), provider=provider, data_dir=tmp_path,
        workers=2, delay_s=0, backoff_s=[0.5, 0.5, 0.5],
        on_backoff=lambda _s: opened.append(time.monotonic()),
    )

    assert opened, "the breaker never paused"
    # Searches that *began* inside the pause. One already in flight when it
    # opened is fair - it cannot be recalled - so the window opens a search's
    # length after the pause did.
    started, ends = opened[0] + provider.takes, opened[0] + 0.45
    during = [t for t in provider.started if started < t < ends]
    assert during == [], f"{len(during)} searches were made during the pause"


def test_a_rung_is_not_spent_while_another_worker_is_still_waiting(tmp_path):
    """Two workers must not burn two rungs of the ladder between them.

    With the pause held by one worker only, the other could reach the next
    level before the first had finished sleeping - so 2 + 5 + 15 minutes of
    intended quiet was spent in about the time the longest one alone should
    have taken.
    """
    opened: list[float] = []
    provider = TimeoutProvider(failures=10_000, takes=0.02)
    run_sweep(
        scenario(depth="deep"), provider=provider, data_dir=tmp_path,
        workers=2, delay_s=0, backoff_s=[0.4, 0.4],
        on_backoff=lambda _s: opened.append(time.monotonic()),
    )

    assert len(opened) >= 2, "the ladder never reached its second rung"
    assert opened[1] - opened[0] >= 0.4, (
        "the second rung was spent before the first pause had finished"
    )


def test_a_run_that_recovers_after_a_pause_is_not_marked_throttled(tmp_path):
    """A bad patch is not a refusal. Only failing again after the longest pause
    is evidence enough to abandon the run."""
    provider = TimeoutProvider(failures=THROTTLE_STREAK)
    result = run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0, backoff_s=[0, 0],
    )
    assert not result.throttled
    assert result.completed == result.total
    assert json.loads((result.directory / "status.json").read_text())["state"] == "done"


def test_scattered_timeouts_never_trip_the_breaker(tmp_path):
    """One search in three failing is a bad day, not a wall. The breaker must
    count *consecutive* failures or it would abandon runs worth finishing."""
    provider = TimeoutProvider(failures=10_000, fail_routes={"NRT->MNL"})
    result = run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0, backoff_s=[0, 0],
    )
    assert not result.throttled
    assert result.completed == result.total


# ----------------------------------------------------------------- time budget
#
# Five nightly cloud runs in a row were cancelled at 1h30m and committed
# nothing: the job timeout killed them mid-sweep, before the commit step. A
# sweep that knows its own budget stops itself in time, and the results it
# already has get written back.


class UnhurriedProvider(FakeProvider):
    """Answers, but slowly enough that a budget can expire mid-sweep."""

    def __init__(self, seconds: float = 0.005):
        super().__init__()
        self.seconds = seconds

    def search_leg(self, page, origin, destination, depart, ret=None, adults=1):
        time.sleep(self.seconds)
        return super().search_leg(page, origin, destination, depart, ret, adults)


def test_a_budget_ends_the_sweep_and_keeps_what_it_found(tmp_path):
    from src.cli import run_sweep_command

    # `run_sweep_command` loads the live `scenarios/japan-philippines.json`, which
    # is edited through the UI - it planned 63 explore searches when this was
    # written and 9 after the trip was narrowed to one origin and a pinned
    # crossing. So the margin is made per-search rather than across the plan:
    # one search alone overruns the 60 ms budget, which holds at any plan size.
    result = run_sweep_command(
        "japan-philippines", None, dry_run=False, mode="explore",
        max_minutes=0.001, provider=UnhurriedProvider(0.08), data_dir=tmp_path,
        delay_s=0, notify=False,
    )
    assert result.stopped
    assert result.completed < result.total
    status = json.loads((result.directory / "status.json").read_text())
    assert status["state"] == "stopped"


def test_without_a_budget_the_sweep_runs_to_the_end(tmp_path):
    from src.cli import run_sweep_command

    result = run_sweep_command(
        "japan-philippines", None, dry_run=False, mode="explore",
        max_minutes=None, provider=FakeProvider(), data_dir=tmp_path, delay_s=0,
        notify=False,
    )
    assert not result.stopped
    assert result.completed == result.total


# ------------------------------------------------------------- route accounting
#
# Attempts per route are what separate "nothing is sold on this route" from "we
# never got an answer". `viability.route_stats` has always read them out of
# status.json; they were never written there, so every route counted zero
# attempts and no route could ever be judged dead.


def test_status_records_the_attempts_and_legs_of_every_route(tmp_path):
    result = run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=2, delay_s=0)
    status = json.loads((result.directory / "status.json").read_text())
    assert status["route_searches"] == result.route_searches
    assert status["route_legs"] == result.route_legs
    assert sum(status["route_searches"].values()) == result.total


def test_a_route_that_only_ever_failed_still_records_its_attempts(tmp_path):
    """The distinction the report rests on: asked repeatedly, never answered."""
    result = run_sweep(
        scenario(), provider=FakeProvider(fail_on={"MNL"}), data_dir=tmp_path,
        workers=2, delay_s=0,
    )
    status = json.loads((result.directory / "status.json").read_text())
    assert status["route_searches"]["NRT->MNL"] > 0
    assert status["route_legs"]["NRT->MNL"] == 0


def test_status_counts_failures_per_route_not_just_the_last_twenty(tmp_path):
    """`errors` keeps 20 messages; a starved sweep produces hundreds.

    Without a count per route, a route that timed out every time is
    indistinguishable from one the site answered with an empty page - and those
    two mean opposite things.
    """
    result = run_sweep(
        scenario(), provider=FakeProvider(fail_on={"MNL"}), data_dir=tmp_path,
        workers=2, delay_s=0,
    )
    status = json.loads((result.directory / "status.json").read_text())
    assert status["route_errors"]["NRT->MNL"] == status["route_searches"]["NRT->MNL"]
    assert status["route_errors"].get("PRG->NRT", 0) == 0
    assert sum(status["route_errors"].values()) == status["error_count"]


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


# ------------------------------------------------------------------ coverage
#
# "Did this sweep ask everything it planned to ask?" - the question `state:
# done`, `legs_found` and `error_count` between them could never answer. A route
# that answered on nine dates and never on the tenth reports perfect health on
# every per-route figure while having a hole in the grid.


def test_a_clean_sweep_answers_everything_it_planned(tmp_path):
    result = run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=2, delay_s=0)
    assert result.answered == result.total
    assert result.coverage == 1.0


def test_coverage_falls_when_a_search_is_never_answered(tmp_path):
    provider = FakeProvider(fail_on={"MNL"})
    result = run_sweep(scenario(), provider=provider, data_dir=tmp_path, workers=2, delay_s=0)
    assert result.answered < result.total
    assert 0 < result.coverage < 1.0
    assert result.answered == result.total - len(result.errors)


def test_every_search_writes_a_line_whatever_the_outcome(tmp_path):
    """Legs alone cannot say which dates were asked about, so a date the site
    had nothing on and a date that was never searched read the same."""
    provider = FakeProvider(fail_on={"MNL"})
    result = run_sweep(provider=provider, scenario=scenario(), data_dir=tmp_path, workers=2, delay_s=0)
    rows = [
        json.loads(line)
        for line in (result.directory / "searches.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == result.completed
    assert sum(1 for row in rows if row["answered"]) == result.answered
    assert {row["origin"] for row in rows} >= {"PRG"}


def test_status_records_coverage_for_a_later_reader(tmp_path):
    result = run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=2, delay_s=0)
    status = json.loads((result.directory / "status.json").read_text(encoding="utf-8"))
    assert status["answered"] == status["planned"] == result.total
    assert status["coverage"] == 1.0


# ---------------------------------------------------------------- fill passes


class FlakyProvider(FakeProvider):
    """Fails a route the first `times` attempts, then answers normally."""

    def __init__(self, flaky: str, times: int):
        super().__init__()
        self.flaky = flaky
        self.times = times
        self.seen: dict[tuple, int] = {}

    def search_leg(self, page, origin, destination, depart, ret=None, adults=1):
        key = (origin, destination, depart)
        self.seen[key] = self.seen.get(key, 0) + 1
        if destination == self.flaky and self.seen[key] <= self.times:
            self.calls.append(key)
            raise RuntimeError("transient")
        return super().search_leg(page, origin, destination, depart, ret, adults)


def test_a_search_that_fails_twice_is_still_answered_on_the_third_pass(tmp_path):
    """One retry was not enough to call a sweep complete.

    A search that failed twice, or failed with anything other than a timeout,
    used to be dropped for good - a permanent hole in the date grid that no
    downstream figure could see.
    """
    provider = FlakyProvider(flaky="MNL", times=2)
    result = run_sweep(scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0)
    assert result.coverage == 1.0
    assert not result.errors


def test_a_search_that_never_answers_is_counted_once_not_once_per_pass(tmp_path):
    """Retries must not inflate `completed` past the number of searches."""
    provider = FakeProvider(fail_on={"MNL"})
    result = run_sweep(scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0)
    assert result.completed == result.total


def test_retries_cost_only_what_is_still_failing(tmp_path):
    """Each pass is smaller than the last, so the bound is the failures."""
    provider = FakeProvider(fail_on={"MNL"})
    result = run_sweep(scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0)
    failing = sum(1 for origin, destination, _ in provider.calls if destination == "MNL")
    clean = sum(1 for origin, destination, _ in provider.calls if destination != "MNL")
    assert clean == result.total - len(result.errors)
    assert failing == len(result.errors) * 3


# --------------------------------------------------------------------- shards
#
# A deep sweep is split across several cloud runners so it finishes inside its
# budget without asking pelikan.cz for anything faster than the rate that has
# measured zero timeouts. Each runner is a separate address, so the per-address
# load is unchanged - only the wall clock moves.


def test_shards_partition_the_plan_exactly():
    """No search dropped and none run twice, or coverage means nothing."""
    plan = plan_searches(make_scenario(depth="deep"))
    pieces = [shard_of(plan, index, 3) for index in range(3)]
    assert sum(len(piece) for piece in pieces) == len(plan)
    assert [s for piece in pieces for s in piece].count(plan[0]) == 1
    assert set().union(*(set(piece) for piece in pieces)) == set(plan)


def test_every_shard_gets_a_spread_of_routes():
    """A contiguous slice would hand one runner most of a single route, leaving
    it unable to tell a dead route from its own bad luck."""
    plan = plan_searches(make_scenario(depth="deep"))
    routes = {(s.origin, s.destination) for s in plan}
    for index in range(3):
        assert {(s.origin, s.destination) for s in shard_of(plan, index, 3)} == routes


def test_one_shard_of_one_is_the_whole_plan():
    plan = plan_searches(make_scenario())
    assert shard_of(plan, 0, 1) == plan


def test_an_impossible_shard_is_refused_rather_than_swept():
    """A typo here silently sweeps a fraction of the trip and reports success."""
    plan = plan_searches(make_scenario())
    for index, count in ((0, 0), (3, 3), (-1, 3)):
        with pytest.raises(ValueError):
            shard_of(plan, index, count)


def test_a_sharded_run_searches_only_its_share(tmp_path):
    provider = FakeProvider()
    result = run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0, shard=(0, 3)
    )
    whole = len(plan_searches(scenario()))
    assert result.total == len(shard_of(plan_searches(scenario()), 0, 3))
    assert result.total < whole


def test_a_shard_records_the_whole_plan_it_is_a_share_of(tmp_path):
    """Its own coverage is over its share; `planned` says what the share is of,
    so the merge can report against the trip rather than against the shard."""
    result = run_sweep(
        scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=1, delay_s=0, shard=(1, 3)
    )
    status = json.loads((result.directory / "status.json").read_text(encoding="utf-8"))
    assert status["shard"] == [1, 3]
    assert status["planned"] == len(plan_searches(scenario()))
    assert status["total"] == result.total < status["planned"]


# --------------------------------------------------------------- merging them


def run_shards(tmp_path, count=3, provider=None, **kwargs):
    return [
        run_sweep(
            scenario(),
            provider=provider or FakeProvider(),
            data_dir=tmp_path / f"shard{index}",
            workers=1,
            delay_s=0,
            shard=(index, count),
            **kwargs,
        ).directory
        for index in range(count)
    ]


def test_merging_shards_reconstructs_the_whole_sweep(tmp_path):
    shards = run_shards(tmp_path)
    status = merge_shards(shards, tmp_path / "merged")
    whole = len(plan_searches(scenario()))
    assert status["total"] == status["answered"] == status["planned"] == whole
    assert status["coverage"] == 1.0
    assert len(load_legs(tmp_path / "merged")) == whole


def test_merging_sums_the_per_route_counters(tmp_path):
    shards = run_shards(tmp_path)
    status = merge_shards(shards, tmp_path / "merged")
    assert sum(status["route_searches"].values()) == status["completed"]
    assert set(status["route_searches"]) == {
        f"{origin}->{destination}" for origin, destination in planned_routes(scenario())
    }
    assert not status["routes_with_no_results"]


def test_a_merged_sweep_is_only_as_good_as_its_unhappiest_shard(tmp_path):
    """Calling the whole thing done because two of three finished is how a
    starved sweep gets charted as a price."""
    shards = run_shards(tmp_path)
    broken = json.loads((shards[1] / "status.json").read_text(encoding="utf-8"))
    broken["state"] = "throttled"
    (shards[1] / "status.json").write_text(json.dumps(broken), encoding="utf-8")
    assert merge_shards(shards, tmp_path / "merged")["state"] == "throttled"


def test_merging_shards_of_different_trips_is_refused(tmp_path):
    """Possible whenever a run is dispatched mid-edit: two runners check out
    different commits, and the merge would invent a trip that never existed."""
    shards = run_shards(tmp_path, count=2)
    other = json.loads((shards[1] / "scenario.json").read_text(encoding="utf-8"))
    other["origins"] = ["KTW"]
    (shards[1] / "scenario.json").write_text(json.dumps(other), encoding="utf-8")
    with pytest.raises(ShardMismatch, match="different trips"):
        merge_shards(shards, tmp_path / "merged")


def test_a_shard_whose_job_died_does_not_read_as_a_clean_one(tmp_path):
    shards = run_shards(tmp_path)
    (shards[2] / "status.json").unlink()
    status = merge_shards(shards, tmp_path / "merged")
    assert status["state"] != "done"
    assert status["coverage"] < 1.0


def test_the_merged_sweep_carries_the_shard_roll_call(tmp_path):
    """A run that lost a shard must say so, not report a smaller sweep that
    looks complete."""
    status = merge_shards(run_shards(tmp_path), tmp_path / "merged")
    assert sorted(status["shards"]) == [[0, 3], [1, 3], [2, 3]]
    assert status["shard"] is None


def test_a_lost_shard_thins_every_route_rather_than_deleting_some(tmp_path):
    """The failure this deal is shaped around.

    A shard that is throttled and dies takes its searches with it. If shards
    owned whole routes, those routes would vanish from the merged sweep and read
    downstream as dead routes - the exact confusion between breakage and a quiet
    market this project keeps having to design against.
    """
    shards = run_shards(tmp_path)
    status = merge_shards(shards[:2], tmp_path / "merged")
    assert status["coverage"] < 1.0
    assert not status["routes_with_no_results"]
    assert set(status["route_searches"]) == {
        f"{origin}->{destination}" for origin, destination in planned_routes(scenario())
    }


def test_shards_still_deal_every_route_when_dates_are_few():
    """The exploration plan is three dates a route; three shards get one each."""
    plan = plan_exploration(make_scenario())
    routes = {(s.origin, s.destination) for s in plan}
    for index in range(3):
        assert {(s.origin, s.destination) for s in shard_of(plan, index, 3)} == routes


def test_a_committed_sweep_is_not_mistaken_for_a_shard(tmp_path):
    """What actually went wrong on the first sharded run.

    The upload step took all of `data/sweeps/<id>/`, so the merge was handed 24
    directories - seven of them committed sweeps from a fortnight earlier, which
    have a status.json and no scenario.json. Merging those would have summed a
    fortnight of prices into one run and reported it as tonight's.
    """
    shards = run_shards(tmp_path, count=2)
    old = tmp_path / "history" / "2026-08-06T18-08-34Z"
    old.mkdir(parents=True)
    (old / "status.json").write_text(json.dumps({"state": "done"}), encoding="utf-8")
    with pytest.raises(ShardMismatch, match="not shards of one run"):
        merge_shards([*shards, old], tmp_path / "merged")


# ------------------------------------------------------- browser recycling
#
# pelikan.cz stops answering a browser session after about sixty searches. Two
# cloud sweeps on 20 Aug were cut off after a hard cliff - a steady 10s per
# search, then nothing - at 120 searches on two workers, whether they ran on one
# runner or three. 60 per session both times.


class CountingProvider(FakeProvider):
    """Records how many searches each browser page was asked for.

    Pages are tagged rather than identified by `id()`: a closed page is dropped
    immediately, and CPython happily hands the same address to the next one, so
    counting by id would silently merge two sessions into one.
    """

    NAME = "FAKE"

    def __init__(self, dies_after: int | None = None):
        super().__init__()
        self.dies_after = dies_after
        self.per_page: dict[int, int] = {}

    def search_leg(self, page, origin, destination, depart, ret=None, adults=1):
        tag = getattr(page, "_tag", None)
        if tag is None:
            tag = len(self.per_page) + 1
            page._tag = tag
        seen = self.per_page.get(tag, 0) + 1
        self.per_page[tag] = seen
        # Stands in for the site refusing a session that has asked too much.
        if self.dies_after is not None and seen > self.dies_after:
            from src.providers.pelikan import SearchTimeout

            raise SearchTimeout("session exhausted")
        return super().search_leg(page, origin, destination, depart, ret, adults)


def deep():
    """Enough searches that a session limit is actually reached."""
    return scenario(depth="deep")


def test_a_worker_reuses_one_browser_between_recycles(tmp_path):
    """A browser launch per search would dominate the cost of a sweep."""
    provider = CountingProvider()
    run_sweep(deep(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0, recycle_after=0)
    assert len(provider.per_page) == 1


def test_the_browser_is_replaced_once_it_has_asked_enough(tmp_path):
    provider = CountingProvider()
    result = run_sweep(
        deep(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0, recycle_after=10
    )
    assert result.total > 20, "needs enough searches to recycle more than once"
    assert len(provider.per_page) == -(-result.total // 10)
    assert max(provider.per_page.values()) <= 10


def test_recycling_carries_a_sweep_past_a_session_limit(tmp_path):
    """The whole point. Two cloud sweeps on 20 Aug were cut off after a hard
    cliff - a steady 10s per search, then nothing - at 60 searches per session,
    whether they ran on one runner or three."""
    provider = CountingProvider(dies_after=10)
    result = run_sweep(
        deep(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0,
        recycle_after=10, backoff_s=(0, 0, 0),
    )
    assert result.coverage == 1.0
    assert not result.throttled


def test_without_recycling_a_session_limit_stops_the_sweep_dead(tmp_path):
    """The behaviour being fixed, pinned so it cannot come back unnoticed.

    Note what it reports: `error_count` stays 0, because the breaker abandons
    what is left rather than attempting and failing it. Coverage is the only
    figure that shows the hole.
    """
    provider = CountingProvider(dies_after=10)
    result = run_sweep(
        deep(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0,
        recycle_after=0, backoff_s=(0, 0, 0),
    )
    assert result.throttled
    assert result.coverage < 1.0
    assert not result.errors


# ------------------------------------------------------------- watch mode
#
# A watch is the same runner on a much smaller plan, writing somewhere else.
# Somewhere else matters: a watch run landing in data/sweeps/ would put six tiny
# runs a day into the Results picker, each of them a perfectly healthy-looking
# sweep that priced three days out of seventy.


def watched_scenario(**overrides) -> Scenario:
    from src.scenario import Watch

    defaults = dict(
        watches=[
            Watch(depart_dates=[date(2027, 1, 10), date(2027, 1, 20), date(2027, 1, 30)]),
        ]
    )
    defaults.update(overrides)
    return scenario(**defaults)


def test_a_watch_run_searches_only_the_pinned_dates(tmp_path):
    provider = FakeProvider()
    run_sweep(watched_scenario(), provider=provider, data_dir=tmp_path, delay_s=0, mode="watch")
    assert {call[2] for call in provider.calls} == {
        date(2027, 1, 10), date(2027, 1, 20), date(2027, 1, 30)
    }


def test_a_watch_run_stays_out_of_the_sweep_history(tmp_path):
    result = run_sweep(
        watched_scenario(), provider=FakeProvider(), data_dir=tmp_path, delay_s=0, mode="watch"
    )
    assert (tmp_path / "watch" / "test-scenario").exists()
    assert not (tmp_path / "sweeps").exists()
    assert result.directory.parent.parent.name == "watch"


def test_a_watch_run_records_what_it_was_watching(tmp_path):
    result = run_sweep(
        watched_scenario(), provider=FakeProvider(), data_dir=tmp_path, delay_s=0, mode="watch"
    )
    status = json.loads((result.directory / "status.json").read_text(encoding="utf-8"))
    assert status["mode"] == "watch"
    assert status["watches"] == [["2027-01-10", "2027-01-20", "2027-01-30"]]


def test_a_sweep_records_that_it_was_watching_nothing(tmp_path):
    result = run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, delay_s=0)
    status = json.loads((result.directory / "status.json").read_text(encoding="utf-8"))
    assert status["watches"] == []


def test_a_watch_is_never_plotted_beside_a_sweep():
    """It prices three days out of seventy, so its cheapest is not the trip's.

    The same trap an exploration pass sets, and caught the same way: a watch can
    post a perfectly healthy legs-per-search while having looked at almost
    nothing, and charting it draws a step no fare ever made.
    """
    status = {"state": "done", "mode": "watch", "legs_per_search": 9.5}
    assert is_comparable(status, 4, 4) is False


def test_an_unknown_mode_is_refused(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        run_sweep(scenario(), provider=FakeProvider(), data_dir=tmp_path, mode="wathc")


# ------------------------------------------------------- saying it is waiting
#
# The runner already knew it was waiting out a refusal; it just never wrote it
# down. A probe sat at 80/126 for thirteen minutes while the page showed a green
# "running" dot and "~11 min left", because the only thing that reaches the page
# is status.json and the backoff never reached status.json.


def test_a_backoff_is_written_down_while_it_is_happening(tmp_path):
    """Written when the wait starts, not when it ends.

    Writing it afterwards would describe a fifteen-minute silence only once the
    silence was over, which is exactly the stretch that needs explaining.
    """
    seen: list[dict] = []
    provider = TimeoutProvider(failures=10_000)

    def peek(_seconds):
        directory = next((tmp_path / "sweeps").glob("*/*"))
        seen.append(json.loads((directory / "status.json").read_text()))

    run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0,
        # Short, but not zero: a zero wait has no end to count down to, and the
        # thing being tested is that the end is written where the page can read it.
        backoff_s=[0.05, 0.05], on_backoff=peek,
    )
    assert seen, "the breaker never backed off"
    assert seen[0]["backoff_seconds"] == 0.05
    assert seen[0]["backoff_until"], "no time for the page to count down to"


def test_a_search_that_answers_clears_the_waiting_notice(tmp_path):
    """Otherwise the banner outlives the wait and the run looks stuck while it
    is working."""
    # Fails enough to trip one backoff, then answers everything after it.
    provider = TimeoutProvider(failures=THROTTLE_STREAK)
    result = run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0,
        backoff_s=[0, 0],
    )
    status = json.loads((result.directory / "status.json").read_text())
    assert not status["backoff_until"]
    assert not status["backoff_seconds"]


def test_a_finished_run_is_never_left_looking_like_it_is_waiting(tmp_path):
    provider = TimeoutProvider(failures=10_000)
    result = run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0,
        backoff_s=[0, 0],
    )
    status = json.loads((result.directory / "status.json").read_text())
    assert status["state"] == "throttled"
    assert not status["backoff_until"]


def test_stop_cuts_a_backoff_short_instead_of_waiting_it_out(tmp_path):
    """Stop was a lie for up to fifteen minutes.

    `time.sleep(900)` cannot be interrupted, so the button reported "finishing
    the search in flight" while nothing was in flight and nothing would be for
    a quarter of an hour.
    """
    import threading
    import time as time_module

    stop = threading.Event()
    provider = TimeoutProvider(failures=10_000)

    started = time_module.monotonic()
    run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0,
        # Long enough that waiting it out would be unmistakable in the elapsed time.
        backoff_s=[30, 30], on_backoff=lambda _s: stop.set(), stop=stop,
    )
    assert time_module.monotonic() - started < 10, "the backoff was slept through"


# ------------------------------------------------------- carrying on a run
#
# The site answers about 120 searches from one client. A probe that is refused
# at 80 of 126 has 80 answers worth keeping, and re-asking them to get the
# remaining 46 spends the very budget that ran out. Resuming asks only for what
# is missing, and produces one directory holding the whole run - not a pair the
# reader has to add up.


def stopped_partway(tmp_path, after=4):
    """A run that stopped with part of its plan unasked, and its directory."""
    stop = threading.Event()
    provider = FakeProvider()
    original = provider.search_leg

    def search_leg(*args, **kwargs):
        if len(provider.calls) >= after - 1:
            stop.set()
        return original(*args, **kwargs)

    provider.search_leg = search_leg
    result = run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0, stop=stop,
    )
    assert result.completed < result.total, "the fixture did not stop early"
    return result


def test_resuming_asks_only_for_what_was_never_answered(tmp_path):
    first = stopped_partway(tmp_path)
    provider = FakeProvider()
    run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0,
        resume_from=first.directory,
    )
    asked_again = {(o, d, when) for o, d, when in provider.calls}
    already = {
        (row["origin"], row["destination"], date.fromisoformat(row["depart_date"]))
        for row in _rows(first.directory / "searches.jsonl")
        if row["answered"]
    }
    assert already, "the fixture answered nothing, so there is nothing to skip"
    assert not (asked_again & already), "a resumed run re-asked what it already had"


def test_a_resumed_run_holds_the_whole_plan_when_it_finishes(tmp_path):
    """One directory, not two to add up. The picker lists directories, and a
    run split across two of them reads as two short runs."""
    first = stopped_partway(tmp_path)
    second = run_sweep(
        scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=1, delay_s=0,
        resume_from=first.directory,
    )
    status = json.loads((second.directory / "status.json").read_text())
    assert status["answered"] == status["planned"]
    assert status["coverage"] == 1.0
    assert len(_rows(second.directory / "searches.jsonl")) == status["planned"]


def test_a_resumed_run_keeps_the_flights_the_first_one_found(tmp_path):
    """Truncating them is the easy mistake: the ledger is opened for writing."""
    first = stopped_partway(tmp_path)
    kept = len(_rows(first.directory / "legs.jsonl"))
    assert kept, "the fixture found nothing, so there is nothing to lose"
    second = run_sweep(
        scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=1, delay_s=0,
        resume_from=first.directory,
    )
    assert len(_rows(second.directory / "legs.jsonl")) > kept


def test_a_resumed_run_counts_the_earlier_searches_from_its_first_status(tmp_path):
    """Otherwise it opens at 0/126 and looks like the 80 were thrown away."""
    first = stopped_partway(tmp_path)
    before = json.loads((first.directory / "status.json").read_text())
    seen: list[int] = []
    run_sweep(
        scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=1, delay_s=0,
        resume_from=first.directory,
        on_progress=lambda done, total, label: seen.append(done),
    )
    assert seen[0] > before["completed"], "the resumed run restarted the count"


def test_a_search_that_failed_is_asked_again_rather_than_skipped(tmp_path):
    """A timeout is exactly the thing a later run should retry. It is recorded
    in the ledger like any other search, so skipping every recorded row would
    make a refusal permanent."""
    provider = FakeProvider(fail_on={"MNL"})
    first = run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0,
    )
    assert first.errors, "the fixture failed nothing"
    again = FakeProvider()
    run_sweep(
        scenario(), provider=again, data_dir=tmp_path, workers=1, delay_s=0,
        resume_from=first.directory,
    )
    assert any(d == "MNL" for _o, d, _when in again.calls), "the failed route was never retried"


def test_a_resumed_run_says_which_run_it_continues(tmp_path):
    first = stopped_partway(tmp_path)
    second = run_sweep(
        scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=1, delay_s=0,
        resume_from=first.directory,
    )
    status = json.loads((second.directory / "status.json").read_text())
    assert status["resumed_from"] == first.directory.name


def test_resuming_a_run_that_answered_everything_asks_for_nothing(tmp_path):
    first = run_sweep(
        scenario(), provider=FakeProvider(), data_dir=tmp_path, workers=1, delay_s=0,
    )
    provider = FakeProvider()
    second = run_sweep(
        scenario(), provider=provider, data_dir=tmp_path, workers=1, delay_s=0,
        resume_from=first.directory,
    )
    assert provider.calls == []
    # And still reports the whole plan rather than a run of nothing.
    status = json.loads((second.directory / "status.json").read_text())
    assert status["answered"] == status["planned"]


def _rows(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------------------------------- how the work is split up
#
# Two bugs have lived in this split. `searches[i::workers]` put every worker on
# the same origin-destination pair at the same moment, and the fix - contiguous
# runs - was correct only while the plan was emitted leg by leg. Now that it is
# dealt across routes so a run cut short still chains, contiguous runs advance
# through the routes in step: measured on the real trip, two workers shared a
# route at 33 of 33 steps.


def routes_at_each_step(plan, workers):
    """How often the workers are on the same route at the same moment."""
    parts = _chunk(plan, workers)
    steps = min(len(part) for part in parts)
    return sum(
        1
        for index in range(steps)
        if len({(part[index].origin, part[index].destination) for part in parts})
        < len(parts)
    ), steps


def test_workers_do_not_advance_through_the_routes_in_step():
    plan = plan_searches(scenario(depth="deep"))
    for workers in (2, 3):
        together, steps = routes_at_each_step(plan, workers)
        assert together == 0, f"{workers} workers shared a route {together}/{steps} times"


def test_splitting_still_partitions_the_plan_exactly():
    """Staggering rotates a worker's chunk; it must never change what is in it.
    Coverage and the shard arithmetic are both counted off this."""
    plan = plan_searches(scenario(depth="deep"))
    for workers in (1, 2, 3, 5):
        pieces = [search for part in _chunk(plan, workers) for search in part]
        assert len(pieces) == len(plan)
        assert set(pieces) == set(plan)


def test_more_workers_than_routes_still_splits_rather_than_refusing():
    """The collision is unavoidable at that point - there are not enough routes
    to go round - and pretending otherwise would only move it."""
    plan = plan_searches(scenario(depth="deep"))
    parts = _chunk(plan, 40)
    assert sum(len(part) for part in parts) == len(plan)


def test_an_empty_plan_produces_no_workers():
    assert _chunk([], 4) == []
