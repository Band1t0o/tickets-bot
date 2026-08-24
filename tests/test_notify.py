"""Tests for price-improvement and health alerting."""
from __future__ import annotations

import json
from datetime import date

from src.models import Itinerary, Leg
from src.notify_discord import (
    COLOR_GOOD,
    COLOR_RISE,
    build_health_alert,
    build_price_embed,
    load_best,
    load_last,
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


# ------------------------------------------------------- which way it moved
#
# The webhook secret only reached Actions on 21 Aug, so `best.json` had never
# once been written and the "beat the best so far" alert had never fired. What
# was wanted alongside it was the other direction: a total that has jumped since
# the last run, as a sign the fare is climbing and the window is closing.
#
# Both are said on the same message rather than sent as two. There is one price
# a night and one thing worth knowing about it - which way it went - and two
# posts about one number is how a watcher becomes something you mute.


def test_a_first_reading_says_it_is_the_first():
    embed = build_price_embed("S", [pick("cheapest", 30000)])
    assert "first" in embed["description"].lower(), embed["description"]


def test_a_new_best_is_green_and_says_so():
    embed = build_price_embed(
        "S", [pick("cheapest", 28000)], previous_best=30000, previous_last=30000
    )
    assert embed["color"] == COLOR_GOOD
    assert "down 2,000" in embed["description"]
    assert "best" in embed["description"].lower()
    assert "new best" in embed["title"], embed["title"]


def test_a_rise_since_the_last_run_is_amber_and_names_what_it_rose_from():
    """The signal asked for: not "this is dear" but "this got dearer", which is
    what says the window is closing rather than that Frankfurt exists."""
    embed = build_price_embed(
        "S", [pick("cheapest", 33000)], previous_best=28000, previous_last=31000
    )
    assert embed["color"] == COLOR_RISE
    assert "up 2,000" in embed["description"], embed["description"]
    assert "31,000" in embed["description"]
    assert "↗" in embed["title"] or "up since" in embed["title"]


def test_a_rise_too_small_to_mean_anything_is_not_called_a_rise():
    """1% is the same bar `watch.py` uses for a fall. Below it a fare has not
    moved, it has rounded, and a watcher that says so nightly gets muted."""
    embed = build_price_embed(
        "S", [pick("cheapest", 30100)], previous_best=28000, previous_last=30000
    )
    assert embed["color"] != COLOR_RISE
    assert "up" not in embed["description"], embed["description"]
    assert "unchanged" in embed["description"].lower()


def test_a_fall_that_is_still_not_a_best_says_both_things():
    """Cheaper than yesterday and dearer than the best is the common case, and
    reporting only the first would read as news it is not."""
    embed = build_price_embed(
        "S", [pick("cheapest", 29000)], previous_best=27000, previous_last=31000
    )
    assert "down 2,000" in embed["description"]
    assert "27,000" in embed["description"], "the best it is still above is not stated"
    assert embed["color"] != COLOR_GOOD


def test_an_unchanged_price_says_unchanged_rather_than_repeating_the_number():
    embed = build_price_embed(
        "S", [pick("cheapest", 30000)], previous_best=28000, previous_last=30000
    )
    assert "unchanged" in embed["description"].lower()


def test_the_last_total_is_recorded_even_when_it_is_not_a_best(tmp_path):
    """Without this there is nothing to compare tomorrow against, and a rise can
    never be seen at all: `best_total` only ever walks downward by design."""
    save_best(tmp_path, 30000, "CZK")
    save_best(tmp_path, 34000, "CZK")
    assert load_best(tmp_path) == 30000, "the best walked upward"
    assert load_last(tmp_path) == 34000


def test_the_last_total_follows_a_new_best_too(tmp_path):
    save_best(tmp_path, 30000, "CZK")
    save_best(tmp_path, 27000, "CZK")
    assert load_best(tmp_path) == 27000
    assert load_last(tmp_path) == 27000


def test_picks_keep_their_own_history(tmp_path):
    """A tier-1 trip dropping 3,000 is news on a day the outright cheapest did
    not move, and one shared figure hid exactly that."""
    save_best(tmp_path, 30000, "CZK", "cheapest")
    save_best(tmp_path, 34000, "CZK", "preferred")
    assert load_last(tmp_path, "cheapest") == 30000
    assert load_last(tmp_path, "preferred") == 34000


def test_load_last_tolerates_a_state_file_written_before_it_existed(tmp_path):
    """Every `best.json` on disk predates this field. Reading one must mean
    "nothing to compare against", not a crash in the reporting step."""
    (tmp_path / "best.json").write_text(
        json.dumps({"cheapest": {"best_total": 30000, "currency": "CZK"}}),
        encoding="utf-8",
    )
    assert load_last(tmp_path) is None
    assert load_best(tmp_path) == 30000


def test_a_state_file_with_a_best_but_no_previous_run_is_read_honestly(tmp_path):
    """Every `best.json` written before `last_total` existed is in this state.
    It can say where the total sits against the best; it cannot claim a
    direction it has nothing to measure from."""
    equal = build_price_embed("S", [pick("cheapest", 30000)], previous_best=30000)
    assert "level with the best" in equal["description"]
    assert "up" not in equal["description"] and "down" not in equal["description"]

    above = build_price_embed("S", [pick("cheapest", 32000)], previous_best=30000)
    assert "above the best of 30,000" in above["description"]


# ------------------------------------------ the two sweeps report separately
#
# A broad sweep and a final sweep price different things: the whole window
# against the handful of days it was narrowed to. The narrowed cheapest is
# almost always the dearer of the two, and it is not a rise.
#
# They shared one high-water mark under `best.json["cheapest"]` until the two
# modes were split apart, which was harmless only while every sweep was narrowed.
# Measured on 24 Aug, immediately after the split: a final run recorded 25,967
# over the broad runs' 21,445, so the next broad sweep would have reported a
# 4,500 CZK drop that no fare ever made -- the trend-chart bug, one layer down.


def test_a_final_sweep_records_its_best_apart_from_the_broad_one(tmp_path):
    from src.notify_discord import alert_key

    save_best(tmp_path, 21445.0, "CZK", alert_key("cheapest", "sweep"))
    save_best(tmp_path, 25967.0, "CZK", alert_key("cheapest", "final"))

    assert load_best(tmp_path, alert_key("cheapest", "sweep")) == 21445.0
    assert load_best(tmp_path, alert_key("cheapest", "final")) == 25967.0


def test_the_broad_key_is_unchanged_so_committed_history_still_reads(tmp_path):
    """`best.json` is committed, and every entry in it was written as `cheapest`.

    Renaming the broad key would restart the high-water mark from nothing and
    report the next ordinary sweep as an all-time low.
    """
    from src.notify_discord import alert_key

    assert alert_key("cheapest", "sweep") == "cheapest"
    assert alert_key("preferred", "sweep") == "preferred"
    assert alert_key("cheapest", "final") == "final:cheapest"


def test_a_narrowed_run_is_not_read_as_a_rise_against_a_broad_one(tmp_path):
    """The reading that would have been wrong, stated as the two calls it is."""
    from src.notify_discord import alert_key

    save_best(tmp_path, 21445.0, "CZK", alert_key("cheapest", "sweep"))

    # Nothing narrowed has been recorded yet, so the first final run is news on
    # its own terms rather than a 4,500 rise against a measurement of the window.
    assert load_best(tmp_path, alert_key("cheapest", "final")) is None
    assert should_alert(25967.0, load_best(tmp_path, alert_key("cheapest", "final")), None)
    assert not should_alert(25967.0, load_best(tmp_path, alert_key("cheapest", "sweep")), None)
