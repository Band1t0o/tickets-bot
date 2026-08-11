"""Scenario definition and storage.

A scenario is a saved search: which airports, which date window, how long to
stay where. Scenarios live as JSON files under `scenarios/` and are committed,
so the scheduled cloud sweep and the local UI read the same definitions.

A trip is an ordered chain of stops, and nothing here names a country. The
previous schema had `japan_airports`, `ph_airports`, `japan_stay_days` and
`ph_stay_days` as literal field names, restated in a Pydantic mirror, an HTML
form and a JS mapper - so a different trip meant editing code in six files.

The chain also removes a whole bug shape rather than fixing an instance of it.
`trip_type` used to select between a two-leg round trip and a three-leg
multi-city, each with its own hand-written planner and combiner branch. The
round-trip branch planned only outbound searches while its combiner branch
required a leg departing the destination, so it could never yield a single
itinerary. Here every leg - including the one home - is emitted by the same
loop, so the two halves cannot disagree about what a trip is.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

DEPTHS = ("quick", "standard", "deep")

# Picks a sweep can report. "cheapest" is the best bag-inclusive total whatever
# the route; "preferred" is the best one flying from and back to the highest
# tier of `preferred_origins` that has anything at all.
NOTIFY_SELECTIONS = ("cheapest", "preferred")

# Days between searched departure dates, per depth.
DEPTH_STEP_DAYS = {"quick": 7, "standard": 3, "deep": 1}

# Airports are typed by hand now that any destination is reachable, and a typo
# would otherwise become a search that quietly finds nothing.
IATA_RE = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class Stop:
    """One place the trip stays before flying on.

    `airports` are alternatives, not a sequence: any of them satisfies this
    stop, and the combiner may arrive at one only if it also departs from that
    same one - landing in Tokyo cannot be followed by departing Osaka.
    """

    airports: list[str]
    stay_days: tuple[int, int]
    label: str = ""

    def describe(self, index: int) -> str:
        """Human name for error messages; falls back to a position."""
        return self.label or f"stop {index + 1}"


@dataclass
class Scenario:
    id: str
    name: str
    origins: list[str]
    stops: list[Stop]
    window_start: date
    window_end: date
    # None means "back where you started". A different list is an open jaw by
    # construction - fly home to somewhere other than the departure airport.
    return_to: list[str] | None = None
    # True drops the final leg entirely: a one-way chain that just ends.
    one_way: bool = False
    adults: int = 1
    depth: str = "standard"
    currency: str = "CZK"
    alert_threshold: int | None = None
    bag_estimate: int = 1500
    enabled: bool = True
    notes: str = ""

    # ---------------------------------------------------- what gets sent
    #
    # These live here rather than in a settings file because the scenario is
    # what the scheduled cloud sweep reads. A preference held anywhere else
    # would never reach the run that actually sends the message.

    # Airports you would rather fly from, best first. An itinerary belongs to a
    # tier when *both* ends of it are in that tier or better, which handles an
    # open jaw without needing a second setting. Empty means no preference, and
    # then only the cheapest is ever reported.
    preferred_origins: list[list[str]] = field(default_factory=list)
    # Which picks to report. See `src/alerts.py` for what each one means.
    notify: list[str] = field(default_factory=lambda: ["cheapest", "preferred"])
    # Stay silent unless a pick actually improved on the best recorded for it.
    # Default True: at two sweeps a day, unconditional reporting is ~60 messages
    # a month, most of them "still 21,324". Set false for a digest every run.
    notify_quiet: bool = True

    # ------------------------------------------------------------------ shape

    @property
    def airport_pools(self) -> list[list[str]]:
        """Candidate airports at each point of the chain, in travel order.

        The single source of truth for the trip's shape: the planner walks
        consecutive pairs to emit searches, and the combiner walks the same
        pairs to chain legs. They cannot drift apart because they read this.
        """
        pools = [self.origins] + [stop.airports for stop in self.stops]
        if not self.one_way:
            pools.append(self.return_to or self.origins)
        return pools

    @property
    def leg_count(self) -> int:
        return len(self.airport_pools) - 1

    @property
    def step_days(self) -> int:
        return DEPTH_STEP_DAYS[self.depth]

    def earliest_departure(self, leg_index: int) -> int:
        """Days after `window_start` that leg `leg_index` may first depart.

        Leg 0 leaves on day zero; every later leg must wait out the minimum
        stays of every stop before it.
        """
        return sum(stop.stay_days[0] for stop in self.stops[:leg_index])

    @property
    def min_trip_days(self) -> int:
        return sum(stop.stay_days[0] for stop in self.stops)

    # ------------------------------------------------------------- validation

    def validate(self) -> None:
        """Raise ValueError with a message the UI can show verbatim."""
        if self.depth not in DEPTHS:
            raise ValueError(f"depth must be one of {DEPTHS}, got {self.depth!r}")
        if not self.origins:
            raise ValueError("origins must list at least one departure airport")
        if not self.stops:
            raise ValueError("a trip needs at least one destination")

        groups = [("origins", self.origins)]
        groups += [(stop.describe(i), stop.airports) for i, stop in enumerate(self.stops)]
        if self.return_to is not None:
            groups.append(("return_to", self.return_to))

        for label, codes in groups:
            if not codes:
                raise ValueError(f"{label} must list at least one airport")
            for code in codes:
                if not IATA_RE.match(code):
                    raise ValueError(f"{label}: {code!r} is not a 3-letter IATA airport code")

        for index, stop in enumerate(self.stops):
            low, high = stop.stay_days
            name = stop.describe(index)
            if low > high:
                raise ValueError(f"{name}: minimum stay ({low}) exceeds maximum ({high})")
            if low < 1:
                raise ValueError(f"{name}: minimum stay must be at least 1 day")

        if self.window_end < self.window_start:
            raise ValueError(
                f"window_end ({self.window_end}) must not precede "
                f"window_start ({self.window_start})"
            )
        if not 1 <= self.adults <= 9:
            raise ValueError(f"adults must be between 1 and 9, got {self.adults}")

        seen: dict[str, int] = {}
        for rank, tier in enumerate(self.preferred_origins, start=1):
            if not tier:
                raise ValueError(f"preferred_origins tier {rank} is empty; remove it or fill it")
            for code in tier:
                if not IATA_RE.match(code):
                    raise ValueError(
                        f"preferred_origins tier {rank}: {code!r} is not a 3-letter IATA code"
                    )
                # Otherwise "the best tier containing this airport" has two
                # answers and the reported pick depends on iteration order.
                if code in seen:
                    raise ValueError(
                        f"preferred_origins: {code} appears in tier {seen[code]} and tier {rank}"
                    )
                seen[code] = rank

        unknown = [name for name in self.notify if name not in NOTIFY_SELECTIONS]
        if unknown:
            raise ValueError(
                f"notify: {', '.join(sorted(unknown))} is not one of {NOTIFY_SELECTIONS}"
            )

        # Without this the planner emits the first leg and nothing else: the
        # later legs have no valid departure dates, so the sweep yields no
        # itineraries at all with nothing obviously wrong.
        available = (self.window_end - self.window_start).days
        if available < self.min_trip_days:
            breakdown = " + ".join(
                f"{stop.stay_days[0]} at {stop.describe(i)}" for i, stop in enumerate(self.stops)
            )
            raise ValueError(
                f"window is {available} days but the minimum stays need "
                f"{self.min_trip_days} ({breakdown}); widen the window or shorten the stays"
            )

    # ---------------------------------------------------------- serialisation

    def to_dict(self) -> dict:
        data = asdict(self)
        data["window_start"] = self.window_start.isoformat()
        data["window_end"] = self.window_end.isoformat()
        data["stops"] = [
            {"label": s.label, "airports": list(s.airports), "stay_days": list(s.stay_days)}
            for s in self.stops
        ]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Scenario:
        payload = _migrate(dict(data))
        payload["window_start"] = date.fromisoformat(payload["window_start"])
        payload["window_end"] = date.fromisoformat(payload["window_end"])
        payload["stops"] = [
            Stop(
                airports=list(s["airports"]),
                stay_days=tuple(s["stay_days"]),
                label=s.get("label", ""),
            )
            for s in payload["stops"]
        ]
        unknown = set(payload) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown scenario fields: {', '.join(sorted(unknown))}")
        return cls(**payload)


def _migrate(payload: dict) -> dict:
    """Translate a pre-chain scenario file into the current shape.

    Kept rather than one-shot converting the files and deleting this: a
    scenario hand-edited from an old example should load, not fail with a schema
    error naming fields the person never typed.
    """
    if "japan_airports" not in payload:
        return payload

    trip_type = payload.pop("trip_type", "multi_city")
    japan = payload.pop("japan_airports", [])
    ph = payload.pop("ph_airports", []) or []
    japan_stay = list(payload.pop("japan_stay_days", (9, 11)))
    ph_stay = list(payload.pop("ph_stay_days", (9, 11)))
    trip_length = list(payload.pop("trip_length_days", (18, 22)))

    if trip_type == "round_trip":
        # The single stop's stay *is* the old trip length: days away from home.
        payload["stops"] = [{"label": "", "airports": japan, "stay_days": trip_length}]
    else:
        payload["stops"] = [
            {"label": "", "airports": japan, "stay_days": japan_stay},
            {"label": "", "airports": ph, "stay_days": ph_stay},
        ]

    # Currency moved out of the field names, so a non-CZK provider does not have
    # to rename the schema to be usable.
    if "alert_threshold_czk" in payload:
        payload["alert_threshold"] = payload.pop("alert_threshold_czk")
    if "bag_estimate_czk" in payload:
        payload["bag_estimate"] = payload.pop("bag_estimate_czk")
    return payload


def save_scenario(scenario: Scenario, directory: Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{scenario.id}.json"
    path.write_text(
        json.dumps(scenario.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
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
