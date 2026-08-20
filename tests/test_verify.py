"""A second opinion on the trip you are about to book.

Not on all 900 legs - on the handful that make up the shortlist. Sweeping
letuska is unaffordable (~60-90s a search against pelikan's ~14s, because it
has no deep link), but re-pricing five legs is minutes, and five legs is what
the decision actually rests on.

No browser here: the checker is a callable, so these run against a fake.
"""
from __future__ import annotations

import json
from datetime import date

from src.verify import verify_shortlist
from tests.conftest import make_scenario


def trip(make_leg, origin, price):
    return [
        make_leg(origin=origin, destination="NRT", depart_date=date(2027, 1, 12),
                 price_amount=price - 6000, checked_bag=True),
        make_leg(origin="NRT", destination="MNL", depart_date=date(2027, 1, 22),
                 price_amount=3000, checked_bag=True),
        make_leg(origin="MNL", destination=origin, depart_date=date(2027, 2, 1),
                 price_amount=3000, checked_bag=True),
    ]


def checker(prices: dict):
    """Stands in for letuska. `prices` maps "PRG->NRT" to what it quotes."""
    calls = []

    def _check(origin, destination, depart):
        calls.append((origin, destination, depart))
        quoted = prices.get(f"{origin}->{destination}")
        return [] if quoted is None else [quoted]

    _check.calls = calls
    return _check


def test_it_prices_only_the_legs_of_the_top_itineraries(make_leg):
    legs = trip(make_leg, "FRA", 21000) + trip(make_leg, "PRG", 30000)
    check = checker({})
    verify_shortlist(legs, make_scenario(origins=["PRG", "FRA"]), check, top=1)
    # One three-leg trip, so three searches - not the twelve legs on disk.
    assert len(check.calls) == 3


def test_a_leg_shared_by_two_itineraries_is_priced_once(make_leg):
    """The top itineraries overlap heavily; re-pricing NRT->MNL five times is
    five wasted minutes against a site that takes a minute a search."""
    legs = trip(make_leg, "FRA", 21000) + trip(make_leg, "PRG", 30000)
    check = checker({})
    verify_shortlist(legs, make_scenario(origins=["PRG", "FRA"]), check, top=5)
    assert len(check.calls) == len(set(check.calls))


def test_agreement_within_tolerance_is_reported_as_agreement(make_leg):
    legs = trip(make_leg, "FRA", 21000)
    check = checker({"FRA->NRT": 15200.0, "NRT->MNL": 3000.0, "MNL->FRA": 3000.0})
    report = verify_shortlist(legs, make_scenario(origins=["FRA"]), check, top=1)
    assert report["verdict"] == "agrees"
    assert report["cheapest_elsewhere"] is None


def test_a_materially_cheaper_quote_is_surfaced(make_leg):
    """The only finding worth interrupting you for: the same leg, less money."""
    legs = trip(make_leg, "FRA", 21000)
    check = checker({"FRA->NRT": 12000.0, "NRT->MNL": 3000.0, "MNL->FRA": 3000.0})
    report = verify_shortlist(legs, make_scenario(origins=["FRA"]), check, top=1)
    assert report["verdict"] == "cheaper_elsewhere"
    finding = report["cheapest_elsewhere"]
    assert finding["route"] == "FRA->NRT"
    assert finding["ours"] == 15000.0
    assert finding["theirs"] == 12000.0
    assert finding["saving_pct"] == 20.0


def test_a_dearer_quote_confirms_rather_than_alarms(make_leg):
    legs = trip(make_leg, "FRA", 21000)
    check = checker({"FRA->NRT": 18000.0, "NRT->MNL": 3000.0, "MNL->FRA": 3000.0})
    assert verify_shortlist(legs, make_scenario(origins=["FRA"]), check, top=1)["verdict"] == "agrees"


