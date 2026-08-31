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
import statistics
import time
from collections import deque
from datetime import UTC, date, datetime

from bs4 import BeautifulSoup

from ..models import Leg
from ..sources import DEFAULTS, Source, load_source
from .base import BaseProvider
from .pelikan_url import build_search_url

# Selectors, timeouts and the "no flights" marker live in `src/sources.py` and
# can be overridden from `data/sources.json` without editing code, because the
# usual way this scraper breaks is the site renaming a class. Aliases for three
# of them used to sit here "so existing references still read"; every such
# reference had gone, and an alias nothing reads is a second place for a
# selector to be wrong. Read them off the `Source` the provider was given.
#
# How often to look while waiting for the cards. Not from `sources.py`: it is a
# property of this provider's polling loop, not of the site.
POLL_INTERVAL_S = 5

# How long to keep waiting for results, decided from how fast this site has
# actually been answering rather than from a fixed ceiling.
#
# The ceiling was costing whole afternoons. A timed-out search takes the full
# `result_timeout_s`, and the local sweep of 11 Aug spent 93% of its worker time
# on searches that never rendered. But it cannot simply be lowered: the clean
# cloud run of the same trip rendered in ~25-30s, so a flat 45s cutoff would
# have started failing perfectly good searches.
#
# So: wait three times as long as this site has recently needed, never less than
# a minute and never more than the configured timeout. Once the site is
# answering in 25s, a page still blank at 75s is not going to arrive.
ADAPTIVE_MULTIPLE = 3.0
# The floor was 60, and 60 is what every failure of the 12 Aug probe reported:
# six searches killed at exactly "no results and no 'no flights' message within
# 60s", four of them the same far-out return date. The adaptive rule is meant to
# stop a *dead* page costing two minutes, not to fail a page that is merely slow
# because the date is eighteen months out and thin. 90 still cuts a dead search
# to three quarters of the ceiling, while leaving a slow one room to arrive.
MIN_WAIT_S = 90
# Below this many samples there is no baseline worth trusting, and a cold site
# genuinely can be slow, so early searches get the full timeout.
MIN_SAMPLES_FOR_ADAPTIVE = 10
# Rolling, because what matters is how the site is behaving now, not at 02:00.
RENDER_SAMPLE_SIZE = 40


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
    # No BASE_URL beside this: the base address comes from `self.source`, so a
    # site that moves its path is repaired from `data/sources.json`. A constant
    # here would be a second answer to that question, and nothing read it.
    NAME = "PELIKAN"

    def __init__(self, source: Source | None = None, data_dir="data"):
        # Read once per provider rather than once per search: a sweep runs
        # hundreds of searches, and re-reading the file for each would let an
        # edit take effect halfway through and make the results incomparable.
        self.source = source or load_source(self.NAME, data_dir)
        # Shared across worker threads on purpose - they are all measuring the
        # same site. `deque` because appends from several threads are safe and
        # `maxlen` gives the rolling window for free.
        self.render_times: deque[float] = deque(maxlen=RENDER_SAMPLE_SIZE)

    def record_render_time(self, seconds: float) -> None:
        self.render_times.append(seconds)

    def wait_budget(self) -> float:
        """Seconds to wait for results before calling this search failed."""
        samples = list(self.render_times)
        if len(samples) < MIN_SAMPLES_FOR_ADAPTIVE:
            return float(self.source.result_timeout_s)
        typical = statistics.median(samples)
        return float(
            min(
                self.source.result_timeout_s,
                max(MIN_WAIT_S, ADAPTIVE_MULTIPLE * typical),
            )
        )

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
        budget = self.wait_budget()
        for poll in range(1, max(1, int(budget // POLL_INTERVAL_S)) + 1):
            time.sleep(POLL_INTERVAL_S)
            if page.locator(source.selectors["card"]).count():
                # Coarse to the poll interval, which is fine for a figure only
                # ever used as a multiple of itself.
                self.record_render_time(poll * POLL_INTERVAL_S)
                break
            if source.no_results_marker in page.inner_text("body"):
                return []
        else:
            raise SearchTimeout(
                f"{origin}->{destination} {depart}: no results and no "
                f"'no flights' message within {int(budget)}s"
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
