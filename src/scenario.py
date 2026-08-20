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

# Days that may be watched at once. Not a taste limit - a budget one. A watch
# prices every airport pair of every leg on its pinned dates, which is 21
# searches a candidate on the Japan/Philippines trip, and pelikan.cz answers
# about 120 per runner before it stops answering at all. Six is the point where
# a fourth-hourly watch of a trip that size still fits in one runner with room
# to spare. `web.app` refuses on the real planned count as well, which is the
# figure that actually binds; this is the cheap guard that needs no planner.
MAX_WATCHES = 6


@dataclass(frozen=True)
class Stop:
    """One place the trip stays before flying on.

    `airports` are alternatives, not a sequence: any of them satisfies this
    stop, and the combiner may arrive at one only if it also departs from that
    same one - landing in Tokyo cannot be followed by departing Osaka.

    Unless the stop is `overland`, which is the one case where landing in Tokyo
    *is* followed by departing Osaka: you cross the country on the ground in
    between. Fly into Haneda and out of Kansai; into Porto and out of Lisbon.
    The airports stay alternatives - arriving and leaving at the same one is
    still allowed - but the chain rule is suspended for this stop alone, and
    nothing about the stay window changes: days are still counted from the leg
    that lands to the leg that leaves.
    """

    airports: list[str]
    stay_days: tuple[int, int]
    label: str = ""
    # Named for what happens rather than "open jaw", which already means the
    # trip-level "starts and ends at different airports" here (see
    # `Itinerary.same_airport` and `combine.best_open_jaw`). One word, two
    # meanings, in files that read each other is how the round-trip and
    # multi-city branches drifted until neither could build a trip.
    overland: bool = False

    def describe(self, index: int) -> str:
        """Human name for error messages; falls back to a position."""
        return self.label or f"stop {index + 1}"


