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
from tests.conftest import make_scenario


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


# --------------------------------------------------- where the message goes
#
# `notify_sweep` used to read the environment directly, so a webhook saved
# through the UI was invisible to a local `python -m src.cli sweep` - the
# setting existed and did nothing.


def test_notify_sweep_uses_a_locally_saved_webhook(tmp_path, monkeypatch):
    from src import notify_discord
    from src.webhook_store import save_webhook

    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    save_webhook("https://discord.com/api/webhooks/1/local-token", tmp_path / ".secrets")
    monkeypatch.setattr(notify_discord, "SECRETS_DIR", tmp_path / ".secrets")

    posted: list[str] = []
    monkeypatch.setattr(notify_discord, "post", lambda url, embeds: posted.append(url) or True)

    notify_discord.notify_sweep(make_scenario(), _empty_result(tmp_path))

    assert posted == ["https://discord.com/api/webhooks/1/local-token"]


def _empty_result(tmp_path):
    """A finished sweep that found nothing - enough to reach the post call via
    the health alert, without needing a full leg set."""
    from types import SimpleNamespace

    directory = tmp_path / "sweeps" / "s" / "2026-08-11T02-00-00Z"
    directory.mkdir(parents=True)
    return SimpleNamespace(
        legs=[], errors=["boom"], total=4, directory=directory,
        routes_with_no_results=[],
    )


def test_a_sweep_with_holes_says_so_beside_its_price():
    """A price you might book on has to come with how much was priced to find it.

    A sweep that answered 460 of 483 searches reports its cheapest total in
    exactly the same words as a complete one, and the difference is whether a
    cheaper trip was ever looked at.
    """
    picks = [pick("cheapest", 27000)]
    embed = build_price_embed("Trip", picks, bag_estimate=1500, coverage=0.87)
    assert "87%" in embed["description"]


def test_a_complete_sweep_does_not_caveat_its_price():
    embed = build_price_embed("Trip", [pick("cheapest", 27000)], bag_estimate=1500, coverage=1.0)
    assert "answered" not in embed["description"]


def test_a_sweep_that_never_recorded_coverage_does_not_caveat_either():
    """Every sweep committed before the field existed."""
    embed = build_price_embed("Trip", [pick("cheapest", 27000)], bag_estimate=1500, coverage=None)
    assert "answered" not in embed["description"]


# ------------------------------------------------------------- watch drops


def _drop(**overrides):
    defaults = dict(
        depart_date="2027-01-12",
        route="VIE → HND → MNL → VIE",
        total=26000.0,
        total_with_bags=27500.0,
        currency="CZK",
        previous_best=30000.0,
        drop=4000.0,
        drop_pct=13.3,
        has_overland=False,
    )
    defaults.update(overrides)
    return defaults


def test_a_watch_embed_names_the_day_that_fell():
    from src.notify_discord import build_watch_embed

    embed = build_watch_embed("Japan then Philippines", [_drop()])
    assert "Japan then Philippines" in embed["title"]
    field = embed["fields"][0]
    assert "2027-01-12" in field["name"]
    assert "26,000" in field["name"]
    assert "30,000" in field["value"]
    assert "13.3%" in field["value"]


def test_a_watch_embed_says_when_a_price_includes_a_journey_you_make_yourself():
    from src.notify_discord import build_watch_embed

    embed = build_watch_embed("Trip", [_drop(has_overland=True, route="VIE → HND ⇢ KIX → MNL")])
    assert "⇢" in embed["fields"][0]["value"]
    assert "overland" in embed["fields"][0]["value"].lower()


def test_nothing_fell_means_no_message_at_all():
    from src.notify_discord import build_watch_embed

    assert build_watch_embed("Trip", []) is None


def test_every_day_that_fell_gets_its_own_field():
    from src.notify_discord import build_watch_embed

    embed = build_watch_embed("Trip", [_drop(), _drop(depart_date="2027-01-19")])
    assert len(embed["fields"]) == 2


def test_notify_watch_posts_through_the_same_webhook_lookup(tmp_path, monkeypatch):
    """A webhook saved in the Sources tab has to reach a local watch too.

    `notify_sweep` read the environment directly once, so the setting existed
    and did nothing for anyone running the CLI by hand.
    """
    from src import notify_discord
    from src.webhook_store import save_webhook

    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    save_webhook("https://discord.com/api/webhooks/1/local-token", tmp_path / ".secrets")
    monkeypatch.setattr(notify_discord, "SECRETS_DIR", tmp_path / ".secrets")

    posted: list[str] = []
    monkeypatch.setattr(notify_discord, "post", lambda url, embeds: posted.append(url) or True)

    assert notify_discord.notify_watch(make_scenario(), [_drop()]) is True
    assert posted == ["https://discord.com/api/webhooks/1/local-token"]


def test_notify_watch_says_nothing_when_nothing_fell(tmp_path, monkeypatch):
    from src import notify_discord

    posted: list[str] = []
    monkeypatch.setattr(notify_discord, "post", lambda url, embeds: posted.append(url) or True)
    assert notify_discord.notify_watch(make_scenario(), []) is False
    assert posted == []
