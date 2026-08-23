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
from datetime import date, timedelta
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
    # Once the crossing is decided, the alternatives are searched for nothing.
    # `overland` opens every in/out combination so a probe can find out which
    # one is worth having; these spend that answer. None means "still open", so
    # a trip that has not been probed behaves exactly as it did before.
    #
    # Two fields rather than one pair because the sides are decided separately:
    # a probe often settles the way out of a country long before the way in.
    # They are only meaningful on an overland stop - see `validate`.
    arrive_via: str | None = None
    depart_via: str | None = None

    def describe(self, index: int) -> str:
        """Human name for error messages; falls back to a position."""
        return self.label or f"stop {index + 1}"

    @property
    def arrive_at(self) -> list[str]:
        """Airports a leg may land at to satisfy this stop."""
        return [self.arrive_via] if self.arrive_via else list(self.airports)

    @property
    def depart_from(self) -> list[str]:
        """Airports the next leg may leave from.

        Equal to `arrive_at` for an ordinary stop by construction, since the
        chain rule already forces leaving from where you landed. It differs
        only where the rule is suspended, which is the one case this exists for.
        """
        return [self.depart_via] if self.depart_via else list(self.airports)


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


@dataclass(frozen=True)
class LegWatch:
    """One route on one date, followed on its own.

    A `Watch` follows a whole chained trip: every airport pair of every leg on
    its pinned dates, 21 searches a candidate on the Japan trip. This follows
    exactly what you point at, and costs one search. The two coexist because
    they answer different questions - a `Watch` asks "is this trip moving", this
    asks "is this ticket moving" - and a decision is usually assembled from the
    second: watch Vienna to Haneda on the 10th and the 12th, and Manila home on
    the 2nd and the 4th, because those were the days that looked promising.

    Deliberately **not** required to be a leg of a trip the sweep could build.
    Picking freely is the whole point; the API says when a route is not one this
    trip searches, and leaves the choice alone.

    There is no cap here. A leg watch is exactly one search, so the honest limit
    is the one `web.app.WATCH_SEARCH_CAP` applies to the whole planned run -
    trip watches and leg watches together - rather than a count of rows that
    would mean something different for each kind.
    """

    origin: str
    destination: str
    depart_date: date
    added_at: str = ""
    # What it cost when it was picked, so the first check can already say which
    # way it has gone rather than only setting a baseline.
    added_price: float | None = None
    currency: str = "CZK"

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.destination}@{self.depart_date.isoformat()}"

    @property
    def route(self) -> str:
        return f"{self.origin}→{self.destination}"


