"""Command line entry points.

`scrape` and `watch` used to live here: a single-route, env-configured loop that
wrote CSV per run and predates the scenario platform entirely. Nothing in the
sweep path read its settings, no test covered it, and its storage layer's
error-recovery path overwrote the run history it was meant to protect. It is in
the git history if it is ever wanted back.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path


def _force_utf8_output() -> None:
    """Print UTF-8 regardless of the console's codepage.

    Windows consoles default to a legacy codepage - cp1250 on a Czech install -
    which cannot encode the arrows in route labels or the Czech text scraped
    from pelikan. `probe-report` died with UnicodeEncodeError on the first "→"
    rather than printing a report that had already been computed.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def run_sweep_command(
    scenario_id: str,
    depth: str | None,
    dry_run: bool,
    mode: str = "sweep",
    max_minutes: float | None = None,
    *,
    provider=None,
    data_dir: Path | str = "data",
    delay_s: float | None = None,
    shard: tuple[int, int] | None = None,
    notify: bool = True,
    resume_from: Path | str | None = None,
):
    """Run one scenario's sweep. Returns the SweepResult.

    `max_minutes` is a wall-clock budget: when it expires the sweep is asked to
    stop the same way the UI's Stop button asks, so it ends cleanly with its
    legs on disk. Thirteen consecutive nightly cloud runs were killed by the job
    timeout mid-sweep and committed nothing at all; a run that stops itself
    inside its budget always reaches the commit step.

    `shard=(index, count)` runs one runner's share of the plan. `notify=False`
    goes with it: a sharded run must report once, from the merge, not once per
    shard - and no shard has the whole result to report anyway.
    """
    import threading

    from .scenario import load_scenario
    from .sweep.planner import PLANS, estimate_minutes
    from .sweep.runner import SEARCH_DELAY_S, run_sweep

    path = Path("scenarios") / f"{scenario_id}.json"
    if not path.exists():
        print(f"No scenario named {scenario_id!r} in scenarios/")
        raise SystemExit(2)

    scenario = load_scenario(path)
    if depth:
        scenario = replace(scenario, depth=depth)
    scenario.validate()

    from .sweep.planner import shard_of

    searches = PLANS[mode](scenario)
    planned = len(searches)
    if shard is not None:
        searches = shard_of(searches, *shard)
    minutes = estimate_minutes(searches)
    label = mode if mode in {"explore", "watch"} else f"depth={scenario.depth}"
    if mode == "final":
        label = f"final depth={scenario.depth}"
    share = f" (shard {shard[0] + 1}/{shard[1]} of {planned})" if shard else ""
    # Only a run the narrowing actually binds may claim to be narrowed by it.
    # Printed unconditionally, this line told every broad dry run it was focused
    # - which is precisely the belief that let the nightly sweep stop pricing
    # most of its window without anyone noticing.
    focus = (
        f" focused {scenario.focus_start}..{scenario.focus_end}"
        if mode == "final" and scenario.focus_start
        else ""
    )
    print(f"[{scenario.id}] {label}{focus} → {len(searches)} searches{share}, ~{minutes} min")

    if dry_run:
        # Deliberately exits before launching a browser, so the Actions budget
        # can be checked without spending any of it.
        # `leg_pools`, so the breakdown names the airports this run will really
        # search. The dry run is what the sweep workflow checks the shard count
        # against, and a pinned crossing it reported as three airports wide
        # would have it sized for a sweep three times the one about to happen.
        pools = scenario.leg_pools
        for leg_index in sorted({s.leg_index for s in searches}):
            count = sum(1 for s in searches if s.leg_index == leg_index)
            origins, destinations = pools[leg_index]
            route = f"{'/'.join(origins)} → {'/'.join(destinations)}"
            print(f"  leg {leg_index} {route}: {count} searches")
        raise SystemExit(0)

    stop = threading.Event()
    timer = None
    if max_minutes:
        print(f"[{scenario.id}] budget {max_minutes} min; will stop cleanly when it runs out")
        timer = threading.Timer(max_minutes * 60, stop.set)
        timer.daemon = True
        timer.start()

    try:
        result = run_sweep(
            scenario,
            mode=mode,
            stop=stop,
            provider=provider,
            data_dir=data_dir,
            shard=shard,
            resume_from=resume_from,
            delay_s=SEARCH_DELAY_S if delay_s is None else delay_s,
            on_progress=lambda done, total, label: (
                print(f"  [{done}/{total}] {label}", flush=True) if done % 10 == 0 else None
            ),
        )
    finally:
        if timer is not None:
            timer.cancel()

    print(f"[{scenario.id}] {len(result.legs)} legs, {len(result.errors)} errors → {result.directory}")
    if result.stopped:
        print(f"[{scenario.id}] stopped on its budget at {result.completed}/{result.total}; "
              "the legs found so far are on disk and worth committing")
    if result.throttled:
        # Deliberately not phrased as breakage. Nothing here needs fixing.
        print(f"[{scenario.id}] the site stopped answering and kept not answering; "
              "gave up rather than grinding. Try later, or from the cloud runner")
    if result.routes_with_no_results:
        # A route searched on every date without ever returning an offer is
        # breakage, not a quiet market, and it reads as neither in a leg count.
        print(f"[{scenario.id}] routes that returned nothing: "
              f"{', '.join(result.routes_with_no_results)}")
    if not result.is_healthy and not (result.throttled or result.stopped):
        # A throttled or budget-stopped run is incomplete, not broken, and has
        # already said so in its own words above.
        print(f"[{scenario.id}] WARNING: sweep looks unhealthy (no legs, or majority failed)")

    print(
        f"[{scenario.id}] answered {result.answered}/{result.total} "
        f"({result.coverage:.1%} of what it was handed)"
    )

    if notify:
        from .notify_discord import notify_sweep

        notify_sweep(scenario, result)
    return result


