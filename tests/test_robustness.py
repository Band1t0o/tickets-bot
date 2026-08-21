"""Failure modes that were previously silent, wrong, or fatal.

Each test here corresponds to a bug that shipped. They are grouped in one file
because what they have in common is the failure shape, not the module: something
that looked fine and was not.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.combine import combine
from src.models import Itinerary, Leg
from src.notify_discord import load_best, save_best, should_alert
from src.sweep.runner import SweepResult, run_sweep
from tests.conftest import make_scenario


def leg(**overrides) -> Leg:
    defaults = dict(
        provider="T",
        origin="PRG",
        destination="NRT",
        depart_date=date(2027, 1, 10),
        airline="QR",
        flight_number=None,
        stops=1,
        price_currency="CZK",
        price_amount=10000.0,
        url="https://example.test/",
    )
    defaults.update(overrides)
    return Leg(**defaults)


# ------------------------------------------------- a leg whose date failed to parse


def test_a_leg_with_no_date_can_still_be_hashed():
    """The parser returns None on three paths, and _dedupe hashes before anything
    checks for it - so one markup tweak turned every search into a crash."""
    assert leg(depart_date=None).content_hash()


def test_two_undated_legs_are_distinguished_by_their_other_fields():
    assert leg(depart_date=None).content_hash() != leg(
        depart_date=None, price_amount=11000.0
    ).content_hash()


def test_a_leg_with_no_date_survives_a_json_round_trip():
    original = leg(depart_date=None)
    restored = Leg.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored.depart_date is None
    assert restored == original


def test_undated_legs_are_dropped_rather_than_chained():
    """A leg with no date cannot have its stay length checked, so it is not
    safe to build a trip from - but it must not take the sweep down either."""
    scenario = make_scenario()
    legs = [
        leg(origin="PRG", destination="NRT", depart_date=None),
        leg(origin="NRT", destination="MNL", depart_date=date(2027, 1, 20)),
        leg(origin="MNL", destination="PRG", depart_date=date(2027, 1, 30)),
    ]
    assert combine(legs, scenario) == []


# ------------------------------------------------------------------- currencies


def test_a_mixed_currency_total_is_refused_rather_than_summed():
    """Adding 10,000 CZK to 400 EUR produced 10,400 of nothing."""
    mixed = Itinerary(legs=[leg(price_currency="CZK"), leg(price_currency="EUR")])
    with pytest.raises(ValueError, match="currencies"):
        _ = mixed.total_price


def test_currencies_lists_what_an_itinerary_actually_mixes():
    mixed = Itinerary(legs=[leg(price_currency="CZK"), leg(price_currency="EUR")])
    assert mixed.currencies == {"CZK", "EUR"}


def test_the_combiner_never_chains_across_currencies():
    scenario = make_scenario()
    legs = [
        leg(origin="PRG", destination="NRT", depart_date=date(2027, 1, 10)),
        leg(
            origin="NRT",
            destination="MNL",
            depart_date=date(2027, 1, 20),
            price_currency="EUR",
            price_amount=200.0,
        ),
        leg(origin="MNL", destination="PRG", depart_date=date(2027, 1, 30)),
    ]
    assert combine(legs, scenario) == []


# --------------------------------------------------------------- the best ratchet


def test_a_threshold_alert_does_not_record_a_worse_best(tmp_path):
    """save_best used to be called whenever an alert was sent.

    With a threshold set, an alert fires on any total under it - including one
    worse than the recorded best - and the recorded best then walked upward,
    destroying the "only on genuine improvement" guarantee.
    """
    save_best(tmp_path, 25000.0, "CZK")
    save_best(tmp_path, 28000.0, "CZK")
    assert load_best(tmp_path) == 25000.0


def test_a_genuine_improvement_is_recorded(tmp_path):
    save_best(tmp_path, 25000.0, "CZK")
    save_best(tmp_path, 21000.0, "CZK")
    assert load_best(tmp_path) == 21000.0


def test_a_threshold_alerts_even_without_an_improvement():
    # The threshold exists to say "book it at this price regardless of trend".
    assert should_alert(24000.0, previous_best=23000.0, threshold=25000.0) is True


def test_without_a_threshold_only_improvements_alert():
    assert should_alert(24000.0, previous_best=23000.0, threshold=None) is False
    assert should_alert(22000.0, previous_best=23000.0, threshold=None) is True


# -------------------------------------------------------------------- the runner


class OneLegProvider:
    NAME = "FAKE"

    def search_leg(self, page, origin, destination, depart, ret=None, adults=1):
        return [leg(origin=origin, destination=destination, depart_date=depart)]


def test_zero_workers_still_runs_every_search(tmp_path):
    """`searches[i::workers]` with workers=0 is an empty list, so the sweep
    reported itself complete having run nothing at all."""
    result = run_sweep(
        make_scenario(depth="quick"),
        provider=OneLegProvider(),
        data_dir=tmp_path,
        workers=0,
        delay_s=0,
    )
    assert result.completed == result.total
    assert result.total > 0


def test_workers_get_contiguous_chunks_not_interleaved_ones():
    """Interleaving sent every worker at the same route simultaneously.

    That is the worst possible pattern against a per-route throttle, and the
    sweep that made timeouts visible reported 58 of 93 searches timing out.

    A chunk may be *rotated* now - the plan is dealt across routes, so two
    contiguous chunks would otherwise advance through the routes in step - but
    it is still one unbroken run of the plan rather than every nth search.
    """
    from src.sweep.planner import plan_searches
    from src.sweep.runner import _chunk

    plan = plan_searches(make_scenario(depth="quick"))
    for workers in (2, 3, 4):
        chunks = _chunk(plan, workers)
        assert sorted(plan.index(s) for c in chunks for s in c) == list(range(len(plan)))
        for chunk in chunks:
            at = sorted(plan.index(search) for search in chunk)
            assert at == list(range(at[0], at[-1] + 1)), "the chunk is not one run"


def test_a_sweep_that_finds_nothing_is_unhealthy():
    assert SweepResult(scenario_id="x", directory=Path("."), total=10).is_healthy is False


# ------------------------------------------------------------- the health alert


def test_legs_found_but_nothing_chaining_blames_the_trip_not_the_scraper():
    """This call site passed a hardcoded legs_found=0.

    So a sweep where hundreds of flights were found but none chained reported
    "all searches completed but returned no flights" - sending you to debug the
    scraper when the scraper had worked and the trip shape was the problem.
    """
    from src.notify_discord import build_health_alert

    body = json.dumps(build_health_alert("Trip", legs_found=412, errors=0, total=200, itineraries=0))
    assert "412" in body
    assert "returned no flights" not in body
    assert "stay ranges" in body


def test_the_health_alert_still_says_so_when_nothing_was_found():
    from src.notify_discord import build_health_alert

    embed = build_health_alert("Trip", legs_found=0, errors=0, total=200)
    assert "no flights" in json.dumps(embed).lower()


def test_a_healthy_sweep_raises_no_alert():
    from src.notify_discord import build_health_alert

    assert build_health_alert("Trip", legs_found=412, errors=0, total=200, itineraries=37) is None
