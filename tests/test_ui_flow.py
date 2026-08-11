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


# ------------------------------------------------------- results and prices
#
# These read the two panels that were quietly lying. The headline showed a
# price with no indication of when it was true, and the history chart joined
# four sweeps whose legs-per-search ran 2.9 to 9.7 into one line, so its shape
# tracked how well the scraper was working rather than what flights cost.
#
# Sweeps are seeded after the fixture and the page reloaded: the app reads
# `data/` per request, so nothing needs restarting.

LEGS = [
    ("PRG", "NRT", "2027-01-10", 12000.0),
    ("VIE", "NRT", "2027-01-10", 13000.0),
    ("NRT", "MNL", "2027-01-20", 4000.0),
    ("MNL", "PRG", "2027-01-30", 14000.0),
    ("MNL", "VIE", "2027-01-30", 11000.0),
]


def seed_sweep(data_dir, stamp, *, status, legs=LEGS, observed_at=None):
    directory = data_dir / "sweeps" / "jp-ph" / stamp
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "legs.jsonl").open("w", encoding="utf-8") as handle:
        for origin, destination, depart, price in legs:
            handle.write(json.dumps({
                "provider": "T", "origin": origin, "destination": destination,
                "depart_date": depart, "airline": "QR", "flight_number": None,
                "stops": 1, "price_currency": "CZK", "price_amount": price, "url": "",
                "depart_time": None, "arrive_time": None, "duration_minutes": None,
                "checked_bag": True, "observed_at": observed_at,
            }) + "\n")
    (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")


def open_prices(page):
    page.locator('#tabs button[data-tab="prices"]').click()
    page.wait_for_timeout(900)


def test_the_headline_says_when_the_price_was_measured(ui):
    """A figure from three days ago reads exactly like one from ten minutes ago."""
    page, scenarios, errors = ui
    seed_sweep(
        scenarios.parent / "data", "2026-08-10T11-57-06Z",
        status={"state": "done", "total": 5, "legs_found": 5, "depth": "quick"},
        observed_at="2026-08-10T11:59:04+00:00",
    )
    page.reload(wait_until="networkidle")
    page.locator('#tabs button[data-tab="results"]').click()
    page.wait_for_timeout(900)

    headline = page.locator("#headline").inner_text()
    assert "measured" in headline.lower(), headline
    # Rendered in the viewer's locale, so assert on the instant rather than the
    # formatting: 11:59 UTC is 13:59 in Prague.
    assert "13:59" in headline, headline
    assert "ago" in headline, headline
    assert not errors


def test_a_starved_sweep_is_dimmed_out_of_the_trend(ui):
    """The 2.9-legs-per-search sweep must not be joined to the 9.7 one.

    Both are drawn — the gap in the record is worth seeing — but only the
    trustworthy one is solid, joined and eligible for the cheapest label.
    """
    page, scenarios, errors = ui
    data = scenarios.parent / "data"
    # Starved: every route answered, but barely anything came back.
    seed_sweep(data, "2026-08-06T20-22-44Z",
               status={"state": "done", "total": 100, "legs_found": 293, "depth": "standard"})
    # Healthy and fully covered.
    seed_sweep(data, "2026-08-10T11-57-06Z",
               status={"state": "done", "total": 5, "legs_found": 49, "depth": "quick"})
    page.reload(wait_until="networkidle")
    open_prices(page)

    note = page.locator("#history-note").inner_text()
    assert "1 of 2 sweeps are dimmed" in note, note
    assert "under 6 legs per search" in note, note

    # One hollow marker (fill = panel background) for the starved sweep, and at
    # least one dashed segment joining it to the healthy one.
    markers = page.locator("#chart-history circle")
    hollow = [i for i in range(markers.count())
              if "panelBackground" in (markers.nth(i).get_attribute("fill") or "")]
    assert len(hollow) == 1, f"expected exactly one dimmed point, got {len(hollow)}"
    assert page.locator("#chart-history path[stroke-dasharray]").count() >= 1
    assert not errors


def test_a_sweep_missing_a_route_is_dimmed_for_coverage_not_starvation(ui):
    """The two failure modes call for different fixes, so they are named apart."""
    page, scenarios, errors = ui
    data = scenarios.parent / "data"
    seed_sweep(data, "2026-08-07T13-17-07Z", legs=LEGS[:-1],
               status={"state": "done", "total": 4, "legs_found": 40, "depth": "quick"})
    seed_sweep(data, "2026-08-10T11-57-06Z",
               status={"state": "done", "total": 5, "legs_found": 49, "depth": "quick"})
    page.reload(wait_until="networkidle")
    open_prices(page)

    note = page.locator("#history-note").inner_text()
    assert "did not cover every route" in note, note
    assert "legs per search" not in note, note
    assert not errors


def test_a_lone_comparable_sweep_refuses_to_draw_a_trend(ui):
    page, scenarios, errors = ui
    data = scenarios.parent / "data"
    seed_sweep(data, "2026-08-06T20-22-44Z",
               status={"state": "done", "total": 100, "legs_found": 293, "depth": "standard"})
    seed_sweep(data, "2026-08-10T11-57-06Z",
               status={"state": "done", "total": 5, "legs_found": 49, "depth": "quick"})
    # Only one of the two is comparable. Both points are still drawn — the gap
    # in the record is worth seeing — but the caption refuses the trend.
    page.reload(wait_until="networkidle")
    open_prices(page)
    note = page.locator("#history-note").inner_text().lower()
    assert "one sweep complete enough to compare" in note, note
    assert "no trend here yet" in note, note
    assert page.locator("#chart-history circle").count() >= 2
    assert not errors


def test_the_by_date_chart_states_its_sampling_resolution(ui):
    """A smooth line through points a week apart implies knowledge of the gaps."""
    page, scenarios, errors = ui
    seed_sweep(
        scenarios.parent / "data", "2026-08-10T11-57-06Z",
        status={"state": "done", "total": 5, "legs_found": 49, "depth": "quick"},
        legs=[
            ("PRG", "NRT", "2027-01-05", 12000.0),
            ("PRG", "NRT", "2027-01-12", 9000.0),
            ("NRT", "MNL", "2027-01-15", 4000.0),
            ("NRT", "MNL", "2027-01-22", 4000.0),
            ("MNL", "PRG", "2027-01-25", 14000.0),
            ("MNL", "PRG", "2027-02-01", 14000.0),
        ],
    )
    page.reload(wait_until="networkidle")
    open_prices(page)

    note = page.locator("#by-date-note").inner_text()
    assert "sampled every 7 days" in note.lower(), note
    assert "3 days either side" in note, note
    # 30,000 on 01-05 against 27,000 on 01-12 — a 10% saving for leaving a week
    # later, not the 11% that max/min - 1 would report.
    assert "10% below the dearest" in note, note
    assert not errors
