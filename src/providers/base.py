from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ..models import Leg


class BaseProvider(ABC):
    """Interface shared by flight sources.

    One method now. A second, `scrape`, existed alongside it during the
    migration to the scenario platform and returned a different model for a
    single round trip; nothing in the sweep path ever called it. Providers that
    cannot be swept - letuska.cz has no deep-link grammar - expose their own
    method instead of pretending to fit here.
    """

    NAME: str = "BASE"

    @abstractmethod
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
