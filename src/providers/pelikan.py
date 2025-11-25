from __future__ import annotations
from typing import Iterable
import os
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Page
from ..models import Offer
from .base import BaseProvider

class PelikanProvider(BaseProvider):
    """
    Pelikan.cz scraper for personal use only.

    IMPORTANT: This scraper is for personal, non-commercial use only.
    - robots.txt disallows /searchform and /api/
    - Use reasonable delays between requests
    """

    NAME = "PELIKAN"
    BASE_URL = "https://www.pelikan.cz"

    def scrape(self, origin: str, destination: str, departure_date: str, adults: int, arrival_date: str) -> Iterable[Offer]:
        """
        Scrape Pelikan.cz using Playwright for JS-rendered content.

        Note: Pelikan uses dynamic search forms, so we need Playwright.
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

                # Click the not use cookies button
                disable_cookies = page.locator("#codeblocks-reject-cookies").first
                disable_cookies.click()
                time.sleep(0.5)

                # Remove default options (Praha and Vídeň) from origin field
                print(f"[{self.NAME}] Removing default origin options...")
                try:
                    # Find all remove buttons in the origin field
                    # The remove buttons are <i> tags with class "input-tags_remove" inside the origin field
                    remove_buttons = page.locator("#pl-departure-from-flights i.input-tags_remove").all()
                    print(f"[{self.NAME}] Found {len(remove_buttons)} default options to remove")
                    
                    # Click each remove button to remove the default options
                    for idx, remove_btn in enumerate(remove_buttons):
                        if remove_btn.is_visible():
                            print(f"[{self.NAME}] Removing default option {idx + 1}...")
                            remove_btn.click()
                            time.sleep(0.3)
                    
                    time.sleep(0.5)
                    print(f"[{self.NAME}] Default options removed")
                except Exception as e:
                    print(f"[{self.NAME}] Warning: Could not remove default options: {e}")

                try:
                    # Fill origin input first
                    print(f"[{self.NAME}] Filling origin field...")

                    # Find the origin input - it's inside div#pl-departure-from-flights with class autocomplete_input_value
                    origin_input = page.locator("div#pl-departure-from-flights input.autocomplete_input_value, div#pl-departure-from-flights input[placeholder='Přidat letiště']").first
                    origin_input.wait_for(state="visible", timeout=10000)

                    # Clear any default value and click to focus
                    print(f"[{self.NAME}] Clicking origin input...")
                    origin_input.click()
                    time.sleep(0.5)

                    # Clear the field first
                    origin_input.fill("")
                    time.sleep(0.3)

                    # Type the origin slowly to trigger autocomplete (300ms debounce in Angular)
                    print(f"[{self.NAME}] Typing origin: {origin}")
                    origin_input.type(origin, delay=100)
                    time.sleep(1.5)  # Wait for debounce + autocomplete to appear


                    # Click the add button to confirm/add the origin
                    print(f"[{self.NAME}] Clicking add button to confirm origin...")
                    add_origin_btn = page.locator("button.btn.btn-alt:has(i:has-text('plus')), button:has(i.icon.icon-m)").first
                    add_origin_btn.click()
                    time.sleep(0.5)

                    # Fill destination input
                    print(f"[{self.NAME}] Filling destination field...")

                    # Find the destination input by ID or placeholder
                    dest_input = page.locator("input#departureToFlightInput, input[placeholder='Přílet do']").first
                    dest_input.wait_for(state="visible", timeout=10000)

                    # Click to focus and open dropdown
                    print(f"[{self.NAME}] Clicking destination input...")
                    dest_input.click()
                    time.sleep(0.5)

                    # Clear the field first
                    dest_input.fill("")
                    time.sleep(0.3)

                    # Type the destination slowly to trigger autocomplete (300ms debounce in Angular)
                    print(f"[{self.NAME}] Typing destination: {destination}")
                    dest_input.type(destination, delay=100)
                    time.sleep(1.0)  # Wait for debounce + autocomplete to appear

                    # Click the add button to confirm/add the destination
                    print(f"[{self.NAME}] Clicking add button to confirm destination...")
                    add_btn = page.locator("button.btn.btn-alt:has(i:has-text('plus')), button:has(i.icon.icon-m)").first
                    add_btn.click()
                    time.sleep(0.5)

                    # Select date
                    self._select_date(page, departure_date)
                    time.sleep(0.5)
                    self._select_date(page, arrival_date)

                    # Select adults count
                    passenger_selector = page.locator(".flights-search-passengers-new-reservation").first
                    print(f"[{self.NAME}] Clicking passenger selector...")
                    passenger_selector.click()
                    time.sleep(0.5)

                    dropdown = page.locator("#dropdown-tickets_person-round_trip").first
                    dropdown.wait_for(state="visible", timeout=5000)
                    print(f"[{self.NAME}] Passenger dropdown opened")
                    
                    # Find the increment button in the adults section
                    # Look for the section with "Dospělý" (Adult) text and its increment button
                    adult_section = dropdown.locator("li:has(span.t:has-text('Dospělý'))").first
                    add_adult_btn = adult_section.locator("a.controls.right").first
                    
                    # Click (adults - 1) times
                    clicks_needed = adults - 1
                    
                    if clicks_needed > 0:
                        print(f"[{self.NAME}] Clicking add adult button {clicks_needed} time(s)...")
                        for i in range(clicks_needed):
                            add_adult_btn.click()
                            time.sleep(0.3)
                    else:
                        print(f"[{self.NAME}] Adults count already at 1")
                    
                    time.sleep(0.5)
                    print(f"[{self.NAME}] Adults count set to {adults}")

                    # Submit search
                    search_btn = page.locator("button.btn-full").first
                    print(f"[{self.NAME}] Clicking search button...")
                    search_btn.click()
                    time.sleep(120)

                    # Parse results
                    print(f"[{self.NAME}] Parsing results...")
                    offers = self._parse_results(page, origin, destination, departure_date)
                    print(f"[{self.NAME}] Found {len(offers)} flight offers")

                    try:
                        screenshot_path = f"debug_pelikan_error_{int(time.time())}.png"
                        page.screenshot(path=screenshot_path, full_page=False)
                        print(f"[{self.NAME}] Debug screenshot saved to {screenshot_path}")
                    except Exception as screenshot_err:
                        print(f"[{self.NAME}] Could not save screenshot: {screenshot_err}")
                    
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
        
        except Exception as e:
            print(f"[{self.NAME}] Failed to scrape: {e}")

        # Be respectful - add delay after scraping
        time.sleep(5.0)

        return offers
    
    def _select_date(self, page: Page, departure_date: str) -> str:
        target_month_name, target_year, target_day = self._parse_date(departure_date)
        target_text = f"{target_month_name}"

        print(f"[{self.NAME}] Looking for calendar month: {target_text}")

        forward_btn = page.locator("a.calendar-i:nth-child(3)")

        # Loop until we find the target month
        max_attempts = 12  # Safety limit (max 12 months forward)
        found = False
        attempt = 0
        
        for attempt in range(max_attempts):
            # Get all visible month elements
            month_elements = page.locator("div.month").all()
            
            print(f"[{self.NAME}] Checking {len(month_elements)} visible month(s)...")
            
            # Check each visible month calendar
            for month_elem in month_elements:
                try:
                    # Get month name from the specific span
                    month_name_elem = month_elem.locator(".month-label_name").first
                    year_elem = month_elem.locator(".month-label_year").first
                    
                    if not month_name_elem.is_visible() or not year_elem.is_visible():
                        continue
                    
                    current_month = month_name_elem.inner_text().strip()
                    current_year = year_elem.inner_text().strip()
                    
                    print(f"[{self.NAME}]   Found month: {current_month} {current_year}")
                    
                    # Check if this matches our target
                    if current_month.upper() == target_month_name.upper() and current_year == str(target_year):
                        print(f"[{self.NAME}] ✓ Found target month: {target_month_name} {target_year}!")
                        found = True
                        break
                except Exception as e:
                    print(f"[{self.NAME}]   Error reading month: {e}")
                    continue
            
            if found:
                break
            
            # Not found, click forward
            print(f"[{self.NAME}] Target not visible, clicking forward... (attempt {attempt + 1})")
            forward_btn.click()
            time.sleep(0.5)  # Wait for calendar to update
            attempt += 1
            if attempt >= max_attempts:
                print(f"[{self.NAME}] Warning: Could not find {target_month_name} {target_year} after {max_attempts} attempts")
                raise Exception(f"[{self.NAME}] Warning: Could not find {target_month_name} {target_year} after {max_attempts} attempts")
        
        if not found:
            print(f"[{self.NAME}] Warning: Could not find {target_month_name} {target_year} after {max_attempts} attempts")
            raise Exception(f"[{self.NAME}] Warning: Could not find {target_month_name} {target_year} after {max_attempts} attempts")
        
        # Select the specific day
        print(f"[{self.NAME}] Selecting day {target_day}...")
        
        # Find all visible day elements in div.right
        # Days are <a class="day"> elements
        all_days = page.locator("div.right a.day").all()
        
        # Find the day that matches our target and is in the correct month
        for day_elem in all_days:
            try:
                day_text = day_elem.inner_text().strip()
                # Check if this day matches our target day number
                if day_text == str(target_day):
                    # Verify this day is in the correct month by checking if month is still visible
                    # Since we already found the month, any day in the visible calendar should work
                    # But we need to make sure it's not disabled
                    class_attr = day_elem.get_attribute("class") or ""
                    if "disabled" not in class_attr:
                        print(f"[{self.NAME}] Clicking day {target_day}...")
                        day_elem.click()
                        time.sleep(0.5)
                        print(f"[{self.NAME}] Date selected: {departure_date}")
                        return
            except Exception as e:
                continue
        
        # Fallback: try to find the day using a more specific selector
        try:
            day_cell = page.locator(f"div.right a.day:has-text('{target_day}'):not(.disabled)").first
            if day_cell.is_visible():
                day_cell.click()
                time.sleep(0.5)
                print(f"[{self.NAME}] Date selected: {departure_date}")
                return
        except Exception as e:
            print(f"[{self.NAME}] Could not find day using fallback selector: {e}")
        
        raise Exception(f"[{self.NAME}] Error: Could not select day {target_day}")
    
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

        target_month_name = czech_months[target_month]
        print(f"[{self.NAME}] Looking for: {target_month_name} {target_year}")
        return target_month_name, target_year, target_date.day

    
    def _parse_results(self, page: Page, origin: str, destination: str, departure_date: str) -> list[Offer]:
        """
        Parse the results from the page.
        """
        offers: list[Offer] = []

        # Find all flight offer boxes
        result_cards = page.locator("div[id^='flight-'], flights-flight").all()
        print(f"[{self.NAME}] Found {len(result_cards)} flight offers")
        
        for idx, card in enumerate(result_cards):
            try:
                print(f"[{self.NAME}] Parsing offer {idx + 1}...")
                
                price_elem = card.locator("div.fly-search-price-info-wrapp").first
        
                if not price_elem.is_visible():
                    continue
                
                # Get text content - should be "20 651 Kč" or similar
                price_text = price_elem.inner_text().strip()
                
                # Extract number and currency
                import re
                clean_price = re.sub(r'\s+', '', price_text)
                price_amount = float(re.search(r'(\d+)', clean_price).group(1))
                currency = "CZK" if "Kč" in price_text else "EUR" 

                print(f"[{self.NAME}] Price: {price_amount} {currency}")

                # Extract all dates - there should be departure and return dates
                all_dates = card.locator("span.fly-item-date-new-reservation.active").all()
                
                departure_date_text = departure_date
                return_date_text = departure_date
                
                if len(all_dates) >= 1:
                    # First date is usually departure
                    departure_date_text = all_dates[0].inner_text().strip()
                    print(f"[{self.NAME}]   Departure date: {departure_date_text}")
                
                if len(all_dates) >= 2:
                    # Second date is usually return
                    return_date_text = all_dates[1].inner_text().strip()
                    print(f"[{self.NAME}]   Return date: {return_date_text}")
                
                # Extract origin and destination from airport elements
                # Get all destination wrappers - first is origin, second is destination
                destination_wrappers = card.locator(".fly-item-destination-wrapp-new-reservation").all()
                
                origin_name = origin
                destination_name = destination
                origin_iata = ""
                destination_iata = ""
                
                if len(destination_wrappers) >= 1:
                    # First wrapper is origin (e.g., "Praha")
                    origin_elem = destination_wrappers[0].locator("h1.airport").first
                    if origin_elem.is_visible():
                        origin_name = origin_elem.inner_text().strip()
                        print(f"[{self.NAME}]   Origin: {origin_name}")
                        
                        # Extract IATA code from place-define
                        origin_place = destination_wrappers[0].locator("div.place-define").first
                        if origin_place.is_visible():
                            origin_place_text = origin_place.inner_text()
                            origin_match = re.search(r'\(([A-Z]{3})\)', origin_place_text)
                            if origin_match:
                                origin_iata = origin_match.group(1)
                                print(f"[{self.NAME}]   Origin IATA: {origin_iata}")
                
                if len(destination_wrappers) >= 2:
                    # Second wrapper is destination (e.g., "Ho Či Minovo Město")
                    destination_elem = destination_wrappers[1].locator("h1.airport").first
                    if destination_elem.is_visible():
                        destination_name = destination_elem.inner_text().strip()
                        print(f"[{self.NAME}]   Destination: {destination_name}")
                        
                        # Extract IATA code from place-define
                        dest_place = destination_wrappers[1].locator("div.place-define").first
                        if dest_place.is_visible():
                            dest_place_text = dest_place.inner_text()
                            dest_match = re.search(r'\(([A-Z]{3})\)', dest_place_text)
                            if dest_match:
                                destination_iata = dest_match.group(1)
                                print(f"[{self.NAME}]   Destination IATA: {destination_iata}")
                
                # Get current page URL for reference
                url = page.url
                if origin_iata != origin or destination_iata != destination:
                    continue
                # Create offer
                offer = Offer(
                    provider=self.NAME,
                    origin=origin,
                    destination=destination,
                    departure_date=departure_date_text,
                    return_date=return_date_text,
                    airline="Unknown",
                    flight_number="Unknown",
                    cabin="Economy",
                    fare_class="Unknown",
                    price_currency=currency,
                    price_amount=price_amount,
                    url=url
                )
                
                offers.append(offer)
                print(f"[{self.NAME}]   ✓ Created offer: {price_amount} {currency} on {departure_date_text} - {return_date_text} from {origin_name} to {destination_name}")
                
            except Exception as e:
                print(f"[{self.NAME}] Error parsing offer {idx + 1}: {e}")
                import traceback
                traceback.print_exc()
                continue
        
        print(f"[{self.NAME}] Successfully parsed {len(offers)} offers")
        return offers