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


# --------------------------------------------------- magnitude and direction
#
# The panel this feeds reported "median move 24, biggest drop 20" for FRA->NRT
# across four days in which it rose 10,832 -> 13,556, a 25% climb. Both figures
# were true and both were useless: `largest_drop` only reads negative deltas,
# and counting changes weighs a 6 Kc move on a 3,850 Kc fare the same as a
# 2,400 Kc one - which is why NRT->MNL, the steadiest route measured, showed
# the highest change rate of the three.


def test_net_change_reports_the_direction_of_travel(tmp_path):
    write_observations(tmp_path, [10832, 11146, 13556])
    route = probe_report(tmp_path).routes["PRG→NRT"]
    assert route.net_change_pct == 25.1


def test_net_change_is_negative_when_a_fare_falls(tmp_path):
    write_observations(tmp_path, [30000, 21324])
    assert probe_report(tmp_path).routes["PRG→NRT"].net_change_pct == -28.9


def test_a_route_that_ends_where_it_began_has_no_net_change(tmp_path):
    """Even though it moved twice on the way — which is what range_pct is for."""
    route = write_observations(tmp_path, [10000, 12000, 10000]) and probe_report(tmp_path)
    assert route.routes["PRG→NRT"].net_change_pct == 0.0
    assert route.routes["PRG→NRT"].range_pct == 20.0


def test_noise_does_not_count_as_a_meaningful_move(tmp_path):
    # 6 Kc on 3,850 is the site rounding, not the market moving.
    write_observations(tmp_path, [3850, 3856, 3850, 3844])
    route = probe_report(tmp_path).routes["PRG→NRT"]
    assert route.change_rate == 1.0, "every step did move, however slightly"
    assert route.meaningful_change_rate == 0.0


def test_a_real_step_counts_as_a_meaningful_move(tmp_path):
    write_observations(tmp_path, [11172, 13556])
    assert probe_report(tmp_path).routes["PRG→NRT"].meaningful_change_rate == 1.0


def test_a_single_observation_has_no_change_figures(tmp_path):
    write_observations(tmp_path, [14480])
    route = probe_report(tmp_path).routes["PRG→NRT"]
    assert route.net_change_pct == 0.0
    assert route.range_pct == 0.0
    assert route.meaningful_change_rate == 0.0


def test_recommendation_is_driven_by_magnitude_not_by_counting(tmp_path):
    """Twenty 0.15% wobbles are not a reason to double the sweep budget.

    The old rule tripped on change *count* alone, so this series - which moves
    at every single step, by six crowns - argued for sweeping twice a day.
    """
    write_observations(tmp_path, [3850, 3856] * 10)
    assert "more often" not in probe_report(tmp_path).recommendation.lower()


def test_recommendation_reports_a_sustained_climb(tmp_path):
    # Four days of FRA->NRT. Whether to sweep more often is the lesser point;
    # that the fare is running away is the one worth putting in words.
    write_observations(tmp_path, [10832, 11146, 12647, 13556])
    text = probe_report(tmp_path).recommendation.lower()
    assert "risen" in text or "rising" in text, text
