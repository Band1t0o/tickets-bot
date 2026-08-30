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
import re
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


def seed_sweep(data_dir, stamp, *, status, legs=LEGS, observed_at=None, url=""):
    directory = data_dir / "sweeps" / "jp-ph" / stamp
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "legs.jsonl").open("w", encoding="utf-8") as handle:
        for row in legs:
            # A fifth element carries the baggage state. Absent means confirmed,
            # which is what every leg seeded before the bag filter existed meant.
            origin, destination, depart, price = row[:4]
            checked_bag = row[4] if len(row) > 4 else True
            handle.write(json.dumps({
                "provider": "T", "origin": origin, "destination": destination,
                "depart_date": depart, "airline": "QR", "flight_number": None,
                # Empty by default, as every seeded leg was before booking links
                # existed: a sweep records whatever the site gave it, and often
                # that is no URL at all.
                "stops": 1, "price_currency": "CZK", "price_amount": price, "url": url,
                "depart_time": None, "arrive_time": None, "duration_minutes": None,
                "checked_bag": checked_bag, "observed_at": observed_at,
            }) + "\n")
    (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")


def open_prices(page):
    """The by-date chart and the focus picked on it: Narrow it down."""
    page.locator('#tabs button[data-tab="narrow"]').click()
    page.wait_for_timeout(900)


def open_trend(page):
    """The history chart and the probe: Follow it.

    Split from `open_prices` because the two used to share a tab and answered
    different questions there - which days, against whether to book now.
    """
    page.locator('#tabs button[data-tab="follow"]').click()
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
    page.locator('#tabs button[data-tab="narrow"]').click()
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
    page.locator('#tabs button[data-tab="map"]').click()
    page.wait_for_timeout(900)


def open_one_run(page):
    """Unfold the single-run report.

    The tab opens on every airport of the trip, judged by whichever run measured
    it best, because that is the question people arrive with. Reading one run on
    its own is still there and still has notices nothing else can carry - how
    much of *that* run's plan came back, which way round it found cheaper - and
    it is folded shut until asked for.
    """
    page.locator("#explore-one-run > summary").click()
    page.wait_for_timeout(400)


def open_results(page):
    page.locator('#tabs button[data-tab="narrow"]').click()
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

    report = page.locator("#explore-verdicts")
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
    prague = page.locator("#explore-verdicts tbody tr", has_text="PRG").first.inner_text()
    vienna = page.locator("#explore-verdicts tbody tr", has_text="VIE").first.inner_text()
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

    page.locator("#explore-verdicts tbody tr", has_text="VIE").first.locator(
        "button", has_text="Remove from trip"
    ).click()
    page.wait_for_timeout(500)

    # Still on the Explore tab, with what you have decided in front of you.
    assert page.locator('section[data-panel="explore"]').is_visible()
    assert "VIE" in page.locator("#explore-pending").inner_text()
    # The row stays, struck through: the table is the comparison you just made,
    # and deleting rows from it would hide what you compared against.
    vienna = page.locator("#explore-verdicts tbody tr", has_text="VIE").first
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

    page.locator("#explore-verdicts tbody tr", has_text="VIE").first.locator(
        "button", has_text="Remove from trip"
    ).click()
    page.wait_for_timeout(500)
    page.locator("#explore-undo").click()
    page.wait_for_timeout(700)

    assert page.locator("#explore-pending").is_hidden()
    page.locator('#tabs button[data-tab="map"]').click()
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

    vienna = page.locator("#explore-verdicts tbody tr", has_text="VIE").first
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
    open_one_run(page)

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
    note = page.locator("#explore-cost-line").inner_text()
    assert "searches" in note and "min" in note, note
    assert not errors


def test_the_price_of_a_probe_survives_the_explanations_being_off(ui):
    """The cost moves with the trip, so it is an answer and not a lesson. It
    used to sit inside the paragraph explaining what a probe is, which is the
    paragraph the explanations switch takes away."""
    page, _, errors = ui
    page.wait_for_timeout(700)

    assert not page.locator("#explore-note").is_visible()      # the lesson
    assert page.locator("#explore-cost-line").is_visible()     # the number
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
    open_one_run(page)

    notice = page.locator("#explore-mismatch")
    assert notice.is_visible()
    assert "BCN" in notice.inner_text(), notice.inner_text()
    # Named, not merely flagged: "different trip" said not to trust the row
    # without saying which part of it to distrust.
    assert "different airports" in page.locator("#explore-select").inner_text()
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
    # The single-run report, because this is a question about a run: what did it
    # search, and what was the choice made against. The merged table above it
    # answers a different one - every airport of the trip as it stands - and an
    # airport taken out and saved is correctly not in that.
    open_one_run(page)

    vienna = page.locator("#explore-report tbody tr", has_text="VIE").first
    assert "is-dropped" not in (vienna.get_attribute("class") or "")
    assert "not in this trip" in vienna.inner_text().lower(), vienna.inner_text()
    assert not errors


def test_the_merged_table_is_about_the_trip_and_not_about_a_run(ui):
    """The other half of the division above. An airport removed and saved is not
    part of "every airport of this trip", so it leaves that table - while the run
    that priced it keeps its row, one disclosure below."""
    page, scenarios, errors = ui
    seed_probe(scenarios)
    seed_searched_trip(scenarios, origins=["PRG", "VIE"])

    page.locator("#origins .chip__remove").last.click()
    page.wait_for_timeout(300)
    page.locator("#save-btn").click()
    page.wait_for_timeout(900)
    page.reload(wait_until="networkidle")
    open_explore(page)

    assert page.locator("#explore-verdicts tbody tr", has_text="VIE").count() == 0
    assert page.locator("#explore-verdicts tbody tr", has_text="PRG").count() > 0
    assert not errors


def test_an_airport_dropped_but_not_yet_saved_stays_on_screen(ui):
    """Struck through rather than removed: the table is a reading of what was
    compared, and deleting rows as you decide would leave you unable to see what
    you decided against. The merged table reads the *saved* trip, so a pending
    drop is still one of its rows - which is what makes this work at all."""
    page, scenarios, errors = ui
    seed_probe(scenarios)
    seed_searched_trip(scenarios, origins=["PRG", "VIE"])
    page.reload(wait_until="networkidle")
    open_explore(page)

    vienna = page.locator("#explore-verdicts tbody tr", has_text="VIE").first
    vienna.locator("button", has_text="Remove from trip").click()
    page.wait_for_timeout(700)

    again = page.locator("#explore-verdicts tbody tr", has_text="VIE").first
    assert "is-dropped" in (again.get_attribute("class") or "")
    assert not page.locator("#explore-pending").is_hidden()
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
    open_trend(page)

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
    open_trend(page)

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
    open_trend(page)
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
#
# Behind the gear since the tabs became three steps: what gets sent is
# configuration, not a decision about which days to fly.


def open_setup(page):
    page.locator("#tabs button[data-tab='setup']").click()
    page.wait_for_selector("#add-tier-btn")


def test_a_preferred_tier_is_typed_and_saved(ui):
    page, scenario_dir, errors = ui
    open_setup(page)
    page.locator("#add-tier-btn").click()
    page.wait_for_timeout(300)

    page.locator('[data-picker="tier-0"] .typeahead input').click()
    page.keyboard.type("VIE", delay=55)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)

    assert chips(page, "#preferred-tiers") == ["VIE"]
    page.locator("#notify-save-btn").click()
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
    open_setup(page)
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
    open_setup(page)
    page.locator("#notify-preferred").uncheck()
    page.locator("#notify-quiet").uncheck()
    page.locator("#notify-save-btn").click()
    page.wait_for_timeout(900)

    saved = json.loads((scenario_dir / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["notify"] == ["cheapest"]
    assert saved["notify_quiet"] is False
    assert not errors


def test_an_empty_tier_row_is_dropped_rather_than_failing_the_save(ui):
    page, scenario_dir, errors = ui
    open_setup(page)
    page.locator("#add-tier-btn").click()
    page.wait_for_timeout(300)
    page.locator("#notify-save-btn").click()
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
    page.locator('#tabs button[data-tab="setup"]').click()
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

    page.locator('#tabs button[data-tab="map"]').click()
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
    page.click("#tabs button[data-tab='setup']")
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
    page.click("#tabs button[data-tab='setup']")
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
# There is one place now. The chart used to be a panel of its own with its own
# badge and its own Save, writing the same `focus_start`/`focus_end` as the
# "Leave between" boxes two panels above it - so a click and a keystroke set one
# field through two controls that never agreed on screen. The chart fills the
# boxes; the narrowing panel's Save is what writes them. These tests assert the
# join, because it is the join that was broken.
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
    # Scrolled into view first: `page.mouse.click` takes viewport coordinates
    # and does not scroll, and the by-date chart now sits under three leg
    # charts - below the fold on a 1400px viewport, where every click landed on
    # whatever happened to be at those coordinates instead.
    page.locator("#chart-by-date").scroll_into_view_if_needed()
    page.wait_for_timeout(150)
    box = page.locator("#chart-by-date svg").bounding_box()
    scale = box["width"] / float(page.locator("#chart-by-date svg").get_attribute("width"))
    left = 44 * scale
    plot = box["width"] - (44 + 16) * scale
    x = box["x"] + left + (plot * index / max(1, total - 1))
    page.mouse.click(x, box["y"] + box["height"] / 2)
    page.wait_for_timeout(500)


def leave_between(page):
    """Whatever the two "Leave between" boxes currently hold."""
    return (
        page.locator("#narrow-out-start").input_value(),
        page.locator("#narrow-out-end").input_value(),
    )


def test_clicking_two_points_fills_the_leave_between_boxes(ui):
    """The click has to land in the field, not in a picker of its own."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_prices(page)

    assert leave_between(page) == ("", "")

    chart_point(page, 1, 5)
    # A half-open range still shows the day it has, or the first click looks
    # like it did nothing at all.
    assert leave_between(page) == ("2027-01-11", ""), leave_between(page)

    chart_point(page, 3, 5)
    assert leave_between(page) == ("2027-01-11", "2027-01-13"), leave_between(page)
    # Priced against the trip on screen, not guessed - and by the panel's own
    # estimate line, not by a second one belonging to the chart.
    cost = page.locator("#narrow-cost").inner_text()
    assert "searches" in cost and "whole window" in cost, cost
    assert not errors


def test_a_picked_range_is_written_by_the_panels_own_save(ui):
    """One Save for the whole narrowing. The chart no longer has one."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_prices(page)

    chart_point(page, 1, 5)
    chart_point(page, 3, 5)
    page.locator("#narrow-save").click()
    page.wait_for_timeout(900)

    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["focus_start"] == "2027-01-11"
    assert saved["focus_end"] == "2027-01-13"
    # And the one badge that reports the narrowing says so, in its own words.
    assert "01-11" in page.locator("#narrow-state").inner_text()
    assert not errors


def test_clearing_takes_the_departure_window_with_it(ui):
    """It used to leave `focus_start` alone, because the departure window had a
    Clear of its own on a panel that no longer exists. That would have left a
    narrowing nothing on screen could undo."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_prices(page)

    chart_point(page, 1, 5)
    chart_point(page, 3, 5)
    page.locator("#narrow-save").click()
    page.wait_for_timeout(900)

    page.locator("#narrow-clear").click()
    page.wait_for_timeout(900)

    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["focus_start"] is None and saved["focus_end"] is None
    assert leave_between(page) == ("", "")
    assert "whole window" in page.locator("#narrow-state").inner_text()
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
    assert leave_between(page) == ("2027-01-12", ""), leave_between(page)
    assert not errors


def test_clicking_backwards_still_reads_as_a_range(ui):
    """The boxes are a from and a to, not a first click and a second one."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_prices(page)

    chart_point(page, 3, 5)
    chart_point(page, 1, 5)
    assert leave_between(page) == ("2027-01-11", "2027-01-13"), leave_between(page)
    assert not errors


def test_a_typed_date_draws_on_the_chart_like_a_clicked_one(ui):
    """One state, drawn twice. The band used to be read off the *saved* trip, so
    a date typed into the box beside it changed nothing on the chart until a
    save had happened."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_prices(page)

    page.locator("#narrow-out-start").fill("2027-01-11")
    page.locator("#narrow-out-start").dispatch_event("change")
    page.locator("#narrow-out-end").fill("2027-01-13")
    page.locator("#narrow-out-end").dispatch_event("change")
    page.wait_for_timeout(900)

    # The band is the one <rect> `lineChart` ever draws, and nothing is saved
    # until the panel's Save is pressed.
    assert page.locator("#chart-by-date svg rect").count() == 1
    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["focus_start"] is None
    assert not errors


def test_an_unpicked_chart_draws_no_band_at_all(ui):
    """The counterpart, so the assertion above is measuring the band and not
    merely finding some rectangle that is always there."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_prices(page)

    assert page.locator("#chart-by-date svg rect").count() == 0
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
    page.locator('#tabs button[data-tab="narrow"]').click()
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
    page.locator('#tabs button[data-tab="narrow"]').click()
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
    page.locator('#tabs button[data-tab="narrow"]').click()
    page.wait_for_timeout(900)
    assert page.locator("#completeness").is_hidden()
    assert not errors


# --------------------------------------------------------------- overland
#
# Arrive at one airport of a stop and leave from another, crossing the country
# on the ground: Haneda in, Kansai out. The setting has to survive the round
# trip to disk - the shape it is stored in lists a stop's fields by hand, so a
# new one is dropped silently - and it must be unavailable where it would mean
# nothing.


def test_ticking_overland_writes_it_to_the_trip_on_disk(ui):
    page, scenarios, errors = ui

    # A second Japanese airport, so there is somewhere else to leave from.
    page.locator("#stops .typeahead input").first.click()
    page.keyboard.type("Kansai", delay=45)
    page.keyboard.press("Enter")
    page.wait_for_timeout(700)
    assert chips(page, "#stops") == ["NRT", "KIX", "MNL"]

    page.locator('#stops .stop__overland input').first.check()
    page.locator("#save-btn").click()
    page.wait_for_timeout(900)

    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["stops"][0]["overland"] is True
    assert saved["stops"][1]["overland"] is False
    assert not errors


def test_overland_cannot_be_ticked_on_a_stop_with_one_airport(ui):
    """Nowhere else to leave from, so the box would do nothing at all."""
    page, _, errors = ui
    box = page.locator('#stops .stop__overland input').first
    assert box.is_disabled()
    assert not errors


def test_a_saved_overland_stop_comes_back_ticked(ui):
    """The reason to test the round trip and not just the write: the trip is
    re-read from disk into the form, and a field the form drops on the way out
    is invisible until a sweep quietly chains the wrong airports."""
    page, scenarios, errors = ui
    page.locator("#stops .typeahead input").first.click()
    page.keyboard.type("Kansai", delay=45)
    page.keyboard.press("Enter")
    page.wait_for_timeout(700)
    page.locator('#stops .stop__overland input').first.check()
    page.locator("#save-btn").click()
    page.wait_for_timeout(900)

    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")
    assert page.locator('#stops .stop__overland input').first.is_checked()
    assert not errors


OVERLAND_LEGS = [
    ("PRG", "NRT", "2027-01-10", 12000.0),   # fly into Tokyo
    ("KIX", "MNL", "2027-01-20", 4000.0),    # ...and out of Osaka
    ("MNL", "PRG", "2027-01-30", 14000.0),
]


def seed_overland_trip(scenarios):
    """The saved trip crosses Japan on the ground, so the legs above chain."""
    save_scenario(
        Scenario(
            id="jp-ph",
            name="Japan then Philippines",
            origins=["PRG", "VIE"],
            stops=[
                Stop(airports=["NRT", "KIX"], stay_days=(9, 11), label="Japan", overland=True),
                Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
            ],
            window_start=date(2027, 1, 5),
            window_end=date(2027, 2, 8),
            depth="quick",
        ),
        scenarios,
    )


def test_results_say_out_loud_that_you_cross_japan_yourself(ui):
    """A total that includes a journey nobody booked has to admit it.

    The route string is built from the leg endpoints, so before this it read
    "PRG → NRT → MNL → PRG" - a route no ticket in the itinerary flies, and no
    hint that the trip needs you to get from Tokyo to Osaka on your own.
    """
    page, scenarios, errors = ui
    seed_overland_trip(scenarios)
    seed_sweep(
        scenarios.parent / "data", "2026-08-11T09-00-00Z",
        status={"state": "done", "total": 3, "legs_found": 3, "depth": "quick"},
        legs=OVERLAND_LEGS,
    )
    page.reload(wait_until="networkidle")
    open_results(page)

    headline = page.locator("#headline").inner_text()
    assert "NRT ⇢ KIX" in headline
    assert "you get from NRT to KIX yourself" in headline
    assert "overland" in page.locator("#results-table tbody").inner_text()
    assert not errors



# ------------------------------------------------------------- the follow step
#
# Picking the two or three trips you are deciding between and following just
# those, every few hours. The step is the whole feature's surface: the
# preferences, how each has moved since, and what checking them costs.


def open_watch(page):
    page.locator('#tabs button[data-tab="follow"]').click()
    page.wait_for_timeout(900)


def seed_watch_observations(data_dir, key="2027-01-10", totals=(30000, 28500)):
    directory = data_dir / "watch" / "jp-ph"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "observations.jsonl").open("a", encoding="utf-8") as handle:
        for index, total in enumerate(totals):
            handle.write(json.dumps({
                "ts": f"2026-08-20T0{index}:00:00+00:00",
                "scenario_id": "jp-ph",
                "depart_date": key,
                "pinned_dates": [key, "2027-01-20", "2027-01-30"],
                "found_dates": [key, "2027-01-20", "2027-01-30"],
                "route": "PRG \u2192 NRT \u2192 MNL \u2192 PRG",
                "total": total, "total_with_bags": total, "currency": "CZK",
                "has_overland": False, "coverage": 1.0, "legs_per_search": 9.5,
                "comparable": True,
            }) + "\n")


def test_the_watch_tab_says_when_nothing_is_being_watched(ui):
    page, _, errors = ui
    open_watch(page)
    assert "no preferences yet" in page.locator("#watch-empty").inner_text().lower()
    assert not errors


def test_a_day_can_be_picked_off_the_last_sweep(ui):
    """The source list is what the sweep already found, cheapest day first."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_watch(page)

    page.locator("#watch-candidates .watch-add").first.click()
    page.wait_for_timeout(900)

    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["preferences"][0]["depart_dates"] == ["2027-01-10", "2027-01-20", "2027-01-30"]
    # Picked at a price, so the first observation can already say which way it
    # went rather than only establishing a baseline.
    assert saved["preferences"][0]["added_price"] == 30000
    assert "2027-01-10" in page.locator("#watch-table").inner_text()
    assert not errors


def test_the_tab_says_what_checking_the_watched_days_will_cost(ui):
    """Six runs a day against a site that answers ~120 searches a runner: the
    cost of a pick is not an implementation detail, it is the constraint."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_watch(page)
    page.locator("#watch-candidates .watch-add").first.click()
    page.wait_for_timeout(900)

    # Said once, above both panels. The two used to carry the same whole-run
    # figure each, so a step costing five searches announced five twice.
    # 5 pairs a preference at the default slack of two days either side, which
    # is five dates a leg: 25. The figure the cap is applied to, so a payload
    # that under-reported it would offer a preference the run cannot afford.
    summary = page.locator("#follow-summary-text").inner_text()
    assert "25 searches" in summary, summary
    assert "min" in summary, summary
    assert "both panels together" in summary, summary
    # The panel's own badge counts what is in the panel, against the cap on it.
    assert page.locator("#watch-cost").inner_text() == "1 of 4"
    assert not errors


def test_a_watched_day_can_be_dropped_again(ui):
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_watch(page)
    page.locator("#watch-candidates .watch-add").first.click()
    page.wait_for_timeout(900)

    page.locator("#watch-table .watch-drop").first.click()
    page.wait_for_timeout(900)
    assert json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))["preferences"] == []
    assert not errors


