"""Tests for the volatility probe: recording observations and reporting on them."""
from __future__ import annotations

import json
from datetime import date

from src.models import Leg
from src.probe import PROBE_ROUTES, probe_report, record_observation


def leg(price: float) -> Leg:
    return Leg(
        provider="TEST", origin="PRG", destination="NRT", depart_date=date(2027, 1, 12),
        airline="QR", flight_number=None, stops=1, price_currency="CZK",
        price_amount=price, url="https://example.test/leg",
    )


def write_observations(tmp_path, prices, origin="PRG", destination="NRT"):
    """`prices` may contain None, meaning a run that returned nothing."""
    path = tmp_path / "observations.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for index, price in enumerate(prices):
            handle.write(json.dumps({
                "ts": f"2026-08-07T{index:02d}:00:00+00:00",
                "origin": origin,
                "destination": destination,
                "depart_date": "2027-01-12",
                "min_price": price,
                "n_offers": 0 if price is None else 10,
                "currency": "CZK",
            }) + "\n")
    return path


def test_observation_records_min_price_and_count(tmp_path):
    rec = record_observation([leg(14480), leg(15200)], "PRG", "NRT", date(2027, 1, 12), tmp_path)
    assert rec["min_price"] == 14480
    assert rec["n_offers"] == 2


def test_observation_with_no_legs_records_none_not_zero(tmp_path):
    # A zero-leg run is scraper breakage, not a flight that costs nothing.
    rec = record_observation([], "PRG", "NRT", date(2027, 1, 12), tmp_path)
    assert rec["min_price"] is None
    assert rec["n_offers"] == 0


def test_observations_append_rather_than_overwrite(tmp_path):
    record_observation([leg(14480)], "PRG", "NRT", date(2027, 1, 12), tmp_path)
    record_observation([leg(15000)], "PRG", "NRT", date(2027, 1, 12), tmp_path)
    lines = (tmp_path / "observations.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2


def test_report_counts_how_often_price_changed(tmp_path):
    write_observations(tmp_path, [14480, 14480, 15200, 14900])
    stats = probe_report(tmp_path)
    route = stats.routes["PRG→NRT"]
    assert route.n_observations == 4
    assert route.n_changes == 2


def test_report_handles_single_observation_without_dividing_by_zero(tmp_path):
    write_observations(tmp_path, [14480])
    assert probe_report(tmp_path).routes["PRG→NRT"].change_rate == 0.0


def test_report_ignores_runs_where_scraper_returned_nothing(tmp_path):
    write_observations(tmp_path, [14480, None, 14480])
    assert probe_report(tmp_path).routes["PRG→NRT"].n_observations == 2


def test_report_measures_change_magnitude(tmp_path):
    write_observations(tmp_path, [10000, 11000])
    route = probe_report(tmp_path).routes["PRG→NRT"]
    assert route.max_change == 1000
    assert route.max_change_pct == 10.0


def test_report_records_the_largest_drop(tmp_path):
    write_observations(tmp_path, [10000, 12000, 9000])
    assert probe_report(tmp_path).routes["PRG→NRT"].largest_drop == 3000


def test_report_handles_a_missing_file(tmp_path):
    assert probe_report(tmp_path).routes == {}


def test_report_separates_routes(tmp_path):
    path = tmp_path / "observations.jsonl"
    rows = []
    for origin, destination, price in [("PRG", "NRT", 14000), ("NRT", "MNL", 4000)]:
        rows.append(json.dumps({
            "ts": "2026-08-07T00:00:00+00:00", "origin": origin, "destination": destination,
            "depart_date": "2027-01-12", "min_price": price, "n_offers": 10, "currency": "CZK",
        }))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert set(probe_report(tmp_path).routes) == {"PRG→NRT", "NRT→MNL"}


def test_recommendation_says_daily_is_fine_when_prices_are_stable(tmp_path):
    write_observations(tmp_path, [14000] * 20)
    assert "daily" in probe_report(tmp_path).recommendation.lower()


def test_recommendation_flags_frequent_large_moves(tmp_path):
    write_observations(tmp_path, [10000, 12000] * 10)
    assert "more often" in probe_report(tmp_path).recommendation.lower()


def test_probe_routes_are_fixed_so_observations_stay_comparable():
    assert len(PROBE_ROUTES) == 3
    assert ("NRT", "MNL", date(2027, 1, 22)) in PROBE_ROUTES
