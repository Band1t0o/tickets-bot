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


def run_sweep_command(scenario_id: str, depth: str | None, dry_run: bool) -> int:
    """Run one scenario's sweep. Returns the number of legs found."""
    from .scenario import load_scenario
    from .sweep.planner import estimate_minutes, plan_searches
    from .sweep.runner import run_sweep

    path = Path("scenarios") / f"{scenario_id}.json"
    if not path.exists():
        print(f"No scenario named {scenario_id!r} in scenarios/")
        raise SystemExit(2)

    scenario = load_scenario(path)
    if depth:
        scenario = replace(scenario, depth=depth)
    scenario.validate()

    searches = plan_searches(scenario)
    minutes = estimate_minutes(searches)
    print(f"[{scenario.id}] depth={scenario.depth} → {len(searches)} searches, ~{minutes} min")

    if dry_run:
        # Deliberately exits before launching a browser, so the Actions budget
        # can be checked without spending any of it.
        pools = scenario.airport_pools
        for leg_index in sorted({s.leg_index for s in searches}):
            count = sum(1 for s in searches if s.leg_index == leg_index)
            route = f"{'/'.join(pools[leg_index])} → {'/'.join(pools[leg_index + 1])}"
            print(f"  leg {leg_index} {route}: {count} searches")
        raise SystemExit(0)

    result = run_sweep(
        scenario,
        on_progress=lambda done, total, label: (
            print(f"  [{done}/{total}] {label}", flush=True) if done % 10 == 0 else None
        ),
    )
    print(f"[{scenario.id}] {len(result.legs)} legs, {len(result.errors)} errors → {result.directory}")
    if result.routes_with_no_results:
        # A route searched on every date without ever returning an offer is
        # breakage, not a quiet market, and it reads as neither in a leg count.
        print(f"[{scenario.id}] routes that returned nothing: "
              f"{', '.join(result.routes_with_no_results)}")
    if not result.is_healthy:
        print(f"[{scenario.id}] WARNING: sweep looks unhealthy (no legs, or majority failed)")

    from .notify_discord import notify_sweep

    notify_sweep(scenario, result)
    return len(result.legs)


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
    p_sweep.add_argument("--dry-run", action="store_true",
                         help="Print the planned search count and estimate, then exit")

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

    args = parser.parse_args()

    if args.cmd == "health-gate":
        raise SystemExit(health_gate_command(args.scenario, args.min_legs_per_search))

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

    run_sweep_command(args.scenario, args.depth, args.dry_run)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
