"""API tests. No browser, no network: sweeps are seeded on disk."""
from __future__ import annotations

import importlib
import json
import threading
from dataclasses import replace
from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.models import Leg
from src.scenario import DEFAULT_SLACK_DAYS, Scenario, Stop, save_scenario

AIRPORTS = [
    {"iata": "PRG", "name": "Vaclav Havel", "city": "Prague", "country": "CZ", "rank": 0,
     "runway_ft": 12189},
    {"iata": "VIE", "name": "Vienna Intl", "city": "Vienna", "country": "AT", "rank": 0,
     "runway_ft": 11811},
    {"iata": "NRT", "name": "Narita Intl", "city": "Narita", "country": "JP", "rank": 0,
     "runway_ft": 13123, "keywords": ["Tokyo"]},
    {"iata": "HND", "name": "Haneda", "city": "Tokyo", "country": "JP", "rank": 0,
     "runway_ft": 11024},
    {"iata": "KRK", "name": "Krakow John Paul II", "city": "Balice", "country": "PL", "rank": 0,
     "runway_ft": 8366},
    {"iata": "DPS", "name": "Denpasar Ngurah Rai", "city": "Kuta", "country": "ID", "rank": 0,
     "runway_ft": 9790, "keywords": ["Bali", "Denpasar"]},
    {"iata": "MNL", "name": "Ninoy Aquino", "city": "Manila", "country": "PH", "rank": 0,
     "runway_ft": 12261},
    {"iata": "BRQ", "name": "Brno-Turany", "city": "Brno", "country": "CZ", "rank": 1,
     "runway_ft": 8694},
]

COUNTRIES = {
    "CZ": "Czech Republic", "AT": "Austria", "JP": "Japan", "PH": "Philippines",
    "PL": "Poland", "ID": "Indonesia",
}

NOTES = {
    "airports": {
        "BRQ": {"verdict": "no_inventory", "note": "No long-haul inventory in either direction"},
    }
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    scenarios = tmp_path / "scenarios"
    data = tmp_path / "data"
    scenarios.mkdir()
    data.mkdir()
    monkeypatch.setenv("SCENARIO_DIR", str(scenarios))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path / ".secrets"))
    # Otherwise a webhook exported in the shell running the tests would win
    # over the file under test, and the store would look broken.
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    (data / "airports.json").write_text(json.dumps(AIRPORTS), encoding="utf-8")
    (data / "countries.json").write_text(json.dumps(COUNTRIES), encoding="utf-8")
    (data / "airport_notes.json").write_text(json.dumps(NOTES), encoding="utf-8")

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

    # The catalogue is memoised per directory, and every test gets a new tmp_path.
    airports_module._raw_catalogue.cache_clear()
    airports_module.load_countries.cache_clear()
    airports_module.load_catalogue.cache_clear()
    airports_module._by_code.cache_clear()
    airports_module.load_notes.cache_clear()
    importlib.reload(app_module)
    return TestClient(app_module.app), data


def seed_sweep(data_dir, stamp="2026-08-06T02-00-00Z", legs=None):
    directory = data_dir / "sweeps" / "jp-ph" / stamp
    directory.mkdir(parents=True)
    legs = (
        legs
        if legs is not None
        else [
            Leg("T", "PRG", "NRT", date(2027, 1, 10), "QR", None, 1, "CZK", 12000.0, "u"),
            Leg("T", "NRT", "MNL", date(2027, 1, 20), "PR", None, 0, "CZK", 4000.0, "u"),
            Leg("T", "MNL", "PRG", date(2027, 1, 30), "QR", None, 1, "CZK", 14000.0, "u"),
            Leg("T", "MNL", "VIE", date(2027, 1, 30), "EY", None, 1, "CZK", 11000.0, "u"),
        ]
    )
    with (directory / "legs.jsonl").open("w") as handle:
        for leg in legs:
            handle.write(json.dumps(leg.to_dict()) + "\n")
    (directory / "status.json").write_text(json.dumps({"state": "done", "total": 4}))
    return stamp


# ------------------------------------------------------------------- airports


def codes(response) -> list[str]:
    return [airport["iata"] for airport in response.json()["airports"]]


def test_airport_search_matches_code_and_city(client):
    api, _ = client
    assert codes(api.get("/api/airports/search?q=PRG")) == ["PRG"]
    assert codes(api.get("/api/airports/search?q=prague")) == ["PRG"]


def test_airport_search_matches_a_country_name(client):
    """"Japan" is the first thing you type for somewhere you have not been.

    It used to return nothing at all: the catalogue stores the ISO code, and
    the search never looked at the country either way.
    """
    api, _ = client
    body = api.get("/api/airports/search?q=Japan").json()
    assert codes(api.get("/api/airports/search?q=Japan")) == ["NRT", "HND"]
    assert body["country"] == "Japan"


def test_a_city_match_still_outranks_its_country(client):
    """Typing "Prague" must not bury PRG under every airport in Czechia."""
    api, _ = client
    assert codes(api.get("/api/airports/search?q=Prague"))[0] == "PRG"


def test_the_names_people_use_beat_the_official_ones(client):
    """Narita's municipality is Narita, so "Tokyo" used to miss it entirely."""
    api, _ = client
    assert codes(api.get("/api/airports/search?q=Tokyo")) == ["NRT", "HND"]


def test_an_exact_alias_outranks_a_city_that_merely_starts_the_same(client):
    """"Bali" resolved to Krakow, whose municipality genuinely is Balice.

    Denpasar says "Bali" nowhere in its city or name - only in its aliases - so
    an exact alias has to beat a city prefix or the wrong airport wins.
    """
    api, _ = client
    assert codes(api.get("/api/airports/search?q=Bali"))[0] == "DPS"


def test_search_reports_what_it_truncated(client):
    api, _ = client
    body = api.get("/api/airports/search?q=Japan&limit=1").json()
    assert body["total"] == 2
    assert len(body["airports"]) == 1


def test_airport_search_needs_a_query(client):
    api, _ = client
    assert api.get("/api/airports/search?q=").json() == {
        "airports": [], "total": 0, "country": None
    }


def test_frequent_airports_come_from_the_saved_trips(client):
    """The quick-pick row is derived, not hardcoded - and knows direction."""
    api, _ = client
    body = api.get("/api/airports/frequent").json()
    assert [a["iata"] for a in body["origins"]] == ["PRG", "VIE"]
    assert sorted(a["iata"] for a in body["destinations"]) == ["MNL", "NRT"]


def test_unknown_airport_is_404(client):
    api, _ = client
    assert api.get("/api/airports/ZZZ").status_code == 404


def test_viability_keeps_the_hand_measured_findings(client):
    """The old catalogue's value was that its verdicts were measured.

    Brno was checked live and had no long-haul inventory at all. No sweep will
    ever rediscover that, because nothing sweeps an airport it cannot use.
    """
    api, _ = client
    body = api.get("/api/viability").json()
    assert body["airports"]["BRQ"]["verdict"] == "no_inventory"
    assert "no long-haul inventory" in body["airports"]["BRQ"]["note"].lower()


def test_viability_derives_verdicts_from_sweep_history(client):
    api, data = client
    seed_sweep(data)
    body = api.get("/api/viability").json()
    assert body["airports"]["NRT"]["verdict"] == "ok"
    assert body["airports"]["NRT"]["legs"] > 0
    assert body["airports"]["NRT"]["min_price"] == 4000.0


def test_viability_calls_a_route_dead_once_it_has_been_asked_enough_times(client):
    """Pins the endpoint's half of a contract that was only ever half kept.

    This has always worked given a status file with attempts in it. Nothing
    wrote one: the runner omitted `route_searches`, so in practice every route
    sat at zero attempts, below the threshold for any verdict, and
    `dead_routes` came back empty however many empty searches were behind it.
    The runner's side is guarded by
    `test_status_records_the_attempts_and_legs_of_every_route`.
    """
    api, data = client
    directory = data / "sweeps" / "jp-ph" / "2026-08-09T02-00-00Z"
    directory.mkdir(parents=True)
    (directory / "legs.jsonl").write_text("", encoding="utf-8")
    (directory / "status.json").write_text(
        json.dumps({"state": "done", "route_searches": {"VIE->NRT": 4}, "route_errors": {}})
    )
    body = api.get("/api/viability").json()
    assert body["dead_routes"] == ["VIE->NRT"]


# ------------------------------------------------------------------ scenarios


def test_lists_seeded_scenarios(client):
    api, _ = client
    body = api.get("/api/scenarios").json()
    assert [s["id"] for s in body["trips"]] == ["jp-ph"]
    assert body["problems"] == []


def test_one_unreadable_trip_does_not_hide_the_others(client, tmp_path):
    """The whole listing used to 400 on one bad file, which the UI drew as an
    empty picker - the same thing it draws when you genuinely have no trips."""
    api, _ = client
    (tmp_path / "scenarios" / "broken.json").write_text("{ nope", encoding="utf-8")

    body = api.get("/api/scenarios").json()

    assert [s["id"] for s in body["trips"]] == ["jp-ph"]
    assert body["problems"][0]["file"] == "broken.json"


def test_rejects_an_invalid_scenario_with_a_readable_message(client):
    api, _ = client
    bad = {
        "id": "bad",
        "name": "Bad",
        "origins": [],
        "stops": [{"label": "Japan", "airports": ["NRT"], "stay_days": [9, 11]}],
        "window_start": "2027-01-05",
        "window_end": "2027-02-08",
    }
    response = api.post("/api/scenarios", json=bad)
    assert response.status_code == 400
    assert "origins" in response.json()["detail"]


def test_rejects_a_bad_airport_code_with_a_readable_message(client):
    api, _ = client
    bad = {
        "id": "bad",
        "name": "Bad",
        "origins": ["Prague"],
        "stops": [{"label": "Japan", "airports": ["NRT"], "stay_days": [9, 11]}],
        "window_start": "2027-01-05",
        "window_end": "2027-02-08",
    }
    response = api.post("/api/scenarios", json=bad)
    assert response.status_code == 400
    assert "IATA" in response.json()["detail"]


def test_creating_a_scenario_round_trips(client):
    api, _ = client
    payload = {
        "id": "grand-tour",
        "name": "Three stops",
        "origins": ["PRG"],
        "stops": [
            {"label": "Japan", "airports": ["NRT"], "stay_days": [7, 9]},
            {"label": "Philippines", "airports": ["MNL"], "stay_days": [7, 9]},
            {"label": "Thailand", "airports": ["BKK"], "stay_days": [5, 7]},
        ],
        "window_start": "2027-01-05",
        "window_end": "2027-03-15",
        "depth": "quick",
    }
    assert api.post("/api/scenarios", json=payload).status_code == 201
    stored = api.get("/api/scenarios/grand-tour").json()
    assert [s["label"] for s in stored["stops"]] == ["Japan", "Philippines", "Thailand"]


def test_creating_over_an_existing_trip_is_refused(client):
    """POST used to overwrite silently, and ids now come from the trip name."""
    api, _ = client
    existing = api.get("/api/scenarios/jp-ph").json()
    existing["name"] = "Something else entirely"
    assert api.post("/api/scenarios", json=existing).status_code == 409
    assert api.get("/api/scenarios/jp-ph").json()["name"] == "Japan then Philippines"


def test_deleting_a_trip_keeps_the_sweeps_it_gathered(client):
    """The measurements cost real Actions minutes; the plan is what is deleted."""
    api, data = client
    stamp = seed_sweep(data)
    assert api.delete("/api/scenarios/jp-ph").status_code == 200
    assert api.get("/api/scenarios/jp-ph").status_code == 404
    assert (data / "sweeps" / "jp-ph" / stamp / "legs.jsonl").exists()


def test_deleting_an_unknown_trip_is_404(client):
    api, _ = client
    assert api.delete("/api/scenarios/never-existed").status_code == 404


def test_put_will_not_write_to_a_different_id_than_the_path(client):
    """PUT used to ignore the path and write wherever the body pointed."""
    api, _ = client
    body = api.get("/api/scenarios/jp-ph").json()
    body["id"] = "somewhere-else"
    response = api.put("/api/scenarios/jp-ph", json=body)
    assert response.status_code == 400
    assert api.get("/api/scenarios/somewhere-else").status_code == 404


@pytest.mark.parametrize("bad_id", ["..", "../secrets", "a/b"])
def test_scenario_ids_that_escape_the_directory_are_rejected(client, bad_id):
    api, _ = client
    assert api.get(f"/api/scenarios/{bad_id}").status_code in (400, 404)


# ------------------------------------------------------------------- estimate


def test_estimate_reports_searches_and_minutes(client):
    api, _ = client
    body = api.post("/api/scenarios/jp-ph/estimate").json()
    assert body["searches"] > 0
    assert body["minutes"] > 0
    assert set(body["per_leg"]) == {"0", "1", "2"}
    assert body["leg_count"] == 3


def test_estimate_labels_each_leg_without_naming_a_country(client):
    api, _ = client
    assert api.post("/api/scenarios/jp-ph/estimate").json()["leg_labels"] == [
        "PRG/VIE → NRT",
        "NRT → MNL",
        "MNL → PRG/VIE",
    ]


def test_deeper_estimate_costs_more(client):
    api, _ = client
    quick = api.post("/api/scenarios/jp-ph/estimate?depth=quick").json()["searches"]
    deep = api.post("/api/scenarios/jp-ph/estimate?depth=deep").json()["searches"]
    assert deep > quick


def test_an_unknown_depth_is_rejected(client):
    api, _ = client
    assert api.post("/api/scenarios/jp-ph/estimate?depth=exhaustive").status_code == 400


# -------------------------------------------------------------------- explore


ROUTES = ["PRG->NRT", "VIE->NRT", "NRT->MNL", "MNL->PRG", "MNL->VIE"]


def seed_explore(data_dir, stamp="2026-08-11T09-00-00Z", errors=None):
    """A reconnaissance pass where Vienna is dear and everything was answered."""
    directory = data_dir / "sweeps" / "jp-ph" / stamp
    directory.mkdir(parents=True)
    legs = [
        Leg("T", "PRG", "NRT", date(2027, 1, 10), "QR", None, 1, "CZK", 11000.0, "u"),
        Leg("T", "VIE", "NRT", date(2027, 1, 10), "QR", None, 2, "CZK", 22000.0, "u"),
        Leg("T", "NRT", "MNL", date(2027, 1, 20), "PR", None, 0, "CZK", 4000.0, "u"),
        Leg("T", "MNL", "PRG", date(2027, 1, 30), "QR", None, 1, "CZK", 14000.0, "u"),
        Leg("T", "MNL", "VIE", date(2027, 1, 30), "EY", None, 1, "CZK", 15000.0, "u"),
    ]
    with (directory / "legs.jsonl").open("w") as handle:
        for leg in legs:
            handle.write(json.dumps(leg.to_dict()) + "\n")
    (directory / "status.json").write_text(
        json.dumps(
            {
                "state": "done",
                "mode": "explore",
                "total": 15,
                "route_searches": {route: 3 for route in ROUTES},
                "route_errors": {route: (errors or {}).get(route, 0) for route in ROUTES},
            }
        )
    )
    return stamp


def test_the_explore_report_ranks_the_origins_against_each_other(client):
    api, data = client
    stamp = seed_explore(data)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/explore").json()
    origins = {row["iata"]: row for row in body["pools"][0]["airports"]}
    assert origins["PRG"]["verdict"] == "best"
    assert origins["VIE"]["verdict"] == "poor"
    assert origins["VIE"]["out_min_stops"] == 2


def test_the_explore_report_names_the_list_each_airport_belongs_to(client):
    api, data = client
    stamp = seed_explore(data)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/explore").json()
    assert [pool["role"] for pool in body["pools"]] == ["origins", "stop", "stop", "origins"]


# --------------------------------------------- reading a sweep of an older trip
#
# Every reader used to load whatever the trip is *now* and read the legs
# through it. Change an airport and the run that found those legs stops making
# sense: the Explore tab lists airports the run never searched, and Results and
# Prices empty out because the itineraries no longer chain through the pools.


def snapshot(data_dir, stamp, scenario: Scenario) -> None:
    """Record which trip a seeded sweep searched, as `run_sweep` now does."""
    path = data_dir / "sweeps" / "jp-ph" / stamp / "scenario.json"
    path.write_text(json.dumps(scenario.to_dict()), encoding="utf-8")


