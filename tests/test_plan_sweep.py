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

from scripts.plan_sweep import choose, jobs, reason_for_nothing
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


def test_the_final_slot_skips_a_trip_that_has_been_narrowed_to_nothing(tmp_path):
    """A final sweep of an unnarrowed trip is the broad sweep, run twice."""
    directory = trips(tmp_path, make_scenario(id="a"))
    data = healthy_sweep(tmp_path, "a")
    assert choose(directory, final=True, data_dir=data) == []


def test_the_final_slot_skips_a_narrowed_trip_that_asked_it_not_to(tmp_path):
    """Two different questions, and they used to have one answer.

    `has_narrowing` asks whether there is anything to sweep. `sweep_narrowing`
    asks whether you want it swept - and narrowing a trip in order to *read* the
    window through it is an ordinary thing to do that was indistinguishable
    from asking for two more runs a day.
    """
    directory = trips(tmp_path, focused(id="a", sweep_narrowing=False))
    data = healthy_sweep(tmp_path, "a")
    assert choose(directory, final=True, data_dir=data) == []


def test_a_dispatch_of_a_trip_that_opted_out_says_which_of_the_two_it_is(tmp_path):
    """Not the same sentence as an unnarrowed trip, because it is not the same
    problem: one needs a narrowing typed, the other needs a box ticked."""
    directory = trips(tmp_path, focused(id="a", sweep_narrowing=False))
    data = healthy_sweep(tmp_path, "a")
    said = reason_for_nothing(directory, "a", final=True, data_dir=data)
    assert "switched off" in said


def test_the_final_slot_runs_a_trip_that_has_a_focus(tmp_path):
    directory = trips(tmp_path, focused(id="a"))
    data = healthy_sweep(tmp_path, "a")
    assert choose(directory, final=True, data_dir=data) == ["a"]


def test_the_final_slot_runs_a_trip_narrowed_only_by_when_it_flies_home(tmp_path):
    """Any one of the three is a narrowing, and a departure window is not special.

    Selecting on the focus alone was the old rule, and it would skip exactly the
    trip whose two nightly runs on 24 Aug were narrowed to 48 searches out of 85
    by a return window with no focus set.
    """
    from datetime import timedelta as delta

    homebound = make_scenario(
        id="a",
        return_focus_start=WINDOW_START + delta(days=25),
        return_focus_end=WINDOW_START + delta(days=30),
    )
    directory = trips(tmp_path, homebound)
    data = healthy_sweep(tmp_path, "a")
    assert choose(directory, final=True, data_dir=data) == ["a"]


def test_the_final_slot_still_runs_after_a_starved_morning(tmp_path):
    """Deliberately ungated, on the watch slot's reasoning rather than the sweep's.

    A starved morning is a reason not to ask the site for 85 searches again. It
    is not a reason to skip the 31 the booking decision is actually waiting on -
    and if the site is still refusing, coverage records that honestly.
    """
    directory = trips(tmp_path, focused(id="a"))
    data = healthy_sweep(tmp_path, "a", legs_per_search=2.9)
    assert choose(directory, final=True, data_dir=data) == ["a"]


def test_a_final_slot_sizes_its_runners_from_the_narrowed_plan(tmp_path):
    """Not from the broad one: a final sweep is a fraction of the searches, and
    handing it the broad plan's runner count would split 31 searches five ways."""
    directory = trips(tmp_path, focused(id="a"))
    broad = jobs(directory, ["a"], depth="deep")
    narrowed = jobs(directory, ["a"], depth="deep", final=True)

    assert len(narrowed) < len(broad)
    assert {entry["shard_count"] for entry in narrowed} == {len(narrowed)}


