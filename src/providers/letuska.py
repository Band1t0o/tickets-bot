from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..models import Offer
from .base import BaseProvider

CZECH_MONTHS = {
    1: "Leden", 2: "Únor", 3: "Březen", 4: "Duben",
    5: "Květen", 6: "Červen", 7: "Červenec", 8: "Srpen",
    9: "Září", 10: "Říjen", 11: "Listopad", 12: "Prosinec",
}


class LetuskaProvider(BaseProvider):
    NAME = "LETUSKA"
    BASE_URL = "https://www.letuska.cz"

    def scrape(self, origin: str, destination: str, departure_date: str, adults: int, arrival_date: str) -> Iterable[Offer]:
        headless = os.getenv("HEADLESS", "true").lower() == "true"
        ua = os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; VietnamTicketsScraper/1.0)")
        offers: list[Offer] = []

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=headless)
                ctx = browser.new_context(user_agent=ua, viewport={"width": 1920, "height": 1080}, locale="cs-CZ")
                page = ctx.new_page()

                print(f"[{self.NAME}] Navigating to {self.BASE_URL}...")
                page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2.0)

                self._accept_cookies(page)

                try:
                    self._fill_origin(page, origin)
                    self._fill_destination(page, destination)
                    self._select_calendar_date(page, departure_date, "departure")
                    time.sleep(0.5)
                    self._select_calendar_date(page, arrival_date, "return")
                    time.sleep(0.5)
                    self._set_adults(page, adults)
                    self._click_search(page)

                    print(f"[{self.NAME}] Waiting for search results (60 seconds)...")
                    time.sleep(60)

                    print(f"[{self.NAME}] Parsing results...")
                    offers = self._parse_results(page, origin, destination, departure_date)

                except (PlaywrightTimeout, Exception) as e:
                    print(f"[{self.NAME}] Error: {e}")
                    try:
                        path = f"debug_letuska_{int(time.time())}.png"
                        page.screenshot(path=path, full_page=False)
                        print(f"[{self.NAME}] Debug screenshot saved to {path}")
                    except Exception:
                        pass

                ctx.close()
                browser.close()

        except Exception as e:
            print(f"[{self.NAME}] Failed to scrape: {e}")

        time.sleep(5.0)
        return offers

    def _accept_cookies(self, page: Page):
        try:
            btn = page.locator("text=SOUHLASÍM")
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                print(f"[{self.NAME}] Cookie consent accepted")
                time.sleep(1)
        except Exception:
            pass

    def _fill_origin(self, page: Page, origin: str):
        if origin.upper() == "PRG":
            return
        print(f"[{self.NAME}] Setting origin to {origin}...")
        departure_btn = page.locator("button.text-dark-blue:nth-child(2) > div:nth-child(1)")
        departure_btn.click()
        time.sleep(0.5)
        page.wait_for_selector(".border-lavender-blue > div:nth-child(1)", timeout=10000)
        origin_input = page.locator(".border-lavender-blue > div:nth-child(1)")
        origin_input.click()
        time.sleep(0.5)
        origin_input.type(origin, delay=100)
        time.sleep(1.5)
        try:
            page.wait_for_selector("whisper-list li", timeout=3000)
            page.locator("whisper-list li").first.click()
            time.sleep(0.5)
        except PlaywrightTimeout:
            page.keyboard.press("Enter")
            time.sleep(0.5)

    def _fill_destination(self, page: Page, destination: str):
        print(f"[{self.NAME}] Setting destination to {destination}...")
        page.locator("button.md\\:col-span-2:nth-child(2)").click()
        time.sleep(0.5)
        page.wait_for_selector(".border-lavender-blue input", timeout=5000)
        dest_input = page.locator(".border-lavender-blue input")
        dest_input.type(destination, delay=100)
        time.sleep(1.5)
        try:
            page.wait_for_selector("whisper-list li", timeout=3000)
            page.locator("whisper-list li").first.click()
            time.sleep(0.5)
        except PlaywrightTimeout:
            page.keyboard.press("Enter")
            time.sleep(0.5)

    def _select_calendar_date(self, page: Page, date_str: str, label: str):
        from datetime import datetime
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        month_name = CZECH_MONTHS[dt.month]
        year = dt.year
        day = dt.day

        print(f"[{self.NAME}] Selecting {label} date: {day} {month_name} {year}...")

        clicked = page.evaluate("""([monthName, year, day]) => {
            const sf = document.querySelector('new-letuska-sf-component');
            if (!sf || !sf.shadowRoot) return 'no sf shadow';
            const cp = sf.shadowRoot.querySelector('calendar-prices');
            if (!cp || !cp.shadowRoot) return 'no calendar-prices shadow';
            const root = cp.shadowRoot;

            const containers = root.querySelectorAll('.flex-shrink-0');
            // First half are month headers, second half are day grids
            const totalMonths = Math.floor(containers.length / 2);
            let monthIndex = -1;
            for (let i = 0; i < totalMonths; i++) {
                const headerText = containers[i].textContent.trim();
                if (headerText.includes(monthName) && headerText.includes(String(year))) {
                    monthIndex = i;
                    break;
                }
            }
            if (monthIndex === -1) return 'month not found: ' + monthName + ' ' + year;

            const gridContainer = containers[totalMonths + monthIndex];
            const grid = gridContainer.querySelector('.grid.grid-cols-7');
            if (!grid) return 'grid not found';

            const buttons = grid.querySelectorAll('button');
            for (const btn of buttons) {
                const spans = btn.querySelectorAll('span');
                for (const span of spans) {
                    if (span.textContent.trim() === String(day) && !span.nextElementSibling) {
                        // This is just the day number span (not the one with price)
                        btn.click();
                        return 'ok';
                    }
                }
                // Fallback: check if button's first span matches
                if (spans.length > 0 && spans[0].textContent.trim() === String(day)) {
                    btn.click();
                    return 'ok';
                }
            }
            return 'day not found: ' + day;
        }""", [month_name, year, day])

        if clicked != "ok":
            print(f"[{self.NAME}] Calendar click failed: {clicked}")
        else:
            print(f"[{self.NAME}] Date selected: {date_str}")
        time.sleep(0.5)

    def _set_adults(self, page: Page, adults: int):
        if adults <= 1:
            return
        print(f"[{self.NAME}] Setting adults to {adults}...")
        pax_btn = page.locator("button:has-text('Osoba')")
        pax_btn.first.click()
        time.sleep(0.5)

        # The +/- buttons come in pairs per passenger type (adult, student, child, infant)
        # The first + button is for adults
        add_btn = page.evaluate("""(clicks) => {
            const sf = document.querySelector('new-letuska-sf-component');
            const root = sf.shadowRoot;
            const buttons = root.querySelectorAll('button');
            // Find all + buttons
            const plusBtns = [];
            for (const b of buttons) {
                if (b.textContent.trim() === '+') plusBtns.push(b);
            }
            if (plusBtns.length === 0) return 'no + buttons found';
            // First + button is for adults
            for (let i = 0; i < clicks; i++) {
                plusBtns[0].click();
            }
            return 'ok';
        }""", adults - 1)

        if add_btn != "ok":
            print(f"[{self.NAME}] Adults button failed: {add_btn}")
        else:
            print(f"[{self.NAME}] Adults set to {adults}")
        time.sleep(0.5)

    def _click_search(self, page: Page):
        print(f"[{self.NAME}] Clicking search...")
        page.evaluate("""() => {
            const sf = document.querySelector('new-letuska-sf-component');
            const root = sf.shadowRoot;
            const buttons = root.querySelectorAll('button');
            for (const b of buttons) {
                if (b.textContent.trim() === 'HLEDAT') {
                    b.click();
                    return;
                }
            }
        }""")

    def _parse_results(self, page: Page, origin: str, destination: str, departure_date: str) -> list[Offer]:
        offers = []
        result_cards = page.locator("app-flight-offer-box.ng-star-inserted").all()
        print(f"[{self.NAME}] Found {len(result_cards)} flight offers")
        result_cards = result_cards[:10]

        for idx, card in enumerate(result_cards):
            try:
                price_text = card.locator(".ftSummary-price .value").first.inner_text(timeout=3000)
                currency_text = card.locator(".ftSummary-price .currency").first.inner_text(timeout=3000)

                clean_price = re.sub(r'\s+', '', price_text)
                price_match = re.search(r'(\d+)', clean_price)
                if not price_match:
                    continue
                price_amount = float(price_match.group(1))
                currency = "CZK" if "Kč" in currency_text else "EUR"

                date_elements = card.locator("div[id='d-date'].flight-date-date").all()
                if len(date_elements) >= 2:
                    departure_date_text = date_elements[0].inner_text()
                    return_date_text = date_elements[1].inner_text()
                else:
                    departure_date_text = card.locator(".flight-date-date").first.inner_text()
                    return_date_text = departure_date_text

                origin_elem = card.locator(".transferBox-from").first
                origin_iata = origin_elem.inner_text(timeout=3000).strip()
                destination_elem = card.locator(".transferBox-to").first
                destination_iata = destination_elem.inner_text(timeout=3000).strip()

                if origin_iata != origin or destination_iata != destination:
                    continue

                offers.append(Offer(
                    provider=self.NAME,
                    origin=origin,
                    destination=destination,
                    departure_date=departure_date_text,
                    return_date=return_date_text,
                    airline="Unknown",
                    flight_number=None,
                    cabin="Economy",
                    fare_class=None,
                    price_currency=currency,
                    price_amount=price_amount,
                    url=page.url,
                ))
                print(f"[{self.NAME}] Offer: {price_amount} {currency} on {departure_date_text} - {return_date_text}")

            except Exception as e:
                print(f"[{self.NAME}] Error parsing offer {idx + 1}: {e}")
                continue

        return offers
