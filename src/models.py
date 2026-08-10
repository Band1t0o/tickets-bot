from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass
class Offer:
    """Legacy round-trip offer.

    Retained so the existing `scrape`/`watch` CLI paths keep working while the
    scenario platform is built around Leg/Itinerary. New code should use Leg.
    """

    provider: str
    origin: str
    destination: str
    departure_date: str  # YYYY-MM-DD
    return_date: str | None  # YYYY-MM-DD
    airline: str | None
    flight_number: str | None
    cabin: str | None
    fare_class: str | None
    price_currency: str
    price_amount: float
    url: str

    def content_hash(self) -> str:
        body = f"{self.provider}|{self.origin}|{self.destination}|{self.departure_date}|{self.airline}|{self.flight_number}|{self.cabin}|{self.fare_class}|{self.price_currency}|{self.price_amount}|{self.url}"
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Leg:
    """A single one-way flight between two airports on one date.

    `price_amount` is **per person**. Verified live: pelikan.cz returns
    byte-identical card prices for P:1000E_0_0, P:2000E_0_0 and P:3000E_0_0
    while the results page correctly reports 1/2/3 passengers. Use
    `Itinerary.total_for_party()` to price a group.

    `content_hash()` deliberately includes airline, flight_number and stops.
    The old Offer model hardcoded the first two to "Unknown", which made every
    result on a page hash identically - ten distinct flights collapsed to one
    hash, and the notifier reported ten "new offers" for a single flight.
    """

    provider: str
    origin: str
    destination: str
    depart_date: date
    airline: str | None
    flight_number: str | None
    stops: int | None
    price_currency: str
    price_amount: float
    url: str
    depart_time: str | None = None  # "18:25" local
    arrive_time: str | None = None  # "08:00" local
    duration_minutes: int | None = None
    # True = checked bag included, False = explicitly excluded, None = the site
    # only reveals it after "POKRAČOVAT" (typical of low-cost carriers).
    # Deliberately NOT in content_hash(): baggage belongs to the fare, not the
    # flight, and hashing it would let one flight hash two ways.
    checked_bag: bool | None = None

    def content_hash(self) -> str:
        body = "|".join(
            str(part)
            for part in (
                self.provider,
                self.origin,
                self.destination,
                self.depart_date.isoformat(),
                self.depart_time,
                self.arrive_time,
                self.airline,
                self.flight_number,
                self.stops,
                self.price_currency,
                self.price_amount,
            )
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["depart_date"] = self.depart_date.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Leg:
        payload = dict(data)
        payload["depart_date"] = date.fromisoformat(payload["depart_date"])
        return cls(**payload)


@dataclass
class Itinerary:
    """One or more legs bought together as a trip.

    Round trip is two legs; the Europe -> Japan -> Philippines -> Europe trip is
    three. Legs are stored in travel order.
    """

    legs: list[Leg] = field(default_factory=list)

    @property
    def total_price(self) -> float:
        """Per-person total across all legs."""
        return sum(leg.price_amount for leg in self.legs)

    def total_for_party(self, adults: int) -> float:
        """Total for `adults` travellers, since leg prices are per person."""
        return self.total_price * adults

    @property
    def legs_needing_bag(self) -> list[Leg]:
        """Legs where a checked bag is not confirmed included."""
        return [leg for leg in self.legs if leg.checked_bag is not True]

    def total_with_bags(self, bag_estimate: float) -> float:
        """Per-person total once an estimated bag fee is added where needed.

        Ranking on the headline fare compares a low-cost carrier's bagless price
        against a legacy carrier's bag-inclusive one, which systematically
        flatters the former. This is an estimate and is always labelled as one -
        the site only quotes the real fee after "POKRAČOVAT".
        """
        return self.total_price + bag_estimate * len(self.legs_needing_bag)

    @property
    def same_airport(self) -> bool:
        """True when the trip ends where it started (not an open jaw)."""
        if not self.legs:
            return False
        return self.legs[0].origin == self.legs[-1].destination

    @property
    def currency(self) -> str:
        return self.legs[0].price_currency if self.legs else "CZK"

    @property
    def departure_date(self) -> date | None:
        return self.legs[0].depart_date if self.legs else None

    @property
    def return_date(self) -> date | None:
        return self.legs[-1].depart_date if self.legs else None

    @property
    def route(self) -> str:
        if not self.legs:
            return ""
        return " → ".join([self.legs[0].origin] + [leg.destination for leg in self.legs])

    def to_dict(self, bag_estimate: float = 0) -> dict:
        return {
            "legs": [leg.to_dict() for leg in self.legs],
            "total_price": self.total_price,
            "total_with_bags": self.total_with_bags(bag_estimate),
            "bags_needed": len(self.legs_needing_bag),
            "bag_estimate": bag_estimate,
            "currency": self.currency,
            "same_airport": self.same_airport,
            "route": self.route,
        }
