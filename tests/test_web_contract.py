"""The page and the server it talks to must be the same generation.

This exists because of a real hour lost. Two `uvicorn` processes were left
running from an earlier commit. Static files are read from disk per request, so
both served the *newest* HTML and JS; only the Python was frozen at import time.
The page therefore asked a stale server for `/api/sources` and for scenarios
carrying fields that version had never heard of, got 404 and 400 back, and
rendered an empty trip picker and an empty Prices tab.

An empty picker is what "you have no saved trips" looks like. So the app said
"your data is gone" when the truth was "restart me". Nothing on disk had
changed.

The fix is a single integer both sides carry. These tests keep the two copies
honest, because a constant that only one side ever updates detects nothing.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from src.web.app import API_CONTRACT, app

STATIC = Path(__file__).resolve().parents[1] / "src" / "web" / "static"


def _js_contract() -> int:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    match = re.search(r"const EXPECTED_CONTRACT\s*=\s*(\d+)", source)
    assert match, "app.js must declare `const EXPECTED_CONTRACT = <n>`"
    return int(match.group(1))


def test_the_page_and_the_server_agree_on_the_contract():
    """Bumping one side without the other would make every page look stale."""
    assert _js_contract() == API_CONTRACT


def test_version_endpoint_reports_the_contract():
    body = TestClient(app).get("/api/version").json()
    assert body["contract"] == API_CONTRACT


def test_version_endpoint_is_cheap_enough_to_ask_first():
    """It runs before anything else on every page load, so it must not touch
    the filesystem or a scenario file - the very things that may be broken."""
    body = TestClient(app).get("/api/version").json()
    assert set(body) == {"contract"}


# --------------------------------------------------------- one control, one wording
#
# These are not style policing. Each guards a drift that was actually on screen:
# the same depth select spelled two different ways on two tabs, and thirty-one
# hand-set margins that made every panel sit at a slightly different height from
# the one beside it.


def _html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _depth_options(select_id: str) -> list[str]:
    """The option labels of one depth select, in the order they are offered."""
    source = _html()
    start = source.index(f'id="{select_id}"')
    block = source[start : source.index("</select>", start)]
    return re.findall(r"<option[^>]*>(.*?)</option>", block)


def test_both_depth_selects_offer_the_same_words():
    """One control, two panels. It read `Quick - a few dates` on Map it out and
    `quick - every 7 days` on Final sweeps: same value, different case, different
    order, and one of them declining to say what the sampling step actually is."""
    assert _depth_options("depth") == _depth_options("final-depth")


def test_the_depth_options_say_their_sampling_step():
    """`a few dates` is the one label that cannot be checked against a plan."""
    for option in _depth_options("depth"):
        assert "every" in option, f"{option!r} does not say how often it samples"


def test_the_markup_carries_no_hand_set_margins():
    """Spacing belongs to the stylesheet, where it can be one scale.

    Inline, it was twelve different values - 0, 6, 8, 10, 12, 14, 16, 18, 20, 22 -
    chosen a panel at a time, which is why no two sub-sections on the page began
    at the same distance from the heading above them."""
    offenders = re.findall(r'style="[^"]*margin[^"]*"', _html())
    assert offenders == [], f"inline margins left in index.html: {offenders}"


def test_every_colour_the_stylesheets_use_is_a_token_that_exists():
    """A `var(--name)` with no definition is not a failure anywhere - it
    resolves to `currentColor`, or to nothing, and the page still draws.

    Six of them had accumulated: `--color-border` bordered the airport chips and
    the stop cards in whatever the text colour happened to be, and
    `--color-surface` left the airport dropdown with no background at all. All
    six looked deliberate on screen, which is exactly why they lasted.
    """
    styles = STATIC / "styles"
    defined: set[str] = set()
    used: set[str] = set()
    for sheet in sorted(styles.glob("*.css")):
        # Comments stripped first. The note that recorded this very problem
        # wrote `--color-border:` in prose, and an earlier version of this
        # audit read that as the definition and reported nothing wrong.
        body = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
        defined |= set(re.findall(r"(--[\w-]+)\s*:", body))
        used |= set(re.findall(r"var\((--[\w-]+)", body))
    assert not (used - defined), f"used but never defined: {sorted(used - defined)}"


# The tab bar lost a step and four paragraphs went on describing it. "The step
# after this one" named a tab that no longer exists, "a step back" and "the step
# before" named the switch you had just flipped, and "run one above" pointed at
# buttons on the other side of it. The page was the only documentation of a
# layout it had stopped having.
STALE_STEP_WORDING = [
    "step after this",
    "a step back",
    "the step before",
    "Run one above",
    "run one above",
    # The switch between the two populations, deleted when the broad runs moved
    # to Map it out. An empty state went on telling people to flip it for
    # another session, because this list only ever read the markup and that
    # sentence is written by a renderer.
    "Switch to",
    "the whole window”",
    "Run a narrow sweep here",
]


def test_no_screen_describes_a_step_or_a_switch_that_was_merged_away():
    """Both files, because half this app's prose is written by a renderer.

    Checking only the markup is why "Switch to the whole window and press Run a
    narrow sweep here" survived the switch being deleted: it lives in a template
    literal in `app.js`, and nothing was looking there.
    """
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    # Comments record why wording is what it is, including wording that was
    # replaced, and those quotations are not on screen.
    visible = re.sub(r"<!--.*?-->", "", markup, flags=re.S)
    # Two passes, and a class that cannot match a newline for the line
    # comments. With `re.S` a single `//.*$` is greedy across newlines and
    # backtracks only to the *last* line end in the file, so one `//` near the
    # top erases everything after it - which is exactly what this test did on
    # its first outing: it passed against a deliberately reintroduced stale
    # string, and proving that it bites is the only reason it was caught.
    script = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
    script = re.sub(r"^[ \t]*//[^\r\n]*$", "", script, flags=re.M)
    visible += script
    found = [phrase for phrase in STALE_STEP_WORDING if phrase in visible]
    assert not found, f"the page still points at something that was merged away: {found}"
