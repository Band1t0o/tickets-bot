"""Regenerate `data/airports.json` from the OurAirports dataset.

Run when the catalogue needs refreshing; the output is committed, so nothing at
runtime ever touches the network:

    python scripts/build_airports.py

Source: https://ourairports.com/data/ - released into the **public domain**, no
account, no API key, no rate limit. That is why it was chosen over a commercial
airport API: a personal tool should not acquire a credential and a quota to
answer "which airports exist".

The filter is `iata_code` present and `scheduled_service == "yes"`. Both matter.
Roughly 80,000 rows describe every airfield on record, most of them airstrips
with no commercial flights and no IATA code - offering them in a picker would
bury the airports someone might actually fly from.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

SOURCE_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OUTPUT = Path("data/airports.json")

# Kept on each entry so a search can rank a capital city's main hub above a
# regional field that happens to share a prefix.
SIZE_RANK = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "tickets-bot/airports"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URL
        return response.read().decode("utf-8")


def build(rows: list[dict]) -> list[dict]:
    airports = []
    for row in rows:
        iata = (row.get("iata_code") or "").strip().upper()
        if len(iata) != 3 or not iata.isalpha():
            continue
        if (row.get("scheduled_service") or "").strip() != "yes":
            continue
        airports.append(
            {
                "iata": iata,
                "name": (row.get("name") or "").strip(),
                "city": (row.get("municipality") or "").strip(),
                "country": (row.get("iso_country") or "").strip(),
                "rank": SIZE_RANK.get((row.get("type") or "").strip(), 3),
            }
        )
    # Deduplicate on IATA, keeping the largest. The dataset has a handful of
    # collisions where a closed field kept a code a bigger airport now uses.
    best: dict[str, dict] = {}
    for airport in airports:
        current = best.get(airport["iata"])
        if current is None or airport["rank"] < current["rank"]:
            best[airport["iata"]] = airport
    return sorted(best.values(), key=lambda a: a["iata"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE_URL, help="URL or local CSV path")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    source = str(args.source)
    if source.startswith(("http://", "https://")):
        print(f"downloading {source}")
        text = fetch(source)
    else:
        text = Path(source).read_text(encoding="utf-8")

    rows = list(csv.DictReader(io.StringIO(text)))
    airports = build(rows)
    if not airports:
        print("refusing to write an empty catalogue", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(airports, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    size_kb = args.output.stat().st_size / 1024
    print(f"{len(rows):,} rows in -> {len(airports):,} airports out ({size_kb:.0f} KB)")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
