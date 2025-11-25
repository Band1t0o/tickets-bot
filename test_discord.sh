#!/bin/bash
# Test Discord notifications locally

set -e

echo "🚀 Running scraper with DEMO_STATIC provider..."
python -m src.cli scrape --provider DEMO_STATIC

echo ""
echo "📊 Checking offer count..."
OFFER_COUNT=$(cat data/latest_offer_count.txt 2>/dev/null || echo "0")
echo "Found $OFFER_COUNT new offers"

if [ "$OFFER_COUNT" -gt 0 ]; then
    echo ""
    echo "📢 Sending Discord notification..."
    python -m src.notify_discord "$OFFER_COUNT"
    echo "✅ Notification sent!"
else
    echo "⚠️  No new offers, skipping notification"
fi

echo ""
echo "Done! Check your Discord channel."
