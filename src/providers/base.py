from __future__ import annotations

from abc import ABC
from datetime import date
from typing import Iterable

from ..models import Leg, Offer


class BaseProvider(ABC):
    """Interface shared by flight sources.

    Two entry points exist during the migration to the scenario platform:

    - `search_leg` is the current interface. It searches a single one-way (or
      round-trip) hop and returns Legs, which the sweep runner assembles into
      itineraries.
    - `scrape` is the legacy round-trip interface still used by the
      `scrape`/`watch` CLI commands.

    Neither is abstract, so a provider may implement only the one it supports;
    calling the other raises NotImplementedError rather than failing at
    instantiation.
    """

    NAME: str = "BASE"

    def search_leg(
        self,
        page,
        origin: str,
        destination: str,
        depart: date,
        ret: date | None = None,
        adults: int = 1,
    ) -> list[Leg]:
        """Search one hop using a caller-supplied Playwright page."""
        raise NotImplementedError(f"{self.NAME} does not implement search_leg")

    def scrape(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        adults: int,
        arrival_date: str,
    ) -> Iterable[Offer]:
        """Legacy round-trip search."""
        raise NotImplementedError(f"{self.NAME} does not implement scrape")
