"""The airports near home, ranked by how easy they are to get to.

Not a record of what you have used - `airports.frequent_airports` already counts
that, off the saved trips - and not `Scenario.preferred_origins`, which ranks by
which airport you would rather Discord report about. This is the third and most
basic axis, and it is the one nothing knew: Brno is a tram ride, Prague is a
morning, Vienna is a coach. Frequency cannot discover that, because the reason a
convenient airport goes unused is usually that it has no inventory.

Global rather than per trip, because it is a fact about where you live and not
about any one journey.

**The defaults live in code and the file only overrides them**, the same shape
as `sources.py`: a missing or corrupt `data/home_airports.json` degrades to
exactly the previous behaviour - suggestions built from what your trips already
use - rather than emptying the chips beside every airport box.
"""
from __future__ import annotations

import json
from pathlib import Path

from .scenario import IATA_RE

DATA_DIR = Path("data")
RANKING_FILE = "home_airports.json"


def load_ranking(data_dir: Path | str = DATA_DIR) -> list[str]:
    """Your airports, most convenient first. Empty when nothing is set.

    Empty is a legitimate answer and not a failure: it means "no ranking", and
    every caller falls back to what it did before. So is a corrupt file - a
    hand-edited JSON with a trailing comma should cost you the ordering of some
    chips, not the ability to build a trip.
    """
    path = Path(data_dir) / RANKING_FILE
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    codes = payload.get("airports")
    if not isinstance(codes, list):
        return []
    # Filtered rather than rejected wholesale, and deduplicated keeping the
    # first mention: rank is position, so one airport appearing twice would
    # otherwise have two ranks and the reading would depend on which loop found
    # it. Same reason `preferred_origins` refuses a repeated code outright -
    # this one is repaired instead, because nobody typed it into a form.
    seen: set[str] = set()
    ranked: list[str] = []
    for code in codes:
        code = str(code).strip().upper()
        if IATA_RE.match(code) and code not in seen:
            seen.add(code)
            ranked.append(code)
    return ranked


def save_ranking(codes: list[str], data_dir: Path | str = DATA_DIR) -> list[str]:
    """Write the ranking, refusing anything that is not an airport code.

    Refused by name rather than filtered, unlike the read path. A typo typed
    into the form is a mistake worth telling someone about; a typo already on
    disk is one they cannot act on from the page they are looking at.
    """
    ranked: list[str] = []
    for raw in codes:
        code = str(raw).strip().upper()
        if not IATA_RE.match(code):
            raise ValueError(f"{raw!r} is not a 3-letter IATA airport code")
        if code in ranked:
            raise ValueError(f"{code} is in the list twice; an airport has one place in it")
        ranked.append(code)

    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / RANKING_FILE).write_text(
        json.dumps({"airports": ranked}, indent=2) + "\n", encoding="utf-8"
    )
    return ranked
