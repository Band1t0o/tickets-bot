from __future__ import annotations
from typing import Iterable
import os
import time
import requests
from ..models import Offer
from .base import BaseProvider


class SkyscannerAPIProvider(BaseProvider):
    NAME = "SKYSCANNER_API"

    # RapidAPI Skyscanner endpoints
    BASE_URL = "https://sky-scanner3.p.rapidapi.com/flights"

    def __init__(self):
        self.api_key = os.getenv("SKYSCANNER_API_KEY")
        if not self.api_key:
            raise ValueError(
                "SKYSCANNER_API_KEY not found in environment. "
                "Get your API key from https://rapidapi.com/skyscanner/api/skyscanner-api"
            )

        self.headers = {
            "x-rapidapi-key": self.api_key,
            "x-rapidapi-host": "sky-scanner3.p.rapidapi.com",
            "Content-Type": "application/json"
        }

    def scrape(self, origin: str, destination: str, departure_date: str, adults: int, arrival_date: str) -> Iterable[Offer]:
        """
        Scrape flight offers from Skyscanner API.

        This uses the search-one-way endpoint for simplicity.
        For round-trip, use search-return endpoint instead.
        """

        # Step 1: Create search
        search_url = f"{self.BASE_URL}/search-one-way"

        payload = {
            "fromEntityId": origin,  # IATA code or Skyscanner entity ID
            "toEntityId": destination,
            "departDate": departure_date,  # YYYY-MM-DD
            "adults": str(adults),
            "cabinClass": "economy",
            "currency": "EUR",
            "market": "CZ",  # Czech market
            "locale": "cs-CZ"
        }

        try:
            # Make the search request
            print(f"[{self.NAME}] Searching flights {origin} -> {destination} on {departure_date}...")
            response = requests.get(
                search_url,
                headers=self.headers,
                params=payload,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            # Parse results
            offers = self._parse_response(data, origin, destination, departure_date)

            # Rate limiting - be nice to the API
            time.sleep(2.0)

            return offers

        except requests.exceptions.RequestException as e:
            print(f"[{self.NAME}] API request failed: {e}")
            return []
        except Exception as e:
            print(f"[{self.NAME}] Error parsing response: {e}")
            return []

    def _parse_response(self, data: dict, origin: str, destination: str, departure_date: str) -> list[Offer]:
        """
        Parse Skyscanner API response into Offer objects.

        Response structure varies by API version. This handles common formats.
        """
        offers = []

        # Common response structure check
        if not data or "data" not in data:
            print(f"[{self.NAME}] No data in API response")
            return offers

        itineraries = data.get("data", {}).get("itineraries", [])
        legs_data = data.get("data", {}).get("legs", {})

        for itinerary in itineraries:
            try:
                # Extract price
                pricing = itinerary.get("pricing_options", [{}])[0]
                price_amount = pricing.get("price", {}).get("amount", 0)
                price_currency = pricing.get("price", {}).get("unit", "EUR")

                # Get leg details
                leg_ids = itinerary.get("leg_ids", [])
                if not leg_ids:
                    continue

                # Get first leg (outbound)
                leg = legs_data.get(leg_ids[0], {})

                # Extract flight details
                carriers = leg.get("carriers", {}).get("marketing", [{}])
                airline_code = carriers[0].get("id", "Unknown") if carriers else "Unknown"
                airline_name = carriers[0].get("name", airline_code) if carriers else airline_code

                # Flight number (if available)
                segments = leg.get("segments", [])
                flight_number = None
                if segments:
                    flight_number = segments[0].get("operating_carrier", {}).get("flight_number")

                # Cabin class
                segments_data = data.get("data", {}).get("segments", {})
                cabin = "Economy"
                if segments and segments[0].get("id") in segments_data:
                    segment_detail = segments_data.get(segments[0].get("id"), {})
                    cabin = segment_detail.get("cabin_class", "Economy")

                # Create offer
                offer = Offer(
                    provider=self.NAME,
                    origin=origin,
                    destination=destination,
                    departure_date=departure_date,
                    airline=airline_name,
                    flight_number=flight_number,
                    cabin=cabin,
                    fare_class=None,
                    price_currency=price_currency,
                    price_amount=float(price_amount) / 1000 if price_amount > 1000 else float(price_amount),  # Some APIs return cents
                    url=f"https://www.skyscanner.cz/transport/flights/{origin}/{destination}/{departure_date}/"
                )

                offers.append(offer)

            except (KeyError, ValueError, TypeError) as e:
                print(f"[{self.NAME}] Error parsing itinerary: {e}")
                continue

        print(f"[{self.NAME}] Found {len(offers)} flight offers")
        return offers
