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
from dataclasses import asdict, dataclass, field, replace
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

# Preferences that may be followed at once. Not a taste limit - a budget one. A
# preference prices every airport pair of every leg across its pinned dates and
# the slack either side of them, and pelikan.cz answers about 120 searches per
# runner before it stops answering at all.
#
# Four rather than six, because a preference is wider than the watch it replaced:
# the same trip that cost 3 searches pinned costs 15 at the default slack. Four
# is comfortably inside one runner even at the widest slack allowed, and the
# realistic number of trips anyone is deciding between is two or three.
#
# `web.app` refuses on the real planned count as well, which is the figure that
# actually binds - a preference's cost depends on its slack and on how many
# airport pairs each leg has, neither of which a row count can see. This is the
# cheap guard that needs no planner.
MAX_PREFERENCES = 4

# Days either side of each pinned leg date that a preference also prices, and
# the most that may be asked for. Seven would be a fortnight-wide window per leg
# on a trip whose whole point is that it is already decided; past that the
# honest tool is a narrowed sweep, not a follow.
DEFAULT_SLACK_DAYS = 2
MAX_SLACK_DAYS = 7


def _pool_key(role: dict) -> str:
    """The stable name of the pool a `pool_roles` entry describes.

    "origins", "stop:0", "return_to". Used as the key of `Scenario.probe_extra`,
    so that a list of airports to keep probing survives the trip growing a stop.
    """
    if role["role"] == "stop":
        return f"stop:{role['stop_index']}"
    return role["role"]


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
class Preference:
    """One trip you are deciding between, followed on the dates you picked.

    `depart_dates` holds one date per leg, in travel order: the day you leave
    home, the day you fly on, the day you fly back. Pinning all of them - rather
    than pinning the departure and deriving the rest through the stay ranges -
    is what makes a preference cheap enough to re-price every few hours. On the
    Japan/Philippines trip that is 3 searches pinned against 75 for a sweep, and
    the site answers about 120 before it stops answering.

    `slack_days` buys back the one thing pinning gives up. A trip pinned to the
    12th cannot notice that the 14th is two thousand cheaper, and that is the
    question a decision is actually waiting on. The slack prices the days either
    side as well, so it can be answered - while the series drawn for this
    preference stays the price of *the trip you chose*, which is what makes a
    fall in it mean something. See `watch.record_observations`.

    The first date is the key. It is the one a person means by "the 12th", it is
    stable while the trip is edited around it, and it is what the price series
    on the Follow step is drawn against.

    Rank is position in `Scenario.preferences` and nothing hangs off it: it is
    the order they are listed and drawn in, not a priority anything spends a
    search on.
    """

    depart_dates: list[date]
    # "13+14, mid-Jan". Blank is legitimate - `describe` derives one - because a
    # trip dragged off the charts has a name implied by its own dates, and making
    # someone type one before they may follow it is a toll on the useful path.
    label: str = ""
    # Days either side of each pinned leg date that the run also prices. 0 is
    # exactly the old watch: the pinned dates and nothing else.
    slack_days: int = DEFAULT_SLACK_DAYS
    added_at: str = ""
    # What it cost when it was picked, so the tab can say "up 900 since you
    # started following" on the very first observation rather than after two.
    added_price: float | None = None
    currency: str = "CZK"

    @property
    def key(self) -> str:
        return self.depart_dates[0].isoformat() if self.depart_dates else ""

    @property
    def nights(self) -> list[int]:
        """Nights between consecutive departures - the shape of the trip.

        What a person means when they say "13+14": it is the split, not the
        total, that distinguishes two preferences leaving the same week.
        """
        return [
            (later - earlier).days
            for earlier, later in zip(self.depart_dates, self.depart_dates[1:], strict=False)
        ]

    def describe(self) -> str:
        """The label, or a name built from the shape and the day it leaves.

        The day and not just the month, because two preferences of the same
        shape a week apart are exactly the pair worth telling apart - and a
        chart legend naming both of them "13+12, Jan 2027" is a comparison you
        cannot read. Departure dates are unique across a trip's preferences, so
        this is too.
        """
        if self.label:
            return self.label
        if not self.depart_dates:
            return "(no dates)"
        split = "+".join(str(n) for n in self.nights) or "one way"
        leaves = self.depart_dates[0]
        return f"{split} from {leaves.day} {leaves.strftime('%b')}"


