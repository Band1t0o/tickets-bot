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
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path("data")
CATALOGUE_FILE = "airports.json"
NOTES_FILE = "airport_notes.json"


@lru_cache(maxsize=4)
def load_catalogue(data_dir: Path = DATA_DIR) -> list[dict]:
    path = Path(data_dir) / CATALOGUE_FILE
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


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
    """"PRG — Prague, CZ", or just the code when it is not in the catalogue."""
    airport = lookup(code, data_dir)
    if not airport:
        return code
    where = ", ".join(part for part in (airport["city"], airport["country"]) if part)
    return f"{airport['iata']} — {where}" if where else airport["iata"]


def search(query: str, limit: int = 20, data_dir: Path = DATA_DIR) -> list[dict]:
    """Airports matching `query` by IATA code, city or name.

    Ranked so that typing "PRA" surfaces Prague's main airport rather than an
    alphabetically earlier regional field: exact code first, then code prefix,
    then city prefix, then anything containing the query - and within each
    band, larger airports first.
    """
    needle = query.strip().casefold()
    if not needle:
        return []

    scored: list[tuple[tuple[int, int, str], dict]] = []
    for airport in load_catalogue(data_dir):
        code = airport["iata"].casefold()
        city = airport["city"].casefold()
        name = airport["name"].casefold()

        if code == needle:
            band = 0
        elif code.startswith(needle):
            band = 1
        elif city.startswith(needle):
            band = 2
        elif needle in city or needle in name:
            band = 3
        else:
            continue
        scored.append(((band, airport.get("rank", 3), airport["iata"]), airport))

    scored.sort(key=lambda pair: pair[0])
    return [airport for _, airport in scored[:limit]]
