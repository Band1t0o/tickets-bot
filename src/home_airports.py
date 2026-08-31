"""The airports near home, ranked by how easy they are to get to.

Not a record of what you have used - `airports.frequent_airports` already counts
that, off the saved trips - and not `Scenario.preferred_origins`, which ranks by
which airport you would rather Discord report about. This is the third and most
basic axis, and it is the one nothing knew: Brno is a tram ride, Prague is a
morning, Vienna is a coach. Frequency cannot discover that, because the reason a
convenient airport goes unused is usually that it has no inventory.

Global rather than per trip, because it is a fact about where you live and not
about any one journey.

**Tiered, not flat.** Two airports can be equally awkward - Prague and Vienna are
both a morning - and forcing a strict order on them invents a preference nobody
holds. Tiers are also the shape `Scenario.preferred_origins` already has, which
is the point: a trip that says nothing about which airport it would rather be
reported from inherits these verbatim, with no conversion and no second list to
keep in step. That duplication is what this shape removes.

The flat order is still what the *Depart from* chips want, and `load_ranking`
still answers it by flattening.

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


def load_tiers(data_dir: Path | str = DATA_DIR) -> list[list[str]]:
    """Your airports in tiers, best first. Empty when nothing is set.

    Empty is a legitimate answer and not a failure: it means "no ranking", and
    every caller falls back to what it did before. So is a corrupt file - a
    hand-edited JSON with a trailing comma should cost you the ordering of some
    chips, not the ability to build a trip.

    Reads both shapes. `{"tiers": [["BRQ"], ["PRG", "VIE"]]}` is what is written
    now; `{"airports": [...]}` is every file written before tiers existed, and
    it reads as one airport per tier, which is exactly what it meant. So there
    is nothing to migrate and an older checkout keeps working.
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

    raw = payload.get("tiers")
    if not isinstance(raw, list):
        flat = payload.get("airports")
        if not isinstance(flat, list):
            return []
        raw = [[code] for code in flat]

    # Filtered rather than rejected wholesale, and deduplicated keeping the
    # first mention: rank is position, so one airport appearing twice would
    # otherwise have two ranks and the reading would depend on which loop found
    # it. Same reason `preferred_origins` refuses a repeated code outright -
    # this one is repaired instead, because nobody typed it into a form.
    seen: set[str] = set()
    tiers: list[list[str]] = []
    for group in raw:
        # A bare string where a tier is expected is a plausible hand edit, and
        # it means a tier of one.
        codes = [group] if isinstance(group, str) else group
        if not isinstance(codes, list):
            continue
        tier: list[str] = []
        for code in codes:
            code = str(code).strip().upper()
            if IATA_RE.match(code) and code not in seen:
                seen.add(code)
                tier.append(code)
        # An empty tier is a hole in the ranking, not a rank nothing occupies.
        if tier:
            tiers.append(tier)
    return tiers


def load_ranking(data_dir: Path | str = DATA_DIR) -> list[str]:
    """The same airports as one flat order, most convenient first.

    What the *Depart from* chips want: they are a row of buttons, and a row has
    no way to show two airports at the same rank. Within a tier the order is the
    order it was typed in, which is arbitrary and says nothing - that is what
    being in one tier means.
    """
    return [code for tier in load_tiers(data_dir) for code in tier]


def save_tiers(tiers: list[list[str]], data_dir: Path | str = DATA_DIR) -> list[list[str]]:
    """Write the ranking, refusing anything that is not an airport code.

    Refused by name rather than filtered, unlike the read path. A typo typed
    into the form is a mistake worth telling someone about; a typo already on
    disk is one they cannot act on from the page they are looking at.

    An empty tier is dropped rather than refused: a row added and never filled
    is an unfinished edit, and that is already how the trip's own tier editor
    treats one.
    """
    written: list[list[str]] = []
    seen: dict[str, int] = {}
    for rank, group in enumerate(tiers, start=1):
        if isinstance(group, str):
            group = [group]
        if not isinstance(group, list):
            raise ValueError(f"tier {rank} is not a list of airport codes")
        tier: list[str] = []
        for raw in group:
            code = str(raw).strip().upper()
            if not IATA_RE.match(code):
                raise ValueError(f"{raw!r} is not a 3-letter IATA airport code")
            # Otherwise "which tier is this airport in" has two answers and the
            # reading depends on iteration order - the rule `preferred_origins`
            # already enforces, for the same reason.
            if code in seen:
                raise ValueError(
                    f"{code} is in the ranking twice, at tier {seen[code]} and tier "
                    f"{rank}; an airport has one place in it"
                )
            seen[code] = rank
            tier.append(code)
        if tier:
            written.append(tier)

    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / RANKING_FILE).write_text(
        json.dumps({"tiers": written}, indent=2) + "\n", encoding="utf-8"
    )
    return written