def searched_trip() -> Scenario:
    """The trip the seeded sweeps actually flew: out of Prague and Vienna."""
    return Scenario(
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
    )


def move_the_trip_to(api, **changes) -> None:
    """Edit the saved trip after the sweep, the way the user did."""
    trip = api.get("/api/scenarios/jp-ph").json()
    trip.update(changes)
    assert api.put("/api/scenarios/jp-ph", json=trip).status_code == 200


def test_a_report_describes_the_trip_its_run_searched_not_the_trip_now(client):
    api, data = client
    stamp = seed_explore(data)
    snapshot(data, stamp, searched_trip())
    move_the_trip_to(api, origins=["KRK"], return_to=None)

    body = api.get(f"/api/sweeps/jp-ph/{stamp}/explore").json()
    origins = {row["iata"]: row for row in body["pools"][0]["airports"]}
    assert set(origins) == {"PRG", "VIE"}, "the airports this run actually priced"
    assert origins["PRG"]["out_min_price"] == 11000, "its legs must not be discarded"


def test_a_report_names_the_airports_of_the_current_trip_it_never_priced(client):
    api, data = client
    stamp = seed_explore(data)
    snapshot(data, stamp, searched_trip())
    move_the_trip_to(api, origins=["KRK"], return_to=None)

    body = api.get(f"/api/sweeps/jp-ph/{stamp}/explore").json()
    assert body["matches_current_trip"] is False
    assert body["pools"][0]["not_searched"] == ["KRK"]


def test_results_of_an_older_sweep_survive_an_edit_to_the_trip(client):
    """The itineraries chain through the pools, so reading them against a trip
    that no longer contains those airports silently returns nothing."""
    api, data = client
    stamp = seed_sweep(data)
    snapshot(data, stamp, searched_trip())
    move_the_trip_to(api, origins=["KRK"], return_to=None)
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()["itineraries"]


def test_the_price_chart_of_an_older_sweep_survives_an_edit_to_the_trip(client):
    api, data = client
    stamp = seed_sweep(data)
    snapshot(data, stamp, searched_trip())
    move_the_trip_to(api, origins=["KRK"], return_to=None)
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/by-date").json()


def _differs(api, stamp) -> list[str]:
    listed = {s["stamp"]: s for s in api.get("/api/sweeps/jp-ph").json()["sweeps"]}
    return listed[stamp]["differs"]


def test_the_sweep_listing_names_what_a_run_searched_that_the_trip_no_longer_says(client):
    """The picker is where a run is chosen, so it has to say which runs are
    about the trip you are looking at before one is opened and believed."""
    api, data = client
    stamp = seed_explore(data)
    snapshot(data, stamp, searched_trip())
    move_the_trip_to(api, origins=["KRK"], return_to=None)
    assert _differs(api, stamp) == ["airports"]


def test_the_listing_flags_a_run_priced_under_other_stays(client):
    """The edit that drifts fastest and used not to be checked at all.

    `japan-philippines` has twelve sweeps on disk spanning three stay settings,
    every one of them labelled as though it described the trip as it stands.
    """
    api, data = client
    stamp = seed_explore(data)
    snapshot(data, stamp, searched_trip())
    trip = api.get("/api/scenarios/jp-ph").json()
    trip["stops"][0]["stay_days"] = [10, 11]
    assert api.put("/api/scenarios/jp-ph", json=trip).status_code == 200
    assert _differs(api, stamp) == ["stays"]


def test_the_listing_flags_a_run_of_another_window(client):
    api, data = client
    stamp = seed_explore(data)
    snapshot(data, stamp, searched_trip())
    move_the_trip_to(api, window_end="2027-02-20")
    assert _differs(api, stamp) == ["window"]


def test_every_difference_is_named_not_only_the_first(client):
    """Named rather than a boolean so the row says which part to distrust;
    naming one of three would put the boolean back under a longer name."""
    api, data = client
    stamp = seed_explore(data)
    snapshot(data, stamp, searched_trip())
    trip = api.get("/api/scenarios/jp-ph").json()
    trip["origins"] = ["KRK"]
    trip["return_to"] = None
    trip["stops"][0]["stay_days"] = [10, 11]
    trip["window_end"] = "2027-02-20"
    assert api.put("/api/scenarios/jp-ph", json=trip).status_code == 200
    assert _differs(api, stamp) == ["airports", "stays", "window"]


def test_a_sweep_of_the_current_trip_is_not_flagged_in_the_listing(client):
    api, data = client
    stamp = seed_explore(data)
    snapshot(data, stamp, searched_trip())
    assert _differs(api, stamp) == []


def test_a_sweep_from_before_snapshots_existed_is_read_against_the_live_trip(client):
    """Seven of these are already on disk. They must keep working."""
    api, data = client
    stamp = seed_explore(data)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/explore").json()
    assert body["matches_current_trip"] is True
    assert body["pools"][0]["not_searched"] == []


def test_todays_bag_estimate_is_applied_to_an_older_sweep(client):
    """Shape is frozen; how you *read* a result is not. Changing the bag
    estimate must still move the totals of a sweep already on disk."""
    api, data = client
    stamp = seed_sweep(data)
    snapshot(data, stamp, searched_trip())
    before = api.get(f"/api/sweeps/jp-ph/{stamp}/by-date").json()
    move_the_trip_to(api, bag_estimate=3000)
    after = api.get(f"/api/sweeps/jp-ph/{stamp}/by-date").json()
    assert after[0]["cheapest_total_with_bags"] > before[0]["cheapest_total_with_bags"]


def test_the_explore_report_is_404_for_a_sweep_that_does_not_exist(client):
    api, _ = client
    assert api.get("/api/sweeps/jp-ph/2026-01-01T00-00-00Z/explore").status_code == 404


def test_the_exploration_estimate_is_a_fraction_of_a_deep_sweep(client):
    api, _ = client
    explore = api.post("/api/scenarios/jp-ph/estimate?mode=explore").json()
    deep = api.post("/api/scenarios/jp-ph/estimate?depth=deep").json()
    assert explore["searches"] < deep["searches"] / 5
    assert explore["mode"] == "explore"


def test_an_estimate_can_price_an_edited_trip_that_has_not_been_saved(client):
    """The cost badge sits next to the run buttons, so it has to describe the
    trip on screen. Reading it off the file said "63 searches" for a trip the
    user had just narrowed to 42."""
    api, _ = client
    saved = api.post("/api/scenarios/jp-ph/estimate").json()
    edited = api.get("/api/scenarios/jp-ph").json() | {"origins": ["PRG", "VIE", "KRK"]}
    live = api.post("/api/scenarios/jp-ph/estimate", json=edited).json()
    assert live["searches"] > saved["searches"]


def test_estimating_an_edited_trip_does_not_save_it(client):
    api, _ = client
    edited = api.get("/api/scenarios/jp-ph").json() | {"origins": ["PRG", "VIE", "KRK"]}
    api.post("/api/scenarios/jp-ph/estimate", json=edited)
    assert api.get("/api/scenarios/jp-ph").json()["origins"] == ["PRG", "VIE"]


def test_an_estimate_of_an_impossible_trip_is_a_400_not_a_crash(client):
    api, _ = client
    broken = api.get("/api/scenarios/jp-ph").json() | {"origins": []}
    assert api.post("/api/scenarios/jp-ph/estimate", json=broken).status_code == 400


def test_an_unknown_run_mode_is_rejected(client):
    api, _ = client
    assert api.post("/api/scenarios/jp-ph/estimate?mode=guess").status_code == 400
    assert api.post("/api/scenarios/jp-ph/run?mode=guess").status_code == 400


# ----------------------------------------------------------------- stopping


def test_stopping_a_scenario_that_is_not_running_says_so(client):
    api, _ = client
    response = api.post("/api/scenarios/jp-ph/stop")
    assert response.status_code == 409
    assert "running" in response.json()["detail"].lower()


def test_stopping_an_unknown_scenario_is_404(client):
    api, _ = client
    assert api.post("/api/scenarios/nope/stop").status_code == 404


def test_stop_reaches_the_sweep_that_is_actually_running(client, monkeypatch):
    """The wiring, which neither half's own tests can see.

    The runner honours an event and the endpoint sets one; this is the only
    check that it is the *same* event. A real sweep is stood in for, because
    the alternative is launching Chromium at a site that has already throttled
    this client into mass timeouts.
    """
    import src.web.app as app_module

    started, finished = threading.Event(), threading.Event()

    def fake_sweep(scenario, *, stop=None, **kwargs):
        started.set()
        assert stop is not None, "the endpoint launched a sweep with no way to stop it"
        stop.wait(timeout=10)
        finished.set()

    monkeypatch.setattr(app_module, "run_sweep", fake_sweep)
    api, _ = client

    assert api.post("/api/scenarios/jp-ph/run").json()["started"] is True
    assert started.wait(timeout=5), "the sweep thread never started"
    assert api.get("/api/sweeps/jp-ph").json()["running"] is True

    assert api.post("/api/scenarios/jp-ph/stop").json()["stopping"] is True
    assert finished.wait(timeout=5), "the running sweep never saw the stop"


def test_the_listing_says_whether_a_sweeps_legs_are_actually_on_disk(client):
    """`legs_found` is what a run found, not what survived it.

    The 11 Aug local sweep reports 1,167 flights and has no legs.jsonl at all -
    it predated incremental writing, so they only ever existed in the process
    that was killed. Anything reading that number as data to work from gets a
    report with every airport unmeasured, and the Explore tab offered it first
    because it looked like the richest run available.
    """
    api, data = client
    directory = data / "sweeps" / "jp-ph" / "2026-08-11T14-31-07Z"
    directory.mkdir(parents=True)
    (directory / "status.json").write_text(
        json.dumps({"state": "stopped", "total": 615, "legs_found": 1167})
    )
    seed_explore(data)

    sweeps = {sweep["stamp"]: sweep for sweep in api.get("/api/sweeps/jp-ph").json()["sweeps"]}
    assert sweeps["2026-08-11T14-31-07Z"]["has_legs"] is False
    assert sweeps["2026-08-11T09-00-00Z"]["has_legs"] is True


def test_an_empty_legs_file_does_not_count_as_having_legs(client):
    api, data = client
    directory = data / "sweeps" / "jp-ph" / "2026-08-09T02-00-00Z"
    directory.mkdir(parents=True)
    (directory / "legs.jsonl").write_text("", encoding="utf-8")
    (directory / "status.json").write_text(json.dumps({"state": "done", "legs_found": 0}))
    sweeps = {sweep["stamp"]: sweep for sweep in api.get("/api/sweeps/jp-ph").json()["sweeps"]}
    assert sweeps["2026-08-09T02-00-00Z"]["has_legs"] is False


def test_the_sweep_listing_reports_whether_a_stop_was_asked_for(client):
    api, data = client
    seed_explore(data)
    body = api.get("/api/sweeps/jp-ph").json()
    assert body["stopping"] is False
    assert body["sweeps"][0]["mode"] == "explore"


# -------------------------------------------------------------------- results


def test_results_report_both_headline_options(client):
    api, data = client
    stamp = seed_sweep(data)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()
    assert body["best_same_airport"]["total_price"] == 30000
    assert body["best_open_jaw"]["total_price"] == 27000


def test_results_are_sorted_cheapest_first(client):
    api, data = client
    stamp = seed_sweep(data)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()
    totals = [i["total_price"] for i in body["itineraries"]]
    assert totals == sorted(totals)


def test_results_can_be_filtered_to_same_airport(client):
    api, data = client
    stamp = seed_sweep(data)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results?mode=same").json()
    assert all(i["same_airport"] for i in body["itineraries"])


def test_results_report_the_currency(client):
    api, data = client
    stamp = seed_sweep(data)
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()["currency"] == "CZK"


def narrow(api, **fields) -> dict:
    """Put a narrowing on the saved trip, the way the page does."""
    trip = api.get("/api/scenarios/jp-ph").json()
    trip.update(fields)
    return api.put("/api/scenarios/jp-ph", json=trip)


def test_a_narrowed_reading_hides_trips_outside_the_return_window(client):
    api, data = client
    stamp = seed_sweep(data)
    # The seeded trips all fly home on 30 January.
    assert narrow(
        api, return_focus_start="2027-01-20", return_focus_end="2027-01-25"
    ).status_code == 200

    narrowed = api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()
    assert narrowed["itineraries"] == []
    assert narrowed["window"]["applied"] is True

    everything = api.get(f"/api/sweeps/jp-ph/{stamp}/results?window=all").json()
    assert everything["itineraries"]
    assert everything["window"]["applied"] is False


def test_a_narrowed_reading_hides_trips_outside_the_nights_band(client):
    api, data = client
    stamp = seed_sweep(data)
    # The seeded trips are 20 nights: 10 in Japan, 10 in the Philippines.
    narrow(api, total_days=[18, 19])
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()["itineraries"] == []
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/results?window=all").json()["itineraries"]

    narrow(api, total_days=[20, 22])
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()["itineraries"]


def test_the_departure_window_narrows_a_reading_too(client):
    """All three parts of the narrowing, or none of them.

    The focus is read from the live trip like the other two, even though the
    sweep's own snapshot records what it searched under — `is_comparable` reads
    that from `status.json`, so nothing that needed the snapshot's copy lost it.
    """
    api, data = client
    stamp = seed_sweep(data)  # every seeded trip leaves on 10 January
    narrow(api, focus_start="2027-01-05", focus_end="2027-01-08")
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()["itineraries"] == []
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/results?window=all").json()["itineraries"]

    narrow(api, focus_start="2027-01-09", focus_end="2027-01-12")
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()
    assert body["itineraries"]
    assert body["window"]["focus"] == ["2027-01-09", "2027-01-12"]


def test_widening_a_narrowing_is_not_served_from_the_cache(client):
    """Editing a trip does not touch its sweeps, and the memo is keyed on both.

    Keyed on the legs file alone - which is what it was - the second reading
    below returned the first one's result forever. On screen that is a trip you
    have just widened still showing nothing, which reads as the sweep having
    found nothing rather than as a stale answer.
    """
    api, data = client
    stamp = seed_sweep(data)
    narrow(api, total_days=[18, 19])
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()["itineraries"] == []

    narrow(api, total_days=[18, 22])
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()["itineraries"]


def test_the_narrowing_applies_to_a_sweep_that_predates_it(client):
    """The sweeps worth narrowing are the ones swept before it existed.

    `_sweep_scenario` takes shape from the run's own snapshot, so a narrowing
    read from there would only ever apply to sweeps that no longer needed it.
    """
    api, data = client
    stamp = seed_sweep(data)
    # A snapshot with no narrowing on it, as every committed sweep has.
    (data / "sweeps" / "jp-ph" / stamp / "scenario.json").write_text(
        json.dumps(api.get("/api/scenarios/jp-ph").json()), encoding="utf-8"
    )
    narrow(api, total_days=[18, 19])
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()["itineraries"] == []


def test_an_unsatisfiable_narrowing_is_refused_with_a_reason(client):
    api, _ = client
    response = narrow(api, total_days=[40, 50])
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "40-50 nights away is unreachable" in detail
    assert "9-11 at Japan" in detail


def test_a_return_window_outside_the_trip_window_is_refused(client):
    api, _ = client
    response = narrow(
        api, return_focus_start="2027-02-10", return_focus_end="2027-02-14"
    )
    assert response.status_code == 400
    assert "widen the window first" in response.json()["detail"]


def test_the_by_date_chart_and_the_table_narrow_together(client):
    """Two panels on one screen must not report two different populations."""
    api, data = client
    stamp = seed_sweep(data)
    narrow(api, total_days=[18, 19])
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/by-date").json() == []
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/by-date?window=all").json()


def test_watch_candidates_narrow_too(client):
    api, data = client
    stamp = seed_sweep(data)
    narrow(api, total_days=[18, 19])
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/candidates").json()
    assert body["candidates"] == []
    assert body["window"]["applied"] is True
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/candidates?window=all").json()["candidates"]


# -------------------------------------------------------------------- by-leg


def seed_searches(data_dir, rows, stamp="2026-08-06T02-00-00Z"):
    """What the run asked, whatever it got back."""
    directory = data_dir / "sweeps" / "jp-ph" / stamp
    with (directory / "searches.jsonl").open("w") as handle:
        for origin, destination, when, answered in rows:
            handle.write(
                json.dumps(
                    {
                        "origin": origin,
                        "destination": destination,
                        "depart_date": when,
                        "answered": answered,
                    }
                )
                + "\n"
            )


