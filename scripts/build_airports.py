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

`countries.csv` from the same source is written to `data/countries.json`, so a
search for "Japan" can find NRT. The dataset stores only the ISO code on each
airport, which meant typing a country name returned nothing at all - the first
thing most people type when planning a trip somewhere they have not been.

`runways.csv` supplies the longest runway per airport, the only size signal in
the dataset worth anything. Ordering a country's airports alphabetically put
Tokyo Narita 22nd of 28 in Japan, behind Aomori and Saga; ordering by runway
puts NRT, KIX, NGO and HND in the top four. It is a proxy - there are no
passenger numbers here - but it is a measured one, and `type` alone is far too
coarse: 28 Japanese airports are tagged `large_airport`.

`keywords` carries the names people actually use. Without it "Tokyo" missed
Narita entirely - NRT's municipality is Narita - and "Bali" returned Krakow,
whose municipality really is Balice while Denpasar says "Bali" nowhere.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

BASE_URL = "https://davidmegginson.github.io/ourairports-data"
SOURCE_URL = f"{BASE_URL}/airports.csv"
COUNTRIES_URL = f"{BASE_URL}/countries.csv"
RUNWAYS_URL = f"{BASE_URL}/runways.csv"
OUTPUT = Path("data/airports.json")
COUNTRIES_OUTPUT = Path("data/countries.json")

# Kept on each entry so a search can rank a capital city's main hub above a
# regional field that happens to share a prefix.
SIZE_RANK = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "tickets-bot/airports"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URL
        return response.read().decode("utf-8")


def longest_runways(rows: list[dict]) -> dict[str, int]:
    """Longest usable runway in feet, keyed by airport `ident`."""
    longest: dict[str, int] = {}
    for row in rows:
        if (row.get("closed") or "").strip() == "1":
            continue
        try:
            length = int((row.get("length_ft") or "0").strip() or 0)
        except ValueError:
            continue
        ident = (row.get("airport_ident") or "").strip()
        if ident:
            longest[ident] = max(longest.get(ident, 0), length)
    return longest


# Enough to carry the common aliases without doubling the file. Anything longer
# is a full official name, which `name` already holds.
MAX_KEYWORD_LEN = 30
MAX_KEYWORDS = 6


def keywords_for(row: dict, iata: str, city: str) -> list[str]:
    """Alternate names, minus the ones already searchable through other fields."""
    known = {iata.casefold(), city.casefold()}
    out: list[str] = []
    for token in (row.get("keywords") or "").split(","):
        token = token.strip()
        folded = token.casefold()
        if not token or token.isdigit() or len(token) > MAX_KEYWORD_LEN:
            continue
        if folded in known:
            continue
        known.add(folded)
        out.append(token)
    return out[:MAX_KEYWORDS]


def build(rows: list[dict], runways: dict[str, int] | None = None) -> list[dict]:
    runways = runways or {}
    airports = []
    for row in rows:
        iata = (row.get("iata_code") or "").strip().upper()
        if len(iata) != 3 or not iata.isalpha():
            continue
        if (row.get("scheduled_service") or "").strip() != "yes":
            continue
        city = (row.get("municipality") or "").strip()
        airports.append(
            {
                "iata": iata,
                "name": (row.get("name") or "").strip(),
                "city": city,
                "country": (row.get("iso_country") or "").strip(),
                "rank": SIZE_RANK.get((row.get("type") or "").strip(), 3),
                "runway_ft": runways.get((row.get("ident") or "").strip(), 0),
                "keywords": keywords_for(row, iata, city),
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


def build_countries(rows: list[dict]) -> dict[str, str]:
    """`{"CZ": "Czechia", ...}` - what makes a search for "Japan" possible."""
    return {
        code: name
        for row in rows
        if (code := (row.get("code") or "").strip().upper())
        and (name := (row.get("name") or "").strip())
    }


def read_source(source: str) -> str:
    if source.startswith(("http://", "https://")):
        print(f"downloading {source}")
        return fetch(source)
    return Path(source).read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE_URL, help="URL or local CSV path")
    parser.add_argument("--countries", default=COUNTRIES_URL, help="URL or local CSV path")
    parser.add_argument("--runways", default=RUNWAYS_URL, help="URL or local CSV path")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--countries-output", type=Path, default=COUNTRIES_OUTPUT)
    args = parser.parse_args()

    rows = list(csv.DictReader(io.StringIO(read_source(str(args.source)))))
    runway_rows = list(csv.DictReader(io.StringIO(read_source(str(args.runways)))))
    airports = build(rows, longest_runways(runway_rows))
    if not airports:
        print("refusing to write an empty catalogue", file=sys.stderr)
        return 1

    country_rows = list(csv.DictReader(io.StringIO(read_source(str(args.countries)))))
    countries = build_countries(country_rows)
    if not countries:
        print("refusing to write an empty country list", file=sys.stderr)
        return 1

    for path, payload in ((args.output, airports), (args.countries_output, countries)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    size_kb = args.output.stat().st_size / 1024
    country_kb = args.countries_output.stat().st_size / 1024
    measured = sum(1 for a in airports if a["runway_ft"])
    print(f"{len(rows):,} rows in -> {len(airports):,} airports out ({size_kb:.0f} KB)")
    print(f"{len(runway_rows):,} runway rows in -> {measured:,} airports with a runway length")
    print(f"{len(country_rows):,} rows in -> {len(countries):,} countries out ({country_kb:.0f} KB)")
    print(f"wrote {args.output} and {args.countries_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
