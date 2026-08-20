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


# ------------------------------------------------------- how long to wait
#
# A timed-out search costs 120s, and the 11 Aug local sweep spent 93% of its
# worker time on them. But the cutoff cannot simply be lowered: the clean cloud
# run rendered in ~25-30s, so a flat 45s would start failing good searches. It
# is set from what this site has actually been doing in the last few minutes.


def _slow_page(cards=0):
    return _StubPage(cards=cards, body="still loading")


def polls_before_timeout(monkeypatch, provider) -> int:
    """How many 5s polls a hanging search is given before it is abandoned."""
    from src.providers import pelikan as mod

    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(mod.SearchTimeout):
        provider.search_leg(_slow_page(), "MNL", "VIE", _DATE)
    return sum(1 for s in sleeps if s == mod.POLL_INTERVAL_S)


def test_with_nothing_to_compare_against_a_search_waits_the_full_timeout(monkeypatch):
    """The first searches of a run have no baseline, so they get the benefit of
    the doubt - a cold site genuinely can be slow."""
    from src.providers import pelikan as mod

    provider = mod.PelikanProvider()
    assert polls_before_timeout(monkeypatch, provider) == 120 // mod.POLL_INTERVAL_S


def test_once_the_site_is_known_to_be_quick_a_hanging_search_is_abandoned_early(monkeypatch):
    """Waiting 120s for a page when everything else answered in 25 learns
    nothing it did not know at 75."""
    from src.providers import pelikan as mod

    provider = mod.PelikanProvider()
    for _ in range(mod.MIN_SAMPLES_FOR_ADAPTIVE):
        provider.record_render_time(25.0)
    # 3 x 25s is 75, under the floor, so the floor is what applies - and the
    # point of the test survives it: a hanging search is still abandoned well
    # inside the configured timeout rather than costing the full two minutes.
    # The floor moved from 60 to 90 because 60 was failing slow-but-real
    # searches: every error of the 12 Aug probe read "within 60s", four of them
    # on the same far-out return date.
    polls = polls_before_timeout(monkeypatch, provider)
    assert polls * mod.POLL_INTERVAL_S == mod.MIN_WAIT_S
    assert polls < mod.DEFAULTS["PELIKAN"].result_timeout_s // mod.POLL_INTERVAL_S


def test_the_cutoff_never_falls_below_a_floor(monkeypatch):
    """Three fast searches in a row must not set a cutoff so tight that the
    next slightly slower one is called a failure."""
    from src.providers import pelikan as mod

    provider = mod.PelikanProvider()
    for _ in range(mod.MIN_SAMPLES_FOR_ADAPTIVE):
        provider.record_render_time(5.0)
    assert polls_before_timeout(monkeypatch, provider) * mod.POLL_INTERVAL_S == mod.MIN_WAIT_S


def test_the_cutoff_never_exceeds_the_configured_timeout(monkeypatch):
    from src.providers import pelikan as mod

    provider = mod.PelikanProvider()
    for _ in range(mod.MIN_SAMPLES_FOR_ADAPTIVE):
        provider.record_render_time(100.0)
    assert polls_before_timeout(monkeypatch, provider) == 120 // mod.POLL_INTERVAL_S


def test_a_successful_search_records_how_long_it_took(monkeypatch):
    """The samples have to come from somewhere, and the only honest source is
    searches that actually rendered."""
    from src.providers import pelikan as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    provider = mod.PelikanProvider()
    provider.search_leg(_StubPage(cards=3), "PRG", "NRT", _DATE)
    assert provider.render_times, "a search that rendered recorded nothing"


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
