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
from datetime import UTC, date, datetime

from bs4 import BeautifulSoup

from ..models import Leg
from ..sources import DEFAULTS, Source, load_source
from .base import BaseProvider
from .pelikan_url import build_search_url

# Selectors, timeouts and the "no flights" marker now live in `src/sources.py`
# and can be overridden from `data/sources.json` without editing code, because
# the usual way this scraper breaks is the site renaming a class. These names
# are kept as the defaults' values so existing references still read.
CARD_SELECTOR = DEFAULTS["PELIKAN"].selectors["card"]
RESULT_TIMEOUT_S = DEFAULTS["PELIKAN"].result_timeout_s
POLL_INTERVAL_S = 5
NO_RESULTS_MARKER = DEFAULTS["PELIKAN"].no_results_marker


class SearchTimeout(RuntimeError):
    """Results never rendered - the search failed, it did not come back empty.

    Returning [] here instead of raising is how three whole return routes
    (MNL->VIE, CEB->PRG, CEB->FRA) disappeared from a sweep that still reported
    error_count: 0. An empty route and a broken search must never look alike.
    """

# Stop counts are conveyed by an icon alt attribute as well as Czech text.
_STOP_ALT = {"direct": 0, "non-stop": 0, "one-stop": 1, "two-stops": 2, "three-stops": 3}


def _airline_from_card(card) -> str | None:
    """Carrier IATA code, taken from the logo URL (…/carriers/VJ-sq.svg)."""
    for img in card.find_all("img"):
        for attr in ("ng-src", "src", "imagecheck"):
            value = img.get(attr) or ""
            match = re.search(r"/carriers/([A-Z0-9]{2})-sq\.", value)
            if match:
                return match.group(1)
    return None


def _stops_from_card(card) -> int | None:
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


# Symbols the site actually prints, mapped to the code stored on a Leg. Anything
# else is unknown and must stay unknown: the previous `"CZK" if "Kč" in text
# else "EUR"` made EUR the fallback for *every* unrecognised string, so a single
# odd card silently entered the leg set labelled as euros and then got summed
# against crowns.
_CURRENCY_MARKERS = (("Kč", "CZK"), ("€", "EUR"), ("EUR", "EUR"), ("$", "USD"), ("£", "GBP"))


def _price_from_card(card, selectors: dict[str, str]) -> tuple[float | None, str | None]:
    node = card.select_one(selectors["price"])
    if node is None:
        return None, None
    text = node.get_text(" ", strip=True)

    for marker, code in _CURRENCY_MARKERS:
        if marker in text:
            currency = code
            # Only the digits before the symbol: the block also carries badges
            # and passenger counts, and concatenating those inflated the price.
            digits = re.sub(r"[^\d]", "", text.split(marker)[0])
            break
    else:
        return None, None

    if not digits:
        return None, currency
    return float(digits), currency


def _depart_date_from_card(card, selectors: dict[str, str]) -> date | None:
    """Read the date printed on the card.

    The site substitutes nearby dates (asking for 22 Jan can return 23 Jan), so
    the requested date must never be assumed.
    """
    node = card.select_one(selectors["date"])
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


def _times_from_card(card, selectors: dict[str, str]) -> tuple[str | None, str | None]:
    times = [
        node.get_text(" ", strip=True)
        for node in card.select(selectors["time"])
    ]
    times = [t for t in times if re.fullmatch(r"\d{1,2}:\d{2}", t)]
    if not times:
        return None, None
    return times[0], (times[1] if len(times) > 1 else None)


def _duration_from_card(card) -> int | None:
    text = card.get_text(" ", strip=True)
    match = re.search(r"(?:(\d+)d\s*)?(\d+)h\s*(\d+)m", text)
    if not match:
        return None
    days, hours, minutes = match.groups()
    return int(days or 0) * 1440 + int(hours) * 60 + int(minutes)


