"""Scenario definition and storage.

A scenario is a saved search: which airports, which date window, how long to
stay where. Scenarios live as JSON files under `scenarios/` and are committed,
so the scheduled cloud sweep and the local UI read the same definitions.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

TRIP_TYPES = ("round_trip", "multi_city")
DEPTHS = ("quick", "standard", "deep")

# Days between searched departure dates, per depth.
DEPTH_STEP_DAYS = {"quick": 7, "standard": 3, "deep": 1}


@dataclass
class Scenario:
    id: str
    name: str
    trip_type: str
    origins: list[str]
    japan_airports: list[str]
    ph_airports: list[str]
    window_start: date
    window_end: date
    japan_stay_days: tuple[int, int] = (9, 11)
    ph_stay_days: tuple[int, int] = (9, 11)
    trip_length_days: tuple[int, int] = (18, 22)  # round trip only
    adults: int = 1
    depth: str = "standard"
    # Added to any leg that does not confirm an included checked bag, so
    # rankings compare like with like. An estimate, never a quoted fare.
    bag_estimate_czk: int = 1500
    alert_threshold_czk: int | None = None
    enabled: bool = True
    notes: str = ""

    def validate(self) -> None:
        """Raise ValueError with a message the UI can show verbatim."""
        if self.trip_type not in TRIP_TYPES:
            raise ValueError(f"trip_type must be one of {TRIP_TYPES}, got {self.trip_type!r}")
        if self.depth not in DEPTHS:
            raise ValueError(f"depth must be one of {DEPTHS}, got {self.depth!r}")
        if not self.origins:
            raise ValueError("origins must list at least one departure airport")
        if not self.japan_airports:
            raise ValueError("japan_airports must list at least one arrival airport")
        if self.trip_type == "multi_city" and not self.ph_airports:
            raise ValueError("ph_airports is required for a multi_city trip")
        if self.window_end < self.window_start:
            raise ValueError(
                f"window_end ({self.window_end}) must not precede window_start ({self.window_start})"
            )
        for label, span in (
            ("japan_stay_days", self.japan_stay_days),
            ("ph_stay_days", self.ph_stay_days),
            ("trip_length_days", self.trip_length_days),
        ):
            low, high = span
            if low > high:
                raise ValueError(f"{label} minimum ({low}) exceeds maximum ({high})")
            if low < 1:
                raise ValueError(f"{label} minimum must be at least 1 day")
        if not 1 <= self.adults <= 9:
            raise ValueError(f"adults must be between 1 and 9, got {self.adults}")
        if self.trip_type == "multi_city":
            # Without this the planner silently emits leg A only: legs B and C
            # have no valid departure dates, and the sweep yields no itineraries
            # at all with nothing obviously wrong.
            needed = self.japan_stay_days[0] + self.ph_stay_days[0]
            available = (self.window_end - self.window_start).days
            if available < needed:
                raise ValueError(
                    f"window is {available} days but the minimum stays need {needed} "
                    f"({self.japan_stay_days[0]} in Japan + {self.ph_stay_days[0]} in the "
                    "Philippines); widen the window or shorten the stays"
                )

    @property
    def step_days(self) -> int:
        return DEPTH_STEP_DAYS[self.depth]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["window_start"] = self.window_start.isoformat()
        data["window_end"] = self.window_end.isoformat()
        for key in ("japan_stay_days", "ph_stay_days", "trip_length_days"):
            data[key] = list(getattr(self, key))
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Scenario:
        payload = dict(data)
        payload["window_start"] = date.fromisoformat(payload["window_start"])
        payload["window_end"] = date.fromisoformat(payload["window_end"])
        for key in ("japan_stay_days", "ph_stay_days", "trip_length_days"):
            if key in payload and payload[key] is not None:
                payload[key] = tuple(payload[key])
        return cls(**payload)


def save_scenario(scenario: Scenario, directory: Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{scenario.id}.json"
    path.write_text(json.dumps(scenario.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_scenario(path: Path) -> Scenario:
    return Scenario.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def load_scenarios(directory: Path) -> list[Scenario]:
    directory = Path(directory)
    if not directory.exists():
        return []
    return sorted(
        (load_scenario(p) for p in directory.glob("*.json")),
        key=lambda s: s.id,
    )
