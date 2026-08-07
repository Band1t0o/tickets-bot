"""API tests. No browser, no network: sweeps are seeded on disk."""
from __future__ import annotations

import importlib
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.models import Leg
from src.scenario import Scenario, save_scenario


@pytest.fixture
def client(tmp_path, monkeypatch):
    scenarios = tmp_path / "scenarios"
    data = tmp_path / "data"
    scenarios.mkdir()
    data.mkdir()
    monkeypatch.setenv("SCENARIO_DIR", str(scenarios))
    monkeypatch.setenv("DATA_DIR", str(data))

    save_scenario(
        Scenario(
            id="jp-ph", name="Japan then Philippines", trip_type="multi_city",
            origins=["PRG", "VIE"], japan_airports=["NRT"], ph_airports=["MNL"],
            window_start=date(2027, 1, 5), window_end=date(2027, 2, 8),
            japan_stay_days=(9, 11), ph_stay_days=(9, 11), depth="quick",
        ),
        scenarios,
    )

    import src.web.app as app_module

    importlib.reload(app_module)
    return TestClient(app_module.app), data


def seed_sweep(data_dir, stamp="2026-08-06T02-00-00Z", legs=None):
    directory = data_dir / "sweeps" / "jp-ph" / stamp
    directory.mkdir(parents=True)
    legs = legs if legs is not None else [
        Leg("T", "PRG", "NRT", date(2027, 1, 10), "QR", None, 1, "CZK", 12000.0, "u"),
        Leg("T", "NRT", "MNL", date(2027, 1, 20), "PR", None, 0, "CZK", 4000.0, "u"),
        Leg("T", "MNL", "PRG", date(2027, 1, 30), "QR", None, 1, "CZK", 14000.0, "u"),
        Leg("T", "MNL", "VIE", date(2027, 1, 30), "EY", None, 1, "CZK", 11000.0, "u"),
    ]
    with (directory / "legs.jsonl").open("w") as handle:
        for leg in legs:
            handle.write(json.dumps(leg.to_dict()) + "\n")
    (directory / "status.json").write_text(json.dumps({"state": "done", "total": 4}))
    return stamp


def test_airports_lists_measured_viability(client):
    api, _ = client
    body = api.get("/api/airports").json()
    europe = {a["iata"]: a for a in body["europe"]}
    assert europe["BRQ"]["available"] is False
    assert "no long-haul inventory" in europe["BRQ"]["note"].lower()
    assert europe["FRA"]["default"] is True
    assert europe["KRK"]["available"] is True and europe["KRK"]["default"] is False


def test_lists_seeded_scenarios(client):
    api, _ = client
    assert [s["id"] for s in api.get("/api/scenarios").json()] == ["jp-ph"]


def test_rejects_an_invalid_scenario_with_a_readable_message(client):
    api, _ = client
    bad = {
        "id": "bad", "name": "Bad", "trip_type": "multi_city", "origins": [],
        "japan_airports": ["NRT"], "ph_airports": ["MNL"],
        "window_start": "2027-01-05", "window_end": "2027-02-08",
    }
    response = api.post("/api/scenarios", json=bad)
    assert response.status_code == 400
    assert "origins" in response.json()["detail"]


def test_estimate_reports_searches_and_minutes(client):
    api, _ = client
    body = api.post("/api/scenarios/jp-ph/estimate").json()
    assert body["searches"] > 0
    assert body["minutes"] > 0
    assert set(body["per_leg"]) == {"0", "1", "2"}


def test_deeper_estimate_costs_more(client):
    api, _ = client
    quick = api.post("/api/scenarios/jp-ph/estimate?depth=quick").json()["searches"]
    deep = api.post("/api/scenarios/jp-ph/estimate?depth=deep").json()["searches"]
    assert deep > quick


def test_results_report_both_headline_options(client):
    api, data = client
    stamp = seed_sweep(data)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()
    assert body["best_same_airport"]["total_price"] == 30000
    assert body["best_open_jaw"]["total_price"] == 27000


def test_results_are_sorted_cheapest_first(client):
    api, data = client
    stamp = seed_sweep(data)
    totals = [i["total_price"] for i in api.get(f"/api/sweeps/jp-ph/{stamp}/results").json()["itineraries"]]
    assert totals == sorted(totals)


def test_results_can_be_filtered_to_same_airport(client):
    api, data = client
    stamp = seed_sweep(data)
    body = api.get(f"/api/sweeps/jp-ph/{stamp}/results?mode=same").json()
    assert all(i["same_airport"] for i in body["itineraries"])


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
    assert api.get("/api/sweeps/jp-ph/never/results").status_code == 404


def test_sweep_list_is_empty_before_any_run(client):
    api, _ = client
    body = api.get("/api/sweeps/jp-ph").json()
    assert body["sweeps"] == []
    assert body["running"] is False


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
