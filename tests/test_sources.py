"""The scraper's moving parts, held in a file you can edit.

Sites change their URL grammar and their markup, and both break a sweep in
ways that need no code to fix - only the new string. This is that file.

Defaults live in code, not in the file. A missing or corrupt sources.json must
degrade to today's behaviour rather than stop a sweep, on the same principle
that keeps a corrupt best.json from silencing a report.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from src.sources import DEFAULTS, load_source, save_sources


def test_the_defaults_describe_pelikan_without_any_file(tmp_path):
    source = load_source("PELIKAN", data_dir=tmp_path)
    assert source.base_url.startswith("https://www.pelikan.cz")
    assert source.selectors["card"] == "div[id^='flight-']"
    assert "Nena" in source.no_results_marker


def test_a_missing_file_does_not_raise(tmp_path):
    assert load_source("PELIKAN", data_dir=tmp_path) == DEFAULTS["PELIKAN"]


def test_a_corrupt_file_falls_back_to_the_defaults(tmp_path):
    (tmp_path / "sources.json").write_text("{not json", encoding="utf-8")
    assert load_source("PELIKAN", data_dir=tmp_path) == DEFAULTS["PELIKAN"]


def test_an_edited_value_overrides_the_default(tmp_path):
    (tmp_path / "sources.json").write_text(
        json.dumps({"PELIKAN": {"base_url": "https://www.pelikan.cz/en/flights/"}}),
        encoding="utf-8",
    )
    source = load_source("PELIKAN", data_dir=tmp_path)
    assert source.base_url == "https://www.pelikan.cz/en/flights/"
    # Everything not overridden still comes from the defaults, so fixing one
    # string does not require restating the whole file.
    assert source.selectors["card"] == DEFAULTS["PELIKAN"].selectors["card"]


def test_editing_one_selector_keeps_the_others(tmp_path):
    (tmp_path / "sources.json").write_text(
        json.dumps({"PELIKAN": {"selectors": {"card": "div.offer"}}}), encoding="utf-8"
    )
    source = load_source("PELIKAN", data_dir=tmp_path)
    assert source.selectors["card"] == "div.offer"
    assert source.selectors["price"] == DEFAULTS["PELIKAN"].selectors["price"]


def test_an_unknown_source_raises_rather_than_returning_an_empty_one(tmp_path):
    with pytest.raises(KeyError, match="NOPE"):
        load_source("NOPE", data_dir=tmp_path)


def test_sources_round_trip_through_disk(tmp_path):
    source = load_source("PELIKAN", data_dir=tmp_path)
    save_sources({"PELIKAN": source}, data_dir=tmp_path)
    assert load_source("PELIKAN", data_dir=tmp_path) == source


# ------------------------------------------------------------ the URL grammar


def test_the_url_is_built_from_the_configured_template(tmp_path):
    from src.providers.pelikan_url import build_search_url

    source = load_source("PELIKAN", data_dir=tmp_path)
    url = build_search_url("PRG", "NRT", date(2027, 1, 12), source=source)
    assert url.startswith(source.base_url)
    # Dates are bare integers - 2027_1_12, never 2027_01_12.
    assert "DD:2027_1_12" in url
    assert "T:2" in url and "CDF:PRGPRG" in url and "CDT:ANRT" in url


def test_a_changed_base_url_reaches_the_built_url(tmp_path):
    """The whole point: a site moves its path and you fix it without code."""
    from src.providers.pelikan_url import build_search_url

    (tmp_path / "sources.json").write_text(
        json.dumps({"PELIKAN": {"base_url": "https://www.pelikan.cz/sk/letenky/"}}),
        encoding="utf-8",
    )
    url = build_search_url(
        "PRG", "NRT", date(2027, 1, 12), source=load_source("PELIKAN", data_dir=tmp_path)
    )
    assert url.startswith("https://www.pelikan.cz/sk/letenky/")


def test_building_a_url_without_a_source_uses_the_defaults(tmp_path):
    """Callers that never heard of sources.json keep working unchanged."""
    from src.providers.pelikan_url import build_search_url

    assert build_search_url("PRG", "NRT", date(2027, 1, 12)).startswith(
        DEFAULTS["PELIKAN"].base_url
    )


def test_a_round_trip_url_carries_the_return_date(tmp_path):
    from src.providers.pelikan_url import build_search_url

    url = build_search_url("PRG", "NRT", date(2027, 1, 12), date(2027, 1, 30))
    assert "T:1" in url and "DR:2027_1_30" in url


# --------------------------------------------------------------- the parser


def test_the_parser_reads_the_configured_card_selector(tmp_path):
    """Change the selector to something absent and nothing parses.

    This is what makes a broken-markup diagnosis possible from the UI: if the
    selector is wrong the count goes to zero, loudly.
    """
    from pathlib import Path

    from src.providers.pelikan import parse_results_html

    fixture = (Path(__file__).parent / "fixtures" / "pelikan_results.html").read_text(
        encoding="utf-8"
    )
    working = load_source("PELIKAN", data_dir=tmp_path)
    assert parse_results_html(fixture, "PRG", "NRT", source=working)

    from dataclasses import replace

    broken = replace(working, selectors={**working.selectors, "card": "div.nothing-here"})
    assert parse_results_html(fixture, "PRG", "NRT", source=broken) == []
