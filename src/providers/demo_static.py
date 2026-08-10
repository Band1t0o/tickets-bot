from __future__ import annotations

from datetime import date

from ..models import Leg
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
