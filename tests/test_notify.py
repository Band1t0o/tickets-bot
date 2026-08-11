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


def test_price_embed_names_the_trip_and_every_total():
    from src.alerts import Pick

    embed = build_price_embed(
        "Japan then Philippines",
        [Pick("cheapest", itinerary(27000)), Pick("preferred", itinerary(30000), tier=1)],
    )
    text = json.dumps(embed, ensure_ascii=False)
    assert "Japan then Philippines" in text
    assert "27" in text and "30" in text


def test_price_embed_shows_the_delta_against_the_previous_best():
    from src.alerts import Pick

    embed = build_price_embed("S", [Pick("cheapest", itinerary(28000))], previous_best=30000)
    assert "down 2,000" in embed["description"], embed["description"]


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


def test_dark_route_triggers_health_alert():
    # A whole route returning nothing on every date is breakage, not a quiet
    # market. MNL->VIE was dark for an entire sweep that reported zero errors,
    # and it was the return leg of the cheapest real itinerary.
    alert = build_health_alert(
        scenario_name="S",
        legs_found=500,
        errors=0,
        total=300,
        dark_routes=["MNL->VIE", "CEB->PRG"],
    )
    assert alert is not None
    assert "MNL->VIE" in alert["description"]


def test_no_dark_routes_leaves_a_healthy_sweep_silent():
    assert (
        build_health_alert(
            scenario_name="S", legs_found=500, errors=0, total=300, dark_routes=[]
        )
        is None
    )


# ------------------------------------------------------- reporting the picks
#
# One embed per pick, so "cheapest" and "cheapest from an airport you like" are
# both visible with the difference between them stated. Previously the message
# was always "same airport" against "open jaw", which answers a question about
# trip shape rather than about where you would rather fly from.


def pick(name, total, tier=None, premium=0.0, also_preferred=False, origin="PRG"):
    from src.alerts import Pick

    trip = Itinerary(legs=[
        leg(origin, "NRT", date(2027, 1, 10), total * 0.4),
        leg("NRT", "MNL", date(2027, 1, 20), total * 0.15),
        leg("MNL", origin, date(2027, 1, 30), total * 0.45),
    ])
    return Pick(name, trip, tier=tier, premium=premium, also_preferred=also_preferred)


def test_each_pick_becomes_its_own_field():
    embed = build_price_embed("S", [pick("cheapest", 21000, origin="FRA"),
                                    pick("preferred", 30000, tier=1, premium=9000)])
    names = [field["name"] for field in embed["fields"]]
    assert any("Cheapest overall" in n for n in names), names
    assert any("tier-1" in n for n in names), names


def test_the_preferred_field_states_what_the_preference_costs():
    embed = build_price_embed("S", [pick("cheapest", 21000, origin="FRA"),
                                    pick("preferred", 30000, tier=1, premium=9000)])
    preferred = next(f for f in embed["fields"] if "tier-1" in f["name"])
    assert "9" in preferred["value"], preferred["value"]


def test_a_collapsed_pick_is_reported_once_and_says_so():
    embed = build_price_embed("S", [pick("cheapest", 21000, tier=1, also_preferred=True)])
    assert len(embed["fields"]) == 1
    assert "prefer" in embed["fields"][0]["name"].lower()


def test_every_field_says_when_the_price_was_measured():
    """A ping about a price read three days ago is worth less than one about a
    price read ten minutes ago, and the message must not hide which it is."""
    stamped = pick("cheapest", 21000)
    for one in stamped.itinerary.legs:
        one.observed_at = "2026-08-10T11:59:04+00:00"
    embed = build_price_embed("S", [stamped])
    assert "2026-08-10" in embed["fields"][0]["value"]


def test_no_picks_produces_no_embed():
    assert build_price_embed("S", []) is None


# ------------------------------------------------------ per-pick quiet state


def test_best_state_is_tracked_per_pick(tmp_path):
    """The preferred pick improving is news even when the cheapest has not.

    One shared figure meant a tier-1 trip dropping 3,000 CZK went unreported
    whenever Frankfurt happened to stay flat.
    """
    save_best(tmp_path, 21000, "CZK", name="cheapest")
    save_best(tmp_path, 30000, "CZK", name="preferred")
    assert load_best(tmp_path, name="cheapest") == 21000
    assert load_best(tmp_path, name="preferred") == 30000


def test_an_unknown_pick_has_no_recorded_best(tmp_path):
    save_best(tmp_path, 21000, "CZK", name="cheapest")
    assert load_best(tmp_path, name="preferred") is None


def test_the_flat_best_file_still_reads_as_the_cheapest(tmp_path):
    """best.json predates having more than one pick; it recorded the cheapest."""
    (tmp_path / "best.json").write_text(
        json.dumps({"best_total": 23017, "currency": "CZK"}), encoding="utf-8"
    )
    assert load_best(tmp_path, name="cheapest") == 23017
    assert load_best(tmp_path, name="preferred") is None


def test_saving_one_pick_does_not_discard_another(tmp_path):
    save_best(tmp_path, 21000, "CZK", name="cheapest")
    save_best(tmp_path, 30000, "CZK", name="preferred")
    save_best(tmp_path, 20000, "CZK", name="cheapest")
    assert load_best(tmp_path, name="preferred") == 30000


def test_a_worse_total_never_walks_the_recorded_best_upward(tmp_path):
    # With alert_threshold set, an alert fires on any total under it - including
    # one worse than the best already recorded. Letting that overwrite destroyed
    # the "only report genuine improvement" guarantee for every later run.
    save_best(tmp_path, 21000, "CZK", name="cheapest")
    save_best(tmp_path, 25000, "CZK", name="cheapest")
    assert load_best(tmp_path, name="cheapest") == 21000