def test_by_leg_has_one_series_per_leg_of_the_trip(client):
    api, data = client
    stamp = seed_sweep(data)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/by-leg").json()
    assert [leg["label"] for leg in body["legs"]] == [
        "PRG/VIE → NRT",
        "NRT → MNL",
        "MNL → PRG/VIE",
    ]
    assert body["stay_days"] == [[9, 11], [9, 11]]


def test_by_leg_names_the_airport_pair_that_won_each_date(client):
    """The pool line is a cheapest-of, so it has to say cheapest of what."""
    api, data = client
    stamp = seed_sweep(data)
    home = api.get(f"/api/sweeps/jp-ph/{stamp}/by-leg").json()["legs"][2]
    point = next(p for p in home["points"] if p["depart_date"] == "2027-01-30")
    # Vienna at 11,000 beats Prague at 14,000 on the same day.
    assert (point["origin"], point["destination"]) == ("MNL", "VIE")
    assert point["price"] == 11000


def test_by_leg_ranks_on_the_bag_inclusive_fare(client):
    """The same rule the itinerary ranking uses, or the two disagree on screen."""
    api, data = client
    stamp = seed_sweep(
        data,
        legs=[
            Leg("T", "PRG", "NRT", date(2027, 1, 10), "QR", None, 1, "CZK", 12000.0, "u",
                checked_bag=True),
            Leg("T", "VIE", "NRT", date(2027, 1, 10), "W6", None, 1, "CZK", 11000.0, "u",
                checked_bag=False),
        ],
    )
    out = api.get(f"/api/sweeps/jp-ph/{stamp}/by-leg").json()["legs"][0]
    point = next(p for p in out["points"] if p["depart_date"] == "2027-01-10")
    # 11,000 + a 1,500 bag loses to 12,000 with one included.
    assert point["origin"] == "PRG"
    assert point["with_bags"] == 12000


def test_by_leg_distinguishes_nothing_sold_from_never_asked(client):
    """Two opposite facts that would otherwise draw as the same empty gap.

    A date the site answered with no fares is the site having nothing. A date
    absent from `searches.jsonl` was never asked, which is a hole in the sweep.
    The first must appear in the series; the second must not.
    """
    api, data = client
    stamp = seed_sweep(data)
    seed_searches(
        data,
        [
            ("PRG", "NRT", "2027-01-10", True),   # answered, and sold something
            ("PRG", "NRT", "2027-01-17", True),   # answered, sold nothing
            # 2027-01-24 deliberately absent: never asked.
        ],
    )
    points = {p["depart_date"]: p for p in api.get(
        f"/api/sweeps/jp-ph/{stamp}/by-leg"
    ).json()["legs"][0]["points"]}
    assert points["2027-01-10"]["price"] == 12000
    assert points["2027-01-17"]["price"] is None
    assert points["2027-01-17"]["searched"] is True
    assert "2027-01-24" not in points


def test_by_leg_keeps_a_route_that_returned_nothing_at_all(client):
    """Derived from the trip's pools, not from the legs that came back.

    An airport that sold nothing is a finding. Building the route list from
    `legs.jsonl` would delete exactly the routes worth knowing about.
    """
    api, data = client
    stamp = seed_sweep(data)
    routes = api.get(f"/api/sweeps/jp-ph/{stamp}/by-leg").json()["legs"][0]["routes"]
    assert [r["route"] for r in routes] == ["PRG→NRT", "VIE→NRT"]
    vienna = next(r for r in routes if r["route"] == "VIE→NRT")
    assert vienna["points"] == []


def test_by_leg_404s_for_a_sweep_that_is_not_there(client):
    api, _ = client
    assert api.get("/api/sweeps/jp-ph/2026-01-01T00-00-00Z/by-leg").status_code == 404


def test_by_date_series_has_one_entry_per_departure_date(client):
    api, data = client
    stamp = seed_sweep(data)
    series = api.get(f"/api/sweeps/jp-ph/{stamp}/by-date").json()
    assert [row["depart_date"] for row in series] == ["2027-01-10"]
    assert series[0]["cheapest_total"] == 27000


def test_unknown_scenario_is_404(client):
    api, _ = client
    assert api.get("/api/scenarios/nope").status_code == 404


def test_unknown_sweep_is_404(client):
    api, _ = client
    assert api.get("/api/sweeps/jp-ph/2026-01-01T00-00-00Z/results").status_code == 404


def test_a_malformed_sweep_stamp_is_rejected(client):
    api, _ = client
    assert api.get("/api/sweeps/jp-ph/never/results").status_code == 400


def test_sweep_list_is_empty_before_any_run(client):
    api, _ = client
    body = api.get("/api/sweeps/jp-ph").json()
    assert body["sweeps"] == []
    assert body["running"] is False
    assert body["error"] is None


def test_sweep_list_reports_the_current_pace(client):
    """The UI countdown reads these instead of keeping its own copies."""
    api, _ = client
    body = api.get("/api/sweeps/jp-ph").json()
    assert body["seconds_per_search"] > 0
    assert body["workers"] >= 1


def test_history_is_empty_until_sweeps_accumulate(client):
    api, _ = client
    assert api.get("/api/history/jp-ph").json() == []


def test_history_reports_best_total_per_sweep(client):
    api, data = client
    seed_sweep(data, "2026-08-05T02-00-00Z")
    seed_sweep(data, "2026-08-06T02-00-00Z")
    series = api.get("/api/history/jp-ph").json()
    assert len(series) == 2
    assert all(row["best_total"] == 27000 for row in series)


def test_sweep_with_no_legs_yields_no_itineraries(client):
    api, data = client
    stamp = seed_sweep(data, legs=[])
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()
    assert body["itineraries"] == []
    assert body["best_same_airport"] is None


# ------------------------------------------------------- sweep comparability
#
# The "best total over time" chart drew a trend through four sweeps whose
# legs-per-search ran 2.9, 3.7, 7.6 and 9.7. The 2.9 sweep - the one at
# `standard` depth with error_count 0 - was roughly 70% silent failure, and its
# price sat 7% above what the healthy `quick` sweep found. Plotting them as one
# series charts scraper health, not prices.


# The seeded scenario is PRG/VIE -> NRT -> MNL -> PRG/VIE, so it plans four
# routes: PRG->NRT, VIE->NRT, NRT->MNL, MNL->PRG, MNL->VIE. Five, in fact - and
# the default seeded legs cover four of them.
ALL_ROUTES = [
    Leg("T", "PRG", "NRT", date(2027, 1, 10), "QR", None, 1, "CZK", 12000.0, "u"),
    Leg("T", "VIE", "NRT", date(2027, 1, 10), "OS", None, 1, "CZK", 13000.0, "u"),
    Leg("T", "NRT", "MNL", date(2027, 1, 20), "PR", None, 0, "CZK", 4000.0, "u"),
    Leg("T", "MNL", "PRG", date(2027, 1, 30), "QR", None, 1, "CZK", 14000.0, "u"),
    Leg("T", "MNL", "VIE", date(2027, 1, 30), "EY", None, 1, "CZK", 11000.0, "u"),
]


def seed_quality(data_dir, stamp, legs=ALL_ROUTES, **status):
    """A sweep whose status.json carries whatever quality figures a test needs."""
    seed_sweep(data_dir, stamp, legs=legs)
    directory = data_dir / "sweeps" / "jp-ph" / stamp
    payload = {"state": "done", "total": 4, "depth": "quick", "legs_per_search": 9.7}
    payload.update(status)
    (directory / "status.json").write_text(json.dumps(payload))
    return stamp


def test_history_marks_a_starved_sweep_incomparable(client):
    api, data = client
    seed_quality(data, "2026-08-06T20-22-44Z", legs_per_search=2.9, depth="standard")
    row = api.get("/api/history/jp-ph").json()[0]
    assert row["comparable"] is False
    assert row["legs_per_search"] == 2.9


def test_history_marks_a_healthy_fully_covered_sweep_comparable(client):
    api, data = client
    seed_quality(data, "2026-08-10T11-57-06Z")
    row = api.get("/api/history/jp-ph").json()[0]
    assert row["comparable"] is True
    assert row["routes_covered"] == row["routes_planned"] == 5


def test_history_marks_a_sweep_missing_a_route_incomparable(client):
    """Volume is not coverage.

    A sweep can average ten legs a search and still never price one leg home,
    and that missing route is exactly where the cheapest trip tends to hide -
    MNL->VIE went dark for a whole sweep while being the return of the best
    real itinerary.
    """
    api, data = client
    seed_quality(data, "2026-08-07T13-17-07Z", legs=ALL_ROUTES[:-1])
    row = api.get("/api/history/jp-ph").json()[0]
    assert row["comparable"] is False
    assert row["routes_covered"] < row["routes_planned"]


def test_history_marks_an_unhealthy_sweep_incomparable(client):
    api, data = client
    seed_quality(data, "2026-08-07T13-17-07Z", state="unhealthy")
    assert api.get("/api/history/jp-ph").json()[0]["comparable"] is False


def test_legs_per_search_is_derived_when_a_sweep_predates_the_field(client):
    """The four already-committed sweeps have no legs_per_search recorded.

    It is exactly legs_found / total, both of which every status.json has, so
    deriving it classifies them honestly rather than discarding all of them.
    """
    api, data = client
    seed_sweep(data, "2026-08-10T11-57-06Z", legs=ALL_ROUTES)
    directory = data / "sweeps" / "jp-ph" / "2026-08-10T11-57-06Z"
    (directory / "status.json").write_text(
        json.dumps({"state": "done", "total": 10, "legs_found": 97, "depth": "quick"})
    )
    row = api.get("/api/history/jp-ph").json()[0]
    assert row["legs_per_search"] == 9.7
    assert row["comparable"] is True


def test_a_sweep_with_no_countable_searches_is_incomparable(client):
    api, data = client
    seed_quality(data, "2026-08-10T11-57-06Z", total=0, legs_per_search=None)
    assert api.get("/api/history/jp-ph").json()[0]["comparable"] is False


def test_history_reports_depth_and_search_count(client):
    api, data = client
    seed_quality(data, "2026-08-10T11-57-06Z", depth="deep", total=615)
    row = api.get("/api/history/jp-ph").json()[0]
    assert row["depth"] == "deep"
    assert row["searches"] == 615


# ------------------------------------------------------------ observation time


def test_results_carry_when_each_price_was_observed(client):
    api, data = client
    stamp = seed_sweep(data)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()
    best = body["best_same_airport"]
    # Seeded legs have no per-leg stamp, so the sweep directory answers for them.
    assert best["observed_at"] == "2026-08-06T02:00:00+00:00"
    assert all(leg["observed_at"] for leg in best["legs"])


def test_results_report_how_far_apart_the_prices_were_read(client):
    api, data = client
    stamp = "2026-08-06T02-00-00Z"
    seed_sweep(data, stamp, legs=[
        Leg("T", "PRG", "NRT", date(2027, 1, 10), "QR", None, 1, "CZK", 12000.0, "u",
            observed_at="2026-08-06T02:05:00+00:00"),
        Leg("T", "NRT", "MNL", date(2027, 1, 20), "PR", None, 0, "CZK", 4000.0, "u",
            observed_at="2026-08-06T02:35:00+00:00"),
        Leg("T", "MNL", "PRG", date(2027, 1, 30), "QR", None, 1, "CZK", 14000.0, "u",
            observed_at="2026-08-06T03:35:00+00:00"),
    ])
    best = api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()["best_same_airport"]
    assert best["observed_at"] == "2026-08-06T02:05:00+00:00"
    assert best["observed_span_minutes"] == 90


# ------------------------------------------------------------------- probe


def seed_probe(data_dir, prices, origin="FRA", destination="NRT"):
    directory = data_dir / "probe"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "observations.jsonl").open("a", encoding="utf-8") as handle:
        for index, price in enumerate(prices):
            handle.write(json.dumps({
                "ts": f"2026-08-07T{index:02d}:00:00+00:00", "origin": origin,
                "destination": destination, "depart_date": "2027-01-12",
                "min_price": price, "n_offers": 10, "currency": "CZK",
            }) + "\n")


def test_probe_endpoint_serialises_every_derived_rate(client):
    """`asdict` covers fields only, so each computed rate must be named.

    Missing one is silent rather than loud: the UI reads undefined, renders 0%,
    and a route that moved 20% of the time reports as perfectly steady.
    """
    api, data = client
    seed_probe(data, [10832, 11146, 13556])
    route = api.get("/api/probe").json()["routes"]["FRA→NRT"]
    for key in ("change_rate", "meaningful_change_rate", "net_change_pct", "range_pct"):
        assert route.get(key) is not None, f"{key} missing from the probe payload"
    assert route["meaningful_change_rate"] == 1.0
    assert route["net_change_pct"] == 25.1


def test_probe_endpoint_is_empty_without_observations(client):
    api, _ = client
    assert api.get("/api/probe").json()["routes"] == {}


# ------------------------------------------------------------------ sources
#
# The scraper's moving parts, editable without a programmer. When pelikan
# renames a CSS class the sweep goes quiet, and the fix is a new string - the
# endpoints below are so that string can be typed, saved and proven.


def test_sources_are_readable_without_any_file(client):
    api, _ = client
    body = api.get("/api/sources").json()
    assert body["PELIKAN"]["base_url"].startswith("https://www.pelikan.cz")
    assert body["PELIKAN"]["selectors"]["card"]


def test_an_edited_source_is_saved_and_read_back(client):
    api, data = client
    current = api.get("/api/sources").json()
    current["PELIKAN"]["selectors"]["card"] = "div.offer"
    # Only the source being edited is sent. The others are left exactly as they
    # are on disk - and two of them have no selectors to send in the first place.
    assert api.put("/api/sources", json={"PELIKAN": current["PELIKAN"]}).status_code == 200

    assert api.get("/api/sources").json()["PELIKAN"]["selectors"]["card"] == "div.offer"
    # Written where the sweep will read it, not held in memory.
    assert json.loads((data / "sources.json").read_text(encoding="utf-8"))
    assert "div.offer" in (data / "sources.json").read_text(encoding="utf-8")


def test_an_unknown_source_is_rejected_rather_than_stored(client):
    api, _ = client
    response = api.put("/api/sources", json={"MYSTERY": {"base_url": "https://example.test"}})
    assert response.status_code == 400
    assert "MYSTERY" in response.json()["detail"]


def test_a_source_missing_a_required_selector_is_rejected(client):
    """Saving a half-filled selector map would break the sweep at 02:00.

    Rejecting on save turns that into a message now instead of a silent zero
    tomorrow.
    """
    api, _ = client
    current = api.get("/api/sources").json()
    del current["PELIKAN"]["selectors"]["price"]
    response = api.put("/api/sources", json={"PELIKAN": current["PELIKAN"]})
    assert response.status_code == 400
    assert "price" in response.json()["detail"]


def test_an_empty_card_selector_is_rejected(client):
    api, _ = client
    current = api.get("/api/sources").json()
    current["PELIKAN"]["selectors"]["card"] = "  "
    assert api.put("/api/sources", json=current).status_code == 400


def test_a_url_template_naming_a_value_this_app_cannot_fill_is_refused(client):
    """The Sources tab exists so a broken scraper is repairable without editing
    code, which only holds if a bad repair is recoverable from the same screen.

    An unknown placeholder used to save cleanly and then raise KeyError from
    inside `.format` on every search: the test button answered 500 with nothing
    to read, and the next sweep died the same way with no working template to
    fall back on.
    """
    api, _ = client
    current = api.get("/api/sources").json()
    current["PELIKAN"]["url_template"] = "T:{trip_type},CDF:{oops}"
    response = api.put("/api/sources", json={"PELIKAN": current["PELIKAN"]})
    assert response.status_code == 400
    assert "oops" in response.json()["detail"]
    # And nothing was written: the working template is still what a sweep reads.
    assert "{oops}" not in api.get("/api/sources").json()["PELIKAN"]["url_template"]


def test_a_url_template_with_an_unbalanced_brace_is_refused(client):
    api, _ = client
    current = api.get("/api/sources").json()
    current["PELIKAN"]["url_template"] = "T:{trip_type},CDF:{origin"
    response = api.put("/api/sources", json={"PELIKAN": current["PELIKAN"]})
    assert response.status_code == 400
    assert "format string" in response.json()["detail"]