# How far past the window a watched leg may depart.
#
# The final leg of a trip legitimately leaves after `window_end` - the site
# substitutes nearby dates, so the sweep searches past the end of the window for
# exactly that reason (`planner.RETURN_SLACK_DAYS`). Restated here as its own
# number rather than imported, because `scenario` cannot import the planner
# without a cycle, and because this is answering a different question: not "how
# far should a sweep look" but "is this date a typo". Generous on purpose.
WATCH_DATE_SLACK_DAYS = 14


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
    # The other half of a focus: when you want to fly *home*.
    #
    # A focus bounds the first leg and lets the stay ranges derive the rest,
    # which is the right shape while a trip is still being mapped out. It stops
    # being the right shape the moment the return has a reason of its own - the
    # day work starts again, a flat that is let from the 9th - because the only
    # way to reach that date through the stay ranges is to widen them, and
    # widening them re-admits every trip that lands a week early.
    #
    # A bound on the final leg says it directly instead. The planner intersects
    # the two rather than choosing between them, so they cannot contradict:
    # where they leave no room at all, `validate()` says so by name.
    return_focus_start: date | None = None
    return_focus_end: date | None = None
    # Nights the whole trip may last, first leg's departure to final leg's.
    #
    # Not derivable from the stay ranges, and that is the point of it. Japan
    # 10-13 and Philippines 8-13 admits 18 nights and 26 alike; "about 24, and
    # I do not much mind how it splits" is a single constraint neither range can
    # express and both together still cannot, and it is usually the one actually
    # being held. It is also what lets 14+10 be compared against 12+12 instead
    # of one of them being ruled out before it is ever priced.
    #
    # Measured to the final leg's *departure*, because that is the date printed
    # on a ticket. Deliberately not checked against `min_trip_days`, which sums
    # every stop and so over-counts a one-way chain by the stay at its last one;
    # `min_span_days` is the figure this is comparable with.
    total_days: tuple[int, int] | None = None
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
    # Individual routes on individual dates, followed one search each. Kept
    # alongside `watches` rather than replacing them: a trip watch answers what
    # a whole chained trip costs, a leg watch answers what one ticket costs, and
    # the second is how a decision is usually put together.
    leg_watches: list[LegWatch] = field(default_factory=list)
    # Sample the stops in the reverse order too, but only in the probe.
    #
    # "Is it cheaper to fly Philippines first?" is a real question and an
    # expensive one to answer properly: a deep sweep of both orders is twice a
    # ~480-search plan against a site that answers about 120 per runner. The
    # probe prices three dates a leg, so asking it there costs tens of searches
    # rather than hundreds - enough to rule an order out, never enough to pick a
    # day. When the answer is "the other way round", you reorder the stops and
    # sweep that; nothing here reorders anything on its own.
    probe_both_orders: bool = False
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
    def leg_pools(self) -> list[tuple[list[str], list[str]]]:
        """(origins, destinations) per leg, in travel order.

        `airport_pools` holds one list per *place*, which cannot describe a stop
        that is arrived at through one airport and left through another - so
        everything that walks legs reads this instead, and everything that talks
        about a place still reads `airport_pools`. Both are derived from the same
        fields, so the planner and the combiner cannot disagree about the shape
        of a trip any more than they could before.

        Identical to consecutive pairs of `airport_pools` whenever nothing is
        pinned, which is every trip that has not been probed yet.
        """
        pools = self.airport_pools
        arrive, depart = list(pools), list(pools)
        for index, stop in enumerate(self.stops):
            position = index + 1
            arrive[position] = stop.arrive_at
            depart[position] = stop.depart_from
        return [(depart[i], arrive[i + 1]) for i in range(len(pools) - 1)]

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

    def max_stay_after(self, leg_index: int) -> int:
        """Most days the *final* leg can trail leg `leg_index`.

        The mirror of `max_stay_before`, and what turns "be home between these
        two dates" into how early each earlier leg could still legitimately be.
        Slices to `leg_count - 1` for the same reason `remaining_min_stay` does:
        nothing has to happen after the last leg departs.
        """
        return sum(stop.stay_days[1] for stop in self.stops[leg_index : self.leg_count - 1])

    @property
    def min_span_days(self) -> int:
        """Fewest nights from the first leg's departure to the final leg's.

        What `total_days` is comparable with. `min_trip_days` is not: it sums
        every stop, including the last one, which nothing has to wait out
        because the trip ends there. On a round trip the two agree; on a one-way
        chain `min_trip_days` is larger by the stay at the final stop, and using
        it here would reject bands that are perfectly reachable.
        """
        return self.remaining_min_stay(0)

    @property
    def max_span_days(self) -> int:
        return self.max_stay_after(0)

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

            for side, code in (("arrive at", stop.arrive_via), ("leave from", stop.depart_via)):
                if code is None:
                    continue
                # Without overland the chain rule already forces leaving from
                # the airport you landed at, so a pin is not a narrowing but a
                # contradiction. Ignoring it silently would leave someone
                # waiting for Kansai departures nothing was ever going to chain.
                if not stop.overland:
                    raise ValueError(
                        f"{name}: pinning which airport to {side} only means something "
                        f"when you travel overland between them. Tick travelling "
                        f"overland, or clear the pin."
                    )
                if code not in stop.airports:
                    raise ValueError(
                        f"{name}: pinned to {side} {code}, which is not one of its "
                        f"airports ({', '.join(stop.airports)}). Add it to the stop, "
                        f"or pin a different one."
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

        self._validate_narrowing()
        self._validate_watches()
        self._validate_leg_watches()

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

    def _validate_narrowing(self) -> None:
        """The return window, the nights band, and whether anything can meet them.

        A narrowing nothing can satisfy is worse than one that is refused. The
        sweep runs, spends every search, chains not one itinerary, and reports
        the same empty result the site gives when it has no seats - so the
        failure reads as "there are no flights" rather than "you asked for a
        trip that cannot exist". The arithmetic that would notice is already
        here, which is why the noticing is here too.
        """
        if (self.return_focus_start is None) != (self.return_focus_end is None):
            raise ValueError(
                "a return window needs both a first and a last date; "
                "clear both to fly home any time in the window"
            )
        if self.return_focus_start is not None:
            if self.return_focus_end < self.return_focus_start:
                raise ValueError(
                    f"return_focus_end ({self.return_focus_end}) must not precede "
                    f"return_focus_start ({self.return_focus_start})"
                )
            # Same rule as the focus, and for the same reason: outside the
            # window this is not a narrowing but a different trip, and no sweep
            # of the window would be comparable with a sweep of it.
            if (
                self.return_focus_start < self.window_start
                or self.return_focus_end > self.window_end
            ):
                raise ValueError(
                    f"the return window {self.return_focus_start}..{self.return_focus_end} "
                    f"falls outside the window {self.window_start}..{self.window_end}; "
                    f"widen the window first"
                )

        span_low, span_high = self.min_span_days, self.max_span_days
        band = (span_low, span_high)

        if self.total_days is not None:
            low, high = self.total_days
            if low < 0:
                raise ValueError(f"total_days may not be negative, got {low}")
            if high < low:
                raise ValueError(f"total_days runs {low}..{high}, which ends before it starts")
            if high < span_low or low > span_high:
                raise ValueError(
                    f"{low}-{high} nights away is unreachable: the stays allow "
                    f"{span_low}-{span_high} ({self._stay_breakdown()}); "
                    f"change the nights or the stays"
                )
            band = (max(low, span_low), min(high, span_high))

        # Only checkable when both ends are pinned. With one end open the other
        # is bounded by the window, which is loose enough that the check would
        # never fire and would only invite belief that it had.
        if self.focus_start is not None and self.return_focus_start is not None:
            soonest = (self.return_focus_start - self.focus_end).days
            latest = (self.return_focus_end - self.focus_start).days
            if latest < band[0] or soonest > band[1]:
                raise ValueError(
                    f"leaving {self.focus_start}..{self.focus_end} and flying home "
                    f"{self.return_focus_start}..{self.return_focus_end} is {soonest}-{latest} "
                    f"nights away, but {band[0]}-{band[1]} is what the stays"
                    + (" and the nights band" if self.total_days is not None else "")
                    + f" allow ({self._stay_breakdown()}); move one of the two windows"
                )

    def _stay_breakdown(self) -> str:
        """The per-stop ranges, named, for an error about the total of them."""
        return " + ".join(
            f"{stop.stay_days[0]}-{stop.stay_days[1]} at {stop.describe(i)}"
            for i, stop in enumerate(self.stops[: self.leg_count - 1])
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

            # The stay ranges are deliberately *not* checked here.
            #
            # They were, and the reason was sound while it lasted: a watch the
            # combiner could not chain would spend its searches and report no
            # price, which looks exactly like the site having nothing. But a
            # watch pins every leg's date, so its stays are facts rather than a
            # search space, and `watch._admitting` now widens the ranges to
            # admit whatever was pinned before pricing it. There is no longer an
            # unchainable watch for this to catch.
            #
            # What it did catch was the useful case. The per-leg charts exist so
            # that a fifteen-night stay four thousand cheaper than any legal one
            # can be found by eye; refusing to follow it here would have meant
            # the tool could show you the saving and not let you track it.
            # Ordering is still checked above, because a leg that departs before
            # the one before it has arrived is not a preference but an
            # impossibility.

            if not self.window_start <= dates[0] <= self.window_end:
                raise ValueError(
                    f"the watch on {watch.key} leaves outside the window "
                    f"{self.window_start}..{self.window_end}; widen the window or stop "
                    f"watching that day"
                )

    def _validate_leg_watches(self) -> None:
        """A watched leg must be a search that could return something.

        Deliberately light. The route need not be one this trip flies - picking
        freely is the point - so the only things refused are a search that
        cannot be run at all and a date far enough outside the window to be a
        typo rather than a choice.
        """
        seen: set[str] = set()
        for watch in self.leg_watches:
            for label, code in (("origin", watch.origin), ("destination", watch.destination)):
                if not IATA_RE.match(code):
                    raise ValueError(
                        f"watched leg {watch.key}: {code!r} is not a 3-letter IATA "
                        f"airport code ({label})"
                    )
            if watch.origin == watch.destination:
                raise ValueError(
                    f"watched leg {watch.key}: a flight from {watch.origin} to itself "
                    f"is not something any site will price"
                )
            if watch.key in seen:
                raise ValueError(
                    f"{watch.route} on {watch.depart_date} is already being watched; "
                    f"it cannot be watched twice"
                )
            seen.add(watch.key)

            # The window, plus slack for a final leg that legitimately departs
            # after it. Outside that it is a mistyped year, not a choice, and a
            # watch on it would report "nothing found" every four hours forever.
            latest = self.window_end + timedelta(days=WATCH_DATE_SLACK_DAYS)
            if not self.window_start <= watch.depart_date <= latest:
                raise ValueError(
                    f"watched leg {watch.route} departs {watch.depart_date}, outside "
                    f"the window {self.window_start}..{self.window_end}; widen the "
                    f"window or pick a date inside it"
                )

    # ---------------------------------------------------------- serialisation

    def to_dict(self) -> dict:
        data = asdict(self)
        data["window_start"] = self.window_start.isoformat()
        data["window_end"] = self.window_end.isoformat()
        data["focus_start"] = self.focus_start.isoformat() if self.focus_start else None
        data["focus_end"] = self.focus_end.isoformat() if self.focus_end else None
        data["return_focus_start"] = (
            self.return_focus_start.isoformat() if self.return_focus_start else None
        )
        data["return_focus_end"] = (
            self.return_focus_end.isoformat() if self.return_focus_end else None
        )
        # A list, like `stay_days`, so the JSON on disk reads the same way for
        # both kinds of range rather than one being a pair and one an array.
        data["total_days"] = list(self.total_days) if self.total_days is not None else None
        data["watches"] = [
            {
                "depart_dates": [d.isoformat() for d in w.depart_dates],
                "added_at": w.added_at,
                "added_price": w.added_price,
                "currency": w.currency,
            }
            for w in self.watches
        ]
        data["leg_watches"] = [
            {
                "origin": w.origin,
                "destination": w.destination,
                "depart_date": w.depart_date.isoformat(),
                "added_at": w.added_at,
                "added_price": w.added_price,
                "currency": w.currency,
            }
            for w in self.leg_watches
        ]
        data["stops"] = [
            {
                "label": s.label,
                "airports": list(s.airports),
                "stay_days": list(s.stay_days),
                "overland": s.overland,
                "arrive_via": s.arrive_via,
                "depart_via": s.depart_via,
            }
            for s in self.stops
        ]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Scenario:
        payload = _migrate(dict(data))
        payload["window_start"] = date.fromisoformat(payload["window_start"])
        payload["window_end"] = date.fromisoformat(payload["window_end"])
        for key in ("focus_start", "focus_end", "return_focus_start", "return_focus_end"):
            # Absent from every file written before the narrowing existed, which
            # is all of them; `.get` rather than `[]` for exactly that reason.
            value = payload.get(key)
            payload[key] = date.fromisoformat(value) if value else None
        total_days = payload.get("total_days")
        payload["total_days"] = tuple(total_days) if total_days else None
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
        payload["leg_watches"] = [
            LegWatch(
                origin=w["origin"],
                destination=w["destination"],
                depart_date=date.fromisoformat(w["depart_date"]),
                added_at=w.get("added_at", ""),
                added_price=w.get("added_price"),
                currency=w.get("currency", "CZK"),
            )
            # Absent from every file written before leg watches existed.
            for w in payload.get("leg_watches") or []
        ]
        payload["stops"] = [
            Stop(
                airports=list(s["airports"]),
                stay_days=tuple(s["stay_days"]),
                label=s.get("label", ""),
                # Absent from every file written before overland stops existed,
                # and those are committed trips that must keep loading.
                overland=bool(s.get("overland", False)),
                # Likewise, and None rather than "" so "not pinned" is one value
                # and not two that behave the same by accident.
                arrive_via=s.get("arrive_via") or None,
                depart_via=s.get("depart_via") or None,
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
