from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .config import get_settings
from .models import Offer
from .providers import REGISTRY
from .scheduler import loop
from .storage import Storage


def run_once(providers: list[str]) -> int:
    settings = get_settings()
    store = Storage(settings.DATA_DIR, settings.SEEN_FILE)

    # Get all origin-destination combinations
    origins = settings.get_origins()
    destinations = settings.get_destinations()

    print(f"Tracking {len(origins)} origin(s) × {len(destinations)} destination(s) = {len(origins) * len(destinations)} route(s)")

    all_offers: list[Offer] = []

    # Iterate over all origin-destination combinations
    for origin in origins:
        for destination in destinations:
            print(f"\n=== Route: {origin} → {destination} ===")

            for name in providers:
                Provider = REGISTRY[name]
                p = Provider()
                print(f"[{name}] Scraping {origin} → {destination}...")

                offers = list(p.scrape(origin, destination, settings.DEPARTURE_DATE, settings.ADULTS, settings.ARRIVAL_DATE))
                if len(offers) == 0:
                    print(f"[{name}] No offers found for {origin} → {destination}")
                    continue
                new_only = [o for o in offers if store.is_new(o)]
                all_offers.extend(new_only)

                if new_only:
                    stem = f"{origin}-{destination}-{settings.DEPARTURE_DATE}_{name}"
                    csv_path, jsonl_path = store.write(new_only, stem)
                    print(f"[{name}] wrote {len(new_only)} offers -> {csv_path}, {jsonl_path}")
                else:
                    print(f"[{name}] no new offers")

    if all_offers:
        store.mark_seen(all_offers)

    print(f"\n✓ Total new offers across all routes: {len(all_offers)}")

    # Write offer count to a file for GitHub Actions to read
    offer_count_file = Path(settings.DATA_DIR) / "latest_offer_count.txt"
    offer_count_file.parent.mkdir(parents=True, exist_ok=True)
    offer_count_file.write_text(str(len(all_offers)))

    return len(all_offers)

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
        for leg_index in sorted({s.leg_index for s in searches}):
            count = sum(1 for s in searches if s.leg_index == leg_index)
            print(f"  leg {leg_index}: {count} searches")
        raise SystemExit(0)

    result = run_sweep(
        scenario,
        on_progress=lambda done, total, label: (
            print(f"  [{done}/{total}] {label}", flush=True) if done % 10 == 0 else None
        ),
    )
    print(f"[{scenario.id}] {len(result.legs)} legs, {len(result.errors)} errors → {result.directory}")
    if not result.is_healthy:
        print(f"[{scenario.id}] WARNING: sweep looks unhealthy (no legs, or majority failed)")

    from .notify_discord import notify_sweep

    notify_sweep(scenario, result)
    return len(result.legs)


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


def main():
    _force_utf8_output()
    parser = argparse.ArgumentParser(description="Flight scenario watcher")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scrape = sub.add_parser("scrape", help="Legacy single round-trip scrape")
    p_scrape.add_argument("--provider", action="append", choices=list(REGISTRY.keys()))
    p_scrape.add_argument("--commit", action="store_true", help="Used by CI: exit 0 even if no data")

    p_watch = sub.add_parser("watch", help="Run forever with day/night intervals")
    p_watch.add_argument("--provider", action="append", choices=list(REGISTRY.keys()))

    p_sweep = sub.add_parser("sweep", help="Run a sweep for one scenario")
    p_sweep.add_argument("--scenario", required=True)
    p_sweep.add_argument("--depth", choices=["quick", "standard", "deep"],
                         help="Override the scenario's depth")
    p_sweep.add_argument("--dry-run", action="store_true",
                         help="Print the planned search count and estimate, then exit")

    sub.add_parser("probe", help="Sample the fixed volatility-probe routes once")
    sub.add_parser("probe-report", help="Summarise how much probe prices have moved")

    args = parser.parse_args()

    if args.cmd == "probe":
        from .probe import run_probe

        run_probe()
        raise SystemExit(0)

    if args.cmd == "probe-report":
        from .probe import format_report, probe_report

        print(format_report(probe_report()))
        raise SystemExit(0)

    # Settings are loaded lazily: they require ORIGIN/DESTINATION/date env vars
    # that only the legacy scrape and watch commands still use. Loading them up
    # front would make `sweep` fail in CI, where those secrets no longer exist.
    if args.cmd == "sweep":
        run_sweep_command(args.scenario, args.depth, args.dry_run)
        raise SystemExit(0)

    providers = args.provider or list(REGISTRY.keys())

    if args.cmd == "scrape":
        new_count = run_once(providers)
        if args.commit:
            # CI wants success even if nothing new
            raise SystemExit(0)
        raise SystemExit(0 if new_count >= 0 else 1)

    if args.cmd == "watch":
        settings = get_settings()
        for _ in loop(settings.REFRESH_INTERVAL_DAYTIME_MINUTES, settings.REFRESH_INTERVAL_NIGHTTIME_MINUTES):
            run_once(providers)

if __name__ == "__main__":
    main()