def test_the_real_template_still_saves(client):
    """The guard above must not refuse the thing it is guarding."""
    api, _ = client
    current = api.get("/api/sources").json()
    assert api.put("/api/sources", json={"PELIKAN": current["PELIKAN"]}).status_code == 200


def test_testing_a_source_reports_what_the_selectors_found(client, monkeypatch):
    """One click that answers "is it the URL or the markup?"

    Runs against a saved page rather than the network here; the endpoint itself
    is what fetches live.
    """
    from pathlib import Path

    import src.web.app as app_module

    fixture = (
        Path(__file__).parent / "fixtures" / "pelikan_results.html"
    ).read_text(encoding="utf-8")
    monkeypatch.setattr(app_module, "_fetch_for_test", lambda source, url: (200, url, fixture))

    body = api_test_source(client)
    assert body["cards_found"] > 0
    assert body["url"].startswith("https://www.pelikan.cz")
    assert body["sample"]["price_amount"] > 0
    assert body["ok"] is True


def test_testing_a_source_with_a_broken_selector_says_so(client, monkeypatch):
    from pathlib import Path

    import src.web.app as app_module

    fixture = (
        Path(__file__).parent / "fixtures" / "pelikan_results.html"
    ).read_text(encoding="utf-8")
    monkeypatch.setattr(app_module, "_fetch_for_test", lambda source, url: (200, url, fixture))

    api, _ = client
    current = api.get("/api/sources").json()
    current["PELIKAN"]["selectors"]["card"] = "div.nothing-here"
    assert api.put("/api/sources", json={"PELIKAN": current["PELIKAN"]}).status_code == 200

    body = api_test_source(client)
    assert body["ok"] is False
    assert body["cards_found"] == 0
    assert "card" in body["message"].lower()


def api_test_source(client):
    api, _ = client
    response = api.post("/api/sources/PELIKAN/test")
    assert response.status_code == 200, response.text
    return response.json()


def test_a_url_that_404s_is_blamed_on_the_url_not_the_selectors(client, monkeypatch):
    """The one distinction the button exists to make.

    A 404 page loads perfectly well and simply contains no offer cards, so
    reporting "the card selector matched nothing" sends you to rewrite a
    selector that was never wrong.
    """
    import src.web.app as app_module

    monkeypatch.setattr(
        app_module, "_fetch_for_test", lambda source, url: (404, url, "<html>Not found</html>")
    )
    body = api_test_source(client)
    assert body["ok"] is False
    assert "404" in body["message"]
    assert "base_url" in body["message"] or "url_template" in body["message"]
    # Saying the selectors went untested is useful; blaming them is the bug.
    assert "renamed" not in body["message"].lower()
    assert "matched nothing" not in body["message"].lower()


def test_the_sites_own_no_flights_message_is_not_reported_as_breakage(client, monkeypatch):
    """A route with no inventory is data. Only the selectors stay unproven."""
    import src.web.app as app_module

    marker = "Hups! Nenašli jsme žádny let"
    monkeypatch.setattr(
        app_module, "_fetch_for_test", lambda source, url: (200, url, f"<html>{marker}</html>")
    )
    body = api_test_source(client)
    assert body["cards_found"] == 0
    assert "no flights" in body["message"].lower()
    assert "renamed" not in body["message"].lower()


def test_a_silent_redirect_to_the_homepage_is_blamed_on_the_url(client, monkeypatch):
    """pelikan.cz answers 200 for a path that does not exist.

    It quietly bounces to https://www.pelikan.cz/cs, dropping the search
    entirely — so the status is clean, the page is real, and nothing on it is
    an offer card. Measured live: a wrong base_url was reported as renamed
    markup, which is precisely the wrong afternoon to spend. Being redirected
    away from the address asked for is the general signal, and needs no
    knowledge of any particular site.
    """
    import src.web.app as app_module

    monkeypatch.setattr(
        app_module,
        "_fetch_for_test",
        lambda source, url: (200, "https://www.pelikan.cz/cs", "<html>homepage</html>"),
    )
    body = api_test_source(client)
    assert body["ok"] is False
    assert "redirect" in body["message"].lower()
    assert "base_url" in body["message"] or "url_template" in body["message"]
    assert "renamed" not in body["message"].lower()


def test_a_redirect_that_keeps_the_search_path_is_not_treated_as_a_failure(client, monkeypatch):
    """A working search really does redirect — it appends ",LOAD" to the path."""
    from pathlib import Path

    import src.web.app as app_module

    fixture = (
        Path(__file__).parent / "fixtures" / "pelikan_results.html"
    ).read_text(encoding="utf-8")
    monkeypatch.setattr(
        app_module, "_fetch_for_test", lambda source, url: (200, url + ",LOAD/", fixture)
    )
    assert api_test_source(client)["ok"] is True


def test_results_report_no_verification_until_one_is_run(client):
    """Absent must not read as confirmed: the UI says "not cross-checked"."""
    api, data = client
    stamp = seed_sweep(data)
    assert api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()["verification"] is None


def test_results_carry_the_verification_report_once_it_exists(client):
    api, data = client
    stamp = seed_sweep(data)
    (data / "sweeps" / "jp-ph" / stamp / "verify.json").write_text(
        json.dumps({"verdict": "cheaper_elsewhere", "legs_checked": 3,
                    "cheapest_elsewhere": {"route": "PRG->NRT", "saving_pct": 8.0}}),
        encoding="utf-8",
    )
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()
    assert body["verification"]["verdict"] == "cheaper_elsewhere"
    assert body["verification"]["cheapest_elsewhere"]["saving_pct"] == 8.0


def test_a_corrupt_verification_file_does_not_break_the_results(client):
    api, data = client
    stamp = seed_sweep(data)
    (data / "sweeps" / "jp-ph" / stamp / "verify.json").write_text("{not json", encoding="utf-8")
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results")
    assert body.status_code == 200
    assert body.json()["verification"] is None


# ------------------------------------------------------------ notify target
#
# The webhook URL is a bearer token: it never leaves the server whole, in any
# response, at any point. Everything the page needs to show is derivable from
# the masked form.


def test_notify_reports_nothing_configured(client):
    api, _ = client
    body = api.get("/api/notify").json()
    assert body["configured"] is False
    assert body["origin"] == "none"
    assert body["masked"] == ""


def test_saving_a_webhook_never_hands_it_back(client):
    api, _ = client
    real = "https://discord.com/api/webhooks/1409876543210987654/xY2bkQ7fLpZ9wA3tR6vN1sD4gH8j"

    assert api.put("/api/notify", json={"url": real}).status_code == 200

    body = api.get("/api/notify")
    assert body.json()["configured"] is True
    assert "xY2bkQ7fLpZ9wA3tR6vN1sD4gH8j" not in body.text


def test_saving_a_webhook_writes_outside_data(client, tmp_path):
    """`data/` is committed by the scheduled workflow. This must not land there."""
    api, data = client
    real = "https://discord.com/api/webhooks/1409876543210987654/xY2bkQ7fLpZ9wA3tR6vN1sD4gH8j"
    api.put("/api/notify", json={"url": real})

    written = [p for p in data.rglob("*") if "xY2b" in p.read_text(errors="ignore")]
    assert written == []
    assert (tmp_path / ".secrets" / "discord.json").exists()


def test_a_url_that_is_not_a_webhook_is_refused_with_advice(client):
    api, _ = client
    response = api.put("/api/notify", json={"url": "https://discord.com/channels/1/2"})
    assert response.status_code == 400
    assert "Copy Webhook URL" in response.json()["detail"]


def test_removing_a_webhook_leaves_nothing_behind(client, tmp_path):
    api, _ = client
    real = "https://discord.com/api/webhooks/1409876543210987654/xY2bkQ7fLpZ9wA3tR6vN1sD4gH8j"
    api.put("/api/notify", json={"url": real})

    assert api.delete("/api/notify").status_code == 200
    assert api.get("/api/notify").json()["configured"] is False
    assert not (tmp_path / ".secrets" / "discord.json").exists()


def test_testing_the_webhook_before_setting_one_says_so(client):
    api, _ = client
    body = api.post("/api/notify/test")
    assert body.status_code == 400
    assert "no webhook" in body.json()["detail"].lower()


def test_test_message_reports_what_discord_said(client, monkeypatch):
    api, _ = client
    real = "https://discord.com/api/webhooks/1409876543210987654/xY2bkQ7fLpZ9wA3tR6vN1sD4gH8j"
    api.put("/api/notify", json={"url": real})

    import src.web.app as app_module

    sent: list = []
    monkeypatch.setattr(app_module, "post", lambda url, embeds: sent.append(embeds) or True)

    body = api.post("/api/notify/test").json()
    assert body["sent"] is True
    assert "test" in sent[0][0]["title"].lower()


def test_a_refused_test_message_is_reported_not_raised(client, monkeypatch):
    """Discord 404s a deleted webhook. That is an answer worth showing, not a 500."""
    api, _ = client
    real = "https://discord.com/api/webhooks/1409876543210987654/xY2bkQ7fLpZ9wA3tR6vN1sD4gH8j"
    api.put("/api/notify", json={"url": real})

    import src.web.app as app_module

    monkeypatch.setattr(app_module, "post", lambda url, embeds: False)

    body = api.post("/api/notify/test").json()
    assert body["sent"] is False
    assert body["message"]


# --------------------------------------------------- focus and completeness
#
# Two figures a price has to be read with: how much of the plan was actually
# answered to find it, and whether the sweep priced the whole window or a few
# chosen days of it.


def test_history_marks_a_focused_sweep_incomparable_with_a_broad_trip(client):
    """A focused sweep prices a handful of dates, so its cheapest is the
    cheapest of those days - plotting it beside a broad sweep draws a step no
    fare ever made.

    Written against statuses carrying only `focus`, which is what every sweep
    committed before the narrowing was recorded in full still looks like.
    """
    from src.sweep.runner import is_comparable

    focused = {"focus": ["2027-01-12", "2027-01-16"], "return_focus": None,
               "total_days": None}
    broad = {"state": "done", "legs_per_search": 9.0, "focus": None}
    narrow = {"state": "done", "legs_per_search": 9.0, "focus": focused["focus"]}
    assert is_comparable(broad, 21, 21, None)
    assert not is_comparable(narrow, 21, 21, None)
    assert is_comparable(narrow, 21, 21, focused)
    assert not is_comparable(broad, 21, 21, focused)


def test_history_separates_the_broad_runs_from_the_narrowed_ones(client):
    """Two questions, two lines. Joining them draws a step no fare ever made.

    The broad line answers "is there a better week out there"; the narrowed one
    answers "is the trip I have chosen getting cheaper". A point belongs to
    whichever of those it measured, and `series` is how the chart is told.
    """
    api, data = client
    broad = seed_quality(data, "2026-08-20T02-00-00Z", mode="sweep")
    final = seed_quality(
        data, "2026-08-21T13-00-00Z", mode="final",
        narrowing={"focus": ["2027-01-10", "2027-01-12"],
                   "return_focus": None, "total_days": None},
    )

    points = {p["swept_at"]: p for p in api.get("/api/history/jp-ph").json()}

    assert points[broad]["series"] == "broad"
    assert points[final]["series"] == "final"
    assert points[final]["mode"] == "final"


def test_a_broad_run_stays_comparable_on_a_trip_that_has_since_been_narrowed(client):
    """It priced the whole window, and the window has not moved.

    Under the old rule a trip gaining a focus retired every broad sweep behind
    it, because comparability was measured against the trip's focus rather than
    against what each run had actually searched.
    """
    api, data = client
    seed_quality(data, "2026-08-20T02-00-00Z", mode="sweep",
                 narrowing={"focus": None, "return_focus": None, "total_days": None})
    trip = api.get("/api/scenarios/jp-ph").json()
    trip["focus_start"], trip["focus_end"] = "2027-01-12", "2027-01-16"
    assert api.put("/api/scenarios/jp-ph", json=trip).status_code == 200

    point = api.get("/api/history/jp-ph").json()[0]
    assert point["series"] == "broad"
    assert point["comparable"] is True


def test_a_focused_trip_estimates_far_fewer_searches_as_a_final_sweep(client):
    """The whole point of narrowing: the same depth, a fraction of the cost.

    Priced as `mode=final`, because that is the only sweep the narrowing binds.
    Asked as a broad sweep it must come back unchanged - see the test below.
    """
    client, _ = client
    trip = client.get("/api/scenarios/jp-ph").json()
    broad = client.post(
        "/api/scenarios/jp-ph/estimate?depth=deep", json=trip
    ).json()
    trip["focus_start"] = "2027-01-12"
    trip["focus_end"] = "2027-01-16"
    narrow = client.post(
        "/api/scenarios/jp-ph/estimate?depth=deep&mode=final", json=trip
    ).json()
    assert 0 < narrow["searches"] < broad["searches"] / 2
    assert narrow["minutes"] < broad["minutes"]


def test_a_focus_does_not_make_the_broad_sweep_any_cheaper(client):
    """Because it is not supposed to make it any narrower.

    The reading that hid the whole problem: a nightly sweep quoting a fraction
    of its usual cost looked like a saving rather than like a sweep that had
    stopped pricing most of the window.
    """
    client, _ = client
    trip = client.get("/api/scenarios/jp-ph").json()
    broad = client.post("/api/scenarios/jp-ph/estimate?depth=deep", json=trip).json()
    trip["focus_start"] = "2027-01-12"
    trip["focus_end"] = "2027-01-16"
    still_broad = client.post(
        "/api/scenarios/jp-ph/estimate?depth=deep", json=trip
    ).json()
    assert still_broad["searches"] == broad["searches"]


def test_a_final_sweep_of_a_trip_with_no_narrowing_is_refused_with_the_reason(client):
    """400 with the sentence, not a run that prices the window twice."""
    client, _ = client
    trip = client.get("/api/scenarios/jp-ph").json()
    response = client.post("/api/scenarios/jp-ph/estimate?depth=deep&mode=final", json=trip)
    assert response.status_code == 400
    assert "narrow" in response.json()["detail"].lower()


def test_starting_a_final_run_of_an_unnarrowed_trip_is_refused_before_it_starts(client):
    """Refused at the endpoint, not thrown inside the worker thread.

    A thread that raises records its traceback in `_failures` and surfaces as
    "Sweep failed — ValueError: There is nothing to narrow to yet" in the status
    strip, minutes later and in the voice of a crash. It is not a crash; it is a
    button that should not have been pressable.
    """
    client, _ = client
    response = client.post("/api/scenarios/jp-ph/run?mode=final")

    assert response.status_code == 400
    assert "narrow" in response.json()["detail"].lower()
    # And nothing was started, so the next attempt is not met with a 409.
    assert client.post("/api/scenarios/jp-ph/run?mode=final").status_code == 400


def test_a_focus_outside_the_window_is_refused_with_the_reason(client):
    client, _ = client
    trip = client.get("/api/scenarios/jp-ph").json()
    trip["focus_start"] = "2026-12-01"
    trip["focus_end"] = "2027-01-16"
    response = client.put("/api/scenarios/jp-ph", json=trip)
    assert response.status_code == 400
    assert "outside the window" in response.json()["detail"]


def test_a_focus_saves_and_comes_back(client):
    client, _ = client
    trip = client.get("/api/scenarios/jp-ph").json()
    trip["focus_start"] = "2027-01-12"
    trip["focus_end"] = "2027-01-16"
    assert client.put("/api/scenarios/jp-ph", json=trip).status_code == 200
    saved = client.get("/api/scenarios/jp-ph").json()
    assert (saved["focus_start"], saved["focus_end"]) == ("2027-01-12", "2027-01-16")

    # And clearing it goes back to the whole window rather than half a focus.
    saved["focus_start"] = saved["focus_end"] = None
    assert client.put("/api/scenarios/jp-ph", json=saved).status_code == 200
    assert client.get("/api/scenarios/jp-ph").json()["focus_start"] is None


# ------------------------------------------------------------ source roles
#
# Three kinds of source, and the difference matters because a broken one costs
# you different things. The sweep source going quiet means no data at all; the
# check source going quiet means no second opinion; a source that was never
# connected costs nothing and must not be drawn as though it might be on.


def test_every_source_says_what_it_is_for(client):
    api, _ = client
    sources = api.get("/api/sources").json()
    assert sources["PELIKAN"]["role"] == "sweep"
    assert sources["LETUSKA"]["role"] == "check"
    assert sources["SKYSCANNER"]["role"] == "none"