def test_a_leg_the_other_site_cannot_price_is_recorded_not_ignored(make_leg):
    """Silence from the second source is not confirmation from it."""
    legs = trip(make_leg, "FRA", 21000)
    check = checker({"FRA->NRT": 15200.0})
    report = verify_shortlist(legs, make_scenario(origins=["FRA"]), check, top=1)
    assert report["unpriced"] == ["MNL->FRA", "NRT->MNL"]
    assert report["verdict"] == "partial"


def test_a_failing_checker_does_not_take_the_sweep_down_with_it(make_leg):
    def explode(origin, destination, depart):
        raise RuntimeError("letuska changed its form again")

    legs = trip(make_leg, "FRA", 21000)
    report = verify_shortlist(legs, make_scenario(origins=["FRA"]), explode, top=1)
    assert report["verdict"] == "unavailable"
    assert "letuska changed its form" in report["error"]


def test_nothing_to_verify_when_no_itinerary_exists(make_leg):
    report = verify_shortlist([], make_scenario(), checker({}), top=3)
    assert report["verdict"] == "nothing_to_check"


def test_the_report_records_when_it_was_taken(make_leg):
    """A second opinion from last week is not a second opinion."""
    legs = trip(make_leg, "FRA", 21000)
    report = verify_shortlist(legs, make_scenario(origins=["FRA"]), checker({}), top=1)
    assert report["checked_at"]


def test_the_report_is_json_serialisable(make_leg):
    legs = trip(make_leg, "FRA", 21000)
    report = verify_shortlist(legs, make_scenario(origins=["FRA"]), checker({}), top=1)
    assert json.loads(json.dumps(report)) == report


# ------------------------------------------------------- the letuska checker
#
# letuska returns neighbouring days as well as the one asked for ("Lety i v
# okolních dnech"), so taking the cheapest quote outright would compare our
# 3 February leg against their 2 February one and call the difference a saving.


def test_the_checker_keeps_only_quotes_for_the_date_asked_for(monkeypatch):
    from src.models import Leg
    from src.verify import letuska_checker

    def fake(origin, destination, depart, ret=None, adults=1):
        return [
            Leg("LETUSKA", origin, destination, date(2027, 2, 2), "PR", None, 0, "CZK", 4000.0, ""),
            Leg("LETUSKA", origin, destination, date(2027, 2, 3), "PR", None, 0, "CZK", 5543.0, ""),
            Leg("LETUSKA", origin, destination, date(2027, 2, 4), "PR", None, 0, "CZK", 4200.0, ""),
        ]

    import src.providers.letuska as letuska_module

    monkeypatch.setattr(letuska_module.LetuskaProvider, "check_price", staticmethod(fake))
    assert letuska_checker()("NRT", "MNL", date(2027, 2, 3)) == [5543.0]


def test_the_checker_returns_nothing_when_the_requested_date_is_absent(monkeypatch):
    """Better to report the leg as unpriced than to compare a different day."""
    from src.models import Leg
    from src.verify import letuska_checker

    def fake(origin, destination, depart, ret=None, adults=1):
        return [
            Leg("LETUSKA", origin, destination, date(2027, 2, 2), "PR", None, 0, "CZK", 4000.0, "")
        ]

    import src.providers.letuska as letuska_module

    monkeypatch.setattr(letuska_module.LetuskaProvider, "check_price", staticmethod(fake))
    assert letuska_checker()("NRT", "MNL", date(2027, 2, 3)) == []


def test_the_checker_asks_for_a_one_way(monkeypatch):
    """A same-day round trip was priced as a return and read 2.2x dearer."""
    seen = {}

    def fake(origin, destination, depart, ret=None, adults=1):
        seen["ret"] = ret
        return []

    import src.providers.letuska as letuska_module
    from src.verify import letuska_checker

    monkeypatch.setattr(letuska_module.LetuskaProvider, "check_price", staticmethod(fake))
    letuska_checker()("NRT", "MNL", date(2027, 2, 3))
    assert seen["ret"] is None