@dataclass(frozen=True)
class Watch:
    """One candidate trip, tracked on the exact dates it was found on.

    `depart_dates` holds one date per leg, in travel order: the day you leave
    home, the day you fly on, the day you fly back. Pinning all of them - rather
    than pinning the departure and deriving the rest through the stay ranges -
    is what makes a watch cheap enough to run every few hours. On the
    Japan/Philippines trip that is 21 searches against 75, and the site answers
    about 120 before it stops answering.

    What it gives up is stay length: a candidate pinned to nine days in Japan
    will not notice that ten days got cheaper. The daily sweep still prices the
    whole window, which is what it is for.

    The first date is the key. It is the one a person means by "the 12th", it is
    stable while the trip is edited around it, and it is what the price series
    on the Watch tab is drawn against.
    """

    depart_dates: list[date]
    added_at: str = ""
    # What it cost when it was picked, so the tab can say "up 900 since you
    # started watching" on the very first observation rather than after two.
    added_price: float | None = None
    currency: str = "CZK"

    @property
    def key(self) -> str:
        return self.depart_dates[0].isoformat() if self.depart_dates else ""


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
    # Narrows the sweep to first-leg departures inside this range, once a broad
    # sweep has shown which dates are worth watching closely. Both None means the
    # whole window. Deliberately a bound on the *first* leg only: the later legs
    # are derived from it through the stay ranges, so a focus stays a statement
    # about when you leave rather than three ranges that can contradict.
    focus_start: date | None = None
    focus_end: date | None = None
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
    # Candidate trips tracked on their exact dates by the four-hourly watch,
    # rather than by the daily sweep of the whole window. Empty means the trip
    # is not watched at all, and the watch workflow skips it entirely.
    watches: list[Watch] = field(default_factory=list)
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
    def pool_roles(self) -> list[dict]:
        """What each entry of `airport_pools` is, positionally aligned with it.

        Exists because knowing an airport is bad is only half of acting on it -
        something has to know *which list to take it out of*. Derived from the
        same fields as `airport_pools` so the two cannot drift.

        The last pool reports itself as `origins` when `return_to` is None,
        because that is literally the list it is: editing "the way home" of a
        trip that has no separate return airports means editing the origins.
        """
        roles: list[dict] = [{"role": "origins", "stop_index": None, "label": "Departure"}]
        roles += [
            {"role": "stop", "stop_index": index, "label": stop.describe(index)}
            for index, stop in enumerate(self.stops)
        ]
        if not self.one_way:
            roles.append(
                {"role": "return_to", "stop_index": None, "label": "Back to"}
                if self.return_to is not None
                else {"role": "origins", "stop_index": None, "label": "Back home"}
            )
        return roles

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

    def remaining_min_stay(self, leg_index: int) -> int:
        """Days that must still be spent at stops before the *final* leg departs.

        The mirror image of `earliest_departure`, and the bound that was missing.
        A leg planned any later than `horizon - remaining_min_stay(leg)` cannot
        reach a final leg the sweep also searched, so every offer found on it is
        an orphan: measured on the real trip, 132 of a deep sweep's 615 searches
        were spent on dates no itinerary could ever use.

        The slice stops one short of the end on purpose. The last leg arrives
        wherever the trip finishes and nothing has to happen after it, so it
        reserves nothing - true for a one-way chain as much as for a return.
        """
        return sum(stop.stay_days[0] for stop in self.stops[leg_index : self.leg_count - 1])

    def max_stay_before(self, leg_index: int) -> int:
        """Most days this leg can trail the first leg's departure.

        Only meaningful with a focus set: it converts "depart between these two
        dates" into how late each later leg could still legitimately be.
        """
        return sum(stop.stay_days[1] for stop in self.stops[:leg_index])

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
            # Overland means arriving at one of these airports and leaving from
            # another. With one airport there is no other, so the flag can only
            # be a misunderstanding of what it does - and silently ignoring it
            # would leave someone waiting for Kansai departures that were never
            # going to be chained.
            if stop.overland and len(stop.airports) < 2:
                raise ValueError(
                    f"{name}: an overland stop needs at least two airports - one to "
                    f"arrive at and another to leave from. Add the second airport, "
                    f"or untick travelling overland."
                )

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

        if (self.focus_start is None) != (self.focus_end is None):
            raise ValueError(
                "a focus needs both a first and a last departure date; "
                "clear both to watch the whole window"
            )
        if self.focus_start is not None:
            if self.focus_end < self.focus_start:
                raise ValueError(
                    f"focus_end ({self.focus_end}) must not precede "
                    f"focus_start ({self.focus_start})"
                )
            # Outside the window the focus is not a narrowing but a different
            # trip, and the sweep it plans would not be comparable with any of
            # the sweeps of the window it claims to be part of.
            if self.focus_start < self.window_start or self.focus_end > self.window_end:
                raise ValueError(
                    f"the focus {self.focus_start}..{self.focus_end} falls outside the "
                    f"window {self.window_start}..{self.window_end}; widen the window first"
                )

        self._validate_watches()

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

    def _validate_watches(self) -> None:
        """Every watched candidate must be a trip this scenario could produce.

        Checked against the shape rather than merely parsed, because a watch
        that cannot chain is worse than a rejected one: the run spends its
        searches, finds legs, and reports no price at all for that day - which
        looks exactly like the site having nothing.
        """
        if len(self.watches) > MAX_WATCHES:
            raise ValueError(
                f"{len(self.watches)} days watched, but only {MAX_WATCHES} may be watched "
                f"at once - each one is a full set of searches every few hours. "
                f"Stop watching a day before adding another."
            )

        seen: set[str] = set()
        for watch in self.watches:
            dates = watch.depart_dates
            if len(dates) != self.leg_count:
                raise ValueError(
                    f"the watch on {watch.key or '(no date)'} has {len(dates)} date(s) but "
                    f"this trip has {self.leg_count} legs; a watch needs one date per leg"
                )
            if watch.key in seen:
                raise ValueError(f"{watch.key} is already being watched; it cannot be watched twice")
            seen.add(watch.key)

            for earlier, later in zip(dates, dates[1:], strict=False):
                if later <= earlier:
                    raise ValueError(
                        f"the watch on {watch.key} has its legs out of order: "
                        f"{later} does not come after {earlier}"
                    )

            # Sliced one short for the same reason `remaining_min_stay` slices:
            # the last leg arrives where the trip ends and nothing has to happen
            # after it, so the final stop of a one-way chain has no departing
            # leg to measure a stay against.
            for index, stop in enumerate(self.stops[: self.leg_count - 1]):
                days = (dates[index + 1] - dates[index]).days
                low, high = stop.stay_days
                if not low <= days <= high:
                    raise ValueError(
                        f"the watch on {watch.key} spends {days} days at "
                        f"{stop.describe(index)}, outside its {low}-{high} day stay"
                    )

            if not self.window_start <= dates[0] <= self.window_end:
                raise ValueError(
                    f"the watch on {watch.key} leaves outside the window "
                    f"{self.window_start}..{self.window_end}; widen the window or stop "
                    f"watching that day"
                )

    # ---------------------------------------------------------- serialisation

    def to_dict(self) -> dict:
        data = asdict(self)
        data["window_start"] = self.window_start.isoformat()
        data["window_end"] = self.window_end.isoformat()
        data["focus_start"] = self.focus_start.isoformat() if self.focus_start else None
        data["focus_end"] = self.focus_end.isoformat() if self.focus_end else None
        data["watches"] = [
            {
                "depart_dates": [d.isoformat() for d in w.depart_dates],
                "added_at": w.added_at,
                "added_price": w.added_price,
                "currency": w.currency,
            }
            for w in self.watches
        ]
        data["stops"] = [
            {
                "label": s.label,
                "airports": list(s.airports),
                "stay_days": list(s.stay_days),
                "overland": s.overland,
            }
            for s in self.stops
        ]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Scenario:
        payload = _migrate(dict(data))
        payload["window_start"] = date.fromisoformat(payload["window_start"])
        payload["window_end"] = date.fromisoformat(payload["window_end"])
        for key in ("focus_start", "focus_end"):
            value = payload.get(key)
            payload[key] = date.fromisoformat(value) if value else None
        payload["watches"] = [
            Watch(
                depart_dates=[date.fromisoformat(d) for d in w["depart_dates"]],
                added_at=w.get("added_at", ""),
                added_price=w.get("added_price"),
                currency=w.get("currency", "CZK"),
            )
            # Absent from every file written before the Watch tab existed.
            for w in payload.get("watches") or []
        ]
        payload["stops"] = [
            Stop(
                airports=list(s["airports"]),
                stay_days=tuple(s["stay_days"]),
                label=s.get("label", ""),
                # Absent from every file written before overland stops existed,
                # and those are committed trips that must keep loading.
                overland=bool(s.get("overland", False)),
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


def read_scenarios(directory: Path) -> tuple[list[Scenario], list[dict]]:
    """Every trip that loads, plus a named reason for each one that does not.

    Separate from `load_scenarios` because the two callers want opposite
    things. A sweep asked for a specific trip should fail loudly rather than
    quietly run something else. The UI listing every trip should show the ones
    it has: one file with a typo in it is not a reason to render an empty
    picker, and an empty picker is exactly what a deleted database looks like.
    """
    directory = Path(directory)
    if not directory.exists():
        return [], []

    scenarios: list[Scenario] = []
    problems: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            scenarios.append(load_scenario(path))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            problems.append({"file": path.name, "error": str(exc)})
    scenarios.sort(key=lambda s: s.id)
    return scenarios, problems


def load_scenarios(directory: Path) -> list[Scenario]:
    """Strict: one unreadable file raises. Use `read_scenarios` to be told."""
    directory = Path(directory)
    if not directory.exists():
        return []
    return sorted(
        (load_scenario(p) for p in directory.glob("*.json")),
        key=lambda s: s.id,
    )
