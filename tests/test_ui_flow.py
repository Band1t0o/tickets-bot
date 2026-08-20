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
from datetime import date, timedelta

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
    # Without this the webhook test would write a fake URL over the real
    # `.secrets/discord.json` of whoever ran `make test-ui`.
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path / ".secrets"))
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
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


# ----------------------------------------------------------- explore report
#
# The probe is only worth running if its verdict can be acted on, and the
# acting is two clicks in this panel. Driven through the browser because the
# Remove button reaches across into the route editor on the other tab, which no
# unit test of either half would catch.

EXPLORE_LEGS = [
    ("PRG", "NRT", "2027-01-10", 11000.0),
    ("VIE", "NRT", "2027-01-10", 22000.0),   # dear enough to be called poor
    ("NRT", "MNL", "2027-01-20", 4000.0),
    ("MNL", "PRG", "2027-01-30", 14000.0),
    ("MNL", "VIE", "2027-01-30", 15000.0),
]

EXPLORE_ROUTES = ["PRG->NRT", "VIE->NRT", "NRT->MNL", "MNL->PRG", "MNL->VIE"]


def explore_status(errors=None, state="done"):
    return {
        "state": state,
        "mode": "explore",
        "total": 15,
        "completed": 15,
        "legs_found": len(EXPLORE_LEGS),
        "depth": "quick",
        "route_searches": {route: 3 for route in EXPLORE_ROUTES},
        "route_errors": {route: (errors or {}).get(route, 0) for route in EXPLORE_ROUTES},
    }


def open_explore(page):
    page.locator('#tabs button[data-tab="explore"]').click()
    page.wait_for_timeout(900)


def open_results(page):
    page.locator('#tabs button[data-tab="results"]').click()
    page.wait_for_timeout(900)


def seed_probe(scenarios, errors=None, legs=None, state="done"):
    seed_sweep(
        scenarios.parent / "data", "2026-08-11T09-00-00Z",
        status=explore_status(errors, state), legs=EXPLORE_LEGS if legs is None else legs,
    )


def seed_searched_trip(scenarios, stamp="2026-08-11T09-00-00Z", **overrides):
    """Record which trip a seeded sweep searched, as `run_sweep` now does."""
    trip = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8")) | overrides
    path = scenarios.parent / "data" / "sweeps" / "jp-ph" / stamp / "scenario.json"
    path.write_text(json.dumps(trip), encoding="utf-8")


def test_the_explore_tab_shows_a_verdict_for_every_airport(ui):
    page, scenarios, errors = ui
    seed_probe(scenarios)
    page.reload(wait_until="networkidle")
    open_explore(page)

    report = page.locator("#explore-report")
    assert report.is_visible()
    assert "not worth it" in report.inner_text()
    assert not errors


def test_a_dear_origin_is_named_and_the_cheap_one_is_the_benchmark(ui):
    page, scenarios, errors = ui
    seed_probe(scenarios)
    page.reload(wait_until="networkidle")
    open_explore(page)

    # `.first` is the "Flying from" block; the same two airports appear again
    # in the pool for the way home.
    prague = page.locator("#explore-report tbody tr", has_text="PRG").first.inner_text()
    vienna = page.locator("#explore-report tbody tr", has_text="VIE").first.inner_text()
    assert "cheapest here" in prague, prague
    assert "not worth it" in vienna, vienna
    assert "100%" in vienna, vienna   # 22,000 against 11,000
    assert not errors


def test_a_probes_verdicts_are_pointed_to_rather_than_drawn_as_an_empty_table(ui):
    """Results has nothing to show for a probe, and must not imply it found
    nothing - three dates a leg rarely chain into a whole trip."""
    page, scenarios, errors = ui
    seed_probe(scenarios)
    page.reload(wait_until="networkidle")
    open_results(page)

    assert page.locator("#results-scroll").is_hidden()
    assert "Explore tab" in page.locator("#results-empty").inner_text()
    assert not errors