def merge_shards_command(
    scenario_id: str,
    shard_root: Path | str,
    data_dir: Path | str = "data",
    notify: bool = True,
) -> Path:
    """Stitch the shard directories under `shard_root` into one sweep and report.

    `shard_root` is where the workflow downloaded the shard artifacts, each one
    a `data/sweeps/<id>/<stamp>/` tree written by a different runner. The merged
    sweep gets a fresh directory of its own, because everything downstream reads
    one sweep directory and none of the shard stamps is the run's stamp.

    Reporting happens here rather than in each shard: a sharded run is one sweep
    and must post one message, and no single shard holds enough of the result to
    say anything true about the cheapest trip.
    """
    from .scenario import load_scenario
    from .sweep.runner import ShardMismatch, _new_sweep_directory, load_legs, merge_shards

    shard_root = Path(shard_root)
    # Any directory holding a status.json is a shard, however the artifact
    # download nested it. Sorted so a merge is reproducible.
    # Discovered by the snapshot every shard writes before its first search,
    # not by status.json: an older committed sweep has a status and no snapshot,
    # and the first sharded run merged seven of those by mistake.
    shards = sorted(p.parent for p in shard_root.rglob("scenario.json"))
    if not shards:
        print(f"[merge] no shards under {shard_root}; nothing to merge")
        raise SystemExit(2)

    destination = _new_sweep_directory(Path(data_dir) / "sweeps" / scenario_id)
    print(f"[merge] {len(shards)} shard(s) -> {destination}")
    for shard in shards:
        print(f"[merge]   {shard}")
    try:
        status = merge_shards(shards, destination)
    except ShardMismatch as exc:
        print(f"[merge] {exc}")
        raise SystemExit(1) from exc

    print(
        f"[merge] {status['legs_found']} legs, {status['legs_per_search']} legs/search, "
        f"answered {status['answered']}/{status['planned']} ({status['coverage']:.1%}), "
        f"state {status['state']}"
    )
    if status["coverage"] < 1.0:
        # Said out loud, because a merged sweep with holes looks exactly like a
        # complete one in every other figure it reports.
        print(
            f"[merge] INCOMPLETE: {status['planned'] - status['answered']} search(es) were "
            "never answered; the cheapest trip may simply not have been seen"
        )
    if status["routes_with_no_results"]:
        print(f"[merge] routes that returned nothing: {', '.join(status['routes_with_no_results'])}")

    if notify:
        from .notify_discord import notify_sweep
        from .sweep.runner import SweepResult

        scenario = load_scenario(Path("scenarios") / f"{scenario_id}.json")
        # Rebuilt from the merged files so the message describes the whole run.
        result = SweepResult(
            scenario_id=scenario_id,
            directory=destination,
            total=status["total"],
            completed=status["completed"],
            answered=status["answered"],
            legs=load_legs(destination),
            errors=list(status["errors"]),
            started_at=status["started_at"] or "",
            finished_at=status["finished_at"] or "",
            route_searches=dict(status["route_searches"]),
            route_legs=dict(status["route_legs"]),
            route_errors=dict(status["route_errors"]),
        )
        notify_sweep(scenario, result)
    return destination