def parse_results_html(
    html: str, origin: str, destination: str, source: Source | None = None
) -> list[Leg]:
    """Extract Legs from a rendered results page.

    Pure and browser-free so it can be tested against a saved fixture, and so
    the Settings tab can show exactly what a given set of selectors parses.

    Only one card selector is used. The previous one,
    `div[id^='flight-'], flights-flight`, matched each offer twice - the proven
    cause of the duplicate-offer bug.
    """
    source = source or DEFAULTS["PELIKAN"]
    selectors = source.selectors
    soup = BeautifulSoup(html, "lxml")
    legs: list[Leg] = []

    for card in soup.select(selectors["card"]):
        price, currency = _price_from_card(card, selectors)
        if price is None:
            continue
        depart_time, arrive_time = _times_from_card(card, selectors)
        legs.append(
            Leg(
                depart_time=depart_time,
                arrive_time=arrive_time,
                duration_minutes=_duration_from_card(card),
                provider=PelikanProvider.NAME,
                origin=origin,
                destination=destination,
                depart_date=_depart_date_from_card(card, selectors),
                airline=_airline_from_card(card) or "Unknown",
                # Flight numbers live behind a collapsed "Detaily letů" panel and
                # are not in the DOM; extracting them would cost a click per card.
                flight_number=None,
                stops=_stops_from_card(card),
                price_currency=currency,
                price_amount=price,
                url="",
                checked_bag=_checked_bag_from_card(card, selectors),
            )
        )
    return _dedupe(legs)


def _checked_bag_from_card(card, selectors: dict[str, str]) -> bool | None:
    """Whether a checked bag is included, from the card's baggage row.

    The icon filename is the reliable signal - `checked-baggage-include.svg` vs
    `checked-baggage-exclude.svg`. The adjacent text is "Ano"/"Ne" when known and
    "Pro více info o zavazadlech klikněte na POKRAČOVAT" when the site will only
    say after a click, which is the usual case for low-cost carriers. Unknown
    stays None: recording it as included would flatter exactly the fares whose
    real price is a bag fee higher.
    """
    icon = card.select_one(selectors["baggage_icon"])
    src = (icon.get("src") or icon.get("ng-src") or "") if icon else ""
    if "checked-baggage-include" in src:
        return True
    if "checked-baggage-exclude" in src:
        return False

    label = card.select_one(selectors["baggage_label"])
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

    def __init__(self, source: Source | None = None, data_dir="data"):
        # Read once per provider rather than once per search: a sweep runs
        # hundreds of searches, and re-reading the file for each would let an
        # edit take effect halfway through and make the results incomparable.
        self.source = source or load_source(self.NAME, data_dir)

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
        source = self.source
        url = build_search_url(origin, destination, depart, ret, adults, source=source)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

        # Race the offer cards against the site's explicit "no flights" message.
        # Whichever appears first is the answer; if neither does, the search
        # failed and must say so rather than masquerading as an empty route.
        for _ in range(0, source.result_timeout_s, POLL_INTERVAL_S):
            time.sleep(POLL_INTERVAL_S)
            if page.locator(source.selectors["card"]).count():
                break
            if source.no_results_marker in page.inner_text("body"):
                return []
        else:
            raise SearchTimeout(
                f"{origin}->{destination} {depart}: no results and no "
                f"'no flights' message within {source.result_timeout_s}s"
            )

        # Let the last few cards settle before snapshotting the DOM.
        time.sleep(2)
        html = page.content()
        # Stamped from the snapshot, not from when parsing finished, and applied
        # here rather than in the parser so `parse_results_html` stays pure -
        # reading a saved fixture is not an observation of a live price.
        observed_at = datetime.now(UTC).isoformat(timespec="seconds")
        legs = parse_results_html(html, origin, destination, source=source)
        for leg in legs:
            leg.url = url
            leg.observed_at = observed_at
            if leg.depart_date is None:
                leg.depart_date = depart
        return legs