def test_dropped_airports_gather_up_instead_of_being_saved_one_at_a_time(ui):
    """Deciding about a pool of airports is one review and one save.

    Left unsaved on purpose: three sampled dates is enough to show you an
    airport is hopeless and nowhere near enough for a tool to edit your trip
    behind you.
    """
    page, scenarios, errors = ui
    seed_probe(scenarios)
    page.reload(wait_until="networkidle")
    open_explore(page)

    page.locator("#explore-report tbody tr", has_text="VIE").first.locator(
        "button", has_text="Remove from trip"
    ).click()
    page.wait_for_timeout(500)

    # Still on the Explore tab, with what you have decided in front of you.
    assert page.locator('section[data-panel="explore"]').is_visible()
    assert "VIE" in page.locator("#explore-pending").inner_text()
    # The row stays, struck through: the table is the comparison you just made,
    # and deleting rows from it would hide what you compared against.
    vienna = page.locator("#explore-report tbody tr", has_text="VIE").first
    assert "is-dropped" in (vienna.get_attribute("class") or "")
    assert vienna.locator("button", has_text="Remove from trip").count() == 0
    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["origins"] == ["PRG", "VIE"], "the trip must not change without a save"

    page.locator("#explore-save").click()
    page.wait_for_timeout(900)
    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["origins"] == ["PRG"]
    assert page.locator("#explore-pending").is_hidden()
    assert not errors


def test_undo_puts_back_everything_dropped_since_the_last_save(ui):
    page, scenarios, errors = ui
    seed_probe(scenarios)
    page.reload(wait_until="networkidle")
    open_explore(page)

    page.locator("#explore-report tbody tr", has_text="VIE").first.locator(
        "button", has_text="Remove from trip"
    ).click()
    page.wait_for_timeout(500)
    page.locator("#explore-undo").click()
    page.wait_for_timeout(700)

    assert page.locator("#explore-pending").is_hidden()
    page.locator('#tabs button[data-tab="search"]').click()
    assert chips(page, "#origins") == ["PRG", "VIE"]
    assert not errors


def test_an_airport_the_site_never_answered_about_is_not_offered_for_removal(ui):
    """1.9 legs per search: an empty result is usually a timeout, not a verdict.

    Dropping an airport on that basis would retire a good route on the strength
    of the site being slow.
    """
    page, scenarios, errors = ui
    seed_probe(
        scenarios,
        errors={"VIE->NRT": 3, "MNL->VIE": 3},
        legs=[leg for leg in EXPLORE_LEGS if "VIE" not in (leg[0], leg[1])],
    )
    page.reload(wait_until="networkidle")
    open_explore(page)

    vienna = page.locator("#explore-report tbody tr", has_text="VIE").first
    assert "not measured" in vienna.inner_text()
    assert vienna.locator("button", has_text="Remove from trip").count() == 0
    assert not errors


def test_the_explore_tab_can_judge_from_a_full_sweep_not_only_a_probe(ui):
    """A real sweep priced the same routes on far more dates, so its verdict is
    the better one whenever there is one."""
    page, scenarios, errors = ui
    seed_sweep(
        scenarios.parent / "data", "2026-08-11T08-00-00Z",
        status={
            "state": "done", "mode": "sweep", "depth": "quick", "total": 93,
            "legs_found": len(EXPLORE_LEGS),
            "route_searches": {route: 9 for route in EXPLORE_ROUTES},
            "route_errors": {},
        },
        legs=EXPLORE_LEGS,
    )
    page.reload(wait_until="networkidle")
    open_explore(page)

    assert "quick sweep" in page.locator("#explore-select").inner_text()
    assert "not worth it" in page.locator("#explore-report").inner_text()
    assert not errors


def test_a_probe_is_labelled_as_one_in_the_sweep_picker(ui):
    page, scenarios, errors = ui
    seed_probe(scenarios)
    page.reload(wait_until="networkidle")
    open_results(page)
    assert "probe" in page.locator("#sweep-select").inner_text()
    assert not errors


def test_a_stopped_run_says_it_was_stopped_rather_than_finished(ui):
    page, scenarios, errors = ui
    seed_sweep(
        scenarios.parent / "data", "2026-08-11T09-00-00Z",
        status={
            "state": "stopped", "mode": "sweep", "total": 600, "completed": 40,
            "legs_found": 120, "depth": "deep",
        },
    )
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(600)
    assert "Stopped at 40/600" in page.locator("#status-text").inner_text()
    assert "stopped at 40" in page.locator("#sweep-select").inner_text()
    assert not errors


