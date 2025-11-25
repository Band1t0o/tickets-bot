from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional
import hashlib

@dataclass
class Offer:
    provider: str
    origin: str
    destination: str
    departure_date: str  # YYYY-MM-DD
    return_date: Optional[str]  # YYYY-MM-DD
    airline: Optional[str]
    flight_number: Optional[str]
    cabin: Optional[str]
    fare_class: Optional[str]
    price_currency: str
    price_amount: float
    url: str

    def content_hash(self) -> str:
        body = f"{self.provider}|{self.origin}|{self.destination}|{self.departure_date}|{self.airline}|{self.flight_number}|{self.cabin}|{self.fare_class}|{self.price_currency}|{self.price_amount}|{self.url}"
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)
