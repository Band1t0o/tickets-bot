"""Decide which trips a scheduled sweep should run, and print it for Actions.

Lives here rather than as an inline heredoc in the workflow so it can be tested.
The equivalent decision used to be a shell loop over `scenarios/*.json`, which
could not express "skip the focused slot when nothing is focused" without
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

from src.cli import health_gate_command  # noqa: E402
from src.scenario import load_scenarios  # noqa: E402
from src.sweep.planner import plan_searches, shards_for  # noqa: E402

# What the morning sweep must have managed before an afternoon one is worth
# running. Legs per search, not error count: the sweep that was failing most had
# error_count 0 and 2.9 legs per search, where a healthy one returns ~10.
MIN_LEGS_PER_SEARCH = 6.0


def choose(
    directory: Path,
    wanted: str = "",
    focused: bool = False,
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

    `focused` is the 13:00 slot, which watches the dates picked off the price
    chart. Two things can take a trip out of it, and both are decisions rather
    than failures:

    - **No focus.** Nothing has been chosen to watch closely, and sweeping the
      whole window a second time is exactly the load this client has been
      throttled for.
    - **A starved morning.** Following a sweep the site refused with another is
      the most reliable way to make the day's data worse rather than better.
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
        return [s.id for s in trips if s.watches or s.leg_watches]
    if focused:
        trips = [s for s in trips if s.focus_start and s.focus_end]
        trips = [
            s
            for s in trips
            if health_gate_command(s.id, MIN_LEGS_PER_SEARCH, data_dir=data_dir) == 0
        ]
    return [s.id for s in trips]


def reason_for_nothing(
    directory: Path,
    wanted: str,
    focused: bool = False,
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
    if not wanted or choose(directory, wanted, focused, data_dir, watching):
        return ""

    known = [s.id for s in load_scenarios(directory)]
    if wanted not in known:
        listed = ", ".join(sorted(known)) or "none at all"
        return (
            f"No trip is called {wanted!r}. The trips on this branch are: {listed}. "
            "The cloud sweeps the committed branch, so a trip saved only on your "
            "machine is not one of them."
        )
    if watching:
        return (
            f"{wanted!r} is not watching anything, so a watch of it would price "
            "nothing. Pin some days or follow a flight on the Watch tab first."
        )
    if focused:
        return (
            f"{wanted!r} has no focus dates, or its morning sweep came back too "
            "starved to follow with another."
        )
    # choose() with a named trip applies no other filter, so anything reaching
    # here means the planner sized the trip at zero searches.
    return f"{wanted!r} plans no searches at this depth."


def jobs(
    directory: Path,
    chosen: list[str],
    depth: str = "",
    shards: int = 0,
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
        count = shards or shards_for(len(plan_searches(scenario)))
        entries += [
            {"scenario": scenario_id, "shard": index, "shard_count": count}
            for index in range(count)
        ]
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", default="scenarios")
    parser.add_argument("--only", default="", help="A single trip id, from workflow_dispatch")
    parser.add_argument("--focused", action="store_true", help="The focused afternoon slot")
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
    chosen = choose(directory, args.only, args.focused, args.data_dir, args.watching)
    print("scenarios=" + json.dumps(chosen))
    # Only ever non-empty for a dispatch that named a trip and got nothing for
    # it. The workflow turns this into a failing job, so the run goes red saying
    # why instead of green saying nothing.
    print(
        "reason="
        + reason_for_nothing(
            directory, args.only, args.focused, args.data_dir, args.watching
        )
    )
    # One entry per runner, each carrying the count it is a share of. Emitted as
    # a single `include` list rather than as two matrix axes because the count
    # now differs per trip: a cross product of trips and shard indices cannot
    # give one trip five runners and another one.
    print("jobs=" + json.dumps(jobs(directory, chosen, args.depth, max(0, args.shards))))


if __name__ == "__main__":
    main()