def test_the_explore_button_prices_itself_before_you_press_it(ui):
    page, _, errors = ui
    page.wait_for_timeout(700)
    note = page.locator("#explore-note").inner_text()
    assert "searches" in note and "min" in note, note
    assert not errors


# --------------------------------------------- what runs is what is on screen
#
# The run endpoint takes the trip from disk, and the route editor keeps its
# edits in the browser until Save is pressed. Two 25-minute probes were spent
# searching the previous day's trip because nothing joined those two facts up.


def add_origin(page, code: str) -> None:
    page.locator("#origins .typeahead input").click()
    page.keyboard.type(code, delay=55)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)


def catch_the_sweep(monkeypatch):
    """Stand in for the sweep and record the trip it was handed."""
    import src.web.app as app_module

    seen: dict = {}
    started = threading.Event()

    def fake_sweep(scenario, **kwargs):
        seen["origins"] = list(scenario.origins)
        seen["mode"] = kwargs.get("mode")
        started.set()

    monkeypatch.setattr(app_module, "run_sweep", fake_sweep)
    return seen, started


def test_a_probe_searches_the_trip_on_screen_not_the_one_last_saved(ui, monkeypatch):
    """The bug, frozen: BCN is on screen, so BCN is what gets searched."""
    page, _, errors = ui
    seen, started = catch_the_sweep(monkeypatch)

    add_origin(page, "BCN")
    page.locator("#explore-btn").click()          # no Save pressed first

    assert started.wait(timeout=15), "the probe never started"
    assert "BCN" in seen["origins"], f"the probe searched {seen['origins']}"
    assert seen["mode"] == "explore"
    assert not errors


def test_the_probe_button_on_the_explore_tab_saves_the_edits_too(ui, monkeypatch):
    """The tab whose whole design is "drop airports now, save later"."""
    page, scenarios, errors = ui
    seen, started = catch_the_sweep(monkeypatch)
    seed_probe(scenarios)
    page.reload(wait_until="networkidle")

    add_origin(page, "BCN")
    open_explore(page)
    page.locator("#explore-run-btn").click()

    assert started.wait(timeout=15), "the probe never started"
    assert "BCN" in seen["origins"], f"the probe searched {seen['origins']}"
    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert "BCN" in saved["origins"], "the run saved the trip it was about to search"
    assert not errors


def test_an_unsaved_edit_is_visible_beside_the_button_that_will_run_it(ui):
    page, _, errors = ui
    assert page.locator("#dirty-note").is_hidden()
    add_origin(page, "BCN")
    assert page.locator("#dirty-note").is_visible()
    assert "unsaved" in page.locator("#dirty-note").inner_text().lower()

    page.locator("#save-btn").click()
    page.wait_for_timeout(800)
    assert page.locator("#dirty-note").is_hidden()
    assert not errors


def test_a_trip_that_cannot_be_saved_says_so_on_the_tab_you_pressed_run_from(ui, monkeypatch):
    """A save failed from the Explore tab used to print into the Search
    panel, which is hidden - so the button simply did nothing."""
    page, _, errors = ui
    seen, started = catch_the_sweep(monkeypatch)

    for _ in range(2):
        page.locator("#origins .chip__remove").first.click()
        page.wait_for_timeout(300)
    open_explore(page)
    page.locator("#explore-run-btn").click()
    page.wait_for_timeout(900)

    assert not started.is_set(), "an unsavable trip must not be swept"
    complaint = page.locator('section[data-panel="explore"] .badge--error')
    assert complaint.is_visible(), "the reason must be readable where the button is"
    assert "origin" in complaint.inner_text().lower()
    assert not errors


# ------------------------------------------ a report of a trip you have edited


def test_a_probe_of_a_different_trip_says_so_instead_of_listing_its_airports(ui):
    """Exactly what was on screen for two probes: rows for Prague and Vienna,
    nothing at all about the airports the trip now flies from."""
    page, scenarios, errors = ui
    seed_probe(scenarios)
    seed_searched_trip(scenarios, origins=["PRG", "VIE"])
    page.reload(wait_until="networkidle")

    add_origin(page, "BCN")
    page.locator("#save-btn").click()
    page.wait_for_timeout(900)
    open_explore(page)

    notice = page.locator("#explore-mismatch")
    assert notice.is_visible()
    assert "BCN" in notice.inner_text(), notice.inner_text()
    assert "different trip" in page.locator("#explore-select").inner_text()
    assert not errors