def run_watch_command(
    scenario_id: str,
    dry_run: bool = False,
    max_minutes: float | None = None,
    *,
    provider=None,
    data_dir: Path | str = "data",
    delay_s: float | None = None,
    notify: bool = True,
):
    """Re-price the watched candidates once, record them, and report any falls.

    Thin on purpose. The searching is `run_sweep_command` in watch mode - the
    same runner, breaker, browser recycling and incremental writes, because a
    watch fails in all the same ways a sweep does and nothing is gained by a
    second implementation of surviving them. What is watch-specific is only
    what happens either side: refusing to run when nothing is watched, and
    turning the legs into one row per candidate.

    Reporting is separate from the sweep's. `notify_sweep` answers "this is the
    cheapest the trip has been"; a watch answers "one of the days you are
    choosing between moved", and sends nothing at all when none of them did.
    """
    from .scenario import load_scenario
    from .watch import (
        DEFAULT_WATCH_DIR,
        drops,
        leg_drops,
        leg_report,
        record_leg_observations,
        record_observations,
        watch_report,
    )

    path = Path("scenarios") / f"{scenario_id}.json"
    if not path.exists():
        print(f"No scenario named {scenario_id!r} in scenarios/")
        raise SystemExit(2)

    scenario = load_scenario(path)
    if not scenario.watches and not scenario.leg_watches:
        # Not an error condition so much as nothing to do, but it exits non-zero
        # so a workflow that dispatched this by mistake says so instead of
        # committing an empty run that reads as a watch which found nothing.
        print(f"[{scenario_id}] nothing is being watched; pick days on the Watch tab first")
        raise SystemExit(2)

    result = run_sweep_command(
        scenario_id,
        None,
        dry_run,
        mode="watch",
        max_minutes=max_minutes,
        provider=provider,
        data_dir=data_dir,
        delay_s=delay_s,
        notify=False,
    )

    directory = Path(data_dir) / DEFAULT_WATCH_DIR.name / scenario_id
    status = json.loads((result.directory / "status.json").read_text(encoding="utf-8"))
    rows = record_observations(result.legs, scenario, status, directory)
    for row in rows:
        price = "nothing found" if row["total"] is None else f"{row['total']:,.0f} {row['currency']}"
        print(f"[{scenario_id}] {row['depart_date']}: {price}")

    # The individual flights being followed, recorded from the same run's legs.
    # One search each, so they ride along with the trip watch rather than
    # needing a workflow of their own.
    leg_rows = record_leg_observations(result.legs, scenario, status, directory)
    for row in leg_rows:
        price = "nothing found" if row["price"] is None else f"{row['price']:,.0f} {row['currency']}"
        substituted = "" if row["exact"] or row["found_date"] is None else f" (on {row['found_date']})"
        print(f"[{scenario_id}] {row['route']} {row['depart_date']}: {price}{substituted}")

    fell = drops(watch_report(directory), scenario, directory)
    legs_fell = leg_drops(leg_report(directory), scenario, directory)
    if not fell and not legs_fell:
        print(f"[{scenario_id}] nothing fell far enough to be worth a message")
        return result

    print(f"[{scenario_id}] {len(fell)} watched day(s) and {len(legs_fell)} watched leg(s) got cheaper")
    if notify:
        from .notify_discord import notify_watch

        notify_watch(scenario, fell, leg_drops=legs_fell)
    return result


