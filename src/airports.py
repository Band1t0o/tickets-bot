"""Airport lookup over the committed OurAirports extract.

Replaces a hardcoded list of nine European airports plus Japan and the
Philippines. Any airport with scheduled service is now reachable, which is what
lets a scenario name arbitrary places; see `scripts/build_airports.py` for how
`data/airports.json` is produced and why it is committed rather than fetched.

The measured notes from the old catalogue survive in `data/airport_notes.json`.
They were worth keeping because they were *measured* - "Brno has no long-haul
inventory in either direction" is a fact about the world that a search UI should
surface rather than make someone rediscover. Everything a sweep can establish is
derived from sweep history instead; see `viability.py`.
"""
from __future__ import annotations

import json
from collections import Counter
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path("data")
CATALOGUE_FILE = "airports.json"
COUNTRIES_FILE = "countries.json"
NOTES_FILE = "airport_notes.json"


@lru_cache(maxsize=4)
def _raw_catalogue(data_dir: Path = DATA_DIR) -> list[dict]:
    path = Path(data_dir) / CATALOGUE_FILE
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def load_countries(data_dir: Path = DATA_DIR) -> dict[str, str]:
    """`{"CZ": "Czechia", ...}`, empty if the file has not been built yet."""
    path = Path(data_dir) / COUNTRIES_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def load_catalogue(data_dir: Path = DATA_DIR) -> list[dict]:
    """Every airport, each carrying its country's full name.

    The dataset stores only `iso_country`, so "Japan" matched nothing - which is
    the first thing you type when planning a trip somewhere you have not been.
    """
    countries = load_countries(data_dir)
    return [
        {**airport, "country_name": countries.get(airport["country"], airport["country"])}
        for airport in _raw_catalogue(data_dir)
    ]


@lru_cache(maxsize=4)
def _by_code(data_dir: Path = DATA_DIR) -> dict[str, dict]:
    return {airport["iata"]: airport for airport in load_catalogue(data_dir)}


@lru_cache(maxsize=4)
def load_notes(data_dir: Path = DATA_DIR) -> dict[str, dict]:
    path = Path(data_dir) / NOTES_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("airports", {})


def lookup(code: str, data_dir: Path = DATA_DIR) -> dict | None:
    return _by_code(data_dir).get(code.strip().upper())


def describe(code: str, data_dir: Path = DATA_DIR) -> str:
    """"PRG — Prague, Czechia", or just the code when it is not in the catalogue."""
    airport = lookup(code, data_dir)
    if not airport:
        return code
    where = ", ".join(part for part in (airport["city"], airport["country_name"]) if part)
    return f"{airport['iata']} — {where}" if where else airport["iata"]


# Bands, best first.
#
# An exact place name outranks a prefix, which is what stops "Bali" resolving to
# Krakow: Krakow's municipality really is Balice, so it is a city *prefix* match,
# while Denpasar carries "Bali" as an exact alias. A country match is last so
# that "Prague" still puts PRG on top instead of every airport in Czechia.
_CODE_EXACT, _PLACE_EXACT, _CODE_PREFIX, _PLACE_PREFIX, _CONTAINS, _COUNTRY = range(6)


def search_with_meta(query: str, limit: int = 20, data_dir: Path = DATA_DIR) -> dict:
    """Ranked matches plus what got cut, so the UI can say what it is hiding.

    Ranked so that typing "PRA" surfaces Prague's main airport rather than an
    alphabetically earlier regional field: exact code first, then code prefix,
    then city prefix, then anything containing the query, and finally airports
    in a country whose name matches - within each band, larger airports first.

    `country` names the country when the query matched one, so "20 of 34 in
    Japan" can be shown instead of a silently truncated list. Ties inside a band
    break on the longest runway, which is the only size signal the dataset
    carries: `type` alone tags 28 Japanese airports `large_airport`, and
    alphabetical order within that put Narita 22nd, behind Aomori and Saga.
    """
    needle = query.strip().casefold()
    if not needle:
        return {"airports": [], "total": 0, "country": None}

    matched_country: str | None = None
    scored: list[tuple[tuple[int, int, int, str], dict]] = []
    for airport in load_catalogue(data_dir):
        code = airport["iata"].casefold()
        city = airport["city"].casefold()
        name = airport["name"].casefold()
        country = airport["country_name"].casefold()
        # The names people use rather than the official ones: without these,
        # "Tokyo" missed Narita, whose municipality is Narita.
        aliases = [alias.casefold() for alias in airport.get("keywords", [])]

        if code == needle:
            band = _CODE_EXACT
        elif city == needle or needle in aliases:
            band = _PLACE_EXACT
        elif code.startswith(needle):
            band = _CODE_PREFIX
        elif city.startswith(needle) or any(a.startswith(needle) for a in aliases):
            band = _PLACE_PREFIX
        elif needle in city or needle in name:
            band = _CONTAINS
        elif country.startswith(needle):
            band = _COUNTRY
            matched_country = airport["country_name"]
        else:
            continue
        scored.append((
            (band, airport.get("rank", 3), -airport.get("runway_ft", 0), airport["iata"]),
            airport,
        ))

    scored.sort(key=lambda pair: pair[0])
    return {
        "airports": [airport for _, airport in scored[:limit]],
        "total": len(scored),
        "country": matched_country,
    }


def search(query: str, limit: int = 20, data_dir: Path = DATA_DIR) -> list[dict]:
    """Airports matching `query` by IATA code, city, name or country."""
    return search_with_meta(query, limit=limit, data_dir=data_dir)["airports"]


def frequent_airports(
    scenario_dir: Path, data_dir: Path = DATA_DIR, limit: int = 12
) -> dict[str, list[dict]]:
    """Airports you actually use, for one-click chips beside the typeahead.

    A checkbox grid of nine European airports was genuinely faster than typing
    for the departure airports, which barely change. This restores that without
    hardcoding anything: it counts what the saved scenarios already use, split
    by the position they were used in, so it follows you when you start flying
    from somewhere else. `airport_notes.json` seeds it, because those airports
    were measured by hand and never swept - nothing sweeps an airport it cannot
    use, so sweep history alone would forget them.
    """
    from .scenario import load_scenarios  # local: scenario imports nothing from here

    departures: Counter[str] = Counter()
    destinations: Counter[str] = Counter()
    try:
        scenarios = load_scenarios(scenario_dir)
    except (OSError, ValueError, TypeError, KeyError):
        scenarios = []

    for scenario in scenarios:
        departures.update(scenario.origins)
        departures.update(scenario.return_to or [])
        for stop in scenario.stops:
            destinations.update(stop.airports)

    # An airport only ever flown *to* is not a departure suggestion. The notes
    # record no direction, and MNL and CEB are in there as measured arrivals.
    for code, note in load_notes(data_dir).items():
        if note.get("verdict") == "ok" and code not in destinations:
            departures.setdefault(code, 0)

    def top(counts: Counter[str]) -> list[dict]:
        ordered = sorted(counts, key=lambda code: (-counts[code], code))
        found = (lookup(code, data_dir) for code in ordered)
        return [airport for airport in found if airport][:limit]

    return {"origins": top(departures), "destinations": top(destinations)}