def test_an_airport_that_was_never_in_your_trip_is_not_drawn_as_one_you_dropped(ui):
    """Every row struck through as "dropped" is what the old code did when the
    report was about another trip entirely - it read as six decisions you had
    just made rather than as a report of the wrong thing. Reloading clears what
    was dropped in this session, so anything still struck through is a lie."""
    page, scenarios, errors = ui
    seed_probe(scenarios)
    seed_searched_trip(scenarios, origins=["PRG", "VIE"])

    page.locator("#origins .chip__remove").last.click()   # VIE out of the trip
    page.wait_for_timeout(300)
    page.locator("#save-btn").click()
    page.wait_for_timeout(900)
    page.reload(wait_until="networkidle")
    open_explore(page)

    vienna = page.locator("#explore-report tbody tr", has_text="VIE").first
    assert "is-dropped" not in (vienna.get_attribute("class") or "")
    assert "not in this trip" in vienna.inner_text().lower(), vienna.inner_text()
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


# ------------------------------------------------------ notification settings
#
# Which deals get sent, and from where. Typed the way a person types, for the
# same reason the route editor is: the picker below is the same component, and
# the bug it once had was invisible to every scripted click.


def test_a_preferred_tier_is_typed_and_saved(ui):
    page, scenario_dir, errors = ui
    page.locator("#add-tier-btn").click()
    page.wait_for_timeout(300)

    page.locator('[data-picker="tier-0"] .typeahead input').click()
    page.keyboard.type("VIE", delay=55)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)

    assert chips(page, "#preferred-tiers") == ["VIE"]
    page.locator("#save-btn").click()
    page.wait_for_timeout(900)

    saved = json.loads((scenario_dir / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["preferred_origins"] == [["VIE"]]
    assert not errors


def test_an_airport_cannot_sit_in_two_tiers_at_once(ui):
    """The scenario rejects a duplicate, so the picker must not let one exist.

    Otherwise "the best tier holding this airport" has two answers and Save
    fails on something the form could see coming.
    """
    page, _, errors = ui
    for _ in range(2):
        page.locator("#add-tier-btn").click()
        page.wait_for_timeout(250)

    for tier in ("tier-0", "tier-1"):
        page.locator(f'[data-picker="{tier}"] .typeahead input').click()
        page.keyboard.type("PRG", delay=55)
        page.keyboard.press("Enter")
        page.wait_for_timeout(700)

    # Claimed by the second tier, and gone from the first rather than in both.
    assert chips(page, "#preferred-tiers") == ["PRG"]
    assert not errors


def test_notification_choices_round_trip(ui):
    page, scenario_dir, errors = ui
    page.locator("#notify-preferred").uncheck()
    page.locator("#notify-quiet").uncheck()
    page.locator("#save-btn").click()
    page.wait_for_timeout(900)

    saved = json.loads((scenario_dir / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["notify"] == ["cheapest"]
    assert saved["notify_quiet"] is False
    assert not errors


def test_an_empty_tier_row_is_dropped_rather_than_failing_the_save(ui):
    page, scenario_dir, errors = ui
    page.locator("#add-tier-btn").click()
    page.wait_for_timeout(300)
    page.locator("#save-btn").click()
    page.wait_for_timeout(900)

    saved = json.loads((scenario_dir / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["preferred_origins"] == []
    assert page.locator("#save-error").is_hidden()
    assert not errors


# ------------------------------------------------------------- sources tab
#
# The escape hatch: when pelikan renames a class the sweep goes silently to
# zero, and the fix is a string rather than a code change. These check the
# string can be typed, saved, and read back by the thing that sweeps.


def open_sources(page):
    page.locator('#tabs button[data-tab="sources"]').click()
    page.wait_for_selector(".source-card")


def open_repair(page, name="PELIKAN"):
    """Reveal the selector form the way a person reaches it: by pressing Repair."""
    card = page.locator(f'.source-card[data-source="{name}"]')
    card.locator('[data-role="repair"]').click()
    page.wait_for_timeout(300)
    return card


def stub_check(page, ok=True, message="stubbed"):
    """Answer the check without touching pelikan.cz.

    Saving a repair now runs a real search, because "saved" and "working" are
    different claims and the gap between them is why this panel exists. A test
    must not be the thing that closes it.
    """
    page.route("**/api/sources/*/test", lambda route: route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({
            "ok": ok, "message": message, "cards_found": 3 if ok else 0,
            "legs_parsed": 3 if ok else 0, "route": "PRG→NRT 2027-01-10",
            "sample": None, "url": "https://www.pelikan.cz/cs/letenky/x/",
            "checked_at": "2026-08-20T09:00:00+00:00",
        }),
    ))


def test_the_sources_tab_shows_the_live_selectors(ui):
    """Behind Repair now. They are the answer to a question you only have once
    "is it working" has answered no."""
    page, _, errors = ui
    open_sources(page)
    card = open_repair(page)
    assert card.locator('[data-selector="card"]').input_value() == "div[id^='flight-']"
    assert card.locator('[data-field="base_url"]').input_value()         .startswith("https://www.pelikan.cz")
    assert not errors


def test_a_selector_edited_by_hand_reaches_disk(ui):
    """The whole point of the tab: no code change, no redeploy, no me."""
    page, scenario_dir, errors = ui
    stub_check(page)
    open_sources(page)
    card = open_repair(page)

    field = card.locator('[data-selector="card"]')
    field.fill("")
    field.click()
    page.keyboard.type("div.new-offer-class", delay=25)
    card.locator('button:has-text("Save and check again")').click()
    page.wait_for_timeout(900)

    saved = json.loads((scenario_dir.parent / "data" / "sources.json").read_text(encoding="utf-8"))
    assert saved["PELIKAN"]["selectors"]["card"] == "div.new-offer-class"
    assert not errors


def test_saving_a_repair_proves_it_rather_than_claiming_success(ui):
    """"Saved" and "working" are different claims, and the gap between them is
    the whole reason this panel exists - so the save checks itself."""
    page, _, errors = ui
    stub_check(page, ok=True, message="3 card(s) matched and 3 parsed cleanly.")
    open_sources(page)
    card = open_repair(page)
    card.locator('button:has-text("Save and check again")').click()
    page.wait_for_timeout(900)

    assert "3 parsed cleanly" in card.locator('[data-role="outcome"]').inner_text()
    assert "working" in card.locator('[data-role="state"]').inner_text()
    assert not errors


def test_an_empty_selector_is_refused_with_a_reason(ui):
    page, _, errors = ui
    open_sources(page)
    card = open_repair(page)

    card.locator('[data-selector="price"]').fill("")
    card.locator('button:has-text("Save and check again")').click()
    page.wait_for_timeout(900)

    outcome = card.locator('[data-role="outcome"]').inner_text()
    assert "price" in outcome, outcome
    assert not errors


def test_an_edit_survives_leaving_the_tab_and_coming_back(ui):
    page, _, errors = ui
    stub_check(page)
    open_sources(page)
    card = open_repair(page)
    card.locator('[data-field="no_results_marker"]').fill("Nothing found")
    card.locator('button:has-text("Save and check again")').click()
    page.wait_for_timeout(900)

    page.locator('#tabs button[data-tab="search"]').click()
    page.wait_for_timeout(300)
    open_sources(page)
    assert open_repair(page).locator('[data-field="no_results_marker"]')         .input_value() == "Nothing found"
    assert not errors


def test_a_failed_check_opens_the_repair_box_for_you(ui):
    """The one moment those fields are worth looking at, so you should not have
    to go looking for them."""
    page, _, errors = ui
    stub_check(page, ok=False, message="the card selector matched nothing")
    open_sources(page)
    card = page.locator('.source-card[data-source="PELIKAN"]')
    assert card.locator(".source-card__repair").is_hidden()

    card.locator('[data-role="check"]').click()
    page.wait_for_timeout(900)
    assert card.locator(".source-card__repair").is_visible()
    assert "not working" in card.locator('[data-role="state"]').inner_text()
    assert not errors


# ------------------------------------------------- talking to a stale server
#
# The failure these cover cost an afternoon. Two uvicorn processes were left
# running from an older commit; static files are read from disk per request, so
# both served the newest page against an old API. Every call the page needed
# 404'd or 400'd, and the page drew the result as an empty trip picker and empty
# charts - which is exactly what a deleted database looks like. Nothing on disk
# had changed.


def test_an_older_server_says_so_instead_of_showing_an_empty_app(ui):
    page, _, _ = ui
    page.route("**/api/version", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"contract": 0})
    ))
    page.reload(wait_until="networkidle")

    assert page.locator("#blocker").is_visible()
    assert "older code" in page.locator("#blocker-title").inner_text()
    # The point of the banner is that the app is not drawn at all: a page of
    # convincing emptiness is worse than no page.
    assert page.locator("#tabs").is_hidden()
    assert "untouched" in page.locator("#blocker-detail").inner_text()


def test_a_server_too_old_to_have_the_version_endpoint_is_still_caught(ui):
    """The stale processes predated the endpoint itself, so a 404 has to mean
    the same thing as a mismatch - otherwise the check misses the exact case
    that prompted it."""
    page, _, _ = ui
    page.route("**/api/version", lambda route: route.fulfill(
        status=404, content_type="application/json", body='{"detail":"Not Found"}'
    ))
    page.reload(wait_until="networkidle")

    assert page.locator("#blocker").is_visible()
    assert "no version endpoint" in page.locator("#blocker-detail").inner_text()


def test_a_trip_file_that_will_not_parse_does_not_empty_the_picker(ui):
    page, scenarios, _ = ui
    (scenarios / "broken.json").write_text("{ not json at all", encoding="utf-8")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")

    assert page.locator("#scenario-select option").all_inner_texts() == [
        "Japan then Philippines"
    ]
    assert "broken.json" in page.locator("#save-error").inner_text()


def test_an_unreachable_scenario_list_is_named_not_drawn_as_emptiness(ui):
    page, _, _ = ui
    page.route("**/api/scenarios", lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"detail":"disk on fire"}'
    ))
    page.reload(wait_until="networkidle")

    assert page.locator("#blocker").is_visible()
    assert "disk on fire" in page.locator("#blocker-detail").inner_text()
    assert "still there" in page.locator("#blocker-detail").inner_text()


