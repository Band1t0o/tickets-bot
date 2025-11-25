from __future__ import annotations
from typing import Iterable
import os
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Page
from ..models import Offer
from .base import BaseProvider


class LetuskaProvider(BaseProvider):
    """
    Letuska.cz scraper for personal use only.

    IMPORTANT: This scraper is for personal, non-commercial use only.
    - robots.txt disallows /searchform and /api/
    - Use reasonable delays between requests
    """

    NAME = "LETUSKA"
    BASE_URL = "https://www.letuska.cz"

    def scrape(self, origin: str, destination: str, departure_date: str, adults: int, arrival_date: str) -> Iterable[Offer]:
        """
        Scrape Letuska.cz using Playwright for JS-rendered content.

        Note: Letuska uses dynamic search forms, so we need Playwright.
        """
        headless = os.getenv("HEADLESS", "true").lower() == "true"
        ua = os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; VietnamTicketsScraper/1.0)")

        offers: list[Offer] = []

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=headless)
                ctx = browser.new_context(
                    user_agent=ua,
                    viewport={"width": 1920, "height": 1080},
                    locale="cs-CZ"
                )
                page = ctx.new_page()

                # Navigate to homepage
                print(f"[{self.NAME}] Navigating to {self.BASE_URL}...")
                page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=30000)

                # Add delay to be respectful
                time.sleep(2.0)

                # Fill search form - Updated with actual Letuska.cz autocomplete behavior
                try:
                    # Wait for search form to load
                    departure_input = page.locator(".departure > div:nth-child(1) > input:nth-child(2)")
 
                    departure_input.click()
                    print(f"[{self.NAME}] Departure input clicked")
                    time.sleep(0.5)
                    
                    page.wait_for_selector(".flight-search-cmp", timeout=10000)
                    print(f"[{self.NAME}] Search form loaded")
                    
                    origin_whisper = page.locator("whisper-input[iconname='departure']")
                    origin_input = origin_whisper.locator("input[formcontrolname='term']")
                    print(f"[{self.NAME}] Clicking origin field...")
                    origin_input.click()
                    time.sleep(0.5)
                    
                    # Type directly into the input (it's already the right one)
                    print(f"[{self.NAME}] Typing origin: {origin}")
                    origin_input.type(origin, delay=100)
                    time.sleep(1.5)

                        # Click first result in the dropdown (whisper-list)
                    try:
                        page.wait_for_selector("whisper-list li", timeout=3000)
                        # Click the first li element or the strong inside it
                        first_result = page.locator("whisper-list li").first
                        first_result.click()
                        print(f"[{self.NAME}] Selected first origin result")
                        time.sleep(0.5)
                    except PlaywrightTimeout:
                        page.keyboard.press("Enter")
                        time.sleep(0.5)

                    # Destination field
                    page.wait_for_selector(".flight-search-cmp", timeout=10000)
                    print(f"[{self.NAME}] Search form loaded")
                    
                    origin_whisper = page.locator("whisper-input[iconname='departure']")
                    origin_input = origin_whisper.locator("input[formcontrolname='term']")
                    print(f"[{self.NAME}] Clicking destination field...")
                    origin_input.click()
                    time.sleep(0.5)
                    
                    # Type directly into the input (it's already the right one)
                    print(f"[{self.NAME}] Typing destination: {destination}")
                    origin_input.type(destination, delay=100)
                    time.sleep(1.5)

                        # Click first result in the dropdown (whisper-list)
                    try:
                        page.wait_for_selector("whisper-list li", timeout=3000)
                        # Click the first li element or the strong inside it
                        first_result = page.locator("whisper-list li").first
                        first_result.click()
                        print(f"[{self.NAME}] Selected first destination result")
                        time.sleep(0.5)
                    except PlaywrightTimeout:
                        page.keyboard.press("Enter")
                        time.sleep(0.5)
                    
                    # Fill date
                    self.select_date(page, departure_date)
                    time.sleep(0.5)
                    self.select_date(page, arrival_date)
                    time.sleep(0.5)

                    # Select adults count
                    print(f"[{self.NAME}] Setting adults count to {adults}...")
                    
                    # Find the add button for adults (the + button)
                    add_adult_btn = page.locator(".formModal-content > passengers-widget:nth-child(2) > passengers-widget-cmp:nth-child(1) > passenger-line:nth-child(1) > div:nth-child(1) > plus-minus:nth-child(2) > div:nth-child(1) > div:nth-child(3)")
                    
                    # Click the add button (adults - 1) times since it starts at 1
                    # If adults=1, we don't need to click (already at 1)
                    # If adults=2, we click once to go from 1 to 2
                    clicks_needed = adults - 1
                    
                    if clicks_needed > 0:
                        print(f"[{self.NAME}] Clicking add adult button {clicks_needed} time(s)...")
                        for i in range(clicks_needed):
                            add_adult_btn.click()
                            time.sleep(0.3)  # Small delay between clicks
                    else:
                        print(f"[{self.NAME}] Adults count already at 1, no need to add more")
                    
                    time.sleep(0.5)

                    # Submit search
                    search_btn = page.locator(".sbm-search").first
                    print(f"[{self.NAME}] Clicking search button...")
                    search_btn.click()

                    # Wait for search results to load (1 minute as requested)
                    print(f"[{self.NAME}] Waiting for search results (60 seconds)...")
                    time.sleep(60)

                    # After waiting, parse the results
                    print(f"[{self.NAME}] Search completed, parsing results...")

                    offers = self._parse_results(page, origin, destination, departure_date)

                except PlaywrightTimeout as e:
                    print(f"[{self.NAME}] Timeout waiting for search form or results: {e}")
                    # Take a screenshot for debugging
                    try:
                        screenshot_path = f"debug_letuska_timeout_{int(time.time())}.png"
                        page.screenshot(path=screenshot_path, full_page=False)
                        print(f"[{self.NAME}] Debug screenshot saved to {screenshot_path}")
                    except Exception as screenshot_err:
                        print(f"[{self.NAME}] Could not save screenshot: {screenshot_err}")
                except Exception as e:
                    print(f"[{self.NAME}] Error during search: {e}")
                    # Take a screenshot for debugging
                    try:
                        screenshot_path = f"debug_letuska_error_{int(time.time())}.png"
                        page.screenshot(path=screenshot_path, full_page=False)
                        print(f"[{self.NAME}] Debug screenshot saved to {screenshot_path}")
                    except Exception as screenshot_err:
                        print(f"[{self.NAME}] Could not save screenshot: {screenshot_err}")

                ctx.close()
                browser.close()

        except Exception as e:
            print(f"[{self.NAME}] Failed to scrape: {e}")

        # Be respectful - add delay after scraping
        time.sleep(5.0)

        return offers
    
    def select_date(self, page: Page, departure_date: str) -> str:
        target_month_name, target_year, target_day = self._parse_date(departure_date)
        target_text = f"{target_month_name}"

        print(f"[{self.NAME}] Looking for calendar month: {target_text}")

        forward_btn = page.locator("button.btnSquare:nth-child(2)")

        # Loop until we find the target month
        max_attempts = 12  # Safety limit (max 12 months forward)
        found = False

        for attempt in range(max_attempts):
            # Check if the target month is visible in the calendar
            # Look for the year header text
            year_header = page.locator(f"text=/{target_year}/i")
            # if year not found, click forward
            if year_header.count() == 0:
                print(f"[{self.NAME}] Year not found, clicking forward... (attempt {attempt + 1})")
                forward_btn.click()
                time.sleep(0.4)  # Wait for calendar to update
                continue

            month_header = page.locator(f"text=/{target_text}/i")
            
            if month_header.count() > 0 and month_header.first.is_visible():
                print(f"[{self.NAME}] Found target month: {target_text}")
                found = True
                break

            # Not found, click forward
            print(f"[{self.NAME}] Month not found, clicking forward... (attempt {attempt + 1})")
            forward_btn.click()
            time.sleep(0.4)  # Wait for calendar to update

        if not found:
            print(f"[{self.NAME}] Warning: Could not find {target_text} after {max_attempts} attempts")
        else:
            # Select the specific day
            print(f"[{self.NAME}] Selecting day {target_day}...")
            target_month_elem = None
            for month_elem in page.locator("datepick-month").all():
                header_text = month_elem.locator("table > tr:nth-child(1)").first.inner_text()
                if target_text.upper() in header_text.upper():
                    target_month_elem = month_elem
                    break
            
            if target_month_elem:
                # Click the day within this specific month calendar
                day_cell = target_month_elem.locator(f"table td:has-text('{target_day}')").first
                day_cell.click()
                time.sleep(0.5)
                print(f"[{self.NAME}] Date selected: {departure_date}")
            else:
                print(f"[{self.NAME}] Error: Could not locate the target month element")
    
    def _parse_date(self, departure_date: str) -> str:
        """
        Parse the departure date from the config,
        """
        from datetime import datetime
        target_date = datetime.strptime(departure_date, "%Y-%m-%d")
        target_month = target_date.month
        target_year = target_date.year

        # Czech month names (lowercase)
        czech_months = {
            1: "leden", 2: "únor", 3: "březen", 4: "duben",
            5: "květen", 6: "červen", 7: "červenec", 8: "srpen",
            9: "září", 10: "říjen", 11: "listopad", 12: "prosinec"
        }

        target_month_name = czech_months[target_month].upper()
        print(f"[{self.NAME}] Looking for: {target_month_name} {target_year}")
        return target_month_name, target_year, target_date.day
        

    def _parse_results(self, page, origin: str, destination: str, departure_date: str) -> list[Offer]:
        """
        Parse flight results from Letuska.cz search results page.

        """
        offers = []

        # Find all flight offer boxes
        result_cards = page.locator("app-flight-offer-box.ng-star-inserted").all()
        print(f"[{self.NAME}] Found {len(result_cards)} flight offers")

        # Limit to first 10 offers (they're sorted by price, lowest to highest)
        result_cards = result_cards[:10]
        print(f"[{self.NAME}] Processing first {len(result_cards)} offers")

        for idx, card in enumerate(result_cards):
            try:
                # Get price
                price_text = card.locator(".ftSummary-price .value").first.inner_text()
                currency_text = card.locator(".ftSummary-price .currency").first.inner_text()
                print(f"[{self.NAME}] Price: {price_text} {currency_text}")
                
                # Extract numeric price - handle space-separated thousands
                import re
                # Remove all types of whitespace (spaces, non-breaking spaces, etc.)
                clean_price = re.sub(r'\s+', '', price_text)  # Remove all whitespace
                print(f"[{self.NAME}] Clean price: '{clean_price}'")
                price_match = re.search(r'(\d+)', clean_price)
                if price_match:
                    price_amount = float(price_match.group(1))
                    print(f"[{self.NAME}] Extracted price: {price_amount}")
                else:
                    print(f"[{self.NAME}] Could not extract price from: '{price_text}'")
                    continue
                currency = "CZK" if "Kč" in currency_text else "EUR"
                
                # Get both departure and return dates
                date_elements = card.locator("div[id='d-date'].flight-date-date").all()
                
                if len(date_elements) >= 2:
                    departure_date_text = date_elements[0].inner_text()  # First date (02.08.2026)
                    return_date_text = date_elements[1].inner_text()     # Second date (17.08.2026)
                else:
                    # Fallback to single date if only one found
                    departure_date_text = card.locator(".flight-date-date").first.inner_text()
                    return_date_text = departure_date_text
                print(f"[{self.NAME}] Dates: {departure_date_text} - {return_date_text}")

                # Get origin airport code and name
                origin_elem = card.locator("span.transferBox-from").first
                origin_iata = origin_elem.inner_text().strip()  # e.g., "PRG"
                origin_name = origin_elem.get_attribute("title")  # e.g., "Praha, Letiště Václava Havla Praha"
                
                # Get destination airport code and name
                destination_elem = card.locator("span.transferBox-to").first
                destination_iata = destination_elem.inner_text().strip()  # e.g., "SGN"
                destination_name = destination_elem.get_attribute("title")  # e.g., "Ho Či Minovo Město, Letiště Tân Sơn Nhất"
                
                print(f"[{self.NAME}] Origin: {origin_iata} - {origin_name}")
                print(f"[{self.NAME}] Destination: {destination_iata} - {destination_name}")

                if origin_iata != origin or destination_iata != destination:
                    continue
                # Create offer for departure
                departure_offer = Offer(
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
                    url=page.url
                )
                
                offers.append(departure_offer)
                print(f"[{self.NAME}] Departure offer: {price_amount} {currency} on {departure_date_text} - {return_date_text}")
                
            except Exception as e:
                print(f"[{self.NAME}] Error parsing offer {idx + 1}: {e}")
                continue

        return offers