def test_a_source_that_is_not_connected_cannot_be_checked(client):
    """Pretending to test a source nothing reads would be the same lie as a
    sweep reporting error_count: 0 while most of it failed."""
    api, _ = client
    response = api.post("/api/sources/SKYSCANNER/test")
    assert response.status_code == 400
    assert "not connected" in response.json()["detail"]


def test_a_form_driven_source_refuses_selector_edits_with_the_reason(client):
    """Accepting them would save a repair that cannot take effect, then report
    success - the worst of both."""
    api, _ = client
    letuska = api.get("/api/sources").json()["LETUSKA"]
    response = api.put("/api/sources", json={"LETUSKA": letuska})
    assert response.status_code == 400
    assert "no selectors to edit" in response.json()["detail"]


def test_a_source_is_unchecked_until_it_has_been_checked(client):
    api, _ = client
    assert api.get("/api/sources").json()["PELIKAN"]["last_check"] is None


def test_a_check_is_remembered_so_the_card_shows_a_state_on_load(client, monkeypatch):
    """A card that says "unknown until you press the button" is a card you have
    to press a button on to learn anything."""
    from pathlib import Path

    import src.web.app as app_module

    fixture = (
        Path(__file__).parent / "fixtures" / "pelikan_results.html"
    ).read_text(encoding="utf-8")
    monkeypatch.setattr(app_module, "_fetch_for_test", lambda source, url: (200, url, fixture))

    api, _ = client
    fresh = api.post("/api/sources/PELIKAN/test").json()
    assert fresh["checked_at"]

    remembered = api.get("/api/sources").json()["PELIKAN"]["last_check"]
    assert remembered["ok"] == fresh["ok"]
    assert remembered["checked_at"] == fresh["checked_at"]


def test_checking_one_source_does_not_erase_another_check(client, monkeypatch):
    from src.sources import load_checks, save_check

    _, data = client
    save_check("LETUSKA", {"ok": True, "message": "fine"}, data)
    save_check("PELIKAN", {"ok": False, "message": "broken"}, data)
    assert set(load_checks(data)) == {"LETUSKA", "PELIKAN"}


def test_saving_one_source_does_not_revert_another_to_its_defaults(client):
    """Sources are sent one at a time now, so a whole-file overwrite would blank
    every source the page was not editing."""
    api, data = client
    pelikan = api.get("/api/sources").json()["PELIKAN"]
    pelikan["selectors"]["card"] = "div.offer"
    api.put("/api/sources", json={"PELIKAN": pelikan})

    on_disk = json.loads((data / "sources.json").read_text(encoding="utf-8"))
    assert set(on_disk) == {"PELIKAN", "LETUSKA", "SKYSCANNER"}
    assert on_disk["LETUSKA"]["role"] == "check"


# ---------------------------------------------------------------- the watch
#
# Picking days to watch, seeing how they have moved, and the guard that keeps
# the plan inside what pelikan.cz will answer.


def test_candidates_offer_the_cheapest_trip_per_departure_date(client):
    """The Watch tab's source list: what the last sweep found, day by day."""
    api, data = client
    stamp = seed_sweep(data)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/candidates").json()

    assert body["candidates"][0]["depart_date"] == "2027-01-10"
    # The whole point of the endpoint: the leg dates, so a pick can be pinned.
    assert body["candidates"][0]["depart_dates"] == ["2027-01-10", "2027-01-20", "2027-01-30"]
    assert body["candidates"][0]["total"] == 27000


def test_a_trip_starts_out_watching_nothing(client):
    api, _ = client
    body = api.get("/api/watch/jp-ph").json()
    assert body["preferences"] == []
    assert body["searches"] == 0


def test_adding_a_day_writes_it_to_the_trip(client):
    api, _ = client
    response = api.post(
        "/api/watch/jp-ph",
        json={"depart_dates": ["2027-01-10", "2027-01-20", "2027-01-30"], "added_price": 27000},
    )
    assert response.status_code == 201
    trip = api.get("/api/scenarios/jp-ph").json()
    saved = trip["preferences"][0]
    assert saved["depart_dates"] == ["2027-01-10", "2027-01-20", "2027-01-30"]
    assert saved["added_price"] == 27000
    assert saved["added_at"]
    assert saved["slack_days"] == DEFAULT_SLACK_DAYS


def test_adding_a_day_reports_what_watching_it_will_cost(client):
    api, _ = client
    body = api.post(
        "/api/watch/jp-ph",
        json={"depart_dates": ["2027-01-10", "2027-01-20", "2027-01-30"], "slack_days": 0},
    ).json()
    # 2 origins x 1 Japanese airport, 1 x 1, 1 x 2 = 2 + 1 + 2 per preference.
    assert body["searches"] == 5
    assert body["minutes"] > 0


def test_slack_multiplies_what_a_preference_costs(client):
    """Five dates a leg instead of one, and the badge has to say so.

    The count is what the cap is applied to, so a preference whose cost the
    payload under-reported would be one the page offered to add and the run
    could not afford.
    """
    api, _ = client
    body = api.post(
        "/api/watch/jp-ph",
        json={"depart_dates": ["2027-01-10", "2027-01-20", "2027-01-30"], "slack_days": 2},
    ).json()
    assert body["searches"] == 5 * 5
    assert body["preferences"][0]["slack_days"] == 2


def test_a_hand_picked_day_outside_the_stays_may_still_be_followed(client):
    """Four days in Japan against a 9-11 stay: accepted, and priced.

    See `test_a_watch_may_break_the_stay_windows`. What is still refused is a
    chain that runs backwards, which is an impossibility rather than a taste.
    """
    api, _ = client
    assert api.post(
        "/api/watch/jp-ph",
        json={"depart_dates": ["2027-01-10", "2027-01-14", "2027-01-24"]},
    ).status_code == 201

    backwards = api.post(
        "/api/watch/jp-ph",
        json={"depart_dates": ["2027-01-20", "2027-01-14", "2027-01-24"]},
    )
    assert backwards.status_code == 400
    assert "order" in backwards.json()["detail"]


def test_watching_more_than_the_site_will_answer_is_refused(client, monkeypatch):
    """The cap is on searches, not on days.

    A trip with twenty routes reaches the cliff in three candidates where a
    two-route one would not reach it in ten, and running past it is silent:
    pelikan.cz simply stops answering part way and the run reports a price
    found by looking at half of what it planned to.
    """
    import src.web.app as app_module

    monkeypatch.setattr(app_module, "WATCH_SEARCH_CAP", 6)
    api, _ = client
    api.post(
        "/api/watch/jp-ph",
        json={"depart_dates": ["2027-01-10", "2027-01-20", "2027-01-30"], "slack_days": 0},
    )
    response = api.post(
        "/api/watch/jp-ph",
        json={"depart_dates": ["2027-01-12", "2027-01-22", "2027-02-01"], "slack_days": 0},
    )
    assert response.status_code == 400
    assert "10" in response.json()["detail"]  # the count it would have reached


def test_a_watched_day_can_be_dropped(client):
    api, _ = client
    api.post("/api/watch/jp-ph", json={"depart_dates": ["2027-01-10", "2027-01-20", "2027-01-30"]})
    assert api.delete("/api/watch/jp-ph/2027-01-10").status_code == 200
    assert api.get("/api/scenarios/jp-ph").json()["preferences"] == []


def test_dropping_a_day_that_is_not_watched_is_a_404(client):
    api, _ = client
    assert api.delete("/api/watch/jp-ph/2027-01-10").status_code == 404


def test_the_watch_reports_the_series_it_has_recorded(client):
    api, data = client
    api.post(
        "/api/watch/jp-ph",
        json={"depart_dates": ["2027-01-10", "2027-01-20", "2027-01-30"], "added_price": 30000},
    )

    directory = data / "watch" / "jp-ph"
    directory.mkdir(parents=True)
    with (directory / "observations.jsonl").open("w", encoding="utf-8") as handle:
        for ts, total in (("2026-08-20T02:00:00+00:00", 30000), ("2026-08-20T06:00:00+00:00", 28500)):
            handle.write(json.dumps({
                "ts": ts, "scenario_id": "jp-ph", "depart_date": "2027-01-10",
                "pinned_dates": ["2027-01-10", "2027-01-20", "2027-01-30"],
                "found_dates": ["2027-01-10", "2027-01-20", "2027-01-30"],
                "route": "PRG → NRT → MNL → PRG", "total": total, "total_with_bags": total,
                "currency": "CZK", "has_overland": False, "coverage": 1.0,
                "legs_per_search": 9.5, "comparable": True,
            }) + "\n")

    body = api.get("/api/watch/jp-ph").json()
    candidate = body["preferences"][0]
    assert candidate["depart_date"] == "2027-01-10"
    assert candidate["latest"] == 28500
    assert candidate["net_change"] == -1500
    assert [point["total"] for point in candidate["series"]] == [30000, 28500]
    # Carried through from the trip so the tab can say "down 1,500 since you
    # picked it" rather than only "down since the first observation".
    assert candidate["added_price"] == 30000


# ------------------------------------------- a preference brings its legs along
#
# Following a trip follows each of its flights, because that is how the decision
# is actually read: the trip line says whether to keep waiting, the leg lines say
# which flight is the one moving. They cost nothing extra - `plan_watch` dedupes
# them against the preference's own searches.


PREF = {
    "depart_dates": ["2027-01-10", "2027-01-20", "2027-01-30"],
    "routes": [
        {"origin": "PRG", "destination": "NRT", "price": 12000},
        {"origin": "NRT", "destination": "MNL", "price": 4000},
        {"origin": "MNL", "destination": "PRG", "price": 14000},
    ],
}


def test_saving_a_preference_follows_each_of_its_flights(client):
    api, _ = client
    body = api.post("/api/watch/jp-ph", json=PREF).json()
    assert [leg["key"] for leg in body["legs"]] == [
        "PRG-NRT@2027-01-10", "NRT-MNL@2027-01-20", "MNL-PRG@2027-01-30"
    ]
    # Tagged with the preference that brought them, so the table can say so and
    # can suppress an unfollow that would leave the two lists disagreeing.
    assert {leg["source"] for leg in body["legs"]} == {"2027-01-10"}


def test_a_preferences_legs_cost_nothing_on_top_of_it(client):
    """The dedup is the whole reason this is affordable.

    Every route/date a preference's legs name is one the preference already
    searches, so following them adds no searches at all.
    """
    api, _ = client
    with_legs = api.post("/api/watch/jp-ph", json={**PREF, "slack_days": 0}).json()["searches"]
    api.delete("/api/watch/jp-ph/2027-01-10")
    bare = api.post(
        "/api/watch/jp-ph",
        json={"depart_dates": PREF["depart_dates"], "slack_days": 0},
    ).json()["searches"]
    assert with_legs == bare


def test_dropping_a_preference_drops_its_legs_and_leaves_yours(client):
    """Its own legs only.

    A route you picked by hand was asked for independently of any trip, and
    un-picking the trip is not a request to stop following it.
    """
    api, _ = client
    api.post(
        "/api/watch/jp-ph/legs",
        json={"origin": "FRA", "destination": "NRT", "depart_date": "2027-01-15"},
    )
    api.post("/api/watch/jp-ph", json=PREF)

    body = api.delete("/api/watch/jp-ph/2027-01-10").json()
    assert body["preferences"] == []
    assert [leg["key"] for leg in body["legs"]] == ["FRA-NRT@2027-01-15"]


def test_a_flight_you_already_followed_stays_yours(client):
    """One flight, one row, one series.

    Adding it a second time under the preference would give one ticket two rows
    writing to the same series key - and dropping the preference would then take
    a flight you had picked yourself with it.
    """
    api, _ = client
    api.post(
        "/api/watch/jp-ph/legs",
        json={"origin": "PRG", "destination": "NRT", "depart_date": "2027-01-10"},
    )
    body = api.post("/api/watch/jp-ph", json=PREF).json()

    rows = [leg for leg in body["legs"] if leg["key"] == "PRG-NRT@2027-01-10"]
    assert len(rows) == 1
    assert rows[0]["source"] == ""


def test_moving_a_preference_takes_its_legs_with_it(client):
    """What the "two days later is cheaper" line does.

    A move rather than a new preference: the series belongs to this decision,
    and starting a fresh one on every shift would leave you unable to see that
    the trip has fallen since you began. The key moves with the first date, so
    the legs are re-pointed in the same write or they are orphans no deletion
    could find.
    """
    api, _ = client
    api.post("/api/watch/jp-ph", json=PREF)
    body = api.patch(
        "/api/watch/jp-ph/2027-01-10",
        json={"depart_dates": ["2027-01-12", "2027-01-22", "2027-02-01"]},
    ).json()

    assert body["preferences"][0]["depart_date"] == "2027-01-12"
    assert [leg["key"] for leg in body["legs"]] == [
        "PRG-NRT@2027-01-12", "NRT-MNL@2027-01-22", "MNL-PRG@2027-02-01"
    ]
    assert {leg["source"] for leg in body["legs"]} == {"2027-01-12"}


def test_a_preference_can_be_renamed_and_its_slack_changed(client):
    api, _ = client
    api.post("/api/watch/jp-ph", json={**PREF, "slack_days": 0})
    body = api.patch(
        "/api/watch/jp-ph/2027-01-10", json={"label": "the good one", "slack_days": 2}
    ).json()
    assert body["preferences"][0]["label"] == "the good one"
    assert body["preferences"][0]["slack_days"] == 2
    # And the badge moves with it, because the cap is applied to this figure.
    assert body["searches"] == 25


def test_widening_the_slack_past_what_the_site_answers_is_refused(client, monkeypatch):
    """The cap binds on a change, not only on an addition.

    A preference added inside the budget and then widened would otherwise walk
    straight past the cliff, which is silent: the site simply stops answering
    part way through.
    """
    import src.web.app as app_module

    monkeypatch.setattr(app_module, "WATCH_SEARCH_CAP", 6)
    api, _ = client
    api.post("/api/watch/jp-ph", json={**PREF, "slack_days": 0})
    response = api.patch("/api/watch/jp-ph/2027-01-10", json={"slack_days": 2})
    assert response.status_code == 400
    assert "25" in response.json()["detail"]


# ------------------------------------------------------- results: narrowing
#
# Applied server-side and to the *whole* traversal, not to the fifty rows the
# endpoint returns. `_combination` combines with limit=None, so filtering in the
# browser would narrow the fifty cheapest while the headline cards above them
# still came from all of them - a table and a summary describing two different
# populations, which is the failure mode this codebase keeps designing against.


def _bag_legs():
    """Two ways home from Manila: the cheaper one has no confirmed bag.

    Built so the bag filter and the price ranking disagree. A filter that
    happened to keep the cheapest trip anyway would pass on nothing.
    """
    return [
        Leg("T", "PRG", "NRT", date(2027, 1, 10), "QR", None, 1, "CZK", 12000.0, "u",
            checked_bag=True),
        Leg("T", "VIE", "NRT", date(2027, 1, 10), "OS", None, 1, "CZK", 13000.0, "u",
            checked_bag=True),
        Leg("T", "NRT", "MNL", date(2027, 1, 20), "PR", None, 0, "CZK", 4000.0, "u",
            checked_bag=True),
        Leg("T", "MNL", "PRG", date(2027, 1, 30), "QR", None, 1, "CZK", 9000.0, "u",
            checked_bag=None),
        Leg("T", "MNL", "PRG", date(2027, 1, 30), "EK", None, 1, "CZK", 14000.0, "u",
            checked_bag=True),
        Leg("T", "MNL", "VIE", date(2027, 1, 30), "EY", None, 1, "CZK", 11000.0, "u",
            checked_bag=True),
    ]


def test_results_can_be_narrowed_to_one_departure_airport(client):
    api, data = client
    stamp = seed_sweep(data, legs=_bag_legs())
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results?from_airport=VIE").json()
    assert body["itineraries"]
    assert {i["legs"][0]["origin"] for i in body["itineraries"]} == {"VIE"}


def test_results_can_be_narrowed_to_one_return_airport(client):
    api, data = client
    stamp = seed_sweep(data, legs=_bag_legs())
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results?to_airport=VIE").json()
    assert body["itineraries"]
    assert {i["legs"][-1]["destination"] for i in body["itineraries"]} == {"VIE"}


