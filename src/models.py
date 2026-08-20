from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime


@dataclass
class Leg:
    """A single one-way flight between two airports on one date.

    `price_amount` is **per person**. Verified live: pelikan.cz returns
    byte-identical card prices for P:1000E_0_0, P:2000E_0_0 and P:3000E_0_0
    while the results page correctly reports 1/2/3 passengers. Use
    `Itinerary.total_for_party()` to price a group.

    `content_hash()` deliberately includes airline, flight_number and stops.
    The Offer model this replaced hardcoded the first two to "Unknown", which made every
    result on a page hash identically - ten distinct flights collapsed to one
    hash, and the notifier reported ten "new offers" for a single flight.
    """

    provider: str
    origin: str
    destination: str
    # None when the card's date could not be parsed. The parser has three paths
    # that produce it, and _dedupe hashes every leg before anything checks - so
    # one tweak to the site's date markup used to turn every search into an
    # AttributeError rather than into one skipped card.
    depart_date: date | None
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
    # ISO-8601 UTC moment this price was read off the page. A deep sweep runs
    # ~97 minutes and the probe caught FRA->NRT moving 21% inside a single
    # two-hour window, so a leg from minute 3 and one from minute 95 are not the
    # same measurement. None on legs written before the field existed.
    #
    # Also deliberately NOT in content_hash(), for the same reason as
    # checked_bag but with sharper consequences: it is unique per search, so
    # hashing it would give every leg a distinct digest and turn the parser's
    # _dedupe into a no-op.
    observed_at: str | None = None

    def content_hash(self) -> str:
        body = "|".join(
            str(part)
            for part in (
                self.provider,
                self.origin,
                self.destination,
                self.depart_date.isoformat() if self.depart_date else "",
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
        data["depart_date"] = self.depart_date.isoformat() if self.depart_date else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Leg:
        payload = dict(data)
        raw = payload.get("depart_date")
        payload["depart_date"] = date.fromisoformat(raw) if raw else None
        # `observed_at` is absent from every legs.jsonl written before it
        # existed, and those files are committed history worth keeping
        # readable. The dataclass default covers it.
        return cls(**payload)


@dataclass
class Itinerary:
    """One or more legs bought together as a trip.

    Round trip is two legs; the Europe -> Japan -> Philippines -> Europe trip is
    three. Legs are stored in travel order.
    """

    legs: list[Leg] = field(default_factory=list)

    @property
    def currencies(self) -> set[str]:
        return {leg.price_currency for leg in self.legs}

    @property
    def total_price(self) -> float:
        """Per-person total across all legs.

        Refuses to add across currencies. This used to sum raw floats, so a
        10,000 CZK leg and a 400 EUR leg produced 10,400 of nothing - and the
        pelikan parser made EUR the fallback for *anything* it could not
        recognise, so one odd card was all it took. There is no FX rate here
        and inventing one would hide the problem rather than fix it.
        """
        currencies = self.currencies
        if len(currencies) > 1:
            raise ValueError(
                f"cannot total an itinerary mixing currencies: {', '.join(sorted(currencies))}"
            )
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
        if not self.legs:
            return "CZK"
        currencies = self.currencies
        if len(currencies) > 1:
            raise ValueError(
                f"itinerary mixes currencies: {', '.join(sorted(currencies))}"
            )
        return next(iter(currencies))

    @property
    def departure_date(self) -> date | None:
        return self.legs[0].depart_date if self.legs else None

    @property
    def return_date(self) -> date | None:
        return self.legs[-1].depart_date if self.legs else None

    @property
    def has_overland(self) -> bool:
        """True when a leg departs somewhere the previous one did not land.

        Only possible across a stop marked `overland`: you cross the country on
        the ground in between. Reported so a reader is never told the price of
        a trip without being told it includes a journey nobody booked.
        """
        return any(
            first.destination != second.origin
            for first, second in zip(self.legs, self.legs[1:], strict=False)
        )

    @property
    def route(self) -> str:
        """"VIE → HND ⇢ KIX → MNL → VIE", the ⇢ being a hop you make yourself.

        Built from consecutive pairs rather than by joining every leg's
        destination onto the first origin. That shorter version drops the
        origin of every leg but the first, so an HND-in/KIX-out trip rendered
        as "VIE → HND → MNL → VIE" - a route no ticket in it would fly, silently.
        """
        if not self.legs:
            return ""
        parts = [self.legs[0].origin, "→", self.legs[0].destination]
        for previous, leg in zip(self.legs, self.legs[1:], strict=False):
            if previous.destination != leg.origin:
                parts += ["⇢", leg.origin]
            parts += ["→", leg.destination]
        return " ".join(parts)

    @property
    def _observations(self) -> list[str]:
        return sorted(leg.observed_at for leg in self.legs if leg.observed_at)

    @property
    def observed_at(self) -> str | None:
        """When the *stalest* price in this trip was read.

        The oldest rather than the newest: a trip quoted from a leg priced 90
        minutes ago is 90 minutes old, however fresh the rest of it is.
        """
        stamps = self._observations
        return stamps[0] if stamps else None

    @property
    def observed_span_minutes(self) -> int | None:
        """Minutes between the oldest and newest price in this trip.

        Legs are chained from a sweep that can run for an hour and a half, so a
        total can be assembled from prices that were never true at the same
        moment. This is how far apart they were.
        """
        stamps = self._observations
        if len(stamps) < 2:
            return None
        first = datetime.fromisoformat(stamps[0])
        last = datetime.fromisoformat(stamps[-1])
        return round((last - first).total_seconds() / 60)

    def to_dict(self, bag_estimate: float = 0) -> dict:
        return {
            "legs": [leg.to_dict() for leg in self.legs],
            "total_price": self.total_price,
            "total_with_bags": self.total_with_bags(bag_estimate),
            "bags_needed": len(self.legs_needing_bag),
            "bag_estimate": bag_estimate,
            "currency": self.currency,
            "same_airport": self.same_airport,
            "has_overland": self.has_overland,
            "route": self.route,
            "observed_at": self.observed_at,
            "observed_span_minutes": self.observed_span_minutes,
        }