# ------------------------------------------------------------ webhook panel


def test_a_pasted_channel_url_is_refused_with_the_instruction(ui):
    """The URL from the address bar is the thing people paste. It is not a
    webhook, and Discord answers a 404 hours later inside a scheduled run."""
    page, _, _ = ui
    page.click("#tabs button[data-tab='sources']")
    page.wait_for_selector("#webhook-url")

    page.locator("#webhook-url").click()
    page.keyboard.type("https://discord.com/channels/123/456", delay=10)
    page.click("#webhook-save")
    page.wait_for_timeout(600)

    assert "Copy Webhook URL" in page.locator("#webhook-result").inner_text()
    assert "not set" in page.locator("#notify-origin").inner_text()


def test_a_saved_webhook_is_never_shown_back(ui):
    page, _, _ = ui
    token = "xY2bkQ7fLpZ9wA3tR6vN1sD4gH8j"
    page.click("#tabs button[data-tab='sources']")
    page.wait_for_selector("#webhook-url")

    page.locator("#webhook-url").click()
    page.keyboard.type(f"https://discord.com/api/webhooks/1409876543210987654/{token}", delay=5)
    page.click("#webhook-save")
    page.wait_for_timeout(800)

    assert "saved on this machine" in page.locator("#notify-origin").inner_text()
    assert page.locator("#webhook-url").input_value() == ""
    placeholder = page.locator("#webhook-url").get_attribute("placeholder")
    assert token not in placeholder
    assert "1409876543210987654" in placeholder
    assert token not in page.content()


