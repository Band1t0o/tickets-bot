from __future__ import annotations
from datetime import date
from typing import Iterable
from ..models import Leg, Offer
from .base import BaseProvider

class DemoStaticProvider(BaseProvider):
    NAME = "DEMO_STATIC"

    def search_leg(self, page, origin: str, destination: str, depart: date,
                   ret: date | None = None, adults: int = 1) -> list[Leg]:
        """Two fixed legs, so sweeps and the UI can be exercised without a browser."""
        return [
            Leg(provider=self.NAME, origin=origin, destination=destination,
                depart_date=depart, airline="VN", flight_number="VN750", stops=1,
                price_currency="CZK", price_amount=17500.0,
                url="https://example.test/demo-static/vn750",
                depart_time="10:20", arrive_time="11:40", duration_minutes=1040),
            Leg(provider=self.NAME, origin=origin, destination=destination,
                depart_date=depart, airline="QH", flight_number="QH89", stops=0,
                price_currency="CZK", price_amount=16000.0,
                url="https://example.test/demo-static/qh89",
                depart_time="18:25", arrive_time="08:00", duration_minutes=1775),
        ]

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
