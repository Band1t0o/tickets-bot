"""Which trips a scheduled run sweeps.

This decision used to be a shell loop in the workflow, where "skip the focused
slot when nothing is focused" could not be expressed and nothing could be
tested. Getting it wrong is expensive in both directions: too eager and the
afternoon follows a throttled morning with more load, too shy and a night
produces nothing at all.
"""
from __future__ import annotations

import json
from datetime import timedelta

from scripts.plan_sweep import choose
from src.scenario import save_scenario
from tests.conftest import WINDOW_START, make_scenario


def trips(tmp_path, *scenarios):
    directory = tmp_path / "scenarios"
    for scenario in scenarios:
        save_scenario(scenario, directory)
    return directory


def focused(**overrides):
    return make_scenario(
        focus_start=WINDOW_START + timedelta(days=7),
        focus_end=WINDOW_START + timedelta(days=11),
        **overrides,
    )


def healthy_sweep(tmp_path, scenario_id, **status):
    directory = tmp_path / "data" / "sweeps" / scenario_id / "2026-08-20T02-00-00Z"
    directory.mkdir(parents=True)
    payload = {"state": "done", "legs_per_search": 9.4, **status}
    (directory / "status.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path / "data"


def test_the_nightly_run_sweeps_every_enabled_trip(tmp_path):
    directory = trips(
        tmp_path,
        make_scenario(id="a", enabled=True),
        make_scenario(id="b", enabled=True),
        make_scenario(id="c", enabled=False),
    )
    assert choose(directory) == ["a", "b"]


def test_a_dispatched_run_sweeps_only_the_trip_it_names(tmp_path):
    directory = trips(tmp_path, make_scenario(id="a"), make_scenario(id="b"))
    assert choose(directory, wanted="b") == ["b"]


def test_the_focused_slot_skips_a_trip_with_nothing_chosen_to_watch(tmp_path):
    """Sweeping the whole window twice a day is the load that got this client
    throttled, and there is nothing to watch until dates have been picked."""
    directory = trips(tmp_path, make_scenario(id="a"))
    data = healthy_sweep(tmp_path, "a")
    assert choose(directory, focused=True, data_dir=data) == []


def test_the_focused_slot_runs_a_trip_that_has_a_focus(tmp_path):
    directory = trips(tmp_path, focused(id="a"))
    data = healthy_sweep(tmp_path, "a")
    assert choose(directory, focused=True, data_dir=data) == ["a"]


def test_the_focused_slot_does_not_follow_a_starved_morning(tmp_path):
    """2.9 legs per search with error_count 0 is what a throttled sweep looks
    like, and another run is the surest way to make the day's data worse."""
    directory = trips(tmp_path, focused(id="a"))
    data = healthy_sweep(tmp_path, "a", legs_per_search=2.9)
    assert choose(directory, focused=True, data_dir=data) == []


def test_the_focused_slot_does_not_follow_a_sweep_that_never_finished(tmp_path):
    directory = trips(tmp_path, focused(id="a"))
    data = healthy_sweep(tmp_path, "a", state="throttled")
    assert choose(directory, focused=True, data_dir=data) == []


def test_the_broad_slot_is_never_gated(tmp_path):
    """Only a sweep can open the gate, so gating the morning would deadlock."""
    directory = trips(tmp_path, make_scenario(id="a"))
    data = healthy_sweep(tmp_path, "a", legs_per_search=0.1)
    assert choose(directory, data_dir=data) == ["a"]


# --------------------------------------------------------------- the watch
#
# The watch slot runs every four hours, so what it skips matters more than what
# the daily sweep skips: six wasted runs a day against one.


def watching(**overrides):
    from datetime import date

    from src.scenario import Watch

    return make_scenario(
        watches=[
            Watch(depart_dates=[date(2027, 1, 10), date(2027, 1, 20), date(2027, 1, 30)])
        ],
        **overrides,
    )


def test_the_watch_runs_only_trips_that_are_watching_something(tmp_path):
    directory = trips(tmp_path, watching(id="watched"), make_scenario(id="plain"))
    assert choose(directory, watching=True, data_dir=tmp_path / "data") == ["watched"]


def test_a_disabled_trip_is_not_watched(tmp_path):
    directory = trips(tmp_path, watching(id="watched", enabled=False))
    assert choose(directory, watching=True, data_dir=tmp_path / "data") == []


def test_the_daily_sweep_does_not_care_whether_a_trip_is_watched(tmp_path):
    directory = trips(tmp_path, watching(id="watched"), make_scenario(id="plain"))
    assert choose(directory, data_dir=tmp_path / "data") == ["plain", "watched"]


def test_a_watch_is_not_gated_on_the_morning_sweep(tmp_path):
    """A starved sweep is a reason not to sweep again, not a reason to stop
    watching: the watch is 63 searches against 483, and the days it follows are
    the ones a decision is actually waiting on."""
    directory = trips(tmp_path, watching(id="watched"))
    healthy_sweep(tmp_path, "watched", legs_per_search=2.1)
    assert choose(directory, watching=True, data_dir=tmp_path / "data") == ["watched"]