# ------------------------------------------------------- picking a focus
#
# Once a broad sweep shows which departure dates are cheap, the next one should
# price only those. The dates are picked off the chart rather than typed into
# two date boxes, because the decision is made by looking at the chart and a
# date box beside it is a second place to get the same answer wrong.
#
# Driven by clicking the chart, not by calling the handler: the whole point is
# that a click near a point picks that point.


def seed_a_week_of_dates(data_dir):
    """Five departure dates, so there is a range to pick out of."""
    legs = []
    for offset in range(5):
        # Real date arithmetic, not string formatting: "2027-01-34" parses
        # nowhere and blanks the chart this is meant to be drawing.
        out = date(2027, 1, 10) + timedelta(days=offset)
        legs += [
            ("PRG", "NRT", out.isoformat(), 12000.0 + offset * 100),
            ("NRT", "MNL", (out + timedelta(days=10)).isoformat(), 4000.0),
            ("MNL", "PRG", (out + timedelta(days=20)).isoformat(), 14000.0),
        ]
    seed_sweep(
        data_dir, "2026-08-19T02-00-00Z",
        status={"state": "done", "total": 60, "legs_found": len(legs),
                "legs_per_search": 9.0, "depth": "deep", "coverage": 1.0},
        legs=legs,
    )


def chart_point(page, index, total):
    """Click where the nth of `total` points sits, the way a person would.

    The chart pads 44px left and 16px right inside its own viewBox, and scales
    to the container, so the position is computed from the rendered box rather
    than from a marker's coordinates - a click has to land near a point, not on
    a 4px circle.
    """
    box = page.locator("#chart-by-date svg").bounding_box()
    scale = box["width"] / float(page.locator("#chart-by-date svg").get_attribute("width"))
    left = 44 * scale
    plot = box["width"] - (44 + 16) * scale
    x = box["x"] + left + (plot * index / max(1, total - 1))
    page.mouse.click(x, box["y"] + box["height"] / 2)
    page.wait_for_timeout(500)


