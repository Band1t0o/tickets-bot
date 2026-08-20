"""The scraper's moving parts, in a file you can edit without touching code.

Two things break a sweep and neither needs a programmer to fix, only the new
string: a site changing its URL grammar, and a site changing its markup. Both
live here.

**The defaults live in code, and the file only overrides them.** A missing or
corrupt `data/sources.json` degrades to exactly today's behaviour rather than
stopping a sweep - the same principle that keeps a corrupt `best.json` from
silencing a report. It also means a partial file is legitimate: fixing one
selector does not require restating the other seven, and an override you no
longer want can simply be deleted.

What is deliberately *not* here is waiting and timeout logic. pelikan races
offer cards against the site's own Czech "no flights" string, and that
behaviour is specific enough to the site that expressing it as configuration
would be inventing a language to describe one program. A genuinely new source
gets a provider class; this file keeps an existing one alive.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

# What a source is *for*, which decides what a broken one costs you.
#
# "sweep" is the price feed: if it breaks, the nightly run goes quiet and there
# is no data at all. "check" is a second opinion on a handful of legs, run by
# hand; if it breaks you lose the cross-check, not the prices. "none" is a site
# that was considered and is not connected - kept visible so the tab can say so
# rather than leaving you to wonder whether it is on.
ROLES = ("sweep", "check", "none")


@dataclass(frozen=True)
class Source:
    """Everything about one site that changes without its behaviour changing."""

    name: str
    base_url: str
    # Comma-joined segments appended to base_url. `{ret}` and its separator are
    # dropped for a one-way search.
    url_template: str
    selectors: dict[str, str] = field(default_factory=dict)
    # The site's own wording when a route genuinely has no inventory, as
    # distinct from a search that failed. Only the stable prefix is matched:
    # pelikan's copy mixes Czech and Slovak.
    no_results_marker: str = ""
    result_timeout_s: int = 120
    enabled: bool = True
    # What this source does for the project, and how a person should read it.
    role: str = "sweep"
    label: str = ""
    note: str = ""
    # False when the selectors below are not the whole story - a site driven
    # through its form rather than a deep link has its steps in code, and
    # offering an editable selector box for it would promise a repair the box
    # cannot make.
    repairable: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULTS: dict[str, Source] = {
    "PELIKAN": Source(
        name="PELIKAN",
        base_url="https://www.pelikan.cz/cs/letenky/",
        # T:1 round trip (needs DR), T:2 one-way. T:0 and R:0 return nothing.
        # Dates are bare integers: 2027_1_12, never 2027_01_12. CDF repeats the
        # origin while CDT prefixes the destination with "A"; the asymmetry is
        # the site's.
        url_template="T:{trip_type},P:{adults}000E_0_0,CDF:{origin}{origin},CDT:A{destination},DD:{depart}",
        selectors={
            "card": "div[id^='flight-']",
            "price": ".fly-search-price-info-wrapp",
            "date": ".fly-item-date-new-reservation",
            "time": ".fly-item-time-new-reservation",
            "baggage_icon": "img.baggage-img",
            "baggage_label": ".fly-item-bottom-baggage-new-reservation",
        },
        no_results_marker="Nenašli jsme žádn",
        # 75s was enough for a fast local search (~14s) but not under sweep
        # conditions.
        result_timeout_s=120,
        role="sweep",
        label="pelikan.cz",
        note=(
            "Where every price in this app comes from. Searched by navigating "
            "straight to a deep link, which is what makes a 483-search sweep "
            "affordable at all."
        ),
    ),
    "LETUSKA": Source(
        name="LETUSKA",
        base_url="https://www.letuska.cz",
        # No deep-link grammar exists: every plausible shape 404s and the search
        # is an Angular form whose results render in place. So there is no URL
        # template to repair, and the steps live in providers/letuska.py.
        url_template="",
        selectors={},
        no_results_marker="",
        result_timeout_s=120,
        role="check",
        label="letuska.cz",
        note=(
            "A second opinion on a handful of legs, run by hand with "
            "`src.cli verify`. It has no deep link - the search is a form that "
            "has to be driven - so it is far too slow to sweep, and its steps "
            "live in code rather than in a selector box."
        ),
        repairable=False,
    ),
    "SKYSCANNER": Source(
        name="SKYSCANNER",
        base_url="https://www.skyscanner.cz",
        url_template="",
        selectors={},
        no_results_marker="",
        role="none",
        label="Skyscanner",
        enabled=False,
        note=(
            "Considered as a second sweep source and not connected. Showing an "
            "editable form for a source that does not exist would be the same "
            "lie as a sweep reporting error_count: 0 while most of it failed - "
            "see docs/superpowers/specs/2026-08-11-second-source-spike.md."
        ),
        repairable=False,
    ),
}


def _path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "sources.json"


def load_overrides(data_dir: Path | str = "data") -> dict:
    """Whatever the file says, or {} if it says nothing usable."""
    path = _path(data_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Loud enough to notice in a log, quiet enough not to end the sweep.
        print(f"[sources] {path} is unreadable ({exc}); using built-in defaults")
        return {}
    return payload if isinstance(payload, dict) else {}


def load_source(name: str, data_dir: Path | str = "data") -> Source:
    """One source, with any overrides from disk applied over the defaults."""
    if name not in DEFAULTS:
        raise KeyError(f"{name!r} is not a known source; expected one of {sorted(DEFAULTS)}")

    base = DEFAULTS[name]
    override = load_overrides(data_dir).get(name)
    if not isinstance(override, dict):
        return base

    fields = set(Source.__dataclass_fields__) - {"name"}
    changes = {k: v for k, v in override.items() if k in fields and k != "selectors"}
    if isinstance(override.get("selectors"), dict):
        # Merged, not replaced: correcting one selector must not blank the rest.
        changes["selectors"] = {**base.selectors, **override["selectors"]}
    return replace(base, **changes)


def load_sources(data_dir: Path | str = "data") -> dict[str, Source]:
    return {name: load_source(name, data_dir) for name in DEFAULTS}


def save_sources(sources: dict[str, Source], data_dir: Path | str = "data") -> Path:
    """Write the file whole. Values equal to the default are still written, so
    what you see in the editor is what the sweep will use."""
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: source.to_dict() for name, source in sources.items()}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------- what a check found
#
# A card that says "unknown until you press the button" is a card you have to
# press a button on to learn anything, so every check is written down and read
# back on load. The file is a cache of observations, never configuration: losing
# it costs you the history of checks, never a working sweep.

CHECKS_FILE = "source_checks.json"


def _checks_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / CHECKS_FILE


def load_checks(data_dir: Path | str = "data") -> dict:
    """The last check recorded for each source, or {} if there is no usable file."""
    path = _checks_path(data_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[sources] {path} is unreadable ({exc}); treating every source as unchecked")
        return {}
    return payload if isinstance(payload, dict) else {}


def save_check(name: str, outcome: dict, data_dir: Path | str = "data") -> dict:
    """Record what a check of `name` found, and return every check on file.

    Read-modify-write rather than a whole-file overwrite: checking pelikan must
    not erase what the last letuska check found.
    """
    checks = load_checks(data_dir)
    checks[name] = outcome
    path = _checks_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return checks
