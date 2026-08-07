"""Parser tests against a saved pelikan.cz results page.

Uses a real captured page so the parser can be exercised without a browser or
network. If pelikan.cz changes its markup these tests fail loudly, which is the
point: a sweep that silently returns zero legs looks identical to "no cheap
flights today".
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.providers.pelikan import parse_results_html

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
