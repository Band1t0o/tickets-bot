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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cli import health_gate_command  # noqa: E402
from src.scenario import load_scenarios  # noqa: E402

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
    candidate trips. A trip watching nothing is skipped outright: there is
    nothing to price, and a run of no searches would still write a directory
    and report coverage 0.0.

    `focused` is the 13:00 slot, which watches the dates picked off the price
    chart. Two things can take a trip out of it, and both are decisions rather
    than failures:

    - **No focus.** Nothing has been chosen to watch closely, and sweeping the
      whole window a second time is exactly the load this client has been
      throttled for.
    - **A starved morning.** Following a sweep the site refused with another is
      the most reliable way to make the day's data worse rather than better.
    """
    trips = [s for s in load_scenarios(directory) if s.enabled]
    if wanted:
        trips = [s for s in trips if s.id == wanted]
    if watching:
        # Deliberately no health gate. A starved morning is a reason not to
        # sweep the window again - 483 searches against a site that is refusing
        # - but the watch is 63, and the days it follows are the ones a booking
        # decision is actually waiting on. If the site is refusing, the watch
        # records that honestly through coverage and says nothing.
        return [s.id for s in trips if s.watches]
    if focused:
        trips = [s for s in trips if s.focus_start and s.focus_end]
        trips = [
            s
            for s in trips
            if health_gate_command(s.id, MIN_LEGS_PER_SEARCH, data_dir=data_dir) == 0
        ]
    return [s.id for s in trips]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", default="scenarios")
    parser.add_argument("--only", default="", help="A single trip id, from workflow_dispatch")
    parser.add_argument("--focused", action="store_true", help="The focused afternoon slot")
    parser.add_argument("--watching", action="store_true", help="The four-hourly watch slot")
    parser.add_argument("--shards", type=int, default=3)
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    chosen = choose(Path(args.scenarios), args.only, args.focused, args.data_dir, args.watching)
    count = max(1, args.shards)
    print("scenarios=" + json.dumps(chosen))
    print("shards=" + json.dumps(list(range(count))))
    # Emitted as well as the list, so the sweep job passes the same count to
    # `--shard i/n` that the matrix was built from. Reading it from a workflow
    # env var in two places is how the two drift.
    print(f"shard_count={count}")


if __name__ == "__main__":
    main()