def test_bags_filter_keeps_only_trips_where_every_leg_confirms_one(client):
    api, data = client
    stamp = seed_sweep(data, legs=_bag_legs())
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results?bags=true").json()
    assert body["itineraries"]
    assert all(i["bags_needed"] == 0 for i in body["itineraries"])


def test_bags_filter_drops_the_cheapest_trip_when_its_bag_is_unknown(client):
    """`checked_bag is None` means the site never said, not that a bag is free.

    The 9,000 PRG return is the cheapest way home and its bag is unknown, so a
    bag-inclusive view must not report it - and must not report its total as the
    headline either.
    """
    api, data = client
    stamp = seed_sweep(data, legs=_bag_legs())
    unfiltered = api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()
    filtered = api.get(f"/api/sweeps/jp-ph/{stamp}/results?bags=true").json()
    # PRG -> NRT -> MNL -> PRG on the 9,000 return, whose bag is unknown.
    assert unfiltered["best_same_airport"]["total_price"] == 25000  # 12000+4000+9000
    # The unknown-bag return is gone, and the cheapest closed trip left is the
    # Vienna one rather than Prague on the dearer 14,000 return.
    assert filtered["best_same_airport"]["total_price"] == 28000    # 13000+4000+11000
    assert filtered["best_same_airport"]["legs"][0]["origin"] == "VIE"


def test_headline_cards_follow_the_filter(client):
    """Otherwise the cards summarise a population the table below does not show."""
    api, data = client
    stamp = seed_sweep(data, legs=_bag_legs())
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results?from_airport=VIE").json()
    for card in ("best_same_airport", "best_open_jaw"):
        if body[card]:
            assert body[card]["legs"][0]["origin"] == "VIE"


def test_results_say_whether_anything_is_being_hidden(client):
    """`narrowed`, never "matched out of N".

    Pruning makes an unfiltered traversal a different set, not a superset: it
    drops the Vienna trips because a cheaper Prague trip shares their departure
    date, while a Vienna-only traversal keeps them. So a filtered count can
    exceed an unfiltered one, and a ratio of the two would be a fraction that is
    not a fraction.
    """
    api, data = client
    stamp = seed_sweep(data, legs=_bag_legs())
    everything = api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()
    narrowed = api.get(f"/api/sweeps/jp-ph/{stamp}/results?from_airport=VIE").json()

    assert everything["narrowed"] is False
    assert narrowed["narrowed"] is True
    assert narrowed["matched"] > 0
    # What the unfiltered view costs, so the tab can price the preference.
    assert narrowed["cheapest_unfiltered"] == everything["itineraries"][0]["total_with_bags"]


def test_results_offer_the_airports_actually_present(client):
    """The dropdowns must offer what this sweep found, not what the trip declares.

    A trip listing four origins whose sweep only ever chained two would
    otherwise offer two options that always return nothing.
    """
    api, data = client
    stamp = seed_sweep(data, legs=_bag_legs())
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()
    assert body["start_airports"] == ["PRG", "VIE"]
    assert body["end_airports"] == ["PRG", "VIE"]


def test_an_impossible_filter_returns_nothing_rather_than_everything(client):
    api, data = client
    stamp = seed_sweep(data, legs=_bag_legs())
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results?from_airport=ZZZ").json()
    assert body["itineraries"] == []
    assert body["matched"] == 0
    assert body["narrowed"] is True
    assert body["best_same_airport"] is None
    assert body["best_open_jaw"] is None


# ------------------------------------------------------------- watched legs
#
# A trip watch prices every airport pair of every leg on its pinned dates; a leg
# watch prices exactly one route on one day. They share a budget, because they
# share a run and the site answers about 120 searches from one runner.


def watch_a_leg(api, **overrides):
    body = {"origin": "PRG", "destination": "NRT", "depart_date": "2027-01-10"}
    body.update(overrides)
    return api.post("/api/watch/jp-ph/legs", json=body)


def test_following_a_leg_records_it_on_the_trip(client):
    api, _ = client
    body = watch_a_leg(api).json()
    assert [leg["key"] for leg in body["legs"]] == ["PRG-NRT@2027-01-10"]
    assert body["legs"][0]["route"] == "PRG→NRT"
    assert body["legs"][0]["off_trip"] is False


def test_a_watched_leg_costs_one_search(client):
    api, _ = client
    before = api.get("/api/watch/jp-ph").json()["searches"]
    after = watch_a_leg(api).json()["searches"]
    assert after == before + 1


def test_a_route_the_trip_never_searches_is_allowed_and_flagged(client):
    """Picking freely is the whole point, so it is said rather than refused."""
    api, _ = client
    body = watch_a_leg(api, origin="VIE", destination="DPS").json()
    assert body["legs"][0]["off_trip"] is True


def test_a_leg_and_the_trip_share_one_search_budget(client):
    """Two panels each looking affordable while the run they add up to is not."""
    api, _ = client
    body = watch_a_leg(api).json()
    assert body["cap"] == 110
    assert body["searches"] <= body["cap"]


def test_the_same_flight_cannot_be_followed_twice(client):
    api, _ = client
    assert watch_a_leg(api).status_code == 201
    refused = watch_a_leg(api)
    assert refused.status_code == 400
    assert "already being watched" in refused.json()["detail"]


def test_a_flight_from_an_airport_to_itself_is_refused(client):
    api, _ = client
    refused = watch_a_leg(api, destination="PRG")
    assert refused.status_code == 400
    assert "itself" in refused.json()["detail"]


def test_a_mistyped_year_is_refused_rather_than_watched_forever(client):
    """Outside the window it reports "nothing found" every four hours for good."""
    api, _ = client
    refused = watch_a_leg(api, depart_date="2028-01-10")
    assert refused.status_code == 400
    assert "outside the window" in refused.json()["detail"]


def test_a_final_leg_just_past_the_window_is_still_allowed(client):
    """The site substitutes nearby dates, so the way home legitimately departs
    after the window closes."""
    api, _ = client
    assert watch_a_leg(api, origin="MNL", depart_date="2027-02-10").status_code == 201


def test_a_leg_code_that_is_not_an_airport_is_refused(client):
    api, _ = client
    refused = watch_a_leg(api, destination="Tokyo")
    assert refused.status_code == 400
    assert "IATA" in refused.json()["detail"]


def test_a_followed_leg_survives_a_reload(client):
    api, _ = client
    watch_a_leg(api)
    assert len(api.get("/api/watch/jp-ph").json()["legs"]) == 1


def test_unfollowing_a_leg_leaves_the_others(client):
    api, _ = client
    watch_a_leg(api)
    watch_a_leg(api, depart_date="2027-01-12")
    body = api.delete("/api/watch/jp-ph/legs/PRG-NRT@2027-01-10").json()
    assert [leg["key"] for leg in body["legs"]] == ["PRG-NRT@2027-01-12"]


def test_unfollowing_something_not_followed_is_a_404(client):
    api, _ = client
    assert api.delete("/api/watch/jp-ph/legs/PRG-NRT@2027-01-10").status_code == 404


def test_the_price_it_was_picked_at_travels_with_the_pick(client):
    """So the very first check can say which way it has gone."""
    api, _ = client
    body = watch_a_leg(api, added_price=12000).json()
    assert body["legs"][0]["added_price"] == 12000


def test_checking_now_is_refused_only_when_nothing_at_all_is_followed(client):
    api, _ = client
    assert api.post("/api/watch/jp-ph/run").status_code == 400
    watch_a_leg(api)
    # Not started for real here - the point is that it is no longer refused for
    # having nothing to do.
    assert api.post("/api/watch/jp-ph/run").status_code != 400


# ------------------------------------------------- how much the probe answered
#
# Results and Watch both caveat a run that fell short of its plan. Explore did
# not, and it is the tab that draws conclusions: a 25%-answered probe called
# airports "poor" in exactly the words a complete one uses.


def seed_partial_explore(data_dir, stamp="2026-08-11T10-00-00Z", answered=31, planned=123):
    directory = data_dir / "sweeps" / "jp-ph" / stamp
    directory.mkdir(parents=True)
    (directory / "legs.jsonl").write_text("")
    (directory / "status.json").write_text(
        json.dumps(
            {
                "state": "throttled",
                "mode": "explore",
                "total": planned,
                "answered": answered,
                "planned": planned,
                "coverage": round(answered / planned, 4),
                "route_searches": {route: 1 for route in ROUTES},
                "route_errors": {route: 0 for route in ROUTES},
            }
        )
    )
    return stamp


def test_the_explore_report_says_how_much_of_its_plan_was_answered(client):
    api, data = client
    stamp = seed_partial_explore(data)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/explore").json()
    assert body["coverage"] == 0.252
    assert body["answered"] == 31
    assert body["planned"] == 123


def test_a_complete_probe_reports_full_coverage(client):
    """So the banner can stay quiet rather than nagging on every good run."""
    api, data = client
    stamp = seed_partial_explore(data, stamp="2026-08-11T11-00-00Z", answered=123)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/explore").json()
    assert body["coverage"] == 1.0


def test_a_probe_from_before_coverage_was_recorded_does_not_claim_completeness(client):
    """None must not render as 100%: it means "not known", not "all answered"."""
    api, data = client
    stamp = seed_explore(data, stamp="2026-08-11T12-00-00Z")
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/explore").json()
    assert body["coverage"] is None


# ------------------------------------------------------------------ resuming
#
# A run refused at 80 of 126 has 80 answers worth keeping. Re-asking them to buy
# the remaining 46 spends the budget that ran out in the first place.


def seed_incomplete(data_dir, stamp="2026-08-21T09-55-40Z", state="throttled",
                    answered=80, planned=126):
    directory = data_dir / "sweeps" / "jp-ph" / stamp
    directory.mkdir(parents=True)
    (directory / "legs.jsonl").write_text("")
    (directory / "searches.jsonl").write_text("")
    (directory / "status.json").write_text(
        json.dumps(
            {
                "state": state, "mode": "explore", "total": planned,
                "completed": answered, "answered": answered, "planned": planned,
                "coverage": round(answered / planned, 4),
            }
        )
    )
    return stamp


def test_an_unfinished_run_offers_to_be_carried_on(client):
    api, data = client
    stamp = seed_incomplete(data)
    row = next(
        s for s in api.get("/api/sweeps/jp-ph").json()["sweeps"] if s["stamp"] == stamp
    )
    assert row["resumable"] is True
    assert row["left_to_ask"] == 46


def test_a_finished_run_does_not_offer_to_be_carried_on(client):
    api, data = client
    stamp = seed_incomplete(data, stamp="2026-08-21T08-00-00Z", state="done", answered=126)
    row = next(
        s for s in api.get("/api/sweeps/jp-ph").json()["sweeps"] if s["stamp"] == stamp
    )
    assert row["resumable"] is False


def test_resuming_a_run_that_does_not_exist_is_a_404(client):
    api, _ = client
    assert api.post("/api/scenarios/jp-ph/resume?stamp=2026-01-01T00-00-00Z").status_code == 404


def test_resuming_a_finished_run_is_refused_with_a_reason(client):
    api, data = client
    stamp = seed_incomplete(data, stamp="2026-08-21T08-00-00Z", state="done", answered=126)
    refused = api.post(f"/api/scenarios/jp-ph/resume?stamp={stamp}")
    assert refused.status_code == 400
    assert "nothing left" in refused.json()["detail"].lower()


def test_resuming_while_a_run_is_going_is_refused(client, monkeypatch):
    api, data = client
    stamp = seed_incomplete(data)
    import src.web.app as module

    monkeypatch.setattr(module, "_is_running", lambda _id: True)
    assert api.post(f"/api/scenarios/jp-ph/resume?stamp={stamp}").status_code == 409


def test_carrying_on_a_run_of_a_different_trip_is_refused(client):
    """The answers would be for airports the trip no longer has.

    Resuming copies the earlier run's flights into the new directory and stamps
    it with the trip as it stands now. If the trip was edited in between, that
    directory would claim to have searched airports it never asked about - the
    exact reading that made two probes report the wrong trip.
    """
    api, data = client
    stamp = seed_incomplete(data)
    # The run recorded which trip it searched, and it is not this one.
    searched = json.loads(api.get("/api/scenarios/jp-ph").text)
    searched["stops"][0]["airports"] = ["KIX"]
    (data / "sweeps" / "jp-ph" / stamp / "scenario.json").write_text(json.dumps(searched))

    refused = api.post(f"/api/scenarios/jp-ph/resume?stamp={stamp}")
    assert refused.status_code == 400
    assert "different" in refused.json()["detail"].lower()


def test_carrying_on_a_run_of_the_same_trip_is_allowed(client, monkeypatch):
    api, data = client
    stamp = seed_incomplete(data)
    searched = json.loads(api.get("/api/scenarios/jp-ph").text)
    (data / "sweeps" / "jp-ph" / stamp / "scenario.json").write_text(json.dumps(searched))

    # Stubbed, because the real one launches a browser at pelikan.cz. What is
    # being tested is that the request is accepted and told what to carry on
    # from, not that a sweep runs.
    import src.web.app as module

    asked: dict = {}
    monkeypatch.setattr(module, "run_sweep", lambda *a, **kw: asked.update(kw))

    started = api.post(f"/api/scenarios/jp-ph/resume?stamp={stamp}")
    assert started.status_code == 200
    assert started.json()["searches"] == 46
    assert asked["resume_from"].name == stamp


# ----------------------------------------------------------------- night sweep
#
# The nightly cloud sweep is the only place a sweep of this trip has ever
# finished whole - 483/483 in 19 minutes on 21 Aug, against three throttled
# local runs the same morning. It was also completely invisible from the page:
# nothing said which trips it would run, how many runners they would be split
# across, when it fires, or - the one that actually bit - that the trip it
# sweeps is the trip on the branch, not the trip on this screen.


def enable(client_dir, **overrides):
    """Save a trip the nightly sweep would pick up."""
    defaults = dict(
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
        enabled=True,
    )
    defaults.update(overrides)
    scenario = Scenario(**defaults)
    save_scenario(scenario, client_dir)
    return scenario


def night(api) -> dict:
    response = api.get("/api/night-sweep")
    assert response.status_code == 200
    return response.json()


def trip_named(body, scenario_id) -> dict:
    return next(t for t in body["trips"] if t["id"] == scenario_id)


def test_the_night_sweep_says_which_trips_it_will_run(client, tmp_path):
    api, _ = client
    enable(tmp_path / "scenarios")
    body = night(api)
    assert trip_named(body, "jp-ph")["included"] is True


def test_a_trip_left_out_is_listed_as_left_out_rather_than_hidden(client, tmp_path):
    """Hiding it is how a trip comes to be quietly unswept for a fortnight.
    Both of this repo's trips sat `enabled: false` while their owner watched
    for nightly results."""
    api, _ = client
    enable(tmp_path / "scenarios", enabled=False)
    assert trip_named(night(api), "jp-ph")["included"] is False


