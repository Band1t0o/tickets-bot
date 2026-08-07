"""Tests for price-improvement and health alerting."""
from __future__ import annotations

import json
from datetime import date

from src.models import Itinerary, Leg
from src.notify_discord import (
    build_health_alert,
    build_price_embed,
    load_best,
    save_best,
    should_alert,
)


def leg(origin="PRG", destination="NRT", depart=date(2027, 1, 10), price=10000.0) -> Leg:
    return Leg(
        provider="TEST", origin=origin, destination=destination, depart_date=depart,
        airline="QR", flight_number=None, stops=1, price_currency="CZK",
        price_amount=price, url="https://example.test/leg",
    )


def itinerary(total=30000.0) -> Itinerary:
    return Itinerary(legs=[
        leg("PRG", "NRT", date(2027, 1, 10), total * 0.4),
        leg("NRT", "MNL", date(2027, 1, 20), total * 0.15),
        leg("MNL", "PRG", date(2027, 1, 30), total * 0.45),
    ])


def test_alerts_when_price_improves():
    assert should_alert(best_total=28000, previous_best=30000, threshold=None) is True


def test_silent_when_price_is_unchanged():
    # The old behaviour alerted on any unseen hash, which reported "10 new
    # offers" for a single flight. Silence on no improvement is the fix.
    assert should_alert(best_total=30000, previous_best=30000, threshold=None) is False


def test_silent_when_price_rises():
    assert should_alert(best_total=31000, previous_best=30000, threshold=None) is False


def test_alerts_on_first_run_when_no_previous_best_exists():
    assert should_alert(best_total=30000, previous_best=None, threshold=None) is True


def test_alerts_below_threshold_even_without_improvement():
    assert should_alert(best_total=25000, previous_best=25000, threshold=26000) is True


def test_does_not_alert_above_threshold_without_improvement():
    assert should_alert(best_total=27000, previous_best=27000, threshold=26000) is False


def test_best_state_round_trips_through_disk(tmp_path):
    assert load_best(tmp_path) is None
    save_best(tmp_path, 29000, "CZK")
    assert load_best(tmp_path) == 29000


def test_load_best_tolerates_a_corrupt_state_file(tmp_path):
    (tmp_path / "best.json").write_text("{not json", encoding="utf-8")
    assert load_best(tmp_path) is None


def test_price_embed_names_both_options():
    embed = build_price_embed(
        scenario_name="Japan then Philippines",
        best_same=itinerary(30000),
        best_jaw=itinerary(27000),
        previous_best=31000,
    )
    text = json.dumps(embed, ensure_ascii=False)
    assert "Japan then Philippines" in text
    assert "27" in text and "30" in text


def test_price_embed_shows_the_delta_against_the_previous_best():
    embed = build_price_embed("S", itinerary(28000), None, previous_best=30000)
    assert "2" in json.dumps(embed)  # a 2,000 CZK drop is reported


def test_zero_legs_triggers_health_alert():
    assert build_health_alert(scenario_name="S", legs_found=0, errors=0, total=300) is not None


def test_majority_failures_trigger_health_alert():
    assert build_health_alert(scenario_name="S", legs_found=5, errors=200, total=300) is not None


def test_healthy_sweep_triggers_no_health_alert():
    assert build_health_alert(scenario_name="S", legs_found=174, errors=2, total=300) is None


def test_health_alert_is_visually_distinct():
    alert = build_health_alert(scenario_name="S", legs_found=0, errors=0, total=300)
    # Red, so a broken scraper cannot be mistaken for an ordinary price update.
    assert alert["color"] == 0xE12D39
