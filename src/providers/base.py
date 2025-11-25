from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Iterable
from ..models import Offer

class BaseProvider(ABC):
    NAME: str = "BASE"

    @abstractmethod
    def scrape(self, origin: str, destination: str, departure_date: str, adults: int, arrival_date: str) -> Iterable[Offer]:
        raise NotImplementedError("Subclass must implement scrape method")