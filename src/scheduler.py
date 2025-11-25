from __future__ import annotations
import time
from datetime import datetime

def is_night_hour(now: datetime) -> bool:
    return now.hour >= 22 or now.hour < 6

def loop(day_minutes: int, night_minutes: int):
    while True:
        yield  # let caller perform scrape
        now = datetime.now()
        minutes = night_minutes if is_night_hour(now) else day_minutes
        time.sleep(max(60, minutes * 60))
