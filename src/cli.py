from __future__ import annotations
import argparse
from pathlib import Path
from .config import get_settings
from .models import Offer
from .storage import Storage
from .providers import REGISTRY
from .scheduler import loop

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

def main():
    parser = argparse.ArgumentParser(description="Vietnam Tickets Scraper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scrape = sub.add_parser("scrape", help="Run a single scrape")
    p_scrape.add_argument("--provider", action="append", choices=list(REGISTRY.keys()))
    p_scrape.add_argument("--commit", action="store_true", help="Used by CI: exit 0 even if no data")

    p_watch = sub.add_parser("watch", help="Run forever with day/night intervals")
    p_watch.add_argument("--provider", action="append", choices=list(REGISTRY.keys()))

    args = parser.parse_args()
    settings = get_settings()

    providers = args.provider or list(REGISTRY.keys())

    if args.cmd == "scrape":
        new_count = run_once(providers)
        if args.commit:
            # CI wants success even if nothing new
            raise SystemExit(0)
        raise SystemExit(0 if new_count >= 0 else 1)

    if args.cmd == "watch":
        for _ in loop(settings.REFRESH_INTERVAL_DAYTIME_MINUTES, settings.REFRESH_INTERVAL_NIGHTTIME_MINUTES):
            run_once(providers)

if __name__ == "__main__":
    main()