def test_a_dispatch_of_an_unnarrowed_trip_to_the_final_slot_says_why(tmp_path):
    directory = trips(tmp_path, make_scenario(id="a"))
    data = healthy_sweep(tmp_path, "a")
    said = reason_for_nothing(directory, "a", final=True, data_dir=data)
    assert "narrow" in said.lower()


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

    from src.scenario import Preference

    return make_scenario(
        preferences=[
            Preference(depart_dates=[date(2027, 1, 10), date(2027, 1, 20), date(2027, 1, 30)])
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


def following(**overrides):
    """A trip following one individual flight and no pinned trip at all."""
    from datetime import date

    from src.scenario import LegWatch

    return make_scenario(
        leg_watches=[
            LegWatch(origin="PRG", destination="NRT", depart_date=date(2027, 1, 10))
        ],
        **overrides,
    )


def test_a_trip_following_only_individual_flights_is_still_watched(tmp_path):
    """Selecting on pinned trips alone skipped it entirely.

    Someone follows four flights, the tab quotes four searches a check, and the
    workflow never runs the trip at all — with nothing anywhere saying why.
    """
    directory = trips(tmp_path, following(id="followed"), make_scenario(id="plain"))
    assert choose(directory, watching=True, data_dir=tmp_path / "data") == ["followed"]


def test_a_trip_doing_neither_is_still_skipped(tmp_path):
    """A run of no searches writes a directory and reports coverage 0.0."""
    directory = trips(tmp_path, make_scenario(id="plain"))
    assert choose(directory, watching=True, data_dir=tmp_path / "data") == []


# ------------------------------------------------------- how many runners each
#
# The workflow carried `DEFAULT_SHARDS: 5` as a bare number and applied it to
# every trip in the matrix. It was derived correctly - 483 planned over ~100 a
# runner - but written down as the answer rather than as the rule, so it stayed
# 5 when pinning the Japan crossing took that trip to 66 searches, and five
# runners split thirteen apiece.


def test_a_trip_is_split_across_enough_runners_to_finish(tmp_path):
    directory = trips(tmp_path, make_scenario(id="big", depth="deep", enabled=True))
    assert jobs(directory, ["big"], depth="deep") == [
        {"scenario": "big", "shard": index, "shard_count": 5} for index in range(5)
    ]


def test_a_small_trip_is_not_split_at_all(tmp_path):
    directory = trips(tmp_path, make_scenario(id="small", depth="quick", enabled=True))
    assert jobs(directory, ["small"], depth="quick") == [
        {"scenario": "small", "shard": 0, "shard_count": 1}
    ]


def test_two_trips_in_one_run_are_sized_separately(tmp_path):
    """The reason a single number could never be right. One matrix, two plans.

    With no depth forced, each trip is sized for the depth it is saved at -
    which is what the nightly run sweeps it at.
    """
    directory = trips(
        tmp_path,
        make_scenario(id="big", depth="deep", enabled=True),
        make_scenario(id="small", depth="quick", enabled=True),
    )
    planned = jobs(directory, ["big", "small"])
    assert {job["scenario"]: job["shard_count"] for job in planned} == {
        "big": 5,
        "small": 1,
    }
    assert [job["scenario"] for job in planned] == ["big"] * 5 + ["small"]


def test_the_depth_the_run_will_use_is_the_depth_it_is_sized_for(tmp_path):
    """A trip saved as `quick` and dispatched as `deep` is a seven-times bigger
    plan. Sizing it from the file would hand it one runner and lose most of it."""
    directory = trips(tmp_path, make_scenario(id="t", depth="quick", enabled=True))
    assert jobs(directory, ["t"], depth="quick")[0]["shard_count"] == 1
    assert jobs(directory, ["t"], depth="deep")[0]["shard_count"] == 5


def test_an_explicit_count_still_wins(tmp_path):
    """`workflow_dispatch` offers a shard count for testing the rate limit. It
    has to mean what it says."""
    directory = trips(tmp_path, make_scenario(id="t", depth="deep", enabled=True))
    assert [job["shard_count"] for job in jobs(directory, ["t"], shards=2)] == [2, 2]


def test_every_job_of_a_trip_agrees_on_the_count(tmp_path):
    """The shard passes `--shard i/n` and the merge reports coverage against n.
    Two jobs disagreeing about n is a sweep that cannot add itself up."""
    directory = trips(tmp_path, make_scenario(id="t", depth="deep", enabled=True))
    planned = jobs(directory, ["t"], depth="deep")
    assert {job["shard_count"] for job in planned} == {len(planned)}
    assert sorted(job["shard"] for job in planned) == list(range(len(planned)))


def test_no_trips_means_no_jobs(tmp_path):
    assert jobs(trips(tmp_path), []) == []


# ------------------------------------------------- what a dispatch is allowed to
#
# The `enabled` tick answers "does the *schedule* sweep this trip". It was being
# read as "may this trip be swept at all": `choose` filtered on it before it
# looked at the trip the dispatch named, so asking for a trip that was not in the
# nightly rotation planned nothing, the workflow skipped its sweep and merge, and
# the run went green in twelve seconds having done nothing. Three of those were
# spent before anyone looked at the plan step's output.


def test_a_dispatch_sweeps_the_trip_it_names_even_when_it_is_not_in_the_rotation(tmp_path):
    """Naming a trip is the instruction. The tick is about the schedule."""
    directory = trips(tmp_path, make_scenario(id="off", enabled=False))
    assert choose(directory, wanted="off") == ["off"]


def test_the_schedule_still_leaves_an_unticked_trip_alone(tmp_path):
    """The other half of the same rule, so the tick cannot become decoration."""
    directory = trips(
        tmp_path,
        make_scenario(id="on", enabled=True),
        make_scenario(id="off", enabled=False),
    )
    assert choose(directory) == ["on"]


def test_a_dispatch_can_watch_a_trip_that_is_not_in_the_rotation(tmp_path):
    directory = trips(tmp_path, watching(id="off", enabled=False))
    assert choose(directory, wanted="off", watching=True, data_dir=tmp_path / "data") == ["off"]


# ------------------------------------------------------- saying why it planned
#
# A dispatch that plans nothing is now a red run rather than a green one, and the
# error it prints has to be worth reading: "nothing planned" would leave the same
# twelve-second mystery, just in red.


def test_a_dispatch_that_named_an_unknown_trip_says_so(tmp_path):
    directory = trips(tmp_path, make_scenario(id="real"))
    reason = reason_for_nothing(directory, "typo")
    assert "typo" in reason
    assert "real" in reason


def test_a_dispatch_of_a_trip_with_nothing_to_watch_says_that_instead(tmp_path):
    """It exists and it was named, so the id is not the problem - the mode is."""
    directory = trips(tmp_path, make_scenario(id="plain"))
    reason = reason_for_nothing(directory, "plain", watching=True)
    assert "plain" in reason
    assert "following" in reason.lower()


def test_nothing_to_explain_when_the_dispatch_planned_something(tmp_path):
    directory = trips(tmp_path, make_scenario(id="real"))
    assert reason_for_nothing(directory, "real") == ""


def test_a_probe_is_sized_from_the_probe_plan_not_the_sweeps(tmp_path):
    """A probe dispatched by hand from the app is a fraction of the sweep it
    exists to avoid: grand-tour is 660 searches swept and 36 probed. Sized from
    `plan_searches` those 36 would be dealt across seven runners.
    """
    directory = trips(tmp_path, make_scenario(id="big", depth="deep", enabled=True))
    swept = jobs(directory, ["big"], depth="deep")
    probed = jobs(directory, ["big"], depth="deep", mode="explore")

    assert len(swept) > 1, "this trip must need sharding for the comparison to mean anything"
    assert len(probed) == 1
    assert probed[0]["shard_count"] == 1


def test_the_slot_flags_still_decide_when_no_mode_is_given(tmp_path):
    """The crons pass --final and no --mode. Adding one must not change them."""
    directory = trips(tmp_path, focused(id="narrowed", depth="deep", enabled=True))
    assert jobs(directory, ["narrowed"], depth="deep", final=True) == jobs(
        directory, ["narrowed"], depth="deep", final=True, mode=""
    )
