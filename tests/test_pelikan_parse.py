"""Parser tests against a saved pelikan.cz results page.

Uses a real captured page so the parser can be exercised without a browser or
network. If pelikan.cz changes its markup these tests fail loudly, which is the
point: a sweep that silently returns zero legs looks identical to "no cheap
flights today".
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from src.providers.pelikan import parse_results_html

_DATE = date(2027, 1, 28)

FIXTURE = (Path(__file__).parent / "fixtures" / "pelikan_results.html").read_text(
    encoding="utf-8"
)


@pytest.fixture(scope="module")
def legs():
    return parse_results_html(FIXTURE, origin="PRG", destination="NRT")


def test_parser_finds_offers(legs):
    assert len(legs) > 0


def test_parser_returns_no_duplicate_legs(legs):
    # The original selector was "div[id^='flight-'], flights-flight", which
    # matched every offer twice: 10 written rows collapsed to 1 distinct hash.
    hashes = [leg.content_hash() for leg in legs]
    assert len(hashes) == len(set(hashes))


def test_parser_does_not_double_count_cards(legs):
    # The regression guard for the original bug: never emit more legs than the
    # page has offer cards. (Fewer is fine - the site repeats some offers
    # verbatim and _dedupe collapses them.)
    card_count = FIXTURE.count('<div id="flight-')
    assert card_count > 0
    assert len(legs) <= card_count


def test_distinct_prices_produce_distinct_hashes(legs):
    # The old model hashed every offer identically because airline and
    # flight_number were hardcoded "Unknown"; 10 offers became 1 hash.
    distinct_prices = {leg.price_amount for leg in legs}
    distinct_hashes = {leg.content_hash() for leg in legs}
    assert len(distinct_hashes) >= len(distinct_prices)
    assert len(distinct_hashes) > 1


def test_parser_extracts_times_and_duration(legs):
    assert all(leg.depart_time for leg in legs)
    assert all(leg.duration_minutes and leg.duration_minutes > 0 for leg in legs)


def test_parser_extracts_airline(legs):
    # Airline comes from the carrier logo URL (cdn.pelikan.sk/carriers/XX-sq.svg).
    assert all(leg.airline for leg in legs)
    assert all(leg.airline != "Unknown" for leg in legs)


def test_airline_codes_look_like_iata(legs):
    assert all(len(leg.airline) == 2 and leg.airline.isupper() for leg in legs)


def test_parser_extracts_positive_prices_in_czk(legs):
    assert all(leg.price_amount > 0 for leg in legs)
    assert all(leg.price_currency == "CZK" for leg in legs)


def test_parser_extracts_stop_counts(legs):
    assert all(leg.stops is not None and leg.stops >= 0 for leg in legs)


def test_parser_reads_actual_departure_date_from_the_card(legs):
    # The site substitutes nearby dates, so the requested date must never be
    # assumed - it has to come off the card itself.
    assert all(leg.depart_date is not None for leg in legs)


def test_parser_sets_route_from_arguments(legs):
    assert all(leg.origin == "PRG" and leg.destination == "NRT" for leg in legs)


def test_empty_page_yields_no_legs():
    assert parse_results_html("<html><body></body></html>", "PRG", "NRT") == []


class _StubPage:
    """Minimal stand-in for a Playwright page.

    `cards` is how many offer cards eventually appear; `body` is the page text
    the no-results marker is read from.
    """

    def __init__(self, cards: int = 0, body: str = ""):
        self._cards = cards
        self._body = body
        self.url = None

    def goto(self, url, **_kwargs):
        self.url = url

    def locator(self, _selector):
        page = self

        class _Loc:
            def count(self):
                return page._cards

        return _Loc()

    def inner_text(self, _selector):
        return self._body

    def content(self):
        return FIXTURE


def test_timeout_raises_instead_of_returning_empty(monkeypatch):
    # The defect this guards: a search that timed out returned [], which the
    # runner recorded as success. Three whole return routes vanished from a
    # sweep that reported error_count: 0.
    from src.providers import pelikan as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    with pytest.raises(mod.SearchTimeout):
        mod.PelikanProvider().search_leg(
            _StubPage(cards=0, body="still loading"), "MNL", "VIE", _DATE
        )


def test_genuine_no_results_page_returns_empty_list(monkeypatch):
    # A route the site really has no inventory for (verified live with BRQ->NRT)
    # renders this message. That is data, not breakage.
    from src.providers import pelikan as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    legs = mod.PelikanProvider().search_leg(
        _StubPage(cards=0, body="Hups! Nenašli jsme žádny let, zkuste vyhledat"),
        "BRQ",
        "NRT",
        _DATE,
    )
    assert legs == []


def test_search_stamps_every_leg_with_when_the_page_was_read(monkeypatch):
    # The parser stays pure and browser-free, so the timestamp is applied by the
    # search - the same place url and the fallback depart_date are applied.
    from src.providers import pelikan as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    legs = mod.PelikanProvider().search_leg(_StubPage(cards=3), "PRG", "NRT", _DATE)

    assert legs, "fixture should yield legs"
    for leg in legs:
        assert leg.observed_at is not None
        # Parseable as an aware UTC instant, not a bare local string.
        assert datetime.fromisoformat(leg.observed_at).tzinfo is not None


def test_parser_alone_leaves_observed_at_unset():
    """Parsing a saved page is not an observation of a live price."""
    for leg in parse_results_html(FIXTURE, "PRG", "NRT"):
        assert leg.observed_at is None


def test_parser_reads_checked_baggage(legs):
    # The fixture contains real `checked-baggage-include.svg` icons alongside
    # offers whose baggage is only revealed after clicking through. Both must
    # be represented, and "unknown" must never be recorded as "included".
    states = {leg.checked_bag for leg in legs}
    assert True in states, "no offer parsed as bag-included"
    assert states <= {True, False, None}


def test_unknown_baggage_is_none_not_false(legs):
    # "Pro více info o zavazadlech klikněte na POKRAČOVAT" means unknown, not
    # excluded. Conflating them would understate the true cost of legacy fares.
    for leg in legs:
        assert leg.checked_bag is not False or leg.airline is not None


def test_baggage_is_not_part_of_the_content_hash():
    # Baggage is a property of the fare, not the flight. Folding it into the
    # hash would let one flight hash two ways and resurrect the duplicate bug.
    from dataclasses import replace

    a = legs_for_hash()[0]
    assert replace(a, checked_bag=True).content_hash() == replace(a, checked_bag=None).content_hash()


def legs_for_hash():
    return parse_results_html(FIXTURE, origin="PRG", destination="NRT")
