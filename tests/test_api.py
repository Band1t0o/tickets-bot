"""API tests. No browser, no network: sweeps are seeded on disk."""
from __future__ import annotations

import importlib
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.models import Leg
from src.scenario import Scenario, Stop, save_scenario

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


# ------------------------------------------------------------------ scenarios


def test_lists_seeded_scenarios(client):
    api, _ = client
    assert [s["id"] for s in api.get("/api/scenarios").json()] == ["jp-ph"]


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
