"""Tests for the pelikan.cz deep-link URL grammar.

The grammar was derived empirically by observing the URL the site produces after
a form search, then sweeping variants to find which trip-type codes return
results. `T:1` is round trip and `T:2` is one-way; `T:0` and `R:0` return
nothing at all, so they must never be generated.
"""
from __future__ import annotations

from datetime import date

from src.providers.pelikan_url import build_search_url


def test_one_way_uses_T2_and_no_DR():
    url = build_search_url("PRG", "NRT", date(2027, 1, 10), None, 1)
    assert url == (
        "https://www.pelikan.cz/cs/letenky/"
        "T:2,P:1000E_0_0,CDF:PRGPRG,CDT:ANRT,DD:2027_1_10/"
    )


def test_round_trip_uses_T1_and_DR():
    url = build_search_url("PRG", "NRT", date(2027, 1, 10), date(2027, 1, 20), 1)
    assert "T:1," in url
    assert url.endswith("DD:2027_1_10,DR:2027_1_20/")


def test_month_and_day_are_not_zero_padded():
    # The site uses bare integers: 2027_2_3, never 2027_02_03.
    url = build_search_url("VIE", "MNL", date(2027, 2, 3), None, 1)
    assert "DD:2027_2_3/" in url


def test_adults_encoded_in_passenger_block():
    assert "P:2000E_0_0" in build_search_url("PRG", "NRT", date(2027, 1, 10), None, 2)


def test_origin_iata_is_doubled_and_destination_is_prefixed():
    # CDF repeats the code; CDT prefixes it with "A". Asymmetric, but that is
    # what the site emits.
    url = build_search_url("FRA", "KIX", date(2027, 1, 12), None, 1)
    assert "CDF:FRAFRA" in url
    assert "CDT:AKIX" in url


def test_return_before_departure_is_rejected():
    try:
        build_search_url("PRG", "NRT", date(2027, 1, 20), date(2027, 1, 10), 1)
    except ValueError:
        return
    raise AssertionError("expected ValueError for return date before departure")