def test_each_trip_reports_the_runners_its_plan_needs(client, tmp_path):
    api, _ = client
    enable(tmp_path / "scenarios", depth="deep")
    trip = trip_named(night(api), "jp-ph")
    assert trip["searches"] > 100
    assert trip["runners"] == -(-trip["searches"] // 100)


def test_a_trip_is_sized_for_the_depth_the_night_sweep_forces(client, tmp_path):
    """A schedule supplies no depth input, so the workflow default wins over
    whatever the trip is saved as. Sizing from the file reported a plan seven
    times smaller than the one that would really run."""
    api, _ = client
    enable(tmp_path / "scenarios", depth="quick")
    body = night(api)
    trip = trip_named(body, "jp-ph")
    assert trip["saved_depth"] == "quick"
    assert trip["depth"] == body["forced_depth"] == "deep"
    assert trip["searches"] > 100, "sized from the file rather than the workflow"


def test_a_genuinely_small_trip_reports_one_runner(client, tmp_path):
    api, _ = client
    enable(
        tmp_path / "scenarios",
        origins=["PRG"],
        stops=[Stop(airports=["NRT"], stay_days=(9, 11), label="Japan")],
        window_end=date(2027, 1, 12),
    )
    trip = trip_named(night(api), "jp-ph")
    assert trip["searches"] <= 100
    assert trip["runners"] == 1


def test_the_schedule_comes_from_the_workflow_that_runs_it(client):
    """Two copies of a cron drift, and the one on screen is the one nobody can
    check against Actions."""
    api, _ = client
    schedule = night(api)["schedule"]
    assert [slot["cron"] for slot in schedule]
    assert all(slot["next"] for slot in schedule)
    assert any(slot["mode"] == "final" for slot in schedule), "the final slot is missing"
    assert any(slot["mode"] == "sweep" for slot in schedule), "the broad slot is missing"


def test_a_trip_the_cloud_has_never_seen_says_so(client, tmp_path):
    """A tmp scenario directory is not on any branch, which is the same state
    as a trip you have created and not pushed."""
    api, _ = client
    enable(tmp_path / "scenarios")
    assert trip_named(night(api), "jp-ph")["cloud"]["known"] is False


def test_a_trip_that_matches_the_cloud_reports_no_difference(client, tmp_path, monkeypatch):
    import src.web.app as app_module

    live = enable(tmp_path / "scenarios")
    monkeypatch.setattr(app_module, "_cloud_scenario", lambda _id: live)
    api, _ = client
    cloud = trip_named(night(api), "jp-ph")["cloud"]
    assert cloud["known"] is True
    assert cloud["differs"] == []


def test_a_trip_the_cloud_sweeps_differently_names_what_differs(client, tmp_path, monkeypatch):
    """The live trap on 21 Aug. The nightly sweep was searching three origins
    and two Philippine airports, 483 searches, while the screen showed a
    one-origin trip with the Japan crossing pinned - 66 searches. Nothing said
    so, and the results being read were of the other trip."""
    import src.web.app as app_module

    enable(tmp_path / "scenarios", origins=["PRG"], depth="quick")
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
        depth="deep",
        enabled=True,
    )
    monkeypatch.setattr(app_module, "_cloud_scenario", lambda _id: wider)
    api, _ = client

    body = night(api)
    trip = trip_named(body, "jp-ph")
    assert trip["cloud"]["known"] is True
    assert "the airports it searches" in trip["cloud"]["differs"]
    # Depth is *not* a difference while the workflow forces one: both copies get
    # swept at the same depth, and flagging it would be a false alarm on the one
    # panel whose job is to be trusted.
    assert body["forced_depth"], "the workflow no longer forces a depth"
    assert "how finely it prices" not in trip["cloud"]["differs"]
    # What the cloud will actually spend the night doing, not what this screen
    # would cost. That difference is the whole point of saying it: on 21 Aug it
    # was 483 searches against the 66 the page was showing.
    assert trip["cloud"]["searches"] > trip["searches"]


def test_a_trip_enabled_here_but_not_in_the_cloud_says_that_too(client, tmp_path, monkeypatch):
    import src.web.app as app_module

    live = enable(tmp_path / "scenarios", enabled=True)
    monkeypatch.setattr(
        app_module, "_cloud_scenario", lambda _id: replace(live, enabled=False)
    )
    api, _ = client
    assert "whether it runs at all" in trip_named(night(api), "jp-ph")["cloud"]["differs"]


# ------------------------------------------------------------------- the cloud
#
# Dispatching used to be fire-and-forget: `gh workflow run`, `{"dispatched":
# true}`, and no idea afterwards. On 22 Aug that hid two different failures at
# once - three runs that swept nothing and went green, and two cancelled while
# pending - behind one identical message.


@pytest.fixture
def cloud(monkeypatch):
    """A cloud that is reachable, idle, and records what it was asked to do."""
    from src.web import cloud_runs

    cloud_runs._queue.clear()
    sent: list[tuple] = []
    monkeypatch.setattr(cloud_runs, "dispatch", lambda *a: sent.append(a))
    monkeypatch.setattr(cloud_runs, "lane_is_busy", lambda *a: False)
    monkeypatch.setattr(cloud_runs, "list_runs", lambda *a, **k: [])
    yield sent
    cloud_runs._queue.clear()


def agreeing(monkeypatch, differs=()):
    """Make the branch copy of the trip agree, or differ in named ways."""
    import src.web.app as app_module

    monkeypatch.setattr(app_module, "_fetch_cloud_ref", lambda: None)
    monkeypatch.setattr(
        app_module,
        "_cloud_state",
        lambda *a: {"known": True, "differs": list(differs), "included": True,
                    "searches": 66},
    )


def test_a_cloud_run_of_a_committed_trip_is_dispatched(client, cloud, monkeypatch):
    app, _ = client
    agreeing(monkeypatch)
    body = app.post("/api/scenarios/jp-ph/run-cloud?depth=deep").json()
    assert body["dispatched"] is True
    assert cloud == [("jp-ph", "deep", "sweep")]


def test_a_cloud_run_at_a_depth_that_is_not_a_depth_is_refused(client, cloud, monkeypatch):
    """`run` and `estimate` get this free by validating the scenario they build
    with it. This endpoint builds none - it hands the string to `gh workflow
    run` as an input - so nothing checked it, and an unrecognised depth was
    spent in Actions rather than refused here."""
    app, _ = client
    agreeing(monkeypatch)
    response = app.post("/api/scenarios/jp-ph/run-cloud?depth=thorough")
    assert response.status_code == 400
    assert "thorough" in response.json()["detail"]
    assert cloud == []


def test_a_probe_is_dispatched_to_the_cloud_like_a_sweep(client, cloud, monkeypatch):
    """It always could be - `explore` is in MODES, `_checked_mode` accepts it and
    the workflow forwards the mode verbatim. The button ran it here instead, and
    a probe is ~51 searches against the ~120 this machine gets in a day."""
    app, _ = client
    agreeing(monkeypatch)
    body = app.post("/api/scenarios/jp-ph/run-cloud?mode=explore&depth=deep").json()
    assert body["dispatched"] is True
    assert body["mode"] == "explore"
    assert cloud == [("jp-ph", "deep", "explore")]


def test_a_cloud_probe_of_an_uncommitted_trip_is_refused_by_name(client, cloud, monkeypatch):
    """The cloud probes the branch's airports, not the screen's. Refused here
    rather than as a red run in Actions twenty seconds from now."""
    app, _ = client
    agreeing(monkeypatch, differs=["the airports it searches"])
    response = app.post("/api/scenarios/jp-ph/run-cloud?mode=explore")
    assert response.status_code == 400
    assert "the airports it searches" in response.json()["detail"]
    assert cloud == []


def test_a_cloud_run_is_refused_when_the_branch_holds_a_different_trip(client, cloud, monkeypatch):
    """The cloud sweeps the branch, not the screen. A whole day was spent
    reading results of a trip nobody was still planning."""
    app, _ = client
    agreeing(monkeypatch, differs=["the airports it searches", "the date window"])
    response = app.post("/api/scenarios/jp-ph/run-cloud")
    assert response.status_code == 400
    assert "the airports it searches" in response.json()["detail"]
    assert cloud == []


def test_a_cloud_run_is_refused_when_the_branch_cannot_be_read(client, cloud, monkeypatch):
    """Cannot say is not the same as agrees, and must not be dispatched as if
    it were."""
    app, _ = client
    import src.web.app as app_module

    monkeypatch.setattr(app_module, "_fetch_cloud_ref", lambda: None)
    monkeypatch.setattr(
        app_module, "_cloud_state",
        lambda *a: {"known": False, "differs": [], "included": None, "searches": None},
    )
    assert app.post("/api/scenarios/jp-ph/run-cloud").status_code == 400
    assert cloud == []


def test_running_it_anyway_dispatches_without_consulting_the_branch(client, cloud, monkeypatch):
    """The escape hatch has to actually escape, or the gate is a wall."""
    app, _ = client
    import src.web.app as app_module

    def must_not_run():
        raise AssertionError("forced run still went to the network")

    monkeypatch.setattr(app_module, "_fetch_cloud_ref", must_not_run)
    body = app.post("/api/scenarios/jp-ph/run-cloud?force=true").json()
    assert body["dispatched"] is True
    assert cloud == [("jp-ph", None, "sweep")]


def test_a_run_asked_for_while_the_lane_is_busy_is_held_not_fired(client, cloud, monkeypatch):
    """Dispatching into a busy lane leaves a run pending, and the next dispatch
    cancels it outright. That is how runs 40 and 41 died."""
    from src.web import cloud_runs

    app, _ = client
    agreeing(monkeypatch)
    monkeypatch.setattr(cloud_runs, "lane_is_busy", lambda *a: True)

    body = app.post("/api/scenarios/jp-ph/run-cloud?depth=deep").json()
    assert body["queued"] is True
    assert body["dispatched"] is False
    assert cloud == []
    assert [e["scenario_id"] for e in cloud_runs.queued()] == ["jp-ph"]


def test_a_held_run_can_be_dropped(client, cloud, monkeypatch):
    from src.web import cloud_runs

    app, _ = client
    cloud_runs.enqueue("jp-ph")
    assert app.delete("/api/cloud-queue/jp-ph").status_code == 200
    assert app.delete("/api/cloud-queue/jp-ph").status_code == 404


def test_the_cloud_listing_says_it_cannot_see_rather_than_showing_nothing(client):
    """An empty list reads as 'nothing has ever run'. Without `gh` the truth is
    'this app cannot see Actions', and the schedule is running regardless."""
    app, _ = client
    body = app.get("/api/cloud-runs").json()
    assert body["known"] is False
    assert body["reason"]
    assert body["runs"] == []
    # The crons come out of the workflow file, so they are answerable either way.
    assert body["schedule"]


def test_the_cloud_listing_reports_the_runs_and_what_is_held(client, cloud, monkeypatch):
    from src.web import cloud_runs

    app, _ = client
    monkeypatch.setattr(
        cloud_runs, "list_runs",
        lambda *a, **k: [{"id": 1, "live": False, "swept_nothing": True,
                          "status": "completed", "conclusion": "success"}],
    )
    cloud_runs.enqueue("jp-ph")
    body = app.get("/api/cloud-runs").json()
    assert body["known"] is True
    assert body["runs"][0]["swept_nothing"] is True
    assert body["busy"] is False
    assert [e["scenario_id"] for e in body["queued"]] == ["jp-ph"]


# --------------------------------------------------- results on this machine


def test_the_sync_endpoint_says_it_cannot_tell_rather_than_nothing_missing(client):
    """Without git this cannot see the branch, and must not imply completeness.

    The whole point of the panel is that "no runs missing" and "cannot check"
    look different. Answering 200 with a zero count for both would rebuild the
    blind spot it was written to remove.
    """
    app, _ = client
    body = app.get("/api/cloud-sync").json()
    assert body["known"] is False
    assert body["reason"]
    assert body["missing_count"] == 0
    assert body["can_fast_forward"] is False


def test_reading_the_sync_state_never_waits_on_the_network(client, monkeypatch):
    """Drawing the page must not block on a remote - the rule since `_git`.

    A synchronous fetch here would put a laptop that is briefly offline between
    the user and the Results tab, on every load.
    """
    from src.web import branch_sync

    def must_not_run():
        raise AssertionError("the read path fetched synchronously")

    monkeypatch.setattr(branch_sync, "fetch", must_not_run)
    app, _ = client
    assert app.get("/api/cloud-sync").status_code == 200


def test_a_refused_sync_is_409_carrying_the_reason_verbatim(client, monkeypatch):
    """A checkout with its own commits is not a broken app.

    409, not 500, and git's own sentence rather than a summary of it: when git
    refuses it names the files, and that is the only part worth reading.
    """
    from src.web import branch_sync

    monkeypatch.setattr(
        branch_sync, "pull",
        lambda *a: {"synced": False, "reason": "Your local changes to src/web/app.py "
                                               "would be overwritten", "gained": {}},
    )
    response = client[0].post("/api/cloud-sync")
    assert response.status_code == 409
    assert "src/web/app.py" in response.json()["detail"]


def test_a_sync_that_brought_runs_across_reports_them(client, monkeypatch):
    from src.web import branch_sync

    monkeypatch.setattr(
        branch_sync, "pull",
        lambda *a: {"synced": True, "reason": "", "commits": 7,
                    "gained": {"jp-ph": ["2026-08-22T20-30-46Z"]}},
    )
    body = client[0].post("/api/cloud-sync").json()
    assert body["synced"] is True
    assert body["gained"]["jp-ph"] == ["2026-08-22T20-30-46Z"]


def test_the_branch_is_asked_about_every_saved_trip_not_only_swept_ones(client, monkeypatch):
    """A trip whose runs are *all* still on the branch has no directory here.

    Listing the trips to ask about from the sweeps on disk would leave that trip
    out of exactly the case this exists for.
    """
    from src.web import branch_sync

    asked = []
    monkeypatch.setattr(
        branch_sync, "state",
        lambda data_dir, ids: asked.append(ids) or {"known": True, "missing_count": 0},
    )
    client[0].get("/api/cloud-sync")
    assert asked and "jp-ph" in asked[0]


def test_the_runs_can_be_taken_without_the_merge_that_was_refused(client, monkeypatch):
    """The two refusals are different, and only one of them was ever meant.

    A checkout ahead of the branch cannot fast-forward, and should not. It can
    still be handed run directories it does not have, because copying those
    moves no history and overwrites no file.
    """
    from src.web import branch_sync

    monkeypatch.setattr(
        branch_sync, "take",
        lambda *a: {"took": True, "reason": "",
                    "taken": {"jp-ph": ["2026-08-22T20-30-46Z"]},
                    "can_fast_forward": False, "missing_count": 0},
    )
    body = client[0].post("/api/cloud-sync/take").json()
    assert body["took"] is True
    assert body["taken"]["jp-ph"] == ["2026-08-22T20-30-46Z"]
    # Still diverged afterwards, and still saying so.
    assert body["can_fast_forward"] is False


def test_taking_the_runs_reports_a_refusal_the_same_way_a_sync_does(client, monkeypatch):
    from src.web import branch_sync

    monkeypatch.setattr(
        branch_sync, "take",
        lambda *a: {"took": False, "reason": "git would not copy jp-ph 2026-08-22T20-30-46Z",
                    "taken": {}},
    )
    response = client[0].post("/api/cloud-sync/take")
    assert response.status_code == 409
    assert "2026-08-22T20-30-46Z" in response.json()["detail"]


def test_taking_when_there_is_nothing_to_take_is_not_an_error(client, monkeypatch):
    from src.web import branch_sync

    monkeypatch.setattr(
        branch_sync, "take",
        lambda *a: {"took": False, "already_current": True, "reason": "", "taken": {}},
    )
    assert client[0].post("/api/cloud-sync/take").status_code == 200


# -------------------------------------------------- publishing a trip
#
# The other direction of the same boundary: `cloud-sync` brings the branch's
# results here, and this puts the trip the cloud will run onto the branch. Until
# it existed, every panel that could name the gap ended by asking for a commit
# and a push in a terminal.


def test_publishing_when_the_branch_cannot_be_reached_answers_rather_than_fails(client):
    """Its usual caller is a save that has already succeeded.

    `no_real_git` refuses every git call, which is what a checkout with no
    remote looks like from in here. The file is written either way, so a 500
    would be a complaint about a save that worked - and being offline is not a
    failed save.
    """
    response = client[0].post("/api/scenarios/jp-ph/publish")

    assert response.status_code == 200
    body = response.json()
    assert body["published"] is False
    assert body["reason"]


def test_a_deleted_trip_can_still_be_published_because_that_is_how_it_leaves(client):
    """Not behind `_scenario_or_404`, on purpose.

    Publishing a trip whose file is gone is what takes it off the branch, and a
    404 here would leave the night sweep planning a trip that no longer exists
    anywhere else.
    """
    assert client[0].delete("/api/scenarios/jp-ph").status_code == 200

    assert client[0].post("/api/scenarios/jp-ph/publish").status_code == 200


def test_the_trip_id_is_checked_before_it_becomes_a_path(client):
    """The name goes into a filename and into a URL on github.com.

    `_safe_id` is what stands between the two, and this endpoint is one of the
    few that is deliberately not behind `_scenario_or_404` - so it is the one
    place where an unchecked name would not be caught by the file not existing.
    """
    response = client[0].post("/api/scenarios/..%5C..%5Cwin.ini/publish")

    assert response.status_code == 400


# ------------------------------------------------------- airport verdicts
#
# The Explore tab used to open on a picker of runs by date and time: you chose
# "23 Aug, 10:46 · probe · 48 searches" and only then found out what it said
# about Vienna. That is backwards, and it threw work away - a probe the site
# refused halfway has nothing to say about the airports it never reached, while
# yesterday's complete sweep, on disk, does.