def test_clicking_two_points_picks_a_range_and_prices_it(ui):
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_prices(page)

    assert "whole window" in page.locator("#focus-state").inner_text()

    chart_point(page, 1, 5)
    first = page.locator("#focus-pick").inner_text()
    assert "2027-01-11" in first, first
    # A half-open range must still say something, or the first click looks like
    # it did nothing at all.
    assert "second day" in first, first

    chart_point(page, 3, 5)
    both = page.locator("#focus-pick").inner_text()
    assert "2027-01-11" in both and "2027-01-13" in both, both
    # Priced against the trip on screen, not guessed.
    assert "searches" in both and "whole window" in both, both
    assert not errors


def test_saving_a_focus_writes_it_to_the_trip_on_disk(ui):
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_prices(page)

    chart_point(page, 1, 5)
    chart_point(page, 3, 5)
    page.locator("#focus-save").click()
    page.wait_for_timeout(700)

    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["focus_start"] == "2027-01-11"
    assert saved["focus_end"] == "2027-01-13"
    assert "2027-01-11" in page.locator("#focus-state").inner_text()
    assert not errors


def test_clearing_a_focus_goes_back_to_the_whole_window(ui):
    """Otherwise a date opening up outside the pick would never be found again."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_prices(page)

    chart_point(page, 1, 5)
    chart_point(page, 3, 5)
    page.locator("#focus-save").click()
    page.wait_for_timeout(700)

    page.locator("#focus-clear").click()
    page.wait_for_timeout(700)

    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["focus_start"] is None and saved["focus_end"] is None
    assert "whole window" in page.locator("#focus-state").inner_text()
    assert not errors


def test_a_third_click_starts_the_range_over(ui):
    """A picker that could only ever extend would need a Clear button to undo a
    misclick, which is a button for a mistake the picker caused."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_prices(page)

    chart_point(page, 0, 5)
    chart_point(page, 4, 5)
    chart_point(page, 2, 5)
    note = page.locator("#focus-pick").inner_text()
    assert "2027-01-12" in note, note
    assert "2027-01-10" not in note, note
    assert not errors


# --------------------------------------------------------------- sources
#
# The tab answers one question - is this still working - and keeps the CSS
# selectors behind Repair, where they belong until the answer is no.


def test_the_sources_tab_leads_with_the_discord_webhook(ui):
    """It is what people come here for, and it used to be below a selector form."""
    page, _, errors = ui
    open_sources(page)
    panels = page.locator('section[data-panel="sources"] .panel h2').all_inner_texts()
    assert "Where results get sent" in panels[0], panels
    assert not errors