def seed_a_cheaper_neighbour(data_dir, key="2027-01-10"):
    """One observation whose slack found a better set of dates two days out."""
    directory = data_dir / "watch" / "jp-ph"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "observations.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "ts": "2026-08-20T02:00:00+00:00", "scenario_id": "jp-ph",
            "depart_date": key, "label": "10+10 from 10 Jan", "slack_days": 2,
            "pinned_dates": [key, "2027-01-20", "2027-01-30"],
            "found_dates": [key, "2027-01-20", "2027-01-30"],
            "route": "PRG \u2192 NRT \u2192 MNL \u2192 PRG",
            "total": 30000, "total_with_bags": 30000,
            "nearby_total": 26000, "nearby_total_with_bags": 26000,
            "nearby_dates": ["2027-01-12", "2027-01-22", "2027-02-01"],
            "nearby_route": "PRG \u2192 NRT \u2192 MNL \u2192 PRG",
            "currency": "CZK", "has_overland": False, "coverage": 1.0,
            "legs_per_search": 9.5, "comparable": True,
        }) + "\n")


def test_a_cheaper_set_of_dates_inside_the_slack_is_offered(ui):
    """The entire return on the slack.

    Without this the extra searches buy a number nobody sees, and a preference
    can only ever report that its own Tuesday has not moved.
    """
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    seed_a_cheaper_neighbour(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_watch(page)
    page.locator("#watch-candidates .watch-add").first.click()
    page.wait_for_timeout(900)

    offer = page.locator("#watch-table .pref-offer").inner_text()
    assert "2 days later" in offer, offer
    # `toLocaleString` groups with a non-breaking space, as every other money
    # figure on the page does.
    assert "4 000 CZK cheaper" in offer, offer
    assert "2027-02-01" in offer, offer
    assert not errors


def test_taking_the_offer_moves_the_preference_rather_than_adding_one(ui):
    """The series belongs to the decision.

    A new preference on every shift would leave you unable to see that the trip
    has fallen four thousand since you began looking at it.
    """
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    seed_a_cheaper_neighbour(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_watch(page)
    page.locator("#watch-candidates .watch-add").first.click()
    page.wait_for_timeout(900)

    page.locator("#watch-table .pref-offer button").click()
    page.wait_for_timeout(900)

    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert len(saved["preferences"]) == 1
    assert saved["preferences"][0]["depart_dates"] == [
        "2027-01-12", "2027-01-22", "2027-02-01"
    ]
    assert not errors


def test_a_preference_already_on_the_best_dates_is_offered_nothing(ui):
    """`nearby` includes the pinned days, so an equal figure is not a saving."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    seed_watch_observations(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_watch(page)
    page.locator("#watch-candidates .watch-add").first.click()
    page.wait_for_timeout(900)

    assert page.locator("#watch-table .pref-offer").count() == 0
    assert not errors


def test_the_chart_draws_one_line_per_watched_day(ui):
    """Shared axes, because the question is which of them to book.

    A chart each would make two candidates 200 crowns apart look identical.
    """
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_watch(page)
    # The second click is on the *next* row: a day already being watched has its
    # button disabled, which is the behaviour that stops a day being watched
    # twice and paying for it twice.
    page.locator("#watch-candidates .watch-add").nth(0).click()
    page.wait_for_timeout(700)
    assert page.locator("#watch-candidates .watch-add").nth(0).is_disabled()
    page.locator("#watch-candidates .watch-add").nth(1).click()
    page.wait_for_timeout(700)

    seed_watch_observations(scenarios.parent / "data", "2027-01-10", (30000, 28500))
    seed_watch_observations(scenarios.parent / "data", "2027-01-11", (30100, 30050))
    page.reload(wait_until="networkidle")
    open_watch(page)

    legend = page.locator("#watch-chart .chart-legend__item").all_inner_texts()
    assert len(legend) == 2
    # Named by rank and shape rather than by the raw departure date, and the
    # name carries the *day* - two preferences of the same shape a week apart
    # are exactly the pair this chart exists to compare.
    assert any("10 Jan" in entry for entry in legend), legend
    assert any("11 Jan" in entry for entry in legend), legend
    assert legend[0].startswith("1 ")
    assert not errors


def test_the_tab_reports_how_far_a_watched_day_has_moved(ui):
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_watch(page)
    page.locator("#watch-candidates .watch-add").first.click()
    page.wait_for_timeout(900)

    seed_watch_observations(scenarios.parent / "data", "2027-01-10", (30000, 28500))
    page.reload(wait_until="networkidle")
    open_watch(page)

    # Normalised: the locale groups thousands with a non-breaking space.
    row = page.locator("#watch-table tbody tr").first.inner_text().replace(" ", " ")
    assert "28 500" in row  # what it costs now
    assert "30 000" in row  # what it cost when picked
    assert "-1 500" in row  # and the move between them
    assert not errors


# ----------------------------------------------------- results: filter row
#
# Driven through the controls rather than through the endpoint, because the
# thing worth protecting is that the table and the headline cards above it
# always describe the same population. The server narrows the whole traversal;
# if this page ever starts narrowing the fifty rows it was sent instead, the
# cards keep reporting a Prague trip while the table shows only Vienna ones and
# nothing on screen says which is the answer.

FILTER_LEGS = [
    ("PRG", "NRT", "2027-01-10", 12000.0, True),
    ("VIE", "NRT", "2027-01-10", 13000.0, True),
    ("NRT", "MNL", "2027-01-20", 4000.0, True),
    # The cheapest way home, and the site never said whether a bag is included.
    ("MNL", "PRG", "2027-01-30", 9000.0, None),
    ("MNL", "PRG", "2027-01-30", 14000.0, True),
    ("MNL", "VIE", "2027-01-30", 11000.0, True),
]


def digits(text: str) -> str:
    """Text with every kind of thousands separator squeezed out.

    `money` groups in the browser's own locale, so 25,000 renders as "25 000"
    with a non-breaking space on this machine and "25,000" on another. Asserting
    on either spelling makes the test a statement about the runner's locale.
    """
    return re.sub(r"[\s ,]", "", text)


def seed_for_filters(scenarios, url=""):
    seed_sweep(
        scenarios.parent / "data", "2026-08-11T09-00-00Z",
        status={"state": "done", "mode": "sweep", "total": 6, "completed": 6,
                "legs_found": 6, "depth": "deep", "coverage": 1.0},
        legs=FILTER_LEGS, url=url,
    )


def filter_to(page, *, origin=None, destination=None, bags=False):
    """Set the controls the way a person does, and let each re-fetch settle."""
    if origin is not None:
        page.locator("#filter-from").select_option(origin)
        page.wait_for_timeout(700)
    if destination is not None:
        page.locator("#filter-to").select_option(destination)
        page.wait_for_timeout(700)
    if bags:
        page.locator("#filter-bags").check()
        page.wait_for_timeout(700)


def test_the_filter_offers_the_trips_own_airports(ui):
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_results(page)
    assert page.locator("#filter-from option").count() >= 3  # "any" plus the pools
    assert "PRG" in page.locator("#filter-from").inner_text()
    assert "VIE" in page.locator("#filter-from").inner_text()
    assert not errors


def test_narrowing_to_an_origin_narrows_the_table_and_the_cards_together(ui):
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_results(page)

    filter_to(page, origin="VIE")
    rows = page.locator("#results-table tbody tr").all_inner_texts()
    routes = [text for text in rows if "→" in text]
    assert routes, "expected some itineraries"
    assert all(text.strip().startswith("VIE") for text in routes), routes
    # The cards are the point: they must not still be quoting the Prague trip.
    assert "VIE" in page.locator("#headline").inner_text()
    assert not errors


def test_the_bag_tick_hides_the_cheapest_trip_when_its_bag_is_unknown(ui):
    """9,000 home is the cheapest and its baggage was never stated.

    It has to disappear, and the note has to say the trips went because nothing
    confirmed a bag — not because a bag is known to cost extra.
    """
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_results(page)
    assert "25000CZK" in digits(page.locator("#headline").inner_text())

    filter_to(page, bags=True)
    assert "25000CZK" not in digits(page.locator("#headline").inner_text())
    note = page.locator("#filter-note").inner_text()
    assert "confirms a checked bag" in note
    assert not errors


def test_a_filter_that_matches_nothing_says_so_rather_than_looking_broken(ui):
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_results(page)
    filter_to(page, origin="VIE", destination="PRG", bags=True)
    # VIE out and PRG home exists, but only on the unconfirmed 9,000 fare.
    assert "No trip in this sweep matches" in page.locator("#results-empty").inner_text()
    assert not errors


def test_reset_puts_every_trip_back(ui):
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_results(page)
    before = page.locator("#results-table tbody tr").count()

    filter_to(page, origin="VIE")
    assert page.locator("#filter-reset").is_visible()
    page.locator("#filter-reset").click()
    page.wait_for_timeout(800)

    assert page.locator("#filter-reset").is_hidden()
    assert page.locator("#results-table tbody tr").count() == before
    assert page.locator("#filter-from").input_value() == ""
    assert not errors


def test_every_time_on_screen_is_a_prague_time(ui):
    """The sweep ran at 09:00 UTC, which is 11:00 in Prague in August."""
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_results(page)
    label = page.locator("#sweep-select").inner_text()
    assert "11:00" in label, label
    # And never the raw directory stamp it is built from.
    assert "09-00-00" not in label
    assert not errors


# ------------------------------------------------- pinning an overland crossing


def stop_card(page):
    return page.locator("#stops .stop").first


def make_stop_crossable(page):
    """Give the first stop a second airport, typed the way a person types it.

    The fixture trip has one Japanese airport, and with one airport there is
    nowhere else to leave from - so the overland box is disabled and says why.
    Adding HND is the precondition for every test below, not part of what they
    are checking.
    """
    stop_card(page).locator(".typeahead input").click()
    page.keyboard.type("HND", delay=55)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)


def test_the_pins_appear_only_once_overland_is_ticked(ui):
    """Without overland the chain rule already decides it, so a pin would be a
    contradiction the server refuses by name."""
    page, _, errors = ui
    page.wait_for_timeout(600)
    make_stop_crossable(page)
    assert stop_card(page).locator(".stop__pins").count() == 0

    stop_card(page).locator(".stop__overland input").check()
    page.wait_for_timeout(400)
    assert stop_card(page).locator(".stop__pins").count() == 1
    assert not errors


def test_pinning_a_crossing_drops_the_searches_it_will_cost(ui):
    """The whole reason to pin: the badge beside the run buttons must fall."""
    page, _, errors = ui
    page.wait_for_timeout(600)
    make_stop_crossable(page)
    stop_card(page).locator(".stop__overland input").check()
    page.wait_for_timeout(900)
    before = page.locator("#estimate").inner_text()

    stop_card(page).locator(".stop__pins select").first.select_option("NRT")
    page.wait_for_timeout(1200)
    after = page.locator("#estimate").inner_text()

    assert digits(before) != digits(after), f"{before} -> {after}"
    assert int(re.search(r"\d+", digits(after)).group()) < int(
        re.search(r"\d+", digits(before)).group()
    )
    assert not errors


def test_a_pinned_crossing_survives_a_save_and_reload(ui):
    page, _, errors = ui
    page.wait_for_timeout(600)
    make_stop_crossable(page)
    stop_card(page).locator(".stop__overland input").check()
    page.wait_for_timeout(400)
    selects = stop_card(page).locator(".stop__pins select")
    selects.nth(0).select_option("NRT")
    page.wait_for_timeout(300)
    selects.nth(1).select_option("HND")
    page.wait_for_timeout(300)

    page.locator("#save-btn").click()
    page.wait_for_timeout(1200)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(900)

    reloaded = stop_card(page).locator(".stop__pins select")
    assert reloaded.nth(0).input_value() == "NRT"
    assert reloaded.nth(1).input_value() == "HND"
    assert not errors


def test_unticking_overland_clears_the_pins_rather_than_stranding_them(ui):
    """A stop carrying a pin without overland cannot be saved at all, and the
    control that would explain why is no longer on screen."""
    page, _, errors = ui
    page.wait_for_timeout(600)
    make_stop_crossable(page)
    box = stop_card(page).locator(".stop__overland input")
    box.check()
    page.wait_for_timeout(400)
    stop_card(page).locator(".stop__pins select").first.select_option("NRT")
    page.wait_for_timeout(400)

    box.uncheck()
    page.wait_for_timeout(400)
    page.locator("#save-btn").click()
    page.wait_for_timeout(1200)

    assert page.locator("#save-error").is_hidden()
    assert not errors


# ------------------------------------------------------- which way round to fly

BOTH_ORDER_LEGS = [
    # Japan first: 12,000 + 4,000 + 9,000
    ("PRG", "NRT", "2027-01-10", 12000.0),
    ("NRT", "MNL", "2027-01-24", 4000.0),
    ("MNL", "PRG", "2027-02-06", 9000.0),
    # Philippines first, dearer on every hop: 15,000 + 4,500 + 11,000
    ("PRG", "MNL", "2027-01-10", 15000.0),
    ("MNL", "NRT", "2027-01-24", 4500.0),
    ("NRT", "PRG", "2027-02-06", 11000.0),
]


def seed_both_orders(scenarios, *, reverse_is_cheaper=False):
    """A probe of a trip that asked for both orders, with its scenario snapshot."""
    legs = list(BOTH_ORDER_LEGS)
    if reverse_is_cheaper:
        # Halve the Philippines-first hops so the other way round wins.
        legs = legs[:3] + [(o, d, t, p / 3) for o, d, t, p in legs[3:]]

    data = scenarios.parent / "data"
    stamp = "2026-08-11T09-00-00Z"
    routes = {f"{o}->{d}": 3 for o, d, _, _ in legs}
    seed_sweep(
        data, stamp,
        status={"state": "done", "mode": "explore", "total": len(routes) * 3,
                "completed": len(routes) * 3, "legs_found": len(legs),
                "route_searches": routes, "route_errors": dict.fromkeys(routes, 0)},
        legs=legs,
    )
    trip = Scenario(
        id="jp-ph", name="Japan then Philippines", origins=["PRG"],
        stops=[
            Stop(airports=["NRT"], stay_days=(9, 16), label="Japan"),
            Stop(airports=["MNL"], stay_days=(9, 16), label="Philippines"),
        ],
        window_start=date(2027, 1, 5), window_end=date(2027, 2, 8),
        depth="quick", probe_both_orders=True,
    )
    save_scenario(trip, scenarios)
    (data / "sweeps" / "jp-ph" / stamp / "scenario.json").write_text(
        json.dumps(trip.to_dict()), encoding="utf-8"
    )
    return stamp


def test_the_probe_says_which_way_round_is_cheaper(ui):
    page, scenarios, errors = ui
    seed_both_orders(scenarios)
    page.reload(wait_until="networkidle")
    open_explore(page)
    open_one_run(page)

    verdict = page.locator("#explore-orders")
    assert verdict.is_visible()
    text = verdict.inner_text()
    assert "Japan first" in text and "Philippines first" in text
    assert "cheapest sampled" in text
    # Never presented as a bookable trip: three dates a leg rarely chain.
    assert "not a trip you could book" in text
    assert not errors


def test_a_trip_that_probed_one_order_shows_no_comparison(ui):
    page, scenarios, errors = ui
    seed_probe(scenarios)
    page.reload(wait_until="networkidle")
    open_explore(page)
    open_one_run(page)
    assert page.locator("#explore-orders").is_hidden()
    assert not errors


def test_the_reorder_button_appears_only_when_the_reverse_wins(ui):
    page, scenarios, errors = ui
    seed_both_orders(scenarios)
    page.reload(wait_until="networkidle")
    open_explore(page)
    open_one_run(page)
    assert page.locator("#explore-orders button").count() == 0
    assert not errors


def test_reordering_edits_the_trip_and_leaves_it_for_you_to_save(ui):
    """A probe is a reason to look, not a decision. Nothing saves itself."""
    page, scenarios, errors = ui
    seed_both_orders(scenarios, reverse_is_cheaper=True)
    page.reload(wait_until="networkidle")
    open_explore(page)
    open_one_run(page)

    button = page.locator("#explore-orders button")
    assert button.count() == 1
    button.click()
    page.wait_for_timeout(900)

    # Landed back on Search, with the stops the other way round and unsaved.
    cards = page.locator("#stops .stop__label")
    labels = [cards.nth(i).input_value() for i in range(cards.count())]
    assert labels[:2] == ["Philippines", "Japan"], labels
    assert page.locator("#dirty-note").is_visible()
    assert not errors


# ----------------------------------------------------------- following a leg
#
# The flow the feature exists for: read a trip in Results, see a leg that looks
# promising, follow that one flight. Driven through the buttons rather than the
# endpoint, because what is worth protecting is that the decision can be made
# where it is actually made.


def test_a_leg_can_be_followed_from_the_trip_you_are_reading(ui):
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_results(page)

    page.locator("#results-table details summary").first.click()
    page.wait_for_timeout(300)
    page.locator("#results-table details button", has_text="Follow").first.click()
    page.wait_for_timeout(1200)

    open_watch(page)
    assert page.locator("#leg-watch-table tbody tr").count() == 1
    assert "PRG→NRT" in page.locator("#leg-watch-table").inner_text()
    assert not errors


def test_following_a_leg_carries_the_price_it_was_picked_at(ui):
    """So the first check says which way it went, not merely where it started."""
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_results(page)
    page.locator("#results-table details summary").first.click()
    page.wait_for_timeout(300)
    page.locator("#results-table details button", has_text="Follow").first.click()
    page.wait_for_timeout(1200)

    open_watch(page)
    row = page.locator("#leg-watch-table tbody tr").first.inner_text()
    assert "12000" in digits(row), row
    assert not errors


def test_a_leg_can_be_added_by_hand(ui):
    """Not everything worth following is a leg of a trip the sweep built."""
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_watch(page)

    page.locator("#leg-watch-from").fill("VIE")
    page.locator("#leg-watch-to").fill("NRT")
    page.locator("#leg-watch-date").fill("2027-01-14")
    page.locator("#leg-watch-add-btn").click()
    page.wait_for_timeout(1200)

    assert "VIE→NRT" in page.locator("#leg-watch-table").inner_text()
    assert not errors


def test_a_route_this_trip_never_prices_is_flagged_not_refused(ui):
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_watch(page)

    page.locator("#leg-watch-from").fill("VIE")
    page.locator("#leg-watch-to").fill("DPS")
    page.locator("#leg-watch-date").fill("2027-01-14")
    page.locator("#leg-watch-add-btn").click()
    page.wait_for_timeout(1200)

    table = page.locator("#leg-watch-table").inner_text()
    assert "VIE→DPS" in table
    assert "off-trip" in table
    assert page.locator("#leg-watch-error").is_hidden()
    assert not errors


def test_a_mistyped_year_says_why_rather_than_doing_nothing(ui):
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_watch(page)

    page.locator("#leg-watch-from").fill("PRG")
    page.locator("#leg-watch-to").fill("NRT")
    page.locator("#leg-watch-date").fill("2028-01-14")
    page.locator("#leg-watch-add-btn").click()
    page.wait_for_timeout(1200)

    assert "outside the window" in page.locator("#leg-watch-error").inner_text()
    assert page.locator("#leg-watch-table tbody tr").count() == 0
    assert not errors


def test_the_cost_badge_reports_the_whole_check_not_this_panels_share(ui):
    """They share a budget because they share a run against a site that answers
    about 120 searches from one runner."""
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_watch(page)

    page.locator("#leg-watch-from").fill("PRG")
    page.locator("#leg-watch-to").fill("NRT")
    page.locator("#leg-watch-date").fill("2027-01-14")
    page.locator("#leg-watch-add-btn").click()
    page.wait_for_timeout(1200)

    # The whole planned check, in the one line that speaks for both panels.
    summary = page.locator("#follow-summary-text").inner_text()
    assert "both panels together" in summary, summary
    assert "searches" in summary, summary
    assert page.locator("#leg-watch-cost").inner_text() == "1 flight"
    assert not errors


def test_the_follow_step_says_a_follow_does_not_move_when_the_trip_does(ui):
    """The question the tab is arrived with and never used to answer. A watch
    is pinned to its dates - `_admitting` widens the stays to whatever a
    candidate pinned and drops the narrowing outright - so editing the trip
    changes what the next sweep prices and nothing here."""
    page, _, errors = ui
    open_watch(page)

    summary = page.locator("#follow-summary").inner_text()
    assert "Nothing here moves when you change the trip or the narrowing" in summary, summary
    assert not errors


def test_the_follow_step_names_the_path_that_actually_exists(ui):
    """The empty state sent people to a "Results" tab, which stopped existing
    when the seven tabs became three steps."""
    page, _, errors = ui
    open_watch(page)

    empty = page.locator("#leg-watch-empty").inner_text()
    assert "Narrow it down" in empty, empty
    assert "Every flight, priced on its own" in empty, empty
    assert "Results" not in empty, empty
    assert not errors


def test_following_a_flight_survives_a_reload(ui):
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_watch(page)

    page.locator("#leg-watch-from").fill("PRG")
    page.locator("#leg-watch-to").fill("NRT")
    page.locator("#leg-watch-date").fill("2027-01-14")
    page.locator("#leg-watch-add-btn").click()
    page.wait_for_timeout(1200)

    page.reload(wait_until="networkidle")
    open_watch(page)
    assert page.locator("#leg-watch-table tbody tr").count() == 1
    assert not errors


def test_a_followed_flight_can_be_dropped_again(ui):
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_watch(page)

    page.locator("#leg-watch-from").fill("PRG")
    page.locator("#leg-watch-to").fill("NRT")
    page.locator("#leg-watch-date").fill("2027-01-14")
    page.locator("#leg-watch-add-btn").click()
    page.wait_for_timeout(1200)

    page.locator("#leg-watch-table .watch-drop").first.click()
    page.wait_for_timeout(1200)
    assert page.locator("#leg-watch-table tbody tr").count() == 0
    assert page.locator("#leg-watch-empty").is_visible()
    assert not errors


def test_check_now_wakes_up_for_a_followed_leg_alone(ui):
    """A leg watch is a real run even with no pinned trip behind it."""
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_watch(page)
    assert page.locator("#watch-run-btn").is_disabled()

    page.locator("#leg-watch-from").fill("PRG")
    page.locator("#leg-watch-to").fill("NRT")
    page.locator("#leg-watch-date").fill("2027-01-14")
    page.locator("#leg-watch-add-btn").click()
    page.wait_for_timeout(1200)

    assert page.locator("#watch-run-btn").is_enabled()
    assert not errors


# ------------------------------------- a run that fell short, and carrying on
#
# The three things a throttled run needs to say, none of which it said: how much
# of its plan it answered, that it is waiting rather than hung, and that what it
# did get can be carried on rather than re-bought.


def seed_short_probe(scenarios, *, answered=31, planned=123, state="throttled"):
    """A probe the site refused partway, as the 09:16 run on disk was."""
    status = explore_status(state=state) | {
        "total": planned, "completed": answered, "answered": answered,
        "planned": planned, "coverage": round(answered / planned, 4),
        "unanswered": planned - answered,
    }
    seed_sweep(scenarios.parent / "data", "2026-08-11T09-00-00Z",
               status=status, legs=EXPLORE_LEGS)


def test_a_probe_that_answered_a_quarter_of_its_plan_says_so(ui):
    """It ranked airports off 31 of 123 searches in the words a full run uses."""
    page, scenarios, errors = ui
    seed_short_probe(scenarios)
    page.reload(wait_until="networkidle")
    open_explore(page)
    open_one_run(page)

    banner = page.locator("#explore-coverage")
    assert banner.is_visible()
    text = banner.inner_text()
    assert "25%" in text, text
    assert "never answered" in text
    assert not errors


def test_a_probe_that_answered_everything_stays_quiet(ui):
    """A banner that is always there is a banner nobody reads."""
    page, scenarios, errors = ui
    seed_short_probe(scenarios, answered=123, state="done")
    page.reload(wait_until="networkidle")
    open_explore(page)
    open_one_run(page)

    assert page.locator("#explore-coverage").is_hidden()
    assert not errors


def test_a_probe_from_before_coverage_was_recorded_stays_quiet_too(ui):
    """Not knowing must not be drawn as 100%, nor as a warning."""
    page, scenarios, errors = ui
    seed_probe(scenarios)
    page.reload(wait_until="networkidle")
    open_explore(page)
    open_one_run(page)

    assert page.locator("#explore-coverage").is_hidden()
    assert not errors


def test_a_run_that_fell_short_offers_to_be_carried_on(ui):
    page, scenarios, errors = ui
    seed_short_probe(scenarios)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(900)

    button = page.locator("#resume-btn")
    assert button.is_visible()
    # What it will cost, before it is pressed.
    assert "92" in digits(button.inner_text()), button.inner_text()
    assert not errors


def test_carrying_on_says_what_is_already_in_hand(ui):
    page, scenarios, errors = ui
    seed_short_probe(scenarios)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(900)

    note = page.locator("#resume-note").inner_text()
    assert "31" in digits(note) and "123" in digits(note), note
    # And that it may be refused again, without refusing to try.
    assert "refusing" in note
    assert not errors


def test_a_complete_run_is_not_offered_a_carry_on(ui):
    page, scenarios, errors = ui
    seed_short_probe(scenarios, answered=123, state="done")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(900)

    assert page.locator("#resume-btn").is_hidden()
    assert not errors


def test_the_stop_button_is_where_the_run_was_started(ui):
    """It was a small grey button in a thin bar, and went unfound."""
    page, scenarios, errors = ui
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(600)

    # Hidden with nothing running, but present in both places and readable.
    for locator in ("#stop-btn", "#run-stop-btn"):
        assert page.locator(locator).count() == 1
        assert "Stop" in page.locator(locator).inner_text()
    assert not errors



# ----------------------------------------------------------------- night sweep
#
# The scheduled cloud sweep is where a full-sized trip actually finishes -
# 483/483 in nineteen minutes on 21 Aug, against three throttled local runs the
# same morning - and none of it was visible here. Which trips it runs, how many
# runners they need, when it fires, and above all that it sweeps the trips
# committed to the branch rather than the trip on this screen.


def open_night(page):
    page.locator("#tabs button[data-tab='setup']").click()
    page.wait_for_selector("#night-all .night-list__row")


def test_the_night_sweep_lists_what_it_will_run_and_what_that_costs(ui):
    page, _, _ = ui
    open_night(page)
    row = page.locator("#night-all .night-list__row").first
    assert "Japan then Philippines" in row.inner_text()
    cost = row.locator(".night-list__cost").inner_text()
    assert "searches" in cost and "runner" in cost


def test_the_night_sweep_names_when_it_next_runs(ui):
    """The crons are UTC and live in the workflow. Restating them here by hand
    is how the page and Actions come to disagree about what time it is."""
    page, _, _ = ui
    open_night(page)
    when = page.locator("#night-when").inner_text()
    assert when.startswith("Next ")
    # Both kinds of slot are named, and named differently. They price different
    # things - one the window, two the narrowing - and a schedule that described
    # all three the same way is what let the broad sweep stop being broad
    # without anyone noticing it had.
    assert "whole date window" in when, when
    assert when.count("only what you narrowed to") == 2, when


def test_a_trip_in_the_night_sweep_says_what_tonight_costs_it(ui):
    page, _, _ = ui
    open_night(page)
    assert page.locator("#enabled").is_checked()
    assert "Tonight this trip is" in page.locator("#night-this-trip").inner_text()


def test_a_trip_taken_out_of_the_night_sweep_says_so_rather_than_going_quiet(ui):
    """Both of this repo's trips sat switched off while their owner watched for
    nightly results. Nothing anywhere said the night sweep had nothing to do."""
    page, _, _ = ui
    open_night(page)
    page.locator("#enabled").uncheck()
    page.locator("#night-save-btn").click()
    page.wait_for_timeout(900)

    assert "Not swept overnight" in page.locator("#night-this-trip").inner_text()
    assert page.locator("#night-badge").inner_text() == "nothing scheduled"
    assert "is-out" in (page.locator("#night-all .night-list__row").first.get_attribute("class"))


def test_a_trip_the_branch_has_never_seen_says_the_night_sweep_cannot_run_it(ui, monkeypatch):
    """Ticking the box is not enough: the workflow reads the trips committed to
    the branch, so a trip that was never pushed is not swept whatever this page
    says about it.

    The absence has to be stated, and it used not to be: `_cloud_state` answered
    `known: false` both for "the branch has no such trip" and for "this app
    cannot read the branch", and this panel printed the first one's words for
    both. `no_real_git` in conftest makes every test the second case, so this
    test passed on a setup that was never what it describes - and a checkout with
    no git was told to push a file it may well have pushed already.
    """
    import src.web.app as app_module

    monkeypatch.setattr(
        app_module, "_cloud_state",
        lambda *a: {"known": False, "on_branch": False, "differs": [],
                    "included": None, "searches": None},
    )

    page, _, _ = ui
    page.reload(wait_until="networkidle")
    open_night(page)
    warning = page.locator("#night-cloud")
    assert warning.is_visible()
    assert "not on it" in warning.inner_text()
    assert "commit and" in warning.inner_text().lower()


def test_a_branch_this_app_cannot_read_says_that_and_not_go_and_push_it(ui):
    """The fixture's own case, since `no_real_git` refuses every git call.

    "Cannot say" is a real answer and it is not "your trip is missing". Told the
    second, the only action offered is one that may already be done.
    """
    page, _, _ = ui
    open_night(page)
    warning = page.locator("#night-cloud")
    assert warning.is_visible()
    text = warning.inner_text()
    assert "cannot read" in text
    assert "commit and" not in text.lower()


def test_a_trip_the_branch_has_differently_says_which_trip_tonight_is_about(ui, monkeypatch):
    """The live trap on 21 Aug: the nightly sweep was searching three origins
    and 483 dates while this screen showed a one-origin trip of 66, and the
    results being read were of the other trip."""
    import src.web.app as app_module

    wider = Scenario(
        id="jp-ph",
        name="Japan then Philippines",
        origins=["PRG", "VIE", "FRA"],
        stops=[
            Stop(airports=["NRT", "HND"], stay_days=(9, 11), label="Japan"),
            Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
        ],
        window_start=date(2027, 1, 5),
        window_end=date(2027, 2, 8),
    )
    monkeypatch.setattr(app_module, "_cloud_scenario", lambda _id: wider)

    page, _, _ = ui
    page.reload(wait_until="networkidle")
    open_night(page)

    warning = page.locator("#night-cloud")
    assert warning.is_visible()
    text = warning.inner_text()
    assert "running a different version of this trip" in text
    assert "the airports it searches" in text


# ------------------------------------------------------------ narrowing
#
# The step after the map: state when you leave, when you fly home and how long
# you are away, then pick a combination off the per-leg charts by hand. Driven
# through a real browser because the whole feature is dragging, and a drag that
# lands on the wrong date is exactly the failure a unit test cannot see.


def open_narrow(page):
    page.locator('#tabs button[data-tab="narrow"]').click()
    page.wait_for_timeout(900)


def drag_marker(page, leg_index, fraction, host="#leg-charts"):
    """Drag one leg's marker to `fraction` across the shared date axis.

    By fraction of the rendered box rather than to a date, for the same reason
    `chart_point` clicks that way: the SVG scales to its container, so the only
    honest way to land near a point is to compute from the box on screen.
    """
    svg = page.locator(f"{host} .leg-chart").nth(leg_index).locator("svg").first
    # Scrolled into view first, for the reason `chart_point` gives above:
    # `page.mouse` takes viewport coordinates and does not scroll, so a chart
    # below the fold is dragged at coordinates pointing at something else and
    # the marker simply never moves. The readout above these charts grew a row
    # per leg when it gained the booking links, which was enough to push the
    # third one off a 1400px viewport - and every drag test then asserted
    # against a cursor that had not moved.
    svg.scroll_into_view_if_needed()
    page.wait_for_timeout(150)
    box = svg.bounding_box()
    middle = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] * 0.5, middle)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * fraction, middle, steps=10)
    page.mouse.up()
    page.wait_for_timeout(400)


def test_the_narrow_step_draws_one_chart_per_leg_on_one_axis(ui):
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    charts = page.locator("#leg-charts .leg-chart")
    assert charts.count() == 3, page.locator("#leg-charts").inner_text()

    # One shared date axis is the whole point: three charts that scaled to their
    # own dates would put three different days in one vertical slice.
    widths = [
        charts.nth(i).locator("svg").first.get_attribute("width") for i in range(3)
    ]
    assert len(set(widths)) == 1, widths
    assert not errors


def test_the_readout_prices_the_trip_the_markers_point_at(ui):
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    readout = page.locator("#cursor-readout").inner_text()
    # 12,000 + 4,000 + 14,000, every leg bagged, on the cheapest departure.
    assert "30,000" in readout.replace(" ", ","), readout
    # The split is asserted as arithmetic rather than as one exact pair. Two
    # returns cost the same in this fixture, so which of them wins is a tie the
    # combiner may break either way, and pinning it would make this a test about
    # tie-breaking rather than about the readout.
    stays = [int(n) for n in re.findall(r"(\d+) \+ (\d+) =", readout)[0]]
    away = int(re.search(r"= (\d+) nights away", readout).group(1))
    assert sum(stays) == away
    assert all(9 <= n <= 11 for n in stays), readout
    assert "fits every rule" in readout, readout
    assert not errors


def test_dragging_a_marker_past_a_stay_range_warns_and_does_not_block(ui):
    """The point of the whole panel: a rule you set must not hide a price.

    Nothing here refuses the drag. It says which rule the pick breaks and
    carries on pricing it.
    """
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    before = page.locator("#cursor-readout").inner_text()
    assert "fits every rule" in before, before

    # Drag the middle leg's marker to the far left: a Japan stay far shorter
    # than the 9-11 the trip declares.
    drag_marker(page, 1, 0.66)

    after = page.locator("#cursor-readout").inner_text()
    assert after != before
    assert "nights at Japan" in after, after
    assert "fits every rule" not in after, after
    # Warned, not blocked — the point of the whole panel.
    assert page.locator("#cursor-watch").is_enabled()
    assert not errors


def test_a_pick_that_could_not_exist_says_so_and_cannot_be_followed(ui):
    """A stay outside its range is a preference. Legs out of order are not.

    No sweep, no watch and no airline can price a leg that departs before the
    one before it has arrived, so this is the one pick the panel refuses — and
    it refuses it here rather than letting the server refuse it a click later.
    """
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    drag_marker(page, 1, 0.0)  # onto the first leg's own departure day

    readout = page.locator("#cursor-readout").inner_text()
    assert "cannot exist" in readout, readout
    assert page.locator("#cursor-watch").is_disabled()
    assert not errors


def test_snapping_lands_on_what_the_results_table_calls_cheapest(ui):
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    drag_marker(page, 1, 0.66)
    assert "fits every rule" not in page.locator("#cursor-readout").inner_text()

    page.locator("#cursor-snap").click()
    page.wait_for_timeout(900)
    readout = page.locator("#cursor-readout").inner_text()
    assert "fits every rule" in readout, readout
    assert "30,000" in readout.replace(" ", ","), readout
    assert not errors


def test_saving_a_narrowing_reports_what_it_costs_and_persists(ui):
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    page.fill("#narrow-back-start", "2027-01-28")
    page.fill("#narrow-back-end", "2027-02-02")
    page.fill("#narrow-nights-min", "18")
    page.fill("#narrow-nights-max", "22")
    page.locator("#narrow-save").click()
    page.wait_for_timeout(1200)

    assert "Saved" in page.locator("#narrow-message").inner_text()
    assert "home 01-28" in page.locator("#narrow-state").inner_text()
    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["return_focus_start"] == "2027-01-28"
    assert saved["total_days"] == [18, 22]

    cost = page.locator("#narrow-cost").inner_text()
    assert "against" in cost and "whole window" in cost, cost
    assert not errors


def test_an_impossible_narrowing_is_refused_in_words(ui):
    """The server names the stays that make it impossible; show that sentence."""
    page, scenarios, errors = ui
    page.reload(wait_until="networkidle")
    open_narrow(page)

    page.fill("#narrow-nights-min", "40")
    page.fill("#narrow-nights-max", "50")
    page.locator("#narrow-save").click()
    page.wait_for_timeout(900)

    message = page.locator("#narrow-message").inner_text()
    assert "unreachable" in message, message
    assert "9-11 at Japan" in message, message
    # Refused means not written, not written-and-warned.
    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["total_days"] is None
    assert not errors


def test_a_return_window_past_the_window_offers_to_widen_it(ui):
    """The refusal named the fix and not the box that performs it.

    The window is a field of the trip and the trip lives behind the gear, so
    "widen the window first" meant leaving this step, finding a date field in
    another one, and coming back to retype what was already typed here.
    """
    page, scenarios, errors = ui
    page.reload(wait_until="networkidle")
    open_narrow(page)

    page.fill("#narrow-back-start", "2027-02-04")
    page.fill("#narrow-back-end", "2027-02-12")
    page.wait_for_timeout(600)

    widen = page.locator("#narrow-widen")
    assert widen.is_visible()
    # Named, not "widen it": the new end is what the next sweep spends on.
    assert "2027-02-12" in widen.inner_text(), widen.inner_text()

    widen.click()
    page.wait_for_timeout(900)

    # One save, not two - a wider trip carrying no narrowing must never exist,
    # however briefly, because a sweep firing then would price the whole of it.
    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["window_end"] == "2027-02-12"
    assert saved["return_focus_end"] == "2027-02-12"
    assert saved["return_focus_start"] == "2027-02-04"

    assert "Saved" in page.locator("#narrow-message").inner_text()
    assert widen.is_hidden()
    assert not errors


def test_nothing_offers_to_widen_a_window_that_already_fits(ui):
    """A button that cannot change anything is one more thing to rule out."""
    page, scenarios, errors = ui
    page.reload(wait_until="networkidle")
    open_narrow(page)

    page.fill("#narrow-back-start", "2027-01-28")
    page.fill("#narrow-back-end", "2027-02-02")
    page.wait_for_timeout(600)

    assert page.locator("#narrow-widen").is_hidden()
    assert not errors


def stay_box(page, index, slot):
    return page.locator(f'#narrow-stays input[data-stay="{index}"][data-slot="{slot}"]')


def test_the_stays_are_editable_where_they_are_quoted(ui):
    """The ceiling on the nights band was a step away behind the gear.

    "the stays allow 18-22" is computed from the stop ranges, so a band typed
    past it does nothing and the panel could not say why - the number that
    refuses you was not a number you could reach from here.
    """
    page, scenarios, errors = ui
    page.reload(wait_until="networkidle")
    open_narrow(page)

    assert page.locator("#narrow-stays input[data-stay]").count() == 4
    assert "18–22" in page.locator("#narrow-nights-hint").inner_text()

    stay_box(page, 0, 1).fill("13")
    page.locator("#narrow-nights-hint").click()  # blur, to fire onchange
    page.wait_for_timeout(600)

    # Live off the boxes, not off the saved trip, or it keeps naming the range
    # you have just changed.
    assert "18–24" in page.locator("#narrow-nights-hint").inner_text()
    # Widening is a bigger sweep and nothing else here would say so.
    assert page.locator("#narrow-stays-alert").is_hidden()

    page.locator("#narrow-save").click()
    page.wait_for_timeout(900)

    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["stops"][0]["stay_days"] == [9, 13]
    assert not errors


def test_tightening_a_stay_says_what_it_costs(ui):
    """Widening costs searches, which the estimate reports. Tightening costs
    future prices - those nights stop being asked about - and nothing else on
    the page would mention it. This scenario's own notes record a 9-11 rule
    here throwing away a real 12-day Japan stay.

    It costs nothing already collected, and the sentence has to say so: an
    earlier version claimed the opposite, which reads as a threat to data the
    app cannot in fact touch.
    """
    page, scenarios, errors = ui
    page.reload(wait_until="networkidle")
    open_narrow(page)

    stay_box(page, 0, 0).fill("10")
    page.locator("#narrow-nights-hint").click()
    page.wait_for_timeout(600)

    alert = page.locator("#narrow-stays-alert")
    assert alert.is_visible()
    said = alert.inner_text()
    assert "Japan 9–11 → 10–11" in said, said
    assert "next sweep" in said, said
    # A warning, never a block.
    assert page.locator("#narrow-save").is_enabled()
    assert not errors


def test_saving_a_tighter_stay_moves_nothing_already_on_disk(ui):
    """The claim the warning used to make, checked rather than asserted.

    `_sweep_scenario` reads a run's shape - airports, stops, stays, window -
    off the snapshot that run wrote, so a range typed afterwards cannot filter
    what it found. Both seeded stays are 10 nights and this saves 11-11: were
    the live stays applied to the reading, the table would empty.
    """
    page, scenarios, errors = ui
    data = scenarios.parent / "data"
    seed_a_week_of_dates(data)
    seed_searched_trip(scenarios, "2026-08-19T02-00-00Z")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    before = page.locator("#results-table tbody tr").count()
    assert before > 0

    stay_box(page, 0, 0).fill("11")
    # Blur first, as a person tabbing away would. This is what fires the
    # tightening warning, and while that warning sat above the buttons it grew
    # the panel between a click being aimed at Save and the click landing - the
    # press missed, and the test read an unsaved file. It is below them now.
    page.locator("#narrow-nights-hint").click()
    page.wait_for_timeout(600)
    page.locator("#narrow-save").click()
    page.wait_for_timeout(1200)

    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["stops"][0]["stay_days"] == [11, 11]
    assert page.locator("#results-table tbody tr").count() == before
    assert not errors


def _run_under_other_stays(scenarios, low, high):
    """Seed a week of dates, snapshot the trip that priced them, then move the
    live stays - the state every one of these sweeps is actually read in."""
    data = scenarios.parent / "data"
    seed_a_week_of_dates(data)
    seed_searched_trip(scenarios, "2026-08-19T02-00-00Z")
    trip = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    trip["stops"][0]["stay_days"] = [low, high]
    (scenarios / "jp-ph.json").write_text(json.dumps(trip), encoding="utf-8")


def test_the_cursor_judges_a_pick_against_the_trip_you_want_now(ui):
    """Three of the four rules the readout checks come off the live trip; the
    stay ranges came off the run's snapshot. So a 10-night Japan stay on a
    sweep priced under 9-11 read as fitting every rule you set, weeks after
    the trip had moved to 12-14."""
    page, scenarios, errors = ui
    _run_under_other_stays(scenarios, 12, 14)
    page.reload(wait_until="networkidle")
    open_narrow(page)

    readout = page.locator("#cursor-readout").inner_text()
    # The seeded legs are 10 nights apart, which the run's own 9-11 allowed.
    assert "10 nights at Japan, not 12–14" in readout, readout
    assert not errors


def test_the_step_says_what_the_sweep_on_screen_was_priced_under(ui):
    """Otherwise a table of 10-night stays sits under a box reading 12 with
    nothing connecting them, and the honest answer - that the run was made
    under other stays and cannot show you anything else - is nowhere."""
    page, scenarios, errors = ui
    _run_under_other_stays(scenarios, 12, 14)
    page.reload(wait_until="networkidle")
    open_narrow(page)

    line = page.locator("#narrow-stays-sweep")
    assert line.is_visible()
    said = line.inner_text()
    assert "Japan 9–11" in said, said
    assert "next sweep" in said, said
    assert not errors


def test_nothing_is_said_when_the_sweep_used_the_stays_you_have(ui):
    page, scenarios, errors = ui
    data = scenarios.parent / "data"
    seed_a_week_of_dates(data)
    seed_searched_trip(scenarios, "2026-08-19T02-00-00Z")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    assert page.locator("#narrow-stays-sweep").is_hidden()
    assert not errors


def test_every_picker_says_when_a_run_was_of_another_trip(ui):
    """It used to be one picker out of three, and not the one that matters.

    The flag was passed in by each caller, and only the Explore picker passed
    it - so the Results and Narrow pickers, where a run is chosen to be
    believed, were exactly where a run of another trip looked ordinary. It is
    part of the label itself now, so a picker cannot forget it.
    """
    page, scenarios, errors = ui
    data = scenarios.parent / "data"
    seed_a_week_of_dates(data)
    # The run was of 9-11 nights in Japan; the trip since moved to 12-14.
    seed_searched_trip(scenarios, "2026-08-19T02-00-00Z")
    trip = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    trip["stops"][0]["stay_days"] = [12, 14]
    (scenarios / "jp-ph.json").write_text(json.dumps(trip), encoding="utf-8")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    for picker in ("#sweep-select", "#narrow-sweep"):
        label = page.locator(f'{picker} option[value="2026-08-19T02-00-00Z"]').inner_text()
        assert "different stays" in label, f"{picker}: {label}"

    # And the empty field the narrow picker used to render for a kind it never
    # passed.
    assert " ·  · " not in page.locator("#narrow-sweep option").first.inner_text()
    assert not errors


def test_a_run_that_differs_in_every_way_is_called_a_different_trip(ui):
    """Naming each part is only worth it while some of them still match.

    On real data most rows differ in all three - japan-philippines has fifteen
    such sweeps - and three flags apiece down a picker is a wall to skim past
    rather than a warning. So it degrades to the old wording exactly where the
    old wording was right.
    """
    page, scenarios, errors = ui
    data = scenarios.parent / "data"
    seed_a_week_of_dates(data)
    seed_searched_trip(scenarios, "2026-08-19T02-00-00Z")
    trip = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    trip["origins"] = ["BCN"]
    trip["return_to"] = None
    trip["stops"][0]["stay_days"] = [12, 14]
    trip["window_end"] = "2027-02-20"
    (scenarios / "jp-ph.json").write_text(json.dumps(trip), encoding="utf-8")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    label = page.locator('#sweep-select option[value="2026-08-19T02-00-00Z"]').inner_text()
    assert "a different trip" in label, label
    assert "different stays" not in label, label
    assert not errors


def test_a_trip_field_written_here_is_not_undone_by_the_setup_form(ui):
    """The setup step holds a draft of the trip, filled once on load.

    This panel now writes two of its fields - the window, when widening, and
    the stays - so a stale draft over there would put them back on its next
    Save, with nothing on screen to say a save had been reverted.
    """
    page, scenarios, errors = ui
    page.reload(wait_until="networkidle")
    open_narrow(page)

    page.fill("#narrow-back-start", "2027-02-04")
    page.fill("#narrow-back-end", "2027-02-12")
    page.wait_for_timeout(600)
    page.locator("#narrow-widen").click()
    page.wait_for_timeout(900)

    page.locator('#tabs button[data-tab="map"]').click()
    page.wait_for_timeout(400)
    assert page.locator("#window-end").input_value() == "2027-02-12"

    page.locator("#save-btn").click()
    page.wait_for_timeout(900)

    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["window_end"] == "2027-02-12"
    assert saved["return_focus_end"] == "2027-02-12"
    assert not errors


def test_following_a_rule_breaking_pick_still_works(ui):
    """A check prices the dates it is given; the stay ranges never governed it."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    drag_marker(page, 1, 0.66)

    page.locator("#cursor-watch").click()
    page.wait_for_timeout(900)
    message = page.locator("#cursor-message").inner_text()
    assert "Saved as a preference" in message, message
    assert "breaks a rule" in message, message

    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert len(saved["preferences"]) == 1
    # The flights it was picked on come along with it, one search each and each
    # tagged with the preference that brought it - which is what makes dropping
    # the preference able to drop them too.
    assert [w["source"] for w in saved["leg_watches"]] == ["2027-01-10"] * 3
    assert not errors


def test_expanding_a_leg_shows_one_line_per_airport_pair(ui):
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    # Leg 0 is PRG/VIE -> NRT: two routes, so the toggle is live.
    toggle = page.locator("#leg-charts .leg-chart").nth(0).locator("button.small").first
    assert "2 routes" in toggle.inner_text(), toggle.inner_text()
    toggle.click()
    page.wait_for_timeout(400)

    routes = page.locator("#leg-charts .leg-chart").nth(0).locator(".leg-chart__route")
    assert routes.count() == 2
    assert "PRG→NRT" in routes.nth(0).inner_text()
    assert not errors

def test_the_narrowing_can_be_switched_off_without_re_sweeping(ui):
    """Reading has to stay separable from searching on the page, not only in the API.

    The endpoint took `window=all` from the start and nothing on screen could
    send it, so the one way to see what a narrowing was hiding was to edit a
    URL. What it hides is exactly what you gave up by narrowing.
    """
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    # The seeded legs chain to any span the stays allow, so no nights band can
    # exclude them all. The return window can: they all fly home 30 January to
    # 3 February, and this asks for the week after — inside the trip window, so
    # it saves rather than being refused.
    page.fill("#narrow-back-start", "2027-02-04")
    page.fill("#narrow-back-end", "2027-02-08")
    page.locator("#narrow-save").click()
    page.wait_for_timeout(1400)
    assert "Saved" in page.locator("#narrow-message").inner_text(), (
        page.locator("#narrow-message").inner_text()
    )

    rows = page.locator("#results-table tbody tr")
    assert rows.count() == 0, page.locator("#results-table").inner_text()

    toggle = page.locator("#filter-window")
    assert toggle.is_visible(), "the way back has to be on screen"
    toggle.check()
    page.wait_for_timeout(1200)

    assert page.locator("#results-table tbody tr").count() > 0
    note = page.locator("#filter-note").inner_text()
    assert "home 2027-02-04 to 2027-02-08" in note, note
    assert "same legs read another way" in note, note
    assert not errors

def test_a_hand_picked_trip_survives_leaving_the_step(ui):
    """Several deliberate drags must not be thrown away by a tab click.

    The panel re-reads the sweep whenever the step is opened, and re-snapping
    the cursor on the way past would make it unusable beside any other one.
    """
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    drag_marker(page, 1, 0.66)
    picked = page.locator("#cursor-readout").inner_text()
    assert "nights at Japan" in picked, picked

    page.locator('#tabs button[data-tab="follow"]').click()
    page.wait_for_timeout(700)
    page.locator('#tabs button[data-tab="narrow"]').click()
    page.wait_for_timeout(1400)

    assert page.locator("#cursor-readout").inner_text() == picked
    assert not errors



# ------------------------------------------------ explanations on demand
#
# Every panel here explains itself, and the explanations record failures that
# each cost a day. They were also the first paragraph on every panel of a screen
# already understood, which pushed the numbers below the fold.
#
# The split these tests hold is between teaching and telling: prose about how a
# mechanic works collapses, and anything a renderer writes - a cost, a sampling
# resolution - does not, because it is an answer about the run in front of you.


def test_the_explanations_start_out_of_the_way(ui):
    page, _, errors = ui
    open_narrow(page)

    assert "off" in page.locator("#hints-toggle").inner_text()
    panel = page.locator('section[data-panel="narrow"] .panel').first
    assert "is-quiet" in (panel.get_attribute("class") or "")
    assert not panel.locator(".panel__hint:not(.panel__hint--live)").first.is_visible()
    assert not errors


def test_a_panel_can_be_asked_what_it_is_for(ui):
    page, _, errors = ui
    open_narrow(page)

    panel = page.locator('section[data-panel="narrow"] .panel').first
    panel.locator(".panel__info").click()
    page.wait_for_timeout(300)

    prose = panel.locator(".panel__hint:not(.panel__hint--live)").first
    assert prose.is_visible()
    assert "Three constraints, intersected" in prose.inner_text()
    assert panel.locator(".panel__info").get_attribute("aria-expanded") == "true"
    assert not errors


def test_one_panel_opened_survives_a_reload(ui):
    """The case the per-panel button exists for: one mechanic you have not
    learnt yet, on a screen you otherwise know."""
    page, _, errors = ui
    open_narrow(page)
    page.locator('section[data-panel="narrow"] .panel').first.locator(".panel__info").click()
    page.wait_for_timeout(300)

    page.reload(wait_until="networkidle")
    open_narrow(page)

    panel = page.locator('section[data-panel="narrow"] .panel').first
    assert panel.locator(".panel__hint:not(.panel__hint--live)").first.is_visible()
    # And only that one. The switch is still off for everything else.
    other = page.locator('section[data-panel="narrow"] .panel').nth(1)
    assert "is-quiet" in (other.get_attribute("class") or "")
    assert not errors


def test_a_panel_with_nothing_but_live_lines_gets_no_button(ui):
    """A control that does nothing is worse than no control. The Sweep panel's
    only hint is the filter note, which a renderer writes."""
    page, _, errors = ui
    open_narrow(page)

    panel = page.locator('section[data-panel="results"] .panel').first
    assert panel.locator(".panel__info").count() == 0
    assert "is-quiet" not in (panel.get_attribute("class") or "")
    assert not errors


def test_the_switch_overrules_the_panels_that_disagreed_with_it(ui):
    """A per-panel choice was an answer to the old default and means the
    opposite under the new one, so flipping the switch wins outright."""
    page, _, errors = ui
    open_narrow(page)
    panel = page.locator('section[data-panel="narrow"] .panel').first
    panel.locator(".panel__info").click()      # opened against an "off" default
    page.wait_for_timeout(300)

    page.locator("#hints-toggle").click()      # everything on
    page.wait_for_timeout(300)
    assert "on" in page.locator("#hints-toggle").inner_text()
    assert panel.locator(".panel__hint:not(.panel__hint--live)").first.is_visible()

    page.locator("#hints-toggle").click()      # and everything off again
    page.wait_for_timeout(300)
    assert not panel.locator(".panel__hint:not(.panel__hint--live)").first.is_visible()
    assert not errors


def test_a_live_line_is_never_taken_away_with_the_prose(ui):
    """`#narrow-cost` says what the next sweep costs and moves as the boxes are
    typed. Collapsing it would hide the one number the panel is deciding by."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    cost = page.locator("#narrow-cost")
    assert cost.is_visible()
    assert "searches" in cost.inner_text()
    assert not errors


# ------------------------------------------- the panel you decide from
#
# Every flight priced on its own is where a trip is actually chosen, and it was
# the one panel with no way to buy anything and no way back to the ranking under
# it. The links were already in the payload it fetches.


def test_each_leg_of_a_pick_can_be_booked_and_followed(ui):
    page, scenarios, errors = ui
    seed_for_filters(scenarios, url="https://example.test/search/PRG-NRT")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    rows = page.locator("#cursor-readout .cursor-readout__leg")
    assert rows.count() >= 2, rows.count()
    # A link is a real one, built as a node with .href rather than interpolated.
    link = page.locator("#cursor-readout a").first
    assert link.get_attribute("href").startswith("https://example.test/")
    assert link.get_attribute("rel") == "noopener"
    assert page.locator("#cursor-readout button", has_text="Follow").count() >= 2
    assert not errors


def test_a_leg_the_sweep_saved_no_link_for_says_so(ui):
    """An absent link between two present ones reads as a page that failed
    rather than as a sweep that recorded a price without one."""
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_narrow(page)

    assert page.locator("#cursor-readout a").count() == 0
    assert "no link saved" in page.locator("#cursor-readout").inner_text()
    assert not errors


def test_following_a_leg_from_the_charts_reaches_the_watch(ui):
    """The decision is made here, so the question "is *that* flight moving"
    arrives with the leg in front of you rather than later from a form."""
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_narrow(page)

    page.locator("#cursor-readout button", has_text="Follow").first.click()
    page.wait_for_timeout(1200)

    open_watch(page)
    assert page.locator("#leg-watch-table tbody tr").count() == 1
    assert not errors


def test_a_row_of_the_ranking_can_be_put_on_the_charts(ui):
    """The ranking used to be a dead end: you could read that a trip existed and
    had no way to see it against the curves."""
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_narrow(page)

    row = page.locator("#results-table tbody tr[data-dates]").first
    dates = row.get_attribute("data-dates").split(",")
    row.locator("button", has_text="Put on the charts").click()
    page.wait_for_timeout(700)

    readout = page.locator("#cursor-readout").inner_text()
    for when in dates:
        assert when in readout, (when, readout)
    assert not errors


def test_a_pick_is_findable_in_the_ranking_below_it(ui):
    page, scenarios, errors = ui
    seed_for_filters(scenarios)
    page.reload(wait_until="networkidle")
    open_narrow(page)

    page.locator("#cursor-snap").click()
    page.wait_for_timeout(900)
    page.locator("#cursor-in-table").click()
    page.wait_for_timeout(700)

    marked = page.locator("#results-table tbody tr.is-cursor")
    assert marked.count() == 1
    assert not errors


def test_a_combination_the_ranking_never_offered_says_so_rather_than_nothing(ui):
    """Not a failure: the markers move freely and very often land on a trip the
    narrowed, capped ranking does not contain."""
    page, scenarios, errors = ui
    seed_a_week_of_dates(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_narrow(page)

    # Past the stay range, so the ranking cannot contain it: `combine` only ever
    # chains trips that obeyed the stays.
    drag_marker(page, 1, 0.66)
    assert "nights at Japan" in page.locator("#cursor-readout").inner_text()
    page.locator("#cursor-in-table").click()
    page.wait_for_timeout(700)

    message = page.locator("#cursor-message").inner_text()
    assert "not in the table below" in message, message
    assert page.locator("#results-table tbody tr.is-cursor").count() == 0
    assert not errors


# ----------------------------------------------------------- final sweeps
#
# The step that prices the narrowing rather than the window. It exists because
# the two used to be one thing: saving a narrowing narrowed every sweep,
# including the nightly one, and nothing on the page said so. These tests are
# about the separation holding in the browser - which runs each picker offers,
# and which plan each button starts.


def open_final(page):
    """The narrowed population of the narrowing step.

    It was a step of its own until the two were merged: it held a copy of this
    step's panels drawn over the narrowed runs, which is one step read two ways.
    One helper still, so every test below says "show me the narrowed runs"
    rather than knowing how the switch is built.
    """
    page.locator('#tabs button[data-tab="narrow"]').click()
    page.wait_for_timeout(300)
    page.locator('#population-switch button[data-population="narrow"]').click()
    page.wait_for_timeout(900)


def open_broad(page):
    page.locator('#tabs button[data-tab="narrow"]').click()
    page.wait_for_timeout(300)
    page.locator('#population-switch button[data-population="broad"]').click()
    page.wait_for_timeout(900)


def narrow_the_trip(scenarios, **overrides):
    """Save a narrowing straight onto the trip file, as the panel's Save does."""
    path = scenarios / "jp-ph.json"
    trip = json.loads(path.read_text(encoding="utf-8")) | {
        "focus_start": "2027-01-08", "focus_end": "2027-01-12",
    } | overrides
    path.write_text(json.dumps(trip), encoding="utf-8")


def final_status(**overrides):
    return {
        "state": "done", "mode": "final", "total": 5, "completed": 5,
        "legs_found": 5, "answered": 5, "planned": 5, "coverage": 1.0,
        "legs_per_search": 9.0, "depth": "deep",
        "narrowing": {"focus": ["2027-01-08", "2027-01-12"],
                      "return_focus": None, "total_days": None},
    } | overrides


def broad_status(**overrides):
    return {
        "state": "done", "mode": "sweep", "total": 5, "completed": 5,
        "legs_found": 5, "answered": 5, "planned": 5, "coverage": 1.0,
        "legs_per_search": 9.0, "depth": "deep",
        "narrowing": {"focus": None, "return_focus": None, "total_days": None},
    } | overrides


def test_the_steps_read_in_the_order_the_work_is_done(ui):
    """Order is the argument. The tabs are the order the work is done in.

    Three, not four. "Final sweeps" was a copy of the narrowing step's two
    panels drawn over the narrowed runs, which is one step read two ways - it
    is a switch inside that step now.
    """
    page, _, errors = ui
    labels = page.locator("#tabs button[data-tab]").all_inner_texts()

    assert [t.strip() for t in labels][:3] == [
        "Map it out", "Narrow it down", "Follow it",
    ]
    assert "Final sweeps" not in " ".join(labels)
    assert not errors


def test_a_trip_narrowed_to_nothing_says_so_instead_of_offering_a_run(ui):
    """A narrow sweep of it would be the broad sweep, run twice.

    The controls sit with the boxes that decide what they would search, so the
    notice is the cost line beside the tick rather than a paragraph on a step
    of its own. The server refuses it too - these are the notice, not the guard.
    """
    page, _, errors = ui
    open_broad(page)

    said = page.locator("#narrow-sweep-cost").inner_text()
    assert "nothing narrowed yet" in said, said
    assert page.locator("#final-run-btn").is_disabled()
    assert page.locator("#final-run-cloud-btn").is_disabled()
    assert page.locator("#sweep-narrowing").is_disabled()
    assert not errors


def test_the_panel_says_what_a_narrow_sweep_would_search_and_what_it_costs(ui):
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    page.reload(wait_until="networkidle")
    open_broad(page)

    # What it would search is the boxes themselves, which is the whole reason
    # the run buttons moved next to them: two controls for one field was the
    # mistake the by-date chart was folded into this panel to end.
    assert page.locator("#narrow-out-start").input_value() == "2027-01-08"
    assert page.locator("#narrow-out-end").input_value() == "2027-01-12"
    assert not page.locator("#final-run-btn").is_disabled()

    # Beside the tick, because ticking it is what spends the figure twice a day.
    cost = page.locator("#narrow-sweep-cost").inner_text()
    assert "searches" in cost and "min" in cost, cost
    assert not errors


def test_the_tick_decides_whether_the_afternoon_slots_run(ui):
    """Narrowing to read and narrowing to re-price are different asks.

    They used to be one: `plan_sweep --final` selected on having a narrowing at
    all, so filtering the charts to a rough plan silently bought two more runs
    a day.
    """
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    page.reload(wait_until="networkidle")
    open_broad(page)

    page.locator("#sweep-narrowing").uncheck()
    page.locator("#narrow-save").click()
    page.wait_for_timeout(900)

    saved = json.loads((scenarios / "jp-ph.json").read_text(encoding="utf-8"))
    assert saved["sweep_narrowing"] is False
    # And it still narrows what you read, which is the half that is kept.
    assert saved["focus_start"] == "2027-01-08"
    assert "costs no searches" in page.locator("#narrow-message").inner_text()
    assert not errors

def test_running_from_the_narrowing_panel_sweeps_the_narrowing_not_the_window(ui, monkeypatch):
    """The whole feature, as one keystroke: the button starts `mode=final`.

    On the panel holding the boxes, because that is what it would search.
    """
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    page.reload(wait_until="networkidle")
    seen, started = catch_the_sweep(monkeypatch)
    open_broad(page)

    page.locator("#final-run-btn").click()

    assert started.wait(timeout=15), "the final sweep never started"
    assert seen["mode"] == "final", seen
    assert not errors


def test_the_narrowing_step_offers_only_the_broad_runs(ui):
    """It is where a trip is picked, and picking needs the whole window drawn."""
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    seed_sweep(scenarios.parent / "data", "2026-08-20T02-00-00Z", status=broad_status())
    seed_sweep(scenarios.parent / "data", "2026-08-21T13-00-00Z", status=final_status())
    page.reload(wait_until="networkidle")
    open_narrow(page)

    for picker in ("#narrow-sweep", "#sweep-select"):
        offered = page.locator(f"{picker} option").all_inner_texts()
        assert len(offered) == 1, f"{picker} offered {offered}"
        assert "final" not in offered[0], f"{picker} offered {offered}"
    assert not errors


def test_the_final_step_offers_only_the_narrowed_runs(ui):
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    seed_sweep(scenarios.parent / "data", "2026-08-20T02-00-00Z", status=broad_status())
    seed_sweep(scenarios.parent / "data", "2026-08-21T13-00-00Z", status=final_status())
    page.reload(wait_until="networkidle")
    open_final(page)

    offered = page.locator("#final-sweep-select option").all_inner_texts()
    assert len(offered) == 1, offered
    assert "final" in offered[0], offered
    assert not errors


def test_the_final_step_ranks_the_trips_the_narrowed_run_found(ui):
    """The question the broad table cannot answer: among the trips that fit
    what you decided, is a different one cheaper today?"""
    page, scenarios, errors = ui
    narrow_the_trip(scenarios, focus_start="2027-01-08", focus_end="2027-01-14")
    seed_sweep(scenarios.parent / "data", "2026-08-21T13-00-00Z", status=final_status())
    page.reload(wait_until="networkidle")
    open_final(page)

    rows = page.locator("#final-results-table tbody tr[data-dates]")
    assert rows.count() >= 1, page.locator("#final-results-empty").inner_text()
    assert not errors


def test_a_row_of_the_final_ranking_goes_onto_the_final_charts(ui):
    """It used to be refused, on the grounds that the only charts in the app
    drew a broad run and a narrowed trip would land on another run's prices.
    That was true while this step had no charts of its own. It has."""
    page, scenarios, errors = ui
    narrow_the_trip(scenarios, focus_start="2027-01-08", focus_end="2027-01-14")
    seed_a_narrowed_week(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_final(page)

    row = page.locator("#final-results-table tbody tr[data-dates]").first
    dates = row.get_attribute("data-dates").split(",")
    row.locator("button", has_text="Put on the charts").click()
    page.wait_for_timeout(700)

    readout = page.locator("#final-cursor-readout").inner_text()
    for when in dates:
        assert when in readout, (when, readout)
    assert not errors


def test_the_follow_step_offers_both_kinds_of_run_and_names_which(ui):
    """The one picker that mixes them: by the time you are pinning days, the
    freshest reading of those days is usually the narrowed run."""
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    seed_sweep(scenarios.parent / "data", "2026-08-20T02-00-00Z", status=broad_status())
    seed_sweep(scenarios.parent / "data", "2026-08-21T13-00-00Z", status=final_status())
    page.reload(wait_until="networkidle")
    open_watch(page)
    page.locator("#watch-from-sweep summary").click()
    page.wait_for_timeout(400)

    # textContent, not inner_text: this picker lives inside a `<details>`, and a
    # collapsed one renders its options as empty strings rather than as nothing.
    offered = page.locator("#watch-sweep-select").evaluate(
        "el => [...el.options].map((o) => o.textContent)"
    )
    assert len(offered) == 2, offered
    assert sum("final" in row for row in offered) == 1, offered
    # And it lands on the narrowed one, which priced these days most recently.
    chosen = page.locator("#watch-sweep-select").evaluate(
        "el => el.options[el.selectedIndex].textContent"
    )
    assert "final" in chosen, chosen
    assert not errors


# ------------------------------------------- the narrowed run, priced per leg
#
# The charts, the cursor and the three buttons under them used to exist only on
# "Narrow it down", reading only broad runs. That was right while the question
# was *which week*, and wrong for the question this step asks: which exact
# flights, at today's price. The final sweeps run twice a day against the broad
# sweep's once a night, so the freshest per-leg prices in the app were the ones
# with no chart.


def seed_a_narrowed_week(data_dir, stamp="2026-08-21T13-00-00Z", **status):
    """A final run over five departures, the shape a real one has.

    Measured on the live trip: leg 0 got 5 dates, leg 1 got 7, leg 2 got 9, and
    they do not overlap - three tight clusters on one 21-column axis rather than
    the broad run's ~33 columns of everything against everything.
    """
    legs = []
    for offset in range(5):
        out = date(2027, 1, 8) + timedelta(days=offset)
        legs += [
            ("PRG", "NRT", out.isoformat(), 12000.0 + offset * 100),
            ("NRT", "MNL", (out + timedelta(days=10)).isoformat(), 4000.0),
            ("MNL", "PRG", (out + timedelta(days=20)).isoformat(), 14000.0),
        ]
    seed_sweep(data_dir, stamp, status=final_status(legs_found=len(legs), **status),
               legs=legs)


def open_final_charts(page):
    open_final(page)
    page.locator("#final-leg-charts").scroll_into_view_if_needed()
    page.wait_for_timeout(300)


def test_the_final_step_draws_the_narrowed_run_one_chart_per_leg(ui):
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    seed_a_narrowed_week(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_final_charts(page)

    charts = page.locator("#final-leg-charts .leg-chart")
    assert charts.count() == 3, page.locator("#final-leg-charts").inner_text()
    # One shared axis, the same rule the broad charts follow: three charts
    # scaled to their own dates would put three different days in one slice.
    widths = [charts.nth(i).locator("svg").first.get_attribute("width") for i in range(3)]
    assert len(set(widths)) == 1, widths
    assert not errors


def test_the_switch_shows_one_population_at_a_time(ui):
    """One step, two readings of it, and never both at once.

    Both pairs live under `data-step="narrow"` now; the switch is what decides
    which is on screen. Showing both would put a narrowed run's cheapest
    directly under the broad run's, which is the confusion that made these two
    separate steps in the first place.
    """
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    page.reload(wait_until="networkidle")

    open_broad(page)
    assert page.locator('section[data-panel="narrow"]').is_visible()
    assert not page.locator('section[data-panel="final-results"]').is_visible()

    open_final(page)
    assert page.locator('section[data-panel="final-results"]').is_visible()
    assert not page.locator('section[data-panel="narrow"]').is_visible()
    assert not errors


def test_flipping_the_switch_redraws_rather_than_unhiding(ui):
    """A chart drawn into a hidden section measures its container at zero width.

    So the newly visible pair is re-rendered, not merely unhidden - otherwise
    the narrowed charts arrive as axes with nothing on them.
    """
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    seed_a_narrowed_week(scenarios.parent / "data")
    page.reload(wait_until="networkidle")

    open_broad(page)
    open_final(page)

    width = page.locator("#final-leg-charts svg").first.get_attribute("width")
    assert int(width) > 200, width
    assert not errors


def test_each_population_keeps_the_run_it_was_reading(ui):
    """The reason this is a switch and not one merged picker.

    Flipping across to the narrowed runs and back must not lose the broad run
    you had chosen - which is what one shared `state.stamp` would have done.
    """
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    seed_sweep(scenarios.parent / "data", "2026-08-19T02-00-00Z", status=broad_status())
    seed_sweep(scenarios.parent / "data", "2026-08-20T02-00-00Z", status=broad_status())
    seed_a_narrowed_week(scenarios.parent / "data")
    page.reload(wait_until="networkidle")

    open_broad(page)
    chosen = page.locator("#narrow-sweep").evaluate("el => el.options[1].value")
    page.locator("#narrow-sweep").select_option(chosen)
    page.wait_for_timeout(900)

    open_final(page)
    open_broad(page)
    assert page.locator("#narrow-sweep").input_value() == chosen
    assert not errors


def test_the_narrowing_step_reads_plan_then_pick_then_rank(ui):
    """The same rhythm on either side of the switch. Order is the argument.

    The plan is the narrowing panel and is on the broad side, because a
    narrowing is decided against the whole window; each population then picks
    and ranks over its own runs.
    """
    page, _, errors = ui
    panels = page.locator('section[data-step="narrow"]').evaluate_all(
        "els => els.map((e) => [e.dataset.population, e.dataset.panel])"
    )
    assert panels == [
        ["broad", "narrow"], ["broad", "results"],
        ["narrow", "final-charts"], ["narrow", "final-results"],
    ], panels
    assert not errors


def test_the_final_charts_offer_only_the_narrowed_runs(ui):
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    seed_sweep(scenarios.parent / "data", "2026-08-20T02-00-00Z", status=broad_status())
    seed_a_narrowed_week(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_final_charts(page)

    offered = page.locator("#final-sweep-charts").evaluate(
        "el => [...el.options].map((o) => o.textContent)"
    )
    assert len(offered) == 1, offered
    assert "final" in offered[0], offered
    # And the broad step is still not offered the narrowed one.
    open_narrow(page)
    broad = page.locator("#narrow-sweep").evaluate(
        "el => [...el.options].map((o) => o.textContent)"
    )
    assert all("final" not in row for row in broad), broad
    assert not errors


def test_the_two_steps_keep_their_own_pick(ui):
    """Two populations, two cursors. A drag on the narrowed charts moving the
    broad ones would be the toggle this deliberately is not."""
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    seed_a_week_of_dates(scenarios.parent / "data")
    seed_a_narrowed_week(scenarios.parent / "data")
    page.reload(wait_until="networkidle")

    open_narrow(page)
    before = page.locator("#cursor-readout").inner_text()

    open_final_charts(page)
    drag_marker(page, 0, 0.9, host="#final-leg-charts")

    open_narrow(page)
    assert page.locator("#cursor-readout").inner_text() == before
    assert not errors


def test_following_a_trip_from_the_final_charts_reaches_the_watch(ui):
    """The whole reason these charts belong on this step: the booking decision
    is made against the freshest prices, and this is where they are."""
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    seed_a_narrowed_week(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_final_charts(page)

    page.locator("#final-cursor-watch").click()
    page.wait_for_timeout(1200)

    open_watch(page)
    assert page.locator("#watch-table tbody tr").count() == 1
    assert not errors


def test_a_pick_on_the_final_charts_is_found_in_the_final_ranking(ui):
    """And in the final one - pointing at the broad table from this step would
    send you hunting through a run these prices did not come from."""
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    seed_a_week_of_dates(scenarios.parent / "data")
    seed_a_narrowed_week(scenarios.parent / "data")
    page.reload(wait_until="networkidle")
    open_final_charts(page)

    page.locator("#final-cursor-snap").click()
    page.wait_for_timeout(900)
    page.locator("#final-cursor-in-table").click()
    page.wait_for_timeout(700)

    assert page.locator("#final-results-table tbody tr.is-cursor").count() == 1
    assert page.locator("#results-table tbody tr.is-cursor").count() == 0
    assert not errors


def test_the_final_step_says_when_cloud_runs_are_waiting(ui):
    """It is the step the cloud files two runs a day into, and it was the one
    step that could not say any had arrived."""
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    page.route("**/api/cloud-sync", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({
            "known": True, "missing_count": 1, "can_fast_forward": True,
            "blocked_by": "", "missing": {"jp-ph": ["2026-08-21T13-00-00Z"]},
        }),
    ))
    page.reload(wait_until="networkidle")
    open_final(page)

    notice = page.locator("#final-cloud-sync")
    assert notice.is_visible(), notice.inner_text()
    assert "not on this machine" in notice.inner_text()
    assert page.locator("#final-cloud-sync-get").is_visible()
    assert not errors


# ---------------------------------------------------------- one line, one row
#
# `.row` centred its children, and a `label.field` is a two-line column - a 12px
# label stacked over its control. Centring a ~49px column against ~35px buttons
# put their bottom edges seven pixels apart, in every run row and every filter
# row on every tab. Measured rather than eyeballed, so it cannot drift back.


def bottom_of(page, selector: str) -> float:
    box = page.locator(selector).bounding_box()
    return box["y"] + box["height"]


@pytest.mark.parametrize(
    ("field", "button"),
    [("#depth", "#run-local-btn"), ("#final-depth", "#final-run-btn")],
)
def test_a_control_and_the_button_beside_it_sit_on_one_line(ui, field, button):
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    page.reload(wait_until="networkidle")
    # The narrow sweep's depth and run buttons sit with the boxes that decide
    # what they would search, which is the *broad* panel - they are a decision
    # about the narrowing, not a reading of the narrowed runs.
    if field.startswith("#final"):
        open_broad(page)

    assert abs(bottom_of(page, field) - bottom_of(page, button)) <= 1.0, (
        f"{field} and {button} do not share a bottom edge"
    )
    assert not errors


def test_the_same_control_is_the_same_size_on_every_step(ui):
    """The two selects already agree, because `label.field` sets the size and
    the extra `.small` on one of them changes nothing. Pinned so that stripping
    the redundant class - or adding a size to one panel - cannot quietly make
    the same control two different sizes on two panels."""
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    page.reload(wait_until="networkidle")
    open_broad(page)

    sizes = {
        which: page.locator(which).evaluate("el => getComputedStyle(el).fontSize")
        for which in ("#depth", "#final-depth")
    }
    assert len(set(sizes.values())) == 1, sizes
    assert not errors


def test_the_trend_says_it_is_counting_rather_than_showing_nothing(ui):
    """`/api/history` re-combines every sweep on disk - measured at 7.0s for a
    trip with twelve of them. For all seven seconds the panel was a heading over
    nothing, which is what a trip with no sweeps looks like."""
    page, scenarios, errors = ui
    for day in range(20, 24):
        seed_sweep(scenarios.parent / "data", f"2026-08-{day}T02-00-00Z", status=broad_status())
    page.reload(wait_until="networkidle")

    page.locator('#tabs button[data-tab="follow"]').click()
    # Read immediately, before the request can have come back.
    said = page.locator("#chart-history").inner_text()
    assert "Reading every sweep" in said, said

    # And it is replaced by the chart, not left there.
    page.wait_for_timeout(4000)
    assert page.locator("#chart-history svg").count() == 1
    assert not errors


# ------------------------------------------------------ your airports, ranked
#
# How easy an airport is to get to is a third axis, and nothing knew it. The
# chip row was sorted by how often a code appears in your saved trips, which
# cannot discover convenience - the usual reason a convenient airport goes
# unused is that it has no long-haul inventory.


def open_home_airports(page):
    page.locator('#tabs button[data-tab="setup"]').click()
    page.wait_for_timeout(600)
    page.locator('section[data-panel="home-airports"]').scroll_into_view_if_needed()
    page.wait_for_timeout(300)


def add_home_airport(page, code):
    box = page.locator('section[data-panel="home-airports"] .typeahead input')
    box.click()
    # Typed at human speed and submitted immediately, because a scripted click
    # on a suggestion has passed here against a picker that was entirely broken.
    box.type(code, delay=60)
    page.wait_for_timeout(400)
    box.press("Enter")
    page.wait_for_timeout(300)


def test_a_ranking_becomes_the_chips_in_the_order_it_was_given(ui):
    page, scenarios, errors = ui
    open_home_airports(page)
    # BCN and KIX: the two airports in the fixture catalogue that the fixture
    # trip does not already use, so the chip row has something to offer.
    for code in ("BCN", "KIX"):
        add_home_airport(page, code)
    page.locator("#home-airports-save").click()
    page.wait_for_timeout(900)

    assert (scenarios.parent / "data" / "home_airports.json").exists()
    assert "Saved" in page.locator("#home-airports-message").inner_text()

    page.locator('#tabs button[data-tab="map"]').click()
    page.wait_for_timeout(600)
    offered = page.locator("#origins .chips--suggest .chip__code").all_inner_texts()
    # PRG and VIE are already in the trip, so they are not offered again - the
    # row has always listed what is *not* on the trip yet.
    assert [t.strip() for t in offered] == ["+ BCN", "+ KIX"], offered
    assert not errors


def test_a_ranking_reorders_without_retyping(ui):
    """Position is the whole content of the list, so it has to be editable."""
    page, _, errors = ui
    open_home_airports(page)
    for code in ("BCN", "KIX"):
        add_home_airport(page, code)

    assert page.locator("#home-airports-state").inner_text() == "BCN → KIX"
    page.locator("#home-airports .chip").nth(1).locator(".chip__move").first.click()
    page.wait_for_timeout(300)
    assert page.locator("#home-airports-state").inner_text() == "KIX → BCN"
    assert not errors



# --------------------------------------------------------- switching trips
#
# The picker had never been switched in a browser test: the fixture saves one
# trip, and the assertion above about the list is the whole of the coverage it
# had. So nothing noticed that `loadScenario` fills the route form and stops
# there - every step renderer runs from `showTab` alone, and the step you are
# already standing on is never told the trip underneath it changed.


def save_second_trip(scenarios, **overrides):
    """A second trip, deliberately unlike the fixture's in every visible way.

    `zz-` so it sorts after `jp-ph` and the page still opens on the trip every
    other test in this file expects. One stop rather than two, so even the
    number of date boxes under the preference form differs.
    """
    save_scenario(
        Scenario(
            id="zz-other",
            name="Barcelona to Osaka",
            origins=["BCN"],
            stops=[Stop(airports=["KIX"], stay_days=(4, 6), label="Japan")],
            window_start=date(2027, 3, 1),
            window_end=date(2027, 3, 28),
            focus_start=date(2027, 3, 2),
            focus_end=date(2027, 3, 6),
            total_days=(4, 6),
            depth="quick",
            **overrides,
        ),
        scenarios,
    )


def switch_trip(page, trip_id):
    page.locator("#scenario-select").select_option(trip_id)
    page.wait_for_timeout(1200)


def test_switching_trips_repaints_the_narrowing_step_you_are_standing_on(ui):
    """The complaint, exactly: "the switcher doesn't seem to load my trip".

    It loaded. `loadScenario` refilled the route form and `pollStatus` refilled
    the run pickers, and nothing redrew the step in front of you - so the boxes,
    the badge and the charts went on describing the trip you had just left. On a
    step you are not looking at this is invisible; on the one you are, it reads
    as the switch having done nothing at all.
    """
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    save_second_trip(scenarios)
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")

    open_narrow(page)
    assert page.locator("#narrow-out-start").input_value() == "2027-01-08"

    switch_trip(page, "zz-other")

    assert page.locator("#narrow-out-start").input_value() == "2027-03-02"
    assert page.locator("#narrow-out-end").input_value() == "2027-03-06"
    assert "03-02" in page.locator("#narrow-state").inner_text()
    assert not errors


def test_switching_trips_repaints_what_you_are_following(ui):
    """The same bug on the other step, where it costs more.

    A preference list left over from the previous trip is not merely stale: the
    prices in it are a different trip's, and the row offers to move dates that
    do not belong to the trip named in the picker.
    """
    page, scenarios, errors = ui
    save_second_trip(scenarios)
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")

    open_watch(page)
    # Two stops, so three legs, so three date boxes on the by-hand form.
    assert page.locator("#preference-add-dates input").count() == 3

    switch_trip(page, "zz-other")

    assert page.locator("#preference-add-dates input").count() == 2
    assert not errors


def test_a_server_that_stopped_answering_is_named_not_silent(ui):
    """A dead server used to make the picker look broken instead of absent.

    `api()` let `fetch`'s own "Failed to fetch" through, and the switch handler
    had no `catch`, so the rejection went nowhere: no message, no status strip,
    nothing. That is the same shape as the stale-server trap this app already
    carries a blocker for, and it deserves the same sentence rather than a
    silence to be diagnosed from scratch.
    """
    page, scenarios, _ = ui
    save_second_trip(scenarios)
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")

    page.route("**/api/**", lambda route: route.abort())
    switch_trip(page, "zz-other")

    assert "not answering" in page.locator("body").inner_text()


def test_the_switch_says_which_runs_each_side_shows(ui):
    """`#population-note` shipped in the markup and was never written to.

    The switch is the only thing that says which population you are reading, and
    two labels alone do not say that "the whole window" means the nightly run
    and "just what I chose" means a different, smaller, separately scheduled one.
    """
    page, scenarios, errors = ui
    narrow_the_trip(scenarios, sweep_narrowing=True)
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")

    open_broad(page)
    broad = page.locator("#population-note").inner_text()
    assert "02:00" in broad

    open_final(page)
    narrow = page.locator("#population-note").inner_text()
    assert "13:00" in narrow and "20:00" in narrow
    assert narrow != broad
    assert not errors


def test_starting_a_new_trip_disables_the_narrow_sweep_buttons_where_you_stand(ui):
    """The three buttons on "Map it out" were in `updateNewTripUi` and these
    two, which moved onto the narrowing panel from a step of their own, were not.

    Pressed from the step they live on, which is the only place it shows: the
    narrowing is what usually disables them, and leaving and re-entering the step
    disables them on the way in for that reason instead. Stay put, and they were
    left enabled over a trip that has no file to sweep.
    """
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")
    open_broad(page)
    assert page.locator("#final-run-btn").is_enabled()

    # No navigation afterwards: the bug is what the step is left showing.
    page.locator("#new-trip-btn").click()
    page.wait_for_timeout(800)

    assert page.locator("#final-run-btn").is_disabled()
    assert page.locator("#final-run-cloud-btn").is_disabled()
    assert not errors


def test_switching_back_draws_the_charts_and_not_an_empty_state(ui):
    """The same bug one layer down, and the reason the fix is ordered.

    Every panel on this step draws from `state.sweeps`, and `pollStatus` is what
    refreshes that. Redrawing the step before it ran painted the new trip's boxes
    and badge correctly over the *previous* trip's list of runs, found nothing in
    it this trip could draw, and left "no broad sweep with flights on disk yet"
    above a trip with thirteen of them. Right boxes, empty charts - which is a
    more convincing way of looking broken than not repainting at all.
    """
    page, scenarios, errors = ui
    seed_sweep(scenarios.parent / "data", "2026-08-11T09-00-00Z", status=broad_status())
    save_second_trip(scenarios)
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")

    open_narrow(page)
    assert page.locator("#leg-charts .leg-chart").count() == 3

    switch_trip(page, "zz-other")
    assert page.locator("#leg-charts .leg-chart").count() == 0

    switch_trip(page, "jp-ph")
    assert page.locator("#leg-charts .leg-chart").count() == 3, (
        page.locator("#leg-charts").inner_text()
    )
    assert not errors


def test_the_page_opens_on_a_trip_that_is_being_swept(ui):
    """`scenarios[0]` is alphabetical by id, which is nobody's answer.

    On the real machine that opened a switched-off trip with no sweeps on disk,
    named two words differently from the trip actually being decided. Its empty
    charts are indistinguishable from a trip whose data has gone.
    """
    page, scenarios, errors = ui
    save_second_trip(scenarios, enabled=True)
    save_scenario(
        Scenario(
            id="aa-abandoned",
            name="Abandoned idea",
            origins=["PRG"],
            stops=[Stop(airports=["NRT"], stay_days=(9, 11))],
            window_start=date(2027, 1, 5),
            window_end=date(2027, 2, 8),
            enabled=False,
        ),
        scenarios,
    )
    page.evaluate("() => localStorage.removeItem('tickets-bot:last-trip')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")

    assert page.locator("#scenario-select").input_value() == "jp-ph"
    assert not errors


def test_the_trip_you_were_last_on_is_the_one_that_reopens(ui):
    """A reload in the middle of deciding should not move you to another trip."""
    page, scenarios, errors = ui
    save_second_trip(scenarios, enabled=True)
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")

    switch_trip(page, "zz-other")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")

    assert page.locator("#scenario-select").input_value() == "zz-other"
    assert not errors


def test_the_step_says_what_the_dates_do_with_explanations_collapsed(ui):
    """Explanations are off by default, and that is how this step lost its words.

    Everything the panel said about itself was `.panel__hint`, which collapses -
    so the default view was a pile of date boxes, a checkbox and four buttons
    with no sentence anywhere saying whether the boxes filter, or sweep, or both.
    The questions that answers are the ones the step is unusable without.
    """
    page, scenarios, errors = ui
    narrow_the_trip(scenarios)
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")
    open_broad(page)

    # The default, stated rather than assumed: this is the view being tested.
    assert "Explanations: off" in page.locator("#hints-toggle").inner_text()

    role = page.locator("#narrow-role")
    assert role.is_visible()
    text = role.inner_text()
    assert "filter" in text
    assert "13:00" in text and "20:00" in text
    assert "02:00" in text
    assert not errors


def test_switching_the_narrow_sweep_off_changes_what_the_step_says(ui):
    """Off is not a quieter version of on; it means the dates only filter."""
    page, scenarios, errors = ui
    narrow_the_trip(scenarios, sweep_narrowing=False)
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#origins .chip__code")
    open_broad(page)

    text = page.locator("#narrow-role").inner_text()
    assert "switched off" in text
    assert "02:00" in text, "the broad sweep runs whatever this box says"
    assert not errors

