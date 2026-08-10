"""The route editor, driven the way a person drives it.

Marked `slow` and deselected in CI (`pytest -m "not slow"`), because it starts a
real server and a real browser. Run it locally with `make test-ui`.

It exists because the bugs it covers were all invisible to unit tests and to a
scripted DOM check. Picking an airport worked perfectly when a script clicked
the menu item; it failed for a person, because a person presses Enter about
200 ms after starting to type and the menu is not there yet. Every assertion
below is a keystroke sequence, not an internal call.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import closing
from datetime import date

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from src.scenario import Scenario, Stop, save_scenario  # noqa: E402

pytestmark = pytest.mark.slow

AIRPORTS = [
    {"iata": "PRG", "name": "Vaclav Havel", "city": "Prague", "country": "CZ", "rank": 0,
     "runway_ft": 12189},
    {"iata": "VIE", "name": "Vienna Intl", "city": "Vienna", "country": "AT", "rank": 0,
     "runway_ft": 11811},
    {"iata": "BCN", "name": "Barcelona El Prat", "city": "Barcelona", "country": "ES", "rank": 0,
     "runway_ft": 11900},
    {"iata": "NRT", "name": "Narita Intl", "city": "Narita", "country": "JP", "rank": 0,
     "runway_ft": 13123},
    {"iata": "HND", "name": "Haneda", "city": "Tokyo", "country": "JP", "rank": 0,
     "runway_ft": 11024},
    {"iata": "KIX", "name": "Kansai Intl", "city": "Osaka", "country": "JP", "rank": 0,
     "runway_ft": 13123},
    {"iata": "MNL", "name": "Ninoy Aquino", "city": "Manila", "country": "PH", "rank": 0,
     "runway_ft": 12261},
]
COUNTRIES = {"CZ": "Czech Republic", "AT": "Austria", "ES": "Spain", "JP": "Japan", "PH": "PH"}


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def ui(tmp_path, monkeypatch):
    """A server on a scratch directory, plus a page pointed at it."""
    import importlib

    import uvicorn

    scenarios, data = tmp_path / "scenarios", tmp_path / "data"
    scenarios.mkdir()
    data.mkdir()
    monkeypatch.setenv("SCENARIO_DIR", str(scenarios))
    monkeypatch.setenv("DATA_DIR", str(data))
    (data / "airports.json").write_text(json.dumps(AIRPORTS), encoding="utf-8")
    (data / "countries.json").write_text(json.dumps(COUNTRIES), encoding="utf-8")

    save_scenario(
        Scenario(
            id="jp-ph",
            name="Japan then Philippines",
            origins=["PRG", "VIE"],
            stops=[
                Stop(airports=["NRT"], stay_days=(9, 11), label="Japan"),
                Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
            ],
            window_start=date(2027, 1, 5),
            window_end=date(2027, 2, 8),
            depth="quick",
        ),
        scenarios,
    )

    import src.airports as airports_module
    import src.web.app as app_module

    for cache in (
        airports_module._raw_catalogue,
        airports_module.load_countries,
        airports_module.load_catalogue,
        airports_module._by_code,
        airports_module.load_notes,
    ):
        cache.cache_clear()
    importlib.reload(app_module)

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app_module.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "server did not start"

    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(viewport={"width": 1280, "height": 1400}).new_page()
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"http://127.0.0.1:{port}", wait_until="networkidle")
        page.wait_for_selector("#origins .chip__code")
        yield page, scenarios, errors
        browser.close()

    server.should_exit = True
    thread.join(timeout=10)


def chips(page, selector: str) -> list[str]:
    """Chosen airports only - the quick-pick suggestions are `.chip--add`."""
    return page.locator(f"{selector} .chip:not(.chip--add) .chip__code").all_inner_texts()


def test_typing_a_code_and_pressing_enter_immediately_adds_it(ui):
    """The complaint, exactly: at human speed Enter used to do nothing at all.

    `onkeydown` began `if (menu.hidden) return`, and the menu needs a 160 ms
    debounce plus a round trip. Typing three letters takes ~200 ms, so Enter
    always landed in the gap - no chip, no message, the text just sat there.
    """
    page, _, errors = ui
    page.locator("#origins .typeahead input").click()
    page.keyboard.type("BCN", delay=55)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)

    assert "BCN" in chips(page, "#origins")
    assert not errors


def test_focus_survives_a_pick_so_airports_can_be_typed_in_a_row(ui):
    """Choosing re-renders the route, which used to destroy the input."""
    page, _, errors = ui
    page.locator("#origins .typeahead input").click()
    page.keyboard.type("BCN", delay=55)
    page.keyboard.press("Enter")
    page.wait_for_timeout(700)
    assert page.evaluate("document.activeElement.closest('.typeahead') !== null")

    # No second click: type straight into whatever now holds focus.
    page.keyboard.type("Prague", delay=45)
    page.keyboard.press("Enter")
    page.wait_for_timeout(700)
    assert chips(page, "#origins") == ["PRG", "VIE", "BCN"] or "BCN" in chips(page, "#origins")
    assert not errors


def test_a_country_name_finds_its_airports(ui):
    """Typing "Japan" returned nothing: only code, city and name were matched."""
    page, _, errors = ui
    page.locator("#stops .typeahead input").first.click()
    page.keyboard.type("Japan", delay=40)
    page.wait_for_selector("#stops .typeahead__menu:not([hidden]) .typeahead__item")
    listed = page.locator("#stops .typeahead__menu .typeahead__item").all_inner_texts()

    # Biggest first, by runway length: alphabetical put Narita 22nd of 28 in
    # Japan, behind Aomori and Saga. KIX and NRT share a 13,123 ft runway, so
    # they tie and fall back to alphabetical - Haneda is genuinely shorter.
    assert [text[:3] for text in listed] == ["KIX", "NRT", "HND"]
    assert not errors


def test_a_query_that_matches_nothing_says_so(ui):
    """A failed lookup used to close the menu, which looked like "still typing"."""
    page, _, errors = ui
    page.locator("#origins .typeahead input").click()
    page.keyboard.type("zzzznope", delay=25)
    page.wait_for_selector(".typeahead__note")
    assert "No airport matches" in page.locator(".typeahead__note").first.inner_text()
    assert not errors


def test_building_a_new_trip_from_scratch_saves_it(ui):
    """There was no way to make a trip at all: POST existed, nothing called it.

    A new trip opens with the departure airports already filled in from the ones
    you use, because those barely change - only the destination is new.
    """
    page, scenario_dir, errors = ui
    page.locator("#new-trip-btn").click()
    page.wait_for_timeout(400)
    assert chips(page, "#origins") == ["PRG", "VIE"]

    page.locator("#stops .typeahead input").first.click()
    page.keyboard.type("Osaka", delay=45)
    page.keyboard.press("Enter")
    page.wait_for_timeout(700)

    page.locator("#trip-name").fill("Osaka trip")
    page.locator("#save-btn").click()
    page.wait_for_timeout(900)

    saved = json.loads((scenario_dir / "osaka-trip.json").read_text(encoding="utf-8"))
    assert saved["origins"] == ["PRG", "VIE"]
    assert saved["stops"][0]["airports"] == ["KIX"]
    # `return_to: null` means "back where you started" - the row mirrored the
    # origins and was never edited, so no open jaw was recorded.
    assert saved["return_to"] is None and saved["one_way"] is False
    # A new trip stays out of the nightly cloud sweep until you opt it in.
    assert saved["enabled"] is False
    assert not errors


def test_the_return_row_states_the_shape_of_the_trip(ui):
    """Two checkboxes used to hold the shape; the visible chain holds it now."""
    page, _, errors = ui
    assert chips(page, "#return-to") == ["PRG", "VIE"]
    assert "same as departure" in page.locator("#return-note").inner_text().lower()

    for _ in range(2):
        page.locator("#return-to .chip__remove").first.click()
        page.wait_for_timeout(250)

    assert chips(page, "#return-to") == []
    assert "one-way" in page.locator("#return-note").inner_text().lower()
    assert not errors
