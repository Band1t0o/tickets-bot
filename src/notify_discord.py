"""Discord notifications for sweep results.

Two kinds of message:

1. **Price improvement** - posted only when a scenario's cheapest total drops
   below the best previously recorded, or falls under an absolute threshold.
   The previous behaviour alerted on any content hash it had not seen before,
   which meant a single flight parsed ten times produced "10 New Flight Offers".

2. **Health** - posted when a sweep finds nothing, or fails more than half its
   searches. Without this, a broken scraper is indistinguishable from a quiet
   day: the old workflow died in December 2025 and went unnoticed for 8 months.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

COLOR_GOOD = 0x27AB83   # palette green500
COLOR_INFO = 0x1980D4   # palette blue600
COLOR_ALERT = 0xE12D39  # palette red500


def load_best(directory: Path | str) -> float | None:
    """Best total previously recorded for a scenario, if any."""
    path = Path(directory) / "best.json"
    if not path.exists():
        return None
    try:
        return float(json.loads(path.read_text(encoding="utf-8"))["best_total"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        # A corrupt state file must not stop the sweep from reporting.
        return None


def save_best(directory: Path | str, best_total: float, currency: str) -> None:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "best.json").write_text(
        json.dumps(
            {
                "best_total": best_total,
                "currency": currency,
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def should_alert(
    best_total: float | None, previous_best: float | None, threshold: float | None
) -> bool:
    """True only when this is genuinely news."""
    if best_total is None:
        return False
    if threshold is not None and best_total <= threshold:
        return True
    if previous_best is None:
        return True
    return best_total < previous_best


def _itinerary_field(label: str, itinerary) -> dict:
    lines = []
    for leg in itinerary.legs:
        when = leg.depart_date.isoformat() if leg.depart_date else "?"
        times = f" {leg.depart_time}→{leg.arrive_time}" if leg.depart_time else ""
        stops = f" · {leg.stops} stop(s)" if leg.stops is not None else ""
        lines.append(
            f"**{leg.origin}→{leg.destination}** {when}{times} · {leg.airline}{stops} · "
            f"{leg.price_amount:,.0f} {leg.price_currency}"
        )
    lines.append(f"[Open search]({itinerary.legs[0].url})" if itinerary.legs[0].url else "")
    return {
        "name": f"{label} — {itinerary.total_price:,.0f} {itinerary.currency}",
        "value": "\n".join(line for line in lines if line),
        "inline": False,
    }


def build_price_embed(
    scenario_name: str,
    best_same=None,
    best_jaw=None,
    previous_best: float | None = None,
) -> dict:
    fields = []
    if best_same is not None:
        fields.append(_itinerary_field("Cheapest returning to the same airport", best_same))
    if best_jaw is not None:
        fields.append(_itinerary_field("Cheapest open jaw", best_jaw))

    totals = [i.total_price for i in (best_same, best_jaw) if i is not None]
    best_total = min(totals) if totals else None

    description = f"Best total: **{best_total:,.0f}**" if best_total is not None else "No itineraries found"
    if previous_best is not None and best_total is not None:
        delta = previous_best - best_total
        if delta > 0:
            description += f" — down {delta:,.0f} from {previous_best:,.0f}"

    return {
        "title": f"✈️ {scenario_name}",
        "description": description,
        "color": COLOR_GOOD,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Flight scenario watcher"},
    }


def build_health_alert(
    scenario_name: str, legs_found: int, errors: int, total: int
) -> dict | None:
    """A red embed when the sweep looks broken, otherwise None."""
    if total == 0:
        reason = "the sweep planned no searches at all"
    elif legs_found == 0:
        reason = f"all {total} searches completed but returned no flights"
    elif errors > total / 2:
        reason = f"{errors} of {total} searches failed"
    else:
        return None

    return {
        "title": f"⚠️ {scenario_name}: sweep looks broken",
        "description": (
            f"The scraper {reason}. This usually means pelikan.cz changed its markup. "
            "Silence would otherwise look identical to 'no cheap flights today'."
        ),
        "color": COLOR_ALERT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Flight scenario watcher — health check"},
    }


def post(webhook_url: str, embeds: list[dict]) -> bool:
    if not embeds:
        return False
    try:
        response = requests.post(
            webhook_url,
            json={"username": "Flight Tracker", "embeds": embeds},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as exc:
        print(f"[Discord] failed to send: {exc}")
        return False


def notify_sweep(scenario, result, webhook_url: str | None = None) -> bool:
    """Evaluate a finished sweep and post whatever it warrants."""
    from .combine import best_open_jaw, best_same_airport, combine

    webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("[Discord] DISCORD_WEBHOOK_URL not set, skipping notification")
        return False

    health = build_health_alert(
        scenario.name, len(result.legs), len(result.errors), result.total
    )
    if health is not None:
        return post(webhook_url, [health])

    itineraries = combine(result.legs, scenario)
    if not itineraries:
        return post(
            webhook_url,
            [build_health_alert(scenario.name, 0, len(result.errors), result.total)],
        )

    best_same = best_same_airport(itineraries)
    best_jaw = best_open_jaw(itineraries)
    totals = [i.total_price for i in (best_same, best_jaw) if i is not None]
    best_total = min(totals)

    state_dir = result.directory.parent
    previous_best = load_best(state_dir)

    if not should_alert(best_total, previous_best, scenario.alert_threshold_czk):
        print(f"[Discord] best total {best_total:,.0f} is no better than {previous_best}; staying quiet")
        return False

    sent = post(webhook_url, [build_price_embed(scenario.name, best_same, best_jaw, previous_best)])
    if sent:
        save_best(state_dir, best_total, itineraries[0].currency)
    return sent


if __name__ == "__main__":
    # Legacy entry point kept so the old workflow step does not hard-fail.
    if not os.getenv("DISCORD_WEBHOOK_URL"):
        print("[Discord] DISCORD_WEBHOOK_URL not set, skipping notification")
        sys.exit(0)
    print("[Discord] use `python -m src.cli sweep` — it notifies as part of the run")
