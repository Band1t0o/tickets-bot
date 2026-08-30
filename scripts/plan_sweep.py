"""Decide which trips a scheduled sweep should run, and print it for Actions.

Lives here rather than as an inline heredoc in the workflow so it can be tested.
The equivalent decision used to be a shell loop over `scenarios/*.json`, which
could not express "skip the final slot when nothing is narrowed" without
another layer of shell.

Prints `key=value` lines for `$GITHUB_OUTPUT`.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scenario import load_scenarios  # noqa: E402
from src.sweep.planner import PLANS, plan_searches, shards_for  # noqa: E402


def has_narrowing(scenario) -> bool:
    """Whether a trip says anything less than its whole window.

    Any one of the three counts. Selecting on the focus alone was the old rule
    and it would skip the case that actually happened: on 24 Aug the committed
    japan-philippines trip had no focus and a return window, and its nightly
    runs were narrowed to 48 searches out of 85 by it.
    """
    return bool(
        (scenario.focus_start and scenario.focus_end)
        or (scenario.return_focus_start and scenario.return_focus_end)
        or scenario.total_days
    )


def sweeps_its_narrowing(scenario) -> bool:
    """Whether the 13:00/20:00 slots should re-price this trip's narrowing.

    Two conditions, and they are different questions. `has_narrowing` asks
    whether there is anything to sweep; `sweep_narrowing` asks whether you want
    it swept. Narrowing a trip in order to *read* the window through it is an
    ordinary thing to do - the boxes filter the charts either way - and it used
    to be indistinguishable from asking for two more runs a day.
    """
    return has_narrowing(scenario) and scenario.sweep_narrowing


def choose(
    directory: Path,
    wanted: str = "",
    final: bool = False,
    data_dir: Path | str = "data",
    watching: bool = False,
) -> list[str]:
    """The trip ids this run should sweep.

    `watching` is the four-hourly watch slot, which re-prices the pinned
    candidate trips *and* the individual flights being followed. A trip doing
    neither is skipped outright: there is nothing to price, and a run of no
    searches would still write a directory and report coverage 0.0.

    Both kinds count. Selecting on pinned trips alone would let someone follow
    four flights, see the cost badge quote four searches, and never once be
    checked - the workflow would skip the trip entirely and nothing anywhere
    would say why.

    `final` is the 13:00 and 20:00 slots, which re-price what the trip has been
    narrowed to. A trip narrowed to nothing is skipped: a final sweep of it would
    plan the whole window, which is the 02:00 slot's job and exactly the load
    this client has been throttled for.

    Deliberately no health gate, on the watch slot's reasoning rather than the
    old focused slot's. That gate existed because the afternoon used to re-run
    the whole window - 85 searches at the site a starved morning has just shown
    to be refusing. A final sweep is 31, and the days it prices are the ones a
    booking decision is waiting on. If the site is still refusing, coverage
    records it honestly and nothing is lost.
    """
    trips = list(load_scenarios(directory))
    if wanted:
        # Naming a trip *is* the instruction, so `enabled` does not get a vote
        # here. It used to: the tick was applied first, and dispatching a trip
        # that was not in the nightly rotation planned nothing, skipped the
        # sweep and merge jobs, and reported success in twelve seconds. The tick
        # answers "does the schedule sweep this", which is the branch below.
        trips = [s for s in trips if s.id == wanted]
    else:
        trips = [s for s in trips if s.enabled]
    if watching:
        # Deliberately no health gate. A starved morning is a reason not to
        # sweep the window again - 483 searches against a site that is refusing
        # - but the watch is 63, and the days it follows are the ones a booking
        # decision is actually waiting on. If the site is refusing, the watch
        # records that honestly through coverage and says nothing.
        return [s.id for s in trips if s.preferences or s.leg_watches]
    if final:
        trips = [s for s in trips if sweeps_its_narrowing(s)]
    return [s.id for s in trips]


def reason_for_nothing(
    directory: Path,
    wanted: str,
    final: bool = False,
    data_dir: Path | str = "data",
    watching: bool = False,
) -> str:
    """Why a dispatch that named a trip planned no work, in one sentence.

    A dispatch that plans nothing is a red run now rather than a green one, and
    the error it prints has to be worth reading. "Nothing planned" would leave
    exactly the twelve-second mystery this came from, only in red - so the
    sentence has to separate "no trip is called that" from "that trip is not
    doing the thing this slot runs".

    Empty string when there is nothing to explain.
    """
    if not wanted or choose(directory, wanted, final, data_dir, watching):
        return ""

    trips = {s.id: s for s in load_scenarios(directory)}
    if wanted not in trips:
        listed = ", ".join(sorted(trips)) or "none at all"
        return (
            f"No trip is called {wanted!r}. The trips on this branch are: {listed}. "
            "The cloud sweeps the committed branch, so a trip saved only on your "
            "machine is not one of them."
        )
    if watching:
        return (
            f"{wanted!r} is not following anything, so a check of it would price "
            "nothing. Save a preference or follow a flight on the Follow it step first."
        )
    if final:
        if has_narrowing(trips[wanted]):
            return (
                f"{wanted!r} is narrowed, but has 'Also sweep just these' switched off, "
                "so its narrowing filters what you read and costs no searches."
            )
        return (
            f"{wanted!r} has not been narrowed to anything - no departure window, "
            "no return window, no nights band - so a final sweep of it would price "
            "the whole window the 02:00 sweep already does."
        )
    # choose() with a named trip applies no other filter, so anything reaching
    # here means the planner sized the trip at zero searches.
    return f"{wanted!r} plans no searches at this depth."


def jobs(
    directory: Path,
    chosen: list[str],
    depth: str = "",
    shards: int = 0,
    final: bool = False,
) -> list[dict]:
    """One matrix entry per runner, sized from each trip's own plan.

    This replaced a `DEFAULT_SHARDS: 5` in the workflow that was applied to
    every trip in the run. The 5 was derived correctly - 483 planned over ~100
    a runner - but recorded as the answer rather than as the rule, so it stayed
    5 when pinning the Japan crossing took that trip to 66 searches, and one
    number could never have been right for two trips of different sizes anyway.

    `depth` is the depth the run will *use*, not the one on the file. A trip
    saved as `quick` and dispatched as `deep` is a seven-times bigger plan, and
    sizing it from the file would hand it one runner and lose most of it.

    `shards` is the workflow_dispatch override, which exists for testing the
    rate limit and has to mean what it says.
    """
    by_id = {s.id: s for s in load_scenarios(directory)}
    entries: list[dict] = []
    for scenario_id in chosen:
        scenario = by_id[scenario_id]
        if depth:
            scenario = replace(scenario, depth=depth)
        # Sized from the plan this slot will actually run. A final sweep is a
        # fraction of the broad one - 31 searches against 85 on the real trip -
        # and sizing it from `plan_searches` would deal those 31 across the five
        # runners the broad shape needs, at four searches a runner.
        planner = PLANS["final"] if final else plan_searches
        count = shards or shards_for(len(planner(scenario)))
        entries += [
            {"scenario": scenario_id, "shard": index, "shard_count": count}
            for index in range(count)
        ]
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", default="scenarios")
    parser.add_argument("--only", default="", help="A single trip id, from workflow_dispatch")
    parser.add_argument("--final", action="store_true",
                        help="The 13:00/20:00 slot, which re-prices the narrowing")
    parser.add_argument("--watching", action="store_true", help="The four-hourly watch slot")
    parser.add_argument(
        "--shards",
        type=int,
        default=0,
        help="Force a runner count. Leave at 0 to size each trip from its plan.",
    )
    parser.add_argument("--depth", default="", help="The depth the run will use")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    directory = Path(args.scenarios)
    chosen = choose(directory, args.only, args.final, args.data_dir, args.watching)
    print("scenarios=" + json.dumps(chosen))
    # Only ever non-empty for a dispatch that named a trip and got nothing for
    # it. The workflow turns this into a failing job, so the run goes red saying
    # why instead of green saying nothing.
    print(
        "reason="
        + reason_for_nothing(
            directory, args.only, args.final, args.data_dir, args.watching
        )
    )
    # One entry per runner, each carrying the count it is a share of. Emitted as
    # a single `include` list rather than as two matrix axes because the count
    # now differs per trip: a cross product of trips and shard indices cannot
    # give one trip five runners and another one.
    print("jobs=" + json.dumps(jobs(directory, chosen, args.depth, max(0, args.shards), args.final)))


if __name__ == "__main__":
    main()
