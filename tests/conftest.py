"""Shared test fixtures.

Tests here must never touch the network or launch a browser: the scrapers are
slow (~15s per real search) and the sites are third-party. Provider behaviour is
tested against saved HTML fixtures instead.
"""
from __future__ import annotations

from datetime import date

import pytest


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
        )

    return _make