def watch_report_command(scenario_id: str, data_dir: Path | str = "data") -> int:
    """Print how each watched candidate has moved. Returns the count."""
    from .watch import DEFAULT_WATCH_DIR, watch_report

    report = watch_report(Path(data_dir) / DEFAULT_WATCH_DIR.name / scenario_id)
    candidates = report["candidates"]
    if not candidates:
        print(f"No observations for {scenario_id!r} yet — run `python -m src.cli watch` first.")
        return 0

    print(f"{'departs':12} {'obs':>4} {'first':>10} {'latest':>10} {'move':>9} {'low':>10}  route")
    for key in sorted(candidates):
        c = candidates[key]
        if c["latest"] is None:
            print(f"{key:12} {c['observations']:>4} {'—':>10} {'—':>10} {'—':>9} {'—':>10}  "
                  "nothing trustworthy yet")
            continue
        print(
            f"{key:12} {c['observations']:>4} {c['first']:>10,.0f} {c['latest']:>10,.0f} "
            f"{c['net_change_pct']:>8.1f}% {c['low']:>10,.0f}  {c['route']}"
        )
    return len(candidates)


def health_gate_command(
    scenario_id: str, min_legs_per_search: float, data_dir: Path | str = "data"
) -> int:
    """0 to proceed with another sweep, 1 to skip it.

    Two deep sweeps a day is ~1,230 searches against a site that has already
    throttled this client into 58 of 93 timeouts once, and a starved sweep is
    worse than no sweep: it spends the budget, commits thin results, and its
    price is not comparable with anything. So the second run of the day asks
    the first how it went.

    Lives here rather than in workflow YAML so it can be tested.
    """
    from .sweep.runner import legs_per_search_of

    root = Path(data_dir) / "sweeps" / scenario_id
    directories = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True) if root.exists() else []
    if not directories:
        # Refusing here would deadlock: only a sweep can open the gate.
        print(f"[gate] no sweep yet for {scenario_id!r}; proceeding")
        return 0

    latest = directories[0]
    try:
        status = json.loads((latest / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[gate] {latest.name}: status unreadable ({exc}); skipping this run")
        return 1

    if status.get("state") != "done":
        print(f"[gate] {latest.name}: last sweep state is {status.get('state')!r}; skipping this run")
        return 1

    rate = legs_per_search_of(status)
    if rate is None:
        print(f"[gate] {latest.name}: no legs-per-search recorded or derivable; skipping this run")
        return 1
    if rate < min_legs_per_search:
        print(
            f"[gate] {latest.name}: {rate} legs/search is below {min_legs_per_search}; "
            "skipping this run rather than hammering a site that is already refusing"
        )
        return 1

    print(f"[gate] {latest.name}: {rate} legs/search; proceeding")
    return 0


def verify_command(scenario_id: str, stamp: str | None, top: int) -> int:
    """Re-price a finished sweep's shortlist on letuska and record the result.

    Deliberately a separate command rather than a step inside `sweep`. It takes
    minutes against a site with no deep link, and a sweep that has already
    succeeded must not be put at risk by a second site being down.
    """
    from .scenario import load_scenario
    from .sweep.runner import load_legs
    from .verify import letuska_checker, verify_shortlist

    scenario = load_scenario(Path("scenarios") / f"{scenario_id}.json")
    root = Path("data") / "sweeps" / scenario_id
    directories = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True) if root.exists() else []
    if stamp:
        directories = [p for p in directories if p.name == stamp]
    if not directories:
        print(f"No sweep to verify for {scenario_id!r}")
        raise SystemExit(2)

    directory = directories[0]
    print(f"[verify] {directory.name}: re-pricing the top {top} itineraries on letuska")
    report = verify_shortlist(load_legs(directory), scenario, letuska_checker(scenario.adults), top)

    (directory / "verify.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"[verify] {report['verdict']}: {report['legs_checked']} leg(s) checked")
    for row in report["comparisons"]:
        print(f"   {row['route']} {row['depart_date']}: ours {row['ours']:,.0f} · "
              f"theirs {row['theirs']:,.0f} ({row['saving_pct']:+.1f}%)")
    if report["unpriced"]:
        print(f"[verify] could not be priced there: {', '.join(report['unpriced'])}")
    if report["cheapest_elsewhere"]:
        best = report["cheapest_elsewhere"]
        print(f"[verify] worth a look: {best['route']} is {best['saving_pct']:.1f}% cheaper there")
    return report["legs_checked"]


def check_price_command(origin: str, destination: str, depart: str, ret: str | None) -> int:
    """Price one route on letuska.cz as a second opinion on a pelikan result."""
    from datetime import date

    from .providers.letuska import LetuskaProvider, LetuskaSearchFailed

    try:
        legs = LetuskaProvider().check_price(
            origin.upper(),
            destination.upper(),
            date.fromisoformat(depart),
            date.fromisoformat(ret) if ret else None,
        )
    except LetuskaSearchFailed as exc:
        print(f"letuska search failed: {exc}")
        raise SystemExit(1) from exc

    if not legs:
        print(f"letuska found nothing for {origin}→{destination} on {depart}")
        return 0
    for leg in sorted(legs, key=lambda leg: leg.price_amount):
        when = leg.depart_date.isoformat() if leg.depart_date else "?"
        print(f"  {leg.origin}→{leg.destination} {when} · {leg.price_amount:,.0f} {leg.price_currency}")
    return len(legs)


def main():
    _force_utf8_output()
    from .sweep.runner import MODES

    parser = argparse.ArgumentParser(description="Flight scenario watcher")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sweep = sub.add_parser("sweep", help="Run a sweep for one scenario")
    p_sweep.add_argument("--scenario", required=True)
    p_sweep.add_argument("--depth", choices=["quick", "standard", "deep"],
                         help="Override the scenario's depth")
    p_sweep.add_argument("--mode", choices=list(MODES), default="sweep",
                         help="final: only the dates the trip has been narrowed to. "
                              "explore: every route on three dates, to see which "
                              "airports are worth a real sweep at all")
    p_sweep.add_argument("--max-minutes", type=float,
                         help="Wall-clock budget. The sweep stops cleanly when it runs out, "
                              "keeping what it found, instead of being killed mid-search")
    p_sweep.add_argument("--data-dir", default="data",
                         help="Where to write the sweep. A shard writes outside data/ so its "
                              "artifact holds one sweep rather than the whole committed history")
    p_sweep.add_argument("--resume-from", metavar="STAMP",
                         help="carry on the run in this directory, asking only for the "
                              "searches it never answered (e.g. 2026-08-21T09-55-40Z)")
    p_sweep.add_argument("--shard", metavar="INDEX/COUNT",
                         help="Run one runner's share of the plan, e.g. 0/3. The shards "
                              "partition it exactly; merge them with merge-shards")
    p_sweep.add_argument("--no-notify", action="store_true",
                         help="Do not post to Discord. Implied by --shard: a sharded run "
                              "reports once, from the merge")
    p_sweep.add_argument("--dry-run", action="store_true",
                         help="Print the planned search count and estimate, then exit")

    p_merge = sub.add_parser(
        "merge-shards", help="Stitch sharded sweep directories into one sweep and report"
    )
    p_merge.add_argument("--scenario", required=True)
    p_merge.add_argument("--from", dest="shard_root", required=True,
                         help="Directory the shard artifacts were downloaded into")
    p_merge.add_argument("--no-notify", action="store_true")

    p_check = sub.add_parser("check-price", help="Second opinion on one route, via letuska.cz")
    p_check.add_argument("--from", dest="origin", required=True)
    p_check.add_argument("--to", dest="destination", required=True)
    p_check.add_argument("--depart", required=True, help="YYYY-MM-DD")
    p_check.add_argument("--return", dest="ret", help="YYYY-MM-DD, for a round trip")

    sub.add_parser("probe", help="Sample the fixed volatility-probe routes once")
    sub.add_parser("probe-report", help="Summarise how much probe prices have moved")

    p_watch = sub.add_parser(
        "watch", help="Re-price the watched candidate days once, and report any falls"
    )
    p_watch.add_argument("--scenario", required=True)
    p_watch.add_argument("--data-dir", default="data")
    p_watch.add_argument("--max-minutes", type=float,
                         help="Stop cleanly after this long, keeping what was found")
    p_watch.add_argument("--dry-run", action="store_true",
                         help="Print the planned search count and exit")
    p_watch.add_argument("--no-notify", action="store_true")

    p_watch_report = sub.add_parser(
        "watch-report", help="Show how each watched day has moved"
    )
    p_watch_report.add_argument("--scenario", required=True)
    p_watch_report.add_argument("--data-dir", default="data")

    p_gate = sub.add_parser(
        "health-gate",
        help="Exit non-zero when the last sweep was too starved to justify another",
    )
    p_gate.add_argument("--scenario", required=True)
    p_gate.add_argument("--min-legs-per-search", type=float, default=6.0)

    p_verify = sub.add_parser(
        "verify", help="Re-price a sweep's shortlist on letuska as a second opinion"
    )
    p_verify.add_argument("--scenario", required=True)
    p_verify.add_argument("--stamp", help="A specific sweep; defaults to the newest")
    p_verify.add_argument("--top", type=int, default=3, help="How many itineraries to re-price")

    args = parser.parse_args()

    if args.cmd == "merge-shards":
        merge_shards_command(args.scenario, args.shard_root, notify=not args.no_notify)
        raise SystemExit(0)

    if args.cmd == "health-gate":
        raise SystemExit(health_gate_command(args.scenario, args.min_legs_per_search))

    if args.cmd == "verify":
        verify_command(args.scenario, args.stamp, args.top)
        raise SystemExit(0)

    if args.cmd == "probe":
        from .probe import run_probe

        run_probe()
        raise SystemExit(0)

    if args.cmd == "probe-report":
        from .probe import format_report, probe_report

        print(format_report(probe_report()))
        raise SystemExit(0)

    if args.cmd == "watch":
        run_watch_command(
            args.scenario,
            args.dry_run,
            args.max_minutes,
            data_dir=args.data_dir,
            notify=not args.no_notify,
        )
        raise SystemExit(0)

    if args.cmd == "watch-report":
        watch_report_command(args.scenario, args.data_dir)
        raise SystemExit(0)

    if args.cmd == "check-price":
        check_price_command(args.origin, args.destination, args.depart, args.ret)
        raise SystemExit(0)

    shard = _parse_shard(args.shard)
    resume_from = None
    if args.resume_from:
        from .sweep.runner import MODE_ROOTS

        resume_from = (
            Path(args.data_dir) / MODE_ROOTS[args.mode] / args.scenario / args.resume_from
        )
        if not resume_from.exists():
            raise SystemExit(f"no such run to carry on: {resume_from}")
    run_sweep_command(
        args.scenario,
        args.depth,
        args.dry_run,
        args.mode,
        args.max_minutes,
        data_dir=args.data_dir,
        shard=shard,
        resume_from=resume_from,
        # A shard has only part of the result, so it has nothing true to say
        # about the cheapest trip. The merge is what reports.
        notify=not (args.no_notify or shard is not None),
    )
    raise SystemExit(0)


def _parse_shard(value: str | None) -> tuple[int, int] | None:
    """"0/3" -> (0, 3). Rejected loudly: a typo here silently sweeps a fraction."""
    if not value:
        return None
    try:
        index, _, count = value.partition("/")
        shard = (int(index), int(count))
    except ValueError as exc:
        raise SystemExit(f"--shard must look like INDEX/COUNT, got {value!r}") from exc
    if shard[1] < 1 or not 0 <= shard[0] < shard[1]:
        raise SystemExit(f"--shard {value!r} is not one of 0..{shard[1] - 1} of {shard[1]}")
    return shard


if __name__ == "__main__":
    main()