def seed_verdict_run(data_dir, stamp, priced, *, searches=3):
    """One run pricing exactly `priced`: {(origin, destination): amount}."""
    directory = data_dir / "sweeps" / "jp-ph" / stamp
    directory.mkdir(parents=True)
    with (directory / "legs.jsonl").open("w") as handle:
        for (origin, destination), amount in priced.items():
            leg = Leg(
                "T", origin, destination,
                date(2027, 1, 10) if destination != "PRG" and destination != "VIE" else date(2027, 1, 30),
                "QR", None, 1, "CZK", float(amount), "u",
            )
            handle.write(json.dumps(leg.to_dict()) + "\n")
    (directory / "status.json").write_text(json.dumps({
        "state": "done", "mode": "sweep", "depth": "deep", "coverage": 1.0,
        "route_searches": {f"{o}->{d}": searches for o, d in priced},
        "route_errors": {},
    }))
    return stamp


def narrow_to(api, **fields):
    """Save the trip with one field changed, as the route editor would."""
    trip = api.get("/api/scenarios/jp-ph").json()
    trip.update(fields)
    response = api.put("/api/scenarios/jp-ph", json=trip)
    assert response.status_code == 200, response.json()
    return response.json()


def test_an_airport_taken_out_of_the_trip_keeps_its_verdict(client):
    """The whole point of the probe list. Acting on this table used to destroy
    it: the row you narrowed by - "Vienna is 100% dearer" - vanished the moment
    you took Vienna out, so the comparison could never be checked again."""
    api, data = client
    seed_verdict_run(data, "2026-08-06T02-00-00Z", {
        ("PRG", "NRT"): 12000, ("VIE", "NRT"): 24000,
        ("NRT", "MNL"): 4000, ("MNL", "PRG"): 14000, ("MNL", "VIE"): 15000,
    })
    narrow_to(api, origins=["PRG"], probe_extra={"origins": ["VIE"]})

    pool = api.get("/api/scenarios/jp-ph/airport-verdicts").json()["pools"][0]
    rows = {row["iata"]: row for row in pool["airports"]}
    assert set(rows) == {"PRG", "VIE"}
    assert rows["PRG"]["in_trip"] is True
    # Still judged, and visibly the odd one out rather than silently gone.
    assert rows["VIE"]["in_trip"] is False
    assert rows["VIE"]["verdict"] == "poor"


def test_an_airport_dropped_without_being_kept_does_go(client):
    """The list is typed, not inferred. Editing the route removes an airport
    from the trip and does nothing else - a list that grew on its own would walk
    a 51-search probe up toward the cost of the sweep it exists to avoid."""
    api, data = client
    seed_verdict_run(data, "2026-08-06T02-00-00Z", {
        ("PRG", "NRT"): 12000, ("VIE", "NRT"): 24000,
        ("NRT", "MNL"): 4000, ("MNL", "PRG"): 14000, ("MNL", "VIE"): 15000,
    })
    narrow_to(api, origins=["PRG"])

    pool = api.get("/api/scenarios/jp-ph/airport-verdicts").json()["pools"][0]
    assert [row["iata"] for row in pool["airports"]] == ["PRG"]


def test_a_pool_carries_the_key_the_probe_list_is_addressed_by(client):
    """Sent rather than re-derived on the page. Two places deciding what a pool
    is called is how a list gets edited into a key nothing reads."""
    api, data = client
    seed_verdict_run(data, "2026-08-06T02-00-00Z", {("PRG", "NRT"): 12000})
    body = api.get("/api/scenarios/jp-ph/airport-verdicts").json()
    assert [pool["key"] for pool in body["pools"]] == [
        "origins", "stop:0", "stop:1", "origins",
    ]


def test_a_probe_list_for_a_pool_the_trip_no_longer_has_is_reported(client):
    """Kept on disk deliberately. A list being kept and not used must not also
    be invisible."""
    api, data = client
    seed_verdict_run(data, "2026-08-06T02-00-00Z", {("PRG", "NRT"): 12000})
    narrow_to(api, probe_extra={"stop:9": ["CTS"]})

    body = api.get("/api/scenarios/jp-ph/airport-verdicts").json()
    assert body["probe_extra_unused"] == {"stop:9": ["CTS"]}


def test_every_airport_of_the_trip_is_judged_without_choosing_a_run(client):
    api, data = client
    seed_verdict_run(data, "2026-08-06T02-00-00Z", {
        ("PRG", "NRT"): 12000, ("VIE", "NRT"): 24000,
        ("NRT", "MNL"): 4000, ("MNL", "PRG"): 14000, ("MNL", "VIE"): 15000,
    })

    body = api.get("/api/scenarios/jp-ph/airport-verdicts").json()
    # Per pool, never flattened: Prague and Vienna each stand in two places -
    # flying out and coming home - and are ranked separately in each, because
    # cheap to leave from and dear to come back to is not a cheap airport.
    out = {
        row["iata"]: row["verdict"]
        for row in next(p for p in body["pools"] if p["index"] == 0)["airports"]
    }
    assert out["PRG"] == "best"
    # 24,000 against 12,000 on the same hop is 100% dearer.
    assert out["VIE"] == "poor"

    home = {
        row["iata"]: row["verdict"]
        for row in body["pools"][-1]["airports"]
    }
    assert home["PRG"] == "best"       # 14,000 against Vienna's 15,000
    assert home["VIE"] == "close"      # and only 7% behind it
    assert body["runs_read"] == 1


def test_a_whole_pool_comes_from_one_run_so_its_percentages_compare(client):
    """The verdicts in a pool are scored against the cheapest of that pool, so
    rows taken from different runs would be percentages against different
    baselines printed as though they were one table."""
    api, data = client
    # Older run: both origins priced, and Vienna is the dear one.
    seed_verdict_run(data, "2026-08-05T02-00-00Z", {
        ("PRG", "NRT"): 12000, ("VIE", "NRT"): 24000,
        ("NRT", "MNL"): 4000, ("MNL", "PRG"): 14000, ("MNL", "VIE"): 15000,
    })
    # Newer run reached only Vienna. Taken on its own it would call Vienna the
    # cheapest airport there is, because it is the only one it looked at.
    seed_verdict_run(data, "2026-08-07T02-00-00Z", {
        ("VIE", "NRT"): 24000, ("NRT", "MNL"): 4000, ("MNL", "VIE"): 15000,
    })

    body = api.get("/api/scenarios/jp-ph/airport-verdicts").json()
    origins = next(p for p in body["pools"] if p["index"] == 0)
    assert origins["measured_by"]["stamp"] == "2026-08-05T02-00-00Z"
    assert {row["iata"] for row in origins["airports"]} == {"PRG", "VIE"}
    assert {row["iata"]: row["verdict"] for row in origins["airports"]}["VIE"] == "poor"


def test_recency_breaks_a_tie_between_runs_that_measured_as_much(client):
    api, data = client
    seed_verdict_run(data, "2026-08-05T02-00-00Z", {
        ("PRG", "NRT"): 12000, ("VIE", "NRT"): 24000,
        ("NRT", "MNL"): 4000, ("MNL", "PRG"): 14000, ("MNL", "VIE"): 15000,
    })
    seed_verdict_run(data, "2026-08-09T02-00-00Z", {
        ("PRG", "NRT"): 11000, ("VIE", "NRT"): 23000,
        ("NRT", "MNL"): 4000, ("MNL", "PRG"): 14000, ("MNL", "VIE"): 15000,
    })

    body = api.get("/api/scenarios/jp-ph/airport-verdicts").json()
    for pool in body["pools"]:
        assert pool["measured_by"]["stamp"] == "2026-08-09T02-00-00Z"


def test_an_airport_no_run_has_ever_priced_is_named_not_omitted(client):
    """The absence of a row is not an answer to "what about this one?".

    A run with no snapshot is read as having searched the trip as it stands, so
    an airport added since gets a row of its own saying nothing was measured -
    which is the honest answer, and the one the tab already knows how to draw.
    What it must never be is missing.
    """
    api, data = client
    api.put("/api/scenarios/jp-ph", json={
        **api.get("/api/scenarios/jp-ph").json(),
        "origins": ["PRG", "VIE", "KRK"],
    })
    seed_verdict_run(data, "2026-08-06T02-00-00Z", {
        ("PRG", "NRT"): 12000, ("VIE", "NRT"): 24000,
        ("NRT", "MNL"): 4000, ("MNL", "PRG"): 14000, ("MNL", "VIE"): 15000,
    })

    body = api.get("/api/scenarios/jp-ph/airport-verdicts").json()
    origins = next(p for p in body["pools"] if p["index"] == 0)
    krakow = next(row for row in origins["airports"] if row["iata"] == "KRK")
    assert krakow["total_min"] is None
    # "Unproven", never "no offers": the site was not asked enough times to
    # support a claim about the market.
    assert krakow["verdict"] == "unproven"


def test_an_airport_added_since_the_run_is_named_under_its_pool(client):
    """The other way an airport can be missing: the run recorded a snapshot,
    and that snapshot has no such airport, so there is no row to put it in."""
    api, data = client
    stamp = seed_verdict_run(data, "2026-08-06T02-00-00Z", {
        ("PRG", "NRT"): 12000, ("VIE", "NRT"): 24000,
        ("NRT", "MNL"): 4000, ("MNL", "PRG"): 14000, ("MNL", "VIE"): 15000,
    })
    searched = api.get("/api/scenarios/jp-ph").json()
    (data / "sweeps" / "jp-ph" / stamp / "scenario.json").write_text(
        json.dumps(searched), encoding="utf-8"
    )
    api.put("/api/scenarios/jp-ph", json={**searched, "origins": ["PRG", "VIE", "KRK"]})

    body = api.get("/api/scenarios/jp-ph/airport-verdicts").json()
    origins = next(p for p in body["pools"] if p["index"] == 0)
    assert origins["not_searched"] == ["KRK"]
    assert "KRK" not in {row["iata"] for row in origins["airports"]}


def test_a_run_of_a_differently_shaped_trip_is_skipped_rather_than_lined_up(client):
    """Pools are positional. Lining up pool 2 of a two-stop trip with pool 2 of
    a three-stop one is how a probe of Prague came to be presented as the
    verdict for a trip flying out of Katowice."""
    api, data = client
    stamp = seed_verdict_run(data, "2026-08-06T02-00-00Z", {
        ("PRG", "NRT"): 12000, ("VIE", "NRT"): 24000,
        ("NRT", "MNL"): 4000, ("MNL", "PRG"): 14000, ("MNL", "VIE"): 15000,
    })
    # The run records that it searched a trip with an extra stop.
    trip = api.get("/api/scenarios/jp-ph").json()
    trip["stops"] = [*trip["stops"], {"airports": ["DPS"], "stay_days": [3, 5], "label": "Bali"}]
    (data / "sweeps" / "jp-ph" / stamp / "scenario.json").write_text(
        json.dumps(trip), encoding="utf-8"
    )

    body = api.get("/api/scenarios/jp-ph/airport-verdicts").json()
    assert body["runs_read"] == 0
    assert all(pool["measured_by"] is None for pool in body["pools"])
    assert all(pool["not_searched"] for pool in body["pools"])


def test_the_verdicts_endpoint_is_fine_with_a_trip_nothing_has_swept(client):
    api, _ = client
    body = api.get("/api/scenarios/jp-ph/airport-verdicts").json()
    assert body["runs_read"] == 0
    assert body["pools"] and all(pool["airports"] == [] for pool in body["pools"])


def test_the_verdicts_endpoint_404s_on_a_trip_that_does_not_exist(client):
    api, _ = client
    assert api.get("/api/scenarios/nope/airport-verdicts").status_code == 404


# ---------------------------------------------------------- booking a follow


def test_a_followed_flight_carries_a_link_to_buy_it(client):
    """The watch records a price and never the offer's URL, so the search is
    rebuilt from the three things that define the watch."""
    api, _ = client
    api.post("/api/watch/jp-ph/legs", json={
        "origin": "PRG", "destination": "NRT",
        "depart_date": "2027-01-14", "currency": "CZK",
    })

    leg = api.get("/api/watch/jp-ph").json()["legs"][0]
    assert leg["book_url"].startswith("https://")
    # The three things that define the watch are all in it.
    assert "PRG" in leg["book_url"] and "NRT" in leg["book_url"]
    assert "2027_1_14" in leg["book_url"]


# ------------------------------------------------------- the airport ranking
#
# A global list, ranked by how easy each airport is to reach. It replaces the
# frequency-derived suggestions in the chip row when it is set, because
# frequency cannot discover convenience - the usual reason a convenient airport
# goes unused is that it has no inventory.


def test_without_a_ranking_the_chips_are_what_your_trips_use(client):
    """The behaviour this always had, and the fallback it degrades to."""
    api, _ = client
    body = api.get("/api/airports/frequent").json()
    assert body["ranked"] is False


def test_a_ranking_replaces_the_chips_and_keeps_its_order(client):
    api, _ = client
    api.put("/api/home-airports", json={"airports": ["BRQ", "PRG", "VIE"]})

    body = api.get("/api/airports/frequent").json()
    assert [airport["iata"] for airport in body["origins"]] == ["BRQ", "PRG", "VIE"]
    # So the row can label itself honestly - "yours, in order" is a different
    # claim from "airports you have used".
    assert body["ranked"] is True


def test_the_ranking_does_not_touch_the_destination_chips(client):
    """There is no convenient end to a trip to Japan."""
    api, _ = client
    before = api.get("/api/airports/frequent").json()["destinations"]
    api.put("/api/home-airports", json={"airports": ["BRQ"]})
    assert api.get("/api/airports/frequent").json()["destinations"] == before


def test_the_ranking_comes_back_described_so_the_page_can_show_cities(client):
    api, _ = client
    body = api.put("/api/home-airports", json={"airports": ["PRG"]}).json()
    assert body["airports"] == ["PRG"]
    assert body["described"][0]["city"]


def test_a_mistyped_airport_is_refused_with_words_the_page_can_show(client):
    api, _ = client
    response = api.put("/api/home-airports", json={"airports": ["Brno"]})
    assert response.status_code == 400
    assert "Brno" in response.json()["detail"]


def test_a_trip_the_branch_has_never_seen_is_refused_by_name(client, cloud, monkeypatch):
    """A dispatch of an uncommitted trip cannot do anything but fail.

    `plan` filters on the branch's own scenario files, so an unknown name plans
    nothing, and `dispatched-nothing` then fails the run twenty seconds later.
    That happened for real to a trip saved only on this machine. The sentence
    the workflow printed is the one the app can print before spending a run, so
    it says it here - and offers no override, because there is nothing on the
    other side of one.

    It asks to publish rather than naming the file to commit. The errand is now
    a button: the page reads this phrase, offers to publish, and dispatches
    again unforced.
    """
    app, _ = client
    import src.web.app as app_module

    monkeypatch.setattr(app_module, "_fetch_cloud_ref", lambda: None)
    monkeypatch.setattr(
        app_module, "_cloud_state",
        lambda *a: {"known": False, "on_branch": False, "differs": [],
                    "included": None, "searches": None},
    )

    response = app.post("/api/scenarios/jp-ph/run-cloud")
    detail = response.json()["detail"]
    assert response.status_code == 400
    assert "jp-ph" in detail
    # The exact phrase the page keys its "publish it now and run?" offer off.
    assert "Publish it to the branch first" in detail
    # No escape hatch: the page offers "run it anyway" on that phrase, and a
    # forced dispatch here is a guaranteed red run.
    assert "run it anyway" not in detail.lower()
    assert cloud == []


def test_a_branch_that_cannot_be_read_still_offers_the_override(client, cloud, monkeypatch):
    """"Cannot say" and "definitely not there" are different answers.

    Sweeping the branch's version on purpose is a real thing to want when this
    app simply cannot see the branch; it is not a thing to want when the trip is
    known to be absent from it.
    """
    app, _ = client
    import src.web.app as app_module

    monkeypatch.setattr(app_module, "_fetch_cloud_ref", lambda: None)
    monkeypatch.setattr(
        app_module, "_cloud_state",
        lambda *a: {"known": False, "on_branch": None, "differs": [],
                    "included": None, "searches": None},
    )

    response = app.post("/api/scenarios/jp-ph/run-cloud")
    assert response.status_code == 400
    assert "run it anyway" in response.json()["detail"].lower()
    assert cloud == []