@dataclass(frozen=True)
class LegWatch:
    """One route on one date, followed on its own.

    A `Preference` follows a whole chained trip: every airport pair of every leg
    across its pinned dates and their slack. This follows exactly what you point
    at, and costs one search. The two coexist because they answer different
    questions - a `Preference` asks "is this trip moving", this asks "is this
    ticket moving" - and a decision is usually assembled from the second: follow
    Vienna to Haneda on the 10th and the 12th, and Manila home on the 2nd and
    the 4th, because those were the days that looked promising.

    Deliberately **not** required to be a leg of a trip the sweep could build.
    Picking freely is the whole point; the API says when a route is not one this
    trip searches, and leaves the choice alone.

    There is no cap here. A leg watch is exactly one search, so the honest limit
    is the one `web.app.WATCH_SEARCH_CAP` applies to the whole planned run -
    preferences and leg watches together - rather than a count of rows that
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
    # Empty when you picked this route yourself; a preference's key when it came
    # along with one.
    #
    # Following a preference follows its legs too, and those rows are stored
    # rather than derived at read time. A preference pins dates and not airports,
    # so a derived row would have no stable key on a leg with several airport
    # pairs - and `watch.leg_report` keys an entire series on that string. A row
    # whose key moved would start a new series and abandon the old one, silently.
    #
    # What it costs is a deletion rule: dropping a preference drops its rows.
    # `web.app` owns that, in the one place a preference is removed.
    source: str = ""

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
    # The trips you are deciding between, re-priced on their own dates by the
    # four-hourly run rather than by the daily sweep of the whole window. Empty
    # means the trip is not followed at all, and the watch workflow skips it.
    #
    # Ordered: position is rank, and rank is presentation only.
    preferences: list[Preference] = field(default_factory=list)
    # Individual routes on individual dates, followed one search each. Kept
    # alongside `preferences` rather than replacing them: a preference answers
    # what a whole chained trip costs, a leg watch answers what one ticket costs,
    # and the second is how a decision is usually put together. Some of these
    # rows are a preference's own legs - see `LegWatch.source`.
    leg_watches: list[LegWatch] = field(default_factory=list)
    # Airports the probe keeps asking about, beyond the ones the trip searches.
    #
    # The probe exists to say which airports are worth flying from, and acting
    # on its answer means taking the losers out of the trip. Doing that used to
    # delete the evidence: the verdict table filtered to the trip's own pools,
    # so a dropped airport lost its row, and the next probe never asked about it
    # again. The one number you narrowed *by* - "Katowice is 82% dearer" - was
    # gone the moment you narrowed.
    #
    # So the probe's list is its own, and it is typed rather than inferred.
    # Nothing lands here because you edited the route; removing an airport from
    # a stop removes it from the trip and does nothing else. That is deliberate:
    # a list that grew on its own would quietly walk a 51-search probe up toward
    # the cost of the sweep it exists to avoid.
    #
    # Keyed by role - "origins", "stop:0", "return_to" - and not by position,
    # because pools are positional and lining pool 2 of a two-stop trip up with
    # pool 2 of a three-stop one is how a probe of Prague and Vienna came to be
    # presented as the verdict for a trip flying out of Katowice. A key naming
    # no current pool is kept on disk and not probed; the panel says so, because
    # a stop reordered for an afternoon should not cost a year of probe list.
    probe_extra: dict[str, list[str]] = field(default_factory=dict)
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
    # Whether the 13:00 and 20:00 slots also sweep what this trip is narrowed to.
    #
    # Default True, which is what every trip committed before this field existed
    # was already doing - `plan_sweep --final` selected on having a narrowing at
    # all. Off is for a trip you have narrowed in order to *read* the window
    # through it, without also asking for it to be re-priced twice a day: the
    # boxes filter the charts either way, and the searches are the part worth
    # choosing about.
    sweep_narrowing: bool = True
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

    def reporting_tiers(self, data_dir: Path | str | None = None) -> list[list[str]]:
        """Which airports this trip would rather be reported from, best first.

        Its own `preferred_origins` when it has any, and otherwise the global
        ranking from `data/home_airports.json`. Both are tiers, so this is a
        fallback and not a conversion.

        Inherited at read time rather than copied into the file on save. The
        point of the fallback is that a trip which never said otherwise follows
        the global list *as the global list changes* - writing today's answer
        into the trip would freeze it, and would also make every trip claim a
        preference nobody expressed for it.

        Imported inside the method because `home_airports` imports `IATA_RE`
        from this module.
        """
        if self.preferred_origins:
            return self.preferred_origins
        from .home_airports import DATA_DIR, load_tiers

        return load_tiers(DATA_DIR if data_dir is None else data_dir)

    @property
    def pool_keys(self) -> list[str]:
        """A stable name per pool, positionally aligned with `airport_pools`.

        `probe_extra` is keyed by these rather than by index. Position is the
        one thing about a pool that is guaranteed to change - adding a stop
        renumbers every pool after it - and this whole module already carries
        the scar of reading one trip's pool 2 as another's.

        Derived from `pool_roles` so the two cannot drift, and deliberately the
        same for the last pool of a trip with no separate return airports as for
        its first: that pool *is* the origins list, and probing "the way home"
        of such a trip means probing the airports you leave from.
        """
        return [_pool_key(role) for role in self.pool_roles]

    @property
    def probe_pools(self) -> list[list[str]]:
        """`airport_pools`, widened by whatever `probe_extra` names for each.

        What a probe actually asks about. Order is the trip's own airports
        first, then the extras in the order they were typed, and a code already
        in the pool is not repeated - the planner walks these to emit searches,
        and a duplicate is a real search against a site that answers about 120
        of them per runner.
        """
        pools = []
        for key, airports in zip(self.pool_keys, self.airport_pools, strict=True):
            extra = [code for code in self._probed(key) if code not in airports]
            pools.append([*airports, *extra])
        return pools

    def _probed(self, key: str) -> list[str]:
        """One pool's extras, ignoring anything that is not a list of codes.

        Loading a trip does not validate it: `load_scenario` parses and returns,
        so a hand-edited `"probe_extra": {"origins": "KTW"}` would otherwise be
        iterated as three characters and planned as three airports. The read
        path repairs and the write path refuses, which is the rule
        `home_airports` already follows for the same reason.
        """
        codes = self.probe_extra.get(key)
        return codes if isinstance(codes, list) else []

    @property
    def probe_extra_unused(self) -> dict[str, list[str]]:
        """Entries of `probe_extra` naming a pool this trip no longer has.

        Kept rather than dropped, so a stop removed for an afternoon does not
        cost the list. Reported so that a list which is being kept and not used
        cannot also be invisible.
        """
        live = set(self.pool_keys)
        return {
            key: codes
            for key, codes in self.probe_extra.items()
            if key not in live and isinstance(codes, list) and codes
        }

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

        for key, codes in self.probe_extra.items():
            if not isinstance(codes, list):
                raise ValueError(f"probe_extra[{key!r}] must be a list of airport codes")
            seen_here: set[str] = set()
            for code in codes:
                if not IATA_RE.match(code):
                    raise ValueError(
                        f"probe_extra[{key!r}]: {code!r} is not a 3-letter IATA code"
                    )
                if code in seen_here:
                    raise ValueError(f"probe_extra[{key!r}]: {code} is in the list twice")
                seen_here.add(code)
            # A key naming no current pool is legal and is not probed - see the
            # field's comment. Overlap with the pool's own airports is legal
            # too, and deduplicated by `probe_pools`: refusing it would make
            # putting an airport back into the trip fail to save while it was
            # still listed here.

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
        self._validate_preferences()
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

    def _validate_preferences(self) -> None:
        """Every followed preference must be a trip this scenario could produce.

        Checked against the shape rather than merely parsed, because a preference
        that cannot chain is worse than a rejected one: the run spends its
        searches, finds legs, and reports no price at all for that day - which
        looks exactly like the site having nothing.
        """
        if len(self.preferences) > MAX_PREFERENCES:
            raise ValueError(
                f"{len(self.preferences)} preferences, but only {MAX_PREFERENCES} may be "
                f"followed at once - each one is a set of searches every few hours. "
                f"Drop one before adding another."
            )

        seen: set[str] = set()
        for preference in self.preferences:
            dates = preference.depart_dates
            if len(dates) != self.leg_count:
                raise ValueError(
                    f"the preference leaving {preference.key or '(no date)'} has {len(dates)} "
                    f"date(s) but this trip has {self.leg_count} legs; a preference needs "
                    f"one date per leg"
                )
            if preference.key in seen:
                raise ValueError(
                    f"a preference already leaves on {preference.key}; two cannot share a "
                    f"departure date"
                )
            seen.add(preference.key)

            # Bounded rather than merely non-negative. Slack is priced on every
            # airport pair of every leg, so it multiplies: on a trip with three
            # departure airports, going from 2 to 7 takes one preference from 35
            # searches to 105 - the whole of what the site answers - and the
            # refusal a person would then meet is about the run, not about the
            # number they typed.
            if not 0 <= preference.slack_days <= MAX_SLACK_DAYS:
                raise ValueError(
                    f"the preference leaving {preference.key} asks for "
                    f"{preference.slack_days} days of slack; 0 to {MAX_SLACK_DAYS} is what "
                    f"a follow may spend. Narrow the trip and sweep it instead."
                )

            for earlier, later in zip(dates, dates[1:], strict=False):
                if later <= earlier:
                    raise ValueError(
                        f"the preference leaving {preference.key} has its legs out of order: "
                        f"{later} does not come after {earlier}"
                    )

            # The stay ranges are deliberately *not* checked here.
            #
            # They were, and the reason was sound while it lasted: a preference
            # the combiner could not chain would spend its searches and report no
            # price, which looks exactly like the site having nothing. But a
            # preference pins every leg's date, so its stays are facts rather
            # than a search space, and `watch._admitting` now widens the ranges
            # to admit whatever was pinned before pricing it. There is no longer
            # an unchainable preference for this to catch.
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
                    f"the preference leaving {preference.key} is outside the window "
                    f"{self.window_start}..{self.window_end}; widen the window or drop "
                    f"that preference"
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
        data["preferences"] = [
            {
                "depart_dates": [d.isoformat() for d in p.depart_dates],
                "label": p.label,
                "slack_days": p.slack_days,
                "added_at": p.added_at,
                "added_price": p.added_price,
                "currency": p.currency,
            }
            for p in self.preferences
        ]
        data["leg_watches"] = [
            {
                "origin": w.origin,
                "destination": w.destination,
                "depart_date": w.depart_date.isoformat(),
                "added_at": w.added_at,
                "added_price": w.added_price,
                "currency": w.currency,
                "source": w.source,
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
        payload["preferences"] = [
            Preference(
                depart_dates=[date.fromisoformat(d) for d in p["depart_dates"]],
                label=p.get("label", ""),
                # `.get` with the default rather than `p["slack_days"]`: every
                # row migrated from `watches` predates the field, and the
                # default is what makes those rows preferences rather than
                # pinned watches with a new name.
                slack_days=int(p.get("slack_days", DEFAULT_SLACK_DAYS)),
                added_at=p.get("added_at", ""),
                added_price=p.get("added_price"),
                currency=p.get("currency", "CZK"),
            )
            # Absent from every file written before the Follow step existed.
            for p in payload.get("preferences") or []
        ]
        payload["leg_watches"] = [
            LegWatch(
                origin=w["origin"],
                destination=w["destination"],
                depart_date=date.fromisoformat(w["depart_date"]),
                added_at=w.get("added_at", ""),
                added_price=w.get("added_price"),
                currency=w.get("currency", "CZK"),
                # Absent from every row written before preferences followed
                # their own legs, and every one of those was picked by hand -
                # which is exactly what the empty default means.
                source=w.get("source", ""),
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
        # Normalised on the way in, not validated: a code typed in lower case
        # into the probe panel is the same airport, and the shape check that
        # rejects anything else is `validate`. Absent from every file written
        # before the probe kept its own list, which is all of them.
        payload["probe_extra"] = {
            str(key): (
                [str(code).strip().upper() for code in codes]
                if isinstance(codes, list)
                # Passed through rather than dropped, so `validate` can refuse it
                # by name. The same rule `preferred_origins` follows: a shape
                # nobody could have meant is worth being told about, and a value
                # silently discarded here is a probe list that saves and does
                # nothing.
                else codes
            )
            for key, codes in (payload.get("probe_extra") or {}).items()
        }

        unknown = set(payload) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown scenario fields: {', '.join(sorted(unknown))}")
        return cls(**payload)


def probing(scenario: Scenario) -> Scenario:
    """The trip as the probe searches it: every pool widened by `probe_extra`.

    One helper, called from exactly two places - `PLANS["explore"]`, which sizes
    and plans the probe, and `run_sweep`, which writes the snapshot the results
    are later read against. Both read this, so the plan and the record of the
    plan cannot disagree about which airports were asked about, which is the
    failure this module keeps having to design against.

    Returns the scenario unchanged when nothing is set, so a trip that has never
    used the probe list is not copied on every estimate.
    """
    if not scenario.probe_extra:
        return scenario
    widened = scenario.probe_pools
    keys = scenario.pool_keys
    by_key = dict(zip(keys, widened, strict=True))

    # `origins` covers the way home too on a trip with no separate return
    # airports: `pool_keys` names that last pool `origins`, because it is
    # literally the same list.
    stops = [
        replace(stop, airports=by_key.get(f"stop:{index}", stop.airports))
        for index, stop in enumerate(scenario.stops)
    ]
    return replace(
        scenario,
        origins=by_key.get("origins", scenario.origins),
        stops=stops,
        return_to=(
            by_key.get("return_to", scenario.return_to)
            if scenario.return_to is not None
            else None
        ),
    )


def _migrate(payload: dict) -> dict:
    """Translate an older scenario file into the current shape.

    Kept rather than one-shot converting the files and deleting this: a
    scenario hand-edited from an old example should load, not fail with a schema
    error naming fields the person never typed.
    """
    # A watch is a preference with no slack, and it is promoted to one *with*
    # the default slack rather than to a pinned copy of itself. That is safe
    # because the price plotted for a preference is the chain on its pinned
    # dates whatever the slack is - see `watch.record_observations` - so an
    # observation series recorded before the rename goes on being a series of
    # the same measurement. What the slack adds is the neighbouring days, which
    # nothing before could answer about at all.
    if "watches" in payload:
        payload["preferences"] = payload.pop("watches")

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