def test_every_source_shows_what_it_is_and_whether_it_works(ui):
    page, _, errors = ui
    open_sources(page)
    cards = page.locator(".source-card")
    assert cards.count() == 3
    text = page.locator("#sources-body").inner_text()
    assert "pelikan.cz" in text and "letuska.cz" in text and "Skyscanner" in text
    # Never checked is a state, and it is not the same as broken.
    assert "never checked" in text, text
    assert not errors


def test_a_source_that_is_not_connected_offers_no_check_button(ui):
    """Drawing a Check button for a source nothing reads would invite you to
    test something that does not exist."""
    page, _, errors = ui
    open_sources(page)
    card = page.locator('.source-card[data-source="SKYSCANNER"]')
    assert "not connected" in card.inner_text()
    assert card.locator('[data-role="check"]').count() == 0
    assert not errors


def test_the_selectors_stay_out_of_the_way_until_something_is_wrong(ui):
    page, _, errors = ui
    open_sources(page)
    card = page.locator('.source-card[data-source="PELIKAN"]')
    repair = card.locator(".source-card__repair")
    assert repair.is_hidden()

    card.locator('[data-role="repair"]').click()
    page.wait_for_timeout(300)
    assert repair.is_visible()
    assert card.locator('[data-selector="card"]').input_value() == "div[id^='flight-']"
    assert not errors


def test_a_form_driven_source_offers_no_selectors_to_repair(ui):
    """There is no selector map to be right or wrong about - the steps are in
    code - so a box promising a repair it cannot make is worse than none."""
    page, _, errors = ui
    open_sources(page)
    card = page.locator('.source-card[data-source="LETUSKA"]')
    assert card.locator('[data-role="repair"]').count() == 0
    assert card.locator('[data-role="check"]').count() == 1
    assert not errors


# ------------------------------------------------- reading a partial sweep
#
# The failure this exists for is silence: the sharded run of 20 Aug posted
# `error_count: 0` beside 48 searches it never made, because the circuit breaker
# abandoned them rather than attempting and failing them. Coverage is the figure
# that caught it, and the Results tab is where it has to be said - these are the
# numbers you would book on.


def test_a_sweep_with_holes_says_so_above_the_prices(ui):
    page, scenarios, errors = ui
    seed_sweep(
        scenarios.parent / "data", "2026-08-20T02-00-00Z",
        status={"state": "done", "total": 168, "legs_found": 5, "depth": "deep",
                "legs_per_search": 9.0, "answered": 120, "planned": 168,
                "coverage": 0.714, "unanswered": 48},
    )
    page.reload(wait_until="networkidle")
    page.locator('#tabs button[data-tab="results"]').click()
    page.wait_for_timeout(900)

    notice = page.locator("#completeness")
    assert notice.is_visible()
    text = notice.inner_text()
    assert "71%" in text, text
    assert "could have been the cheap ones" in text, text
    assert not errors


def test_a_complete_sweep_says_nothing_at_all(ui):
    """A banner that is always there is a banner nobody reads."""
    page, scenarios, errors = ui
    seed_sweep(
        scenarios.parent / "data", "2026-08-20T02-00-00Z",
        status={"state": "done", "total": 168, "legs_found": 5, "depth": "deep",
                "answered": 168, "planned": 168, "coverage": 1.0, "unanswered": 0},
    )
    page.reload(wait_until="networkidle")
    page.locator('#tabs button[data-tab="results"]').click()
    page.wait_for_timeout(900)
    assert page.locator("#completeness").is_hidden()
    assert not errors


def test_a_sweep_from_before_coverage_existed_does_not_claim_completeness(ui):
    """Every sweep committed before 20 Aug. Absent must not read as 100%, and
    must not read as a warning either - it is simply not known."""
    page, scenarios, errors = ui
    seed_sweep(
        scenarios.parent / "data", "2026-08-10T11-57-06Z",
        status={"state": "done", "total": 93, "legs_found": 5, "depth": "quick"},
    )
    page.reload(wait_until="networkidle")
    page.locator('#tabs button[data-tab="results"]').click()
    page.wait_for_timeout(900)
    assert page.locator("#completeness").is_hidden()
    assert not errors
