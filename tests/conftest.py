"""Shared test fixtures.

Tests here must never touch the network or launch a browser: the scrapers are
slow (~15s per real search) and the sites are third-party. Provider behaviour is
tested against saved HTML fixtures instead.

The scenario builders live here rather than in each test module because they
used to be copied into four files, and every schema change meant finding all
four. A test that wants a different shape passes overrides.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.scenario import Scenario, Stop

WINDOW_START = date(2027, 1, 5)
WINDOW_END = date(2027, 2, 8)


def make_scenario(**overrides) -> Scenario:
    """A two-stop trip: origins -> stop 1 -> stop 2 -> home."""
    defaults = dict(
        id="japan-philippines",
        name="Japan then Philippines",
        origins=["PRG", "VIE", "FRA"],
        stops=[
            Stop(airports=["NRT", "HND", "KIX"], stay_days=(9, 11), label="Japan"),
            Stop(airports=["MNL", "CEB"], stay_days=(9, 11), label="Philippines"),
        ],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    defaults.update(overrides)
    return Scenario(**defaults)


def make_round_trip(**overrides) -> Scenario:
    """One stop and back: the shape that could never produce an itinerary."""
    defaults = dict(
        id="tokyo",
        name="Tokyo return",
        origins=["PRG"],
        stops=[Stop(airports=["NRT"], stay_days=(18, 20), label="Japan")],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        depth="quick",
    )
    defaults.update(overrides)
    return Scenario(**defaults)


def make_three_stop(**overrides) -> Scenario:
    """A shape the old three-block planner could not express at all."""
    defaults = dict(
        id="grand-tour",
        name="Three stops",
        origins=["PRG"],
        stops=[
            Stop(airports=["NRT"], stay_days=(7, 9), label="Japan"),
            Stop(airports=["MNL"], stay_days=(7, 9), label="Philippines"),
            Stop(airports=["BKK"], stay_days=(5, 7), label="Thailand"),
        ],
        window_start=WINDOW_START,
        window_end=date(2027, 3, 15),
        depth="quick",
    )
    defaults.update(overrides)
    return Scenario(**defaults)


@pytest.fixture
def make_leg():
    """Build a Leg with sensible defaults; override only what a test cares about."""
    # Imported lazily so test collection works before src.models defines Leg.
    from src.models import Leg

    def _make(
        origin: str = "PRG",
        destination: str = "NRT",
        depart_date: date = date(2027, 1, 12),
        airline: str = "Qatar Airways",
        flight_number: str = "QR8100",
        stops: int = 1,
        price_amount: float = 14480.0,
        price_currency: str = "CZK",
        url: str = "https://www.pelikan.cz/cs/letenky/example/",
        provider: str = "PELIKAN",
        checked_bag: bool | None = None,
        observed_at: str | None = None,
    ) -> Leg:
        return Leg(
            provider=provider,
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            airline=airline,
            flight_number=flight_number,
            stops=stops,
            price_currency=price_currency,
            price_amount=price_amount,
            url=url,
            checked_bag=checked_bag,
            observed_at=observed_at,
        )

    return _make
