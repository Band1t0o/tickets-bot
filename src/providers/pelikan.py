"""pelikan.cz provider.

Searches are performed by navigating directly to a deep link (see
`pelikan_url`), which replaced the previous form-driving sequence and cut a
single search from ~150s to 10-15s.

Personal, non-commercial use only. pelikan.cz robots.txt disallows /gf3/ and
/services/; neither is touched here. A politeness delay is applied between
searches by the sweep runner.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import Leg, Offer
from .base import BaseProvider
from .pelikan_url import build_search_url

CARD_SELECTOR = "div[id^='flight-']"
RESULT_TIMEOUT_S = 75
POLL_INTERVAL_S = 5

# The site's own wording when a route genuinely has no inventory, verified live
# against BRQ->NRT: "Hups! Nenašli jsme žádny let, zkuste vyhledat ješte jednou".
# (Their copy mixes Czech and Slovak; match only the stable prefix.)
NO_RESULTS_MARKER = "Nenašli jsme žádn"


class SearchTimeout(RuntimeError):
    """Results never rendered - the search failed, it did not come back empty.

    Returning [] here instead of raising is how three whole return routes
    (MNL->VIE, CEB->PRG, CEB->FRA) disappeared from a sweep that still reported
    error_count: 0. An empty route and a broken search must never look alike.
    """

# Stop counts are conveyed by an icon alt attribute as well as Czech text.
_STOP_ALT = {"direct": 0, "non-stop": 0, "one-stop": 1, "two-stops": 2, "three-stops": 3}


def _airline_from_card(card) -> Optional[str]:
    """Carrier IATA code, taken from the logo URL (…/carriers/VJ-sq.svg)."""
    for img in card.find_all("img"):
        for attr in ("ng-src", "src", "imagecheck"):
            value = img.get(attr) or ""
            match = re.search(r"/carriers/([A-Z0-9]{2})-sq\.", value)
            if match:
                return match.group(1)
    return None


def _stops_from_card(card) -> Optional[int]:
    for img in card.find_all("img"):
        alt = (img.get("alt") or "").strip().lower()
        if alt in _STOP_ALT:
            return _STOP_ALT[alt]
    match = re.search(r"(\d+)\s*přestup", card.get_text(" ", strip=True))
    if match:
        return int(match.group(1))
    if re.search(r"\bp[řr]ím[ýy]\b", card.get_text(" ", strip=True), re.I):
        return 0
    return None


def _price_from_card(card) -> tuple[Optional[float], str]:
    node = card.select_one(".fly-search-price-info-wrapp")
    if node is None:
        return None, "CZK"
    text = node.get_text(" ", strip=True)
    currency = "CZK" if "Kč" in text else "EUR"
    digits = re.sub(r"[^\d]", "", text.split("Kč")[0] if "Kč" in text else text)
    if not digits:
        return None, currency
    return float(digits), currency


def _depart_date_from_card(card) -> Optional[date]:
    """Read the date printed on the card.

    The site substitutes nearby dates (asking for 22 Jan can return 23 Jan), so
    the requested date must never be assumed.
    """
    node = card.select_one(".fly-item-date-new-reservation")
    if node is None:
        return None
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", node.get_text(" ", strip=True))
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _times_from_card(card) -> tuple[Optional[str], Optional[str]]:
    times = [
        node.get_text(" ", strip=True)
        for node in card.select(".fly-item-time-new-reservation")
    ]
    times = [t for t in times if re.fullmatch(r"\d{1,2}:\d{2}", t)]
    if not times:
        return None, None
    return times[0], (times[1] if len(times) > 1 else None)


def _duration_from_card(card) -> Optional[int]:
    text = card.get_text(" ", strip=True)
    match = re.search(r"(?:(\d+)d\s*)?(\d+)h\s*(\d+)m", text)
    if not match:
        return None
    days, hours, minutes = match.groups()
    return int(days or 0) * 1440 + int(hours) * 60 + int(minutes)


def parse_results_html(html: str, origin: str, destination: str) -> list[Leg]:
    """Extract Legs from a rendered results page.

    Pure and browser-free so it can be tested against a saved fixture.

    Only `div[id^='flight-']` is selected. The previous selector,
    `div[id^='flight-'], flights-flight`, matched each offer twice - the proven
    cause of the duplicate-offer bug.
    """
    soup = BeautifulSoup(html, "lxml")
    legs: list[Leg] = []

    for card in soup.select(CARD_SELECTOR):
        price, currency = _price_from_card(card)
        if price is None:
            continue
        depart_time, arrive_time = _times_from_card(card)
        legs.append(
            Leg(
                depart_time=depart_time,
                arrive_time=arrive_time,
                duration_minutes=_duration_from_card(card),
                provider=PelikanProvider.NAME,
                origin=origin,
                destination=destination,
                depart_date=_depart_date_from_card(card),
                airline=_airline_from_card(card) or "Unknown",
                # Flight numbers live behind a collapsed "Detaily letů" panel and
                # are not in the DOM; extracting them would cost a click per card.
                flight_number=None,
                stops=_stops_from_card(card),
                price_currency=currency,
                price_amount=price,
                url="",
                checked_bag=_checked_bag_from_card(card),
            )
        )
    return _dedupe(legs)


def _checked_bag_from_card(card) -> bool | None:
    """Whether a checked bag is included, from the card's baggage row.

    The icon filename is the reliable signal - `checked-baggage-include.svg` vs
    `checked-baggage-exclude.svg`. The adjacent text is "Ano"/"Ne" when known and
    "Pro více info o zavazadlech klikněte na POKRAČOVAT" when the site will only
    say after a click, which is the usual case for low-cost carriers. Unknown
    stays None: recording it as included would flatter exactly the fares whose
    real price is a bag fee higher.
    """
    icon = card.select_one("img.baggage-img")
    src = (icon.get("src") or icon.get("ng-src") or "") if icon else ""
    if "checked-baggage-include" in src:
        return True
    if "checked-baggage-exclude" in src:
        return False

    label = card.select_one(".fly-item-bottom-baggage-new-reservation")
    text = label.get_text(strip=True).casefold() if label else ""
    if text == "ano":
        return True
    if text == "ne":
        return False
    return None


def _dedupe(legs: list[Leg]) -> list[Leg]:
    """Collapse offers identical in every visible field.

    pelikan.cz genuinely lists the same itinerary more than once (observed:
    two cards with identical carrier, times, duration and price), so this is
    deduplication of the site's own repetition - distinct from the old bug,
    where a doubled CSS selector matched each card twice.
    """
    seen: set[str] = set()
    unique: list[Leg] = []
    for leg in legs:
        digest = leg.content_hash()
        if digest not in seen:
            seen.add(digest)
            unique.append(leg)
    return unique


class PelikanProvider(BaseProvider):
    NAME = "PELIKAN"
    BASE_URL = "https://www.pelikan.cz"

    def search_leg(
        self,
        page,
        origin: str,
        destination: str,
        depart: date,
        ret: date | None = None,
        adults: int = 1,
    ) -> list[Leg]:
        """Run one search on an existing Playwright page and return its Legs.

        The page is supplied by the caller so a worker can reuse one browser
        across many searches.
        """
        url = build_search_url(origin, destination, depart, ret, adults)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

        # Race the offer cards against the site's explicit "no flights" message.
        # Whichever appears first is the answer; if neither does, the search
        # failed and must say so rather than masquerading as an empty route.
        for _ in range(0, RESULT_TIMEOUT_S, POLL_INTERVAL_S):
            time.sleep(POLL_INTERVAL_S)
            if page.locator(CARD_SELECTOR).count():
                break
            if NO_RESULTS_MARKER in page.inner_text("body"):
                return []
        else:
            raise SearchTimeout(
                f"{origin}->{destination} {depart}: no results and no "
                f"'no flights' message within {RESULT_TIMEOUT_S}s"
            )

        # Let the last few cards settle before snapshotting the DOM.
        time.sleep(2)
        legs = parse_results_html(page.content(), origin, destination)
        for leg in legs:
            leg.url = url
            if leg.depart_date is None:
                leg.depart_date = depart
        return legs

    def scrape(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        adults: int,
        arrival_date: str,
    ) -> Iterable[Offer]:
        """Legacy round-trip entry point, kept for the `scrape`/`watch` CLI."""
        from playwright.sync_api import sync_playwright

        depart = datetime.strptime(departure_date, "%Y-%m-%d").date()
        ret = datetime.strptime(arrival_date, "%Y-%m-%d").date()
        offers: list[Offer] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_context(
                locale="cs-CZ", viewport={"width": 1600, "height": 1000}
            ).new_page()
            try:
                legs = self.search_leg(page, origin, destination, depart, ret, adults)
                offers = [
                    Offer(
                        provider=self.NAME,
                        origin=leg.origin,
                        destination=leg.destination,
                        departure_date=leg.depart_date.isoformat(),
                        return_date=arrival_date,
                        airline=leg.airline,
                        flight_number=leg.flight_number,
                        cabin="Economy",
                        fare_class=None,
                        price_currency=leg.price_currency,
                        price_amount=leg.price_amount,
                        url=leg.url,
                    )
                    for leg in legs
                ]
            finally:
                browser.close()
        return offers
