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
    from .sweep.planner import estimate_minutes, plan_exploration, plan_searches
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

    searches = plan_exploration(scenario) if mode == "explore" else plan_searches(scenario)
    planned = len(searches)
    if shard is not None:
        searches = shard_of(searches, *shard)
    minutes = estimate_minutes(searches)
    label = "explore" if mode == "explore" else f"depth={scenario.depth}"
    share = f" (shard {shard[0] + 1}/{shard[1]} of {planned})" if shard else ""
    focus = (
        f" focused {scenario.focus_start}..{scenario.focus_end}"
        if scenario.focus_start
        else ""
    )
    print(f"[{scenario.id}] {label}{focus} → {len(searches)} searches{share}, ~{minutes} min")

    if dry_run:
        # Deliberately exits before launching a browser, so the Actions budget
        # can be checked without spending any of it.
        pools = scenario.airport_pools
        for leg_index in sorted({s.leg_index for s in searches}):
            count = sum(1 for s in searches if s.leg_index == leg_index)
            route = f"{'/'.join(pools[leg_index])} → {'/'.join(pools[leg_index + 1])}"
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
    shards = sorted(p.parent for p in shard_root.rglob("status.json"))
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
    parser = argparse.ArgumentParser(description="Flight scenario watcher")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sweep = sub.add_parser("sweep", help="Run a sweep for one scenario")
    p_sweep.add_argument("--scenario", required=True)
    p_sweep.add_argument("--depth", choices=["quick", "standard", "deep"],
                         help="Override the scenario's depth")
    p_sweep.add_argument("--mode", choices=["sweep", "explore"], default="sweep",
                         help="explore: every route on three dates, to see which "
                              "airports are worth a real sweep at all")
    p_sweep.add_argument("--max-minutes", type=float,
                         help="Wall-clock budget. The sweep stops cleanly when it runs out, "
                              "keeping what it found, instead of being killed mid-search")
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

    if args.cmd == "check-price":
        check_price_command(args.origin, args.destination, args.depart, args.ret)
        raise SystemExit(0)

    shard = _parse_shard(args.shard)
    run_sweep_command(
        args.scenario,
        args.depth,
        args.dry_run,
        args.mode,
        args.max_minutes,
        shard=shard,
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
