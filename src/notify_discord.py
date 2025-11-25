"""
Discord webhook notification script.
Sends a message to Discord when new flight offers are found.
"""
from __future__ import annotations
import os
import sys
import json
import requests
from pathlib import Path
from datetime import datetime


def send_discord_notification(webhook_url: str, offers_count: int, data_dir: str = "./data"):
    """
    Send a Discord notification with flight price summary.

    Args:
        webhook_url: Discord webhook URL
        offers_count: Number of new offers found
        data_dir: Directory containing scraped data
    """
    if offers_count == 0:
        print("[Discord] No new offers to notify about")
        return

    # Find the latest data directory (today's date)
    today = datetime.now().strftime("%Y-%m-%d")
    today_dir = Path(data_dir) / today

    # Read all JSONL files from today (excluding DEMO_STATIC files)
    all_offers = []
    if today_dir.exists():
        for jsonl_file in today_dir.glob("*.jsonl"):
            # Skip demo/static files
            if "DEMO_STATIC" in jsonl_file.name or "demo" in jsonl_file.name.lower():
                print(f"[Discord] Skipping demo file: {jsonl_file.name}")
                continue

            with open(jsonl_file, "r") as f:
                for line in f:
                    if line.strip():
                        all_offers.append(json.loads(line))

    # Group offers by route
    routes = {}
    for offer in all_offers:
        route = f"{offer['origin']} → {offer['destination']}"
        if route not in routes:
            routes[route] = []
        routes[route].append(offer)

    # Build Discord embed
    embeds = []

    # Summary embed
    summary_fields = []
    for route, route_offers in routes.items():
        prices = [o['price_amount'] for o in route_offers]
        min_price = min(prices)
        max_price = max(prices)
        currency = route_offers[0]['price_currency']

        summary_fields.append({
            "name": route,
            "value": f"**{len(route_offers)}** offers | {min_price:,.0f} - {max_price:,.0f} {currency}",
            "inline": False
        })

    embeds.append({
        "title": f"✈️ {offers_count} New Flight Offers Found!",
        "description": f"Found new prices on {today}",
        "color": 3447003,  # Blue
        "fields": summary_fields,
        "timestamp": datetime.utcnow().isoformat(),
        "footer": {"text": "Vietnam Tickets Scraper"}
    })

    # Find best 3 deals for EACH route
    for route, route_offers in routes.items():
        # Sort offers by price and get top 3
        sorted_route_offers = sorted(route_offers, key=lambda x: x['price_amount'])[:3]

        if sorted_route_offers:
            best_deals_fields = []
            for i, offer in enumerate(sorted_route_offers, 1):
                best_deals_fields.append({
                    "name": f"#{i} - {offer['price_amount']:,.0f} {offer['price_currency']}",
                    "value": f"**{offer['provider']}**\n"
                             f"Dates: {offer['departure_date']} → {offer.get('return_date', 'N/A')}\n"
                             f"[View offer]({offer.get('url', '#')})" if offer.get('url') else "",
                    "inline": False
                })

            embeds.append({
                "title": f"🏆 Best Deals: {route}",
                "color": 3066993,  # Green
                "fields": best_deals_fields
            })

    # Send webhook
    payload = {
        "username": "Flight Tracker",
        "embeds": embeds
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        print(f"[Discord] Successfully sent notification ({offers_count} offers)")
    except requests.exceptions.RequestException as e:
        print(f"[Discord] Failed to send notification: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Get webhook URL from environment (don't load full settings to avoid requiring scraper env vars)
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    if not webhook_url:
        print("[Discord] DISCORD_WEBHOOK_URL not set, skipping notification")
        sys.exit(0)

    # Get offer count from command line arg or environment
    offers_count = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    send_discord_notification(webhook_url, offers_count)
