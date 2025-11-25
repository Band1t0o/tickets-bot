from __future__ import annotations
from typing import Iterable
from ..models import Offer
from .base import BaseProvider

class DemoStaticProvider(BaseProvider):
    NAME = "DEMO_STATIC"

    def scrape(self, origin: str, destination: str, departure_date: str, adults: int, arrival_date: str) -> Iterable[Offer]:
        # Produces a couple of fake offers to validate the pipeline end-to-end.
        return [
            Offer(
                provider=self.NAME,
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                return_date=arrival_date,
                airline="VN",
                flight_number="VN750",
                cabin="Economy",
                fare_class=None,
                price_currency="EUR",
                price_amount=699.0,
                url="https://example.test/demo-static/vn750",
            ),
            Offer(
                provider=self.NAME,
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                return_date=arrival_date,
                airline="QH",
                flight_number="QH89",
                cabin="Economy",
                fare_class=None,
                price_currency="EUR",
                price_amount=640.0,
                url="https://example.test/demo-static/qh89",
            ),
        ]
