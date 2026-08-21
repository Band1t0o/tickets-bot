"""Execute a planned sweep across several browsers and write results to disk.

Concurrency note: `sync_playwright()` is not shareable across threads. Each
worker thread therefore creates its own Playwright instance and reuses a single
page for every search it handles, which also avoids paying browser startup per
search.

Everything lands under `data/sweeps/<scenario_id>/<timestamp>/` as plain files,
keeping the project database-free so the cloud run can commit results straight
back to the repo.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from ..models import Leg
from ..scenario import Scenario
from .planner import LegSearch, plan_exploration, plan_searches, plan_watch, shard_of

# Politeness delay between searches on the same worker.
#
# Raised from 1.5s/4 workers after the first sweep that could actually report
# failures: 58 of 93 searches timed out. The same rate was present before and
# invisible - the previous "0 errors" sweep averaged 2.9 legs per search where a
# healthy search returns ~10, so roughly 70% of it was already failing silently.
#
# Whether the cause is concurrency or IP-level throttling is not established:
# after a day of probing, even sequential single searches from this machine went
# from ~14s to over 6 minutes, which is consistent with the site throttling the
# client rather than with load per se. Both readings point the same way, so
# these settings are deliberately gentler than measured need, and the daily
# scheduled run is what should validate them - not more hammering.
SEARCH_DELAY_S = 4.0
DEFAULT_WORKERS = 2

# "sweep" prices a trip; "explore" scouts which airports are worth pricing;
# "watch" re-prices a handful of pinned candidate trips on their exact dates.
MODES = ("sweep", "explore", "watch")

# Where each mode's runs are kept. A watch is not a sweep and must not be filed
# as one: six tiny runs a day in `data/sweeps/` would fill the Results picker
# with healthy-looking sweeps that priced three days out of seventy, and the
# richest-looking run offered would be the one that looked at least.
MODE_ROOTS = {"sweep": "sweeps", "explore": "sweeps", "watch": "watch"}

# Times a worker will come back to a search that has not been answered.
#
# One retry was not enough to call a sweep complete: a search that timed out
# twice, or raised anything other than a timeout, was simply lost - and a lost
# search is a hole in the date grid that nothing downstream can see, because a
# route that answered on other dates still looks perfectly healthy.
#
# Three, and each pass is smaller than the last, so the cost is bounded by what
# is still failing rather than by the size of the sweep. The budget stop and the
# circuit breaker end the passes early when the site is refusing outright.
MAX_FILL_PASSES = 3

# Searches one browser session may run before it is thrown away and replaced.
#
# Measured, and the measurement is unusually clean. Two cloud sweeps on 20 Aug
# were cut off after a *hard cliff* - a steady 10s per search for 120 searches,
# then nothing at all, with no slowdown leading up to it:
#
#   1 runner  x 2 workers = 2 sessions -> 120 searches answered
#   3 runners x 2 workers = 6 sessions -> 360 searches answered
#
# 60 per session, both times, whatever the runner count. That is not a rate
# limit and not a per-address quota - both of which the first two readings were
# wrongly taken for. It behaves like a session or cookie budget, and a fresh
# browser context is a fresh session.
#
# 40 leaves room in case the real number is a little under 60, and a context
# restart costs ~1-2s against ~10s per search, so recycling the whole of a deep
# sweep costs well under a minute.
PAGE_RECYCLE_EVERY = 40

# Consecutive timeouts that mean the site is refusing this client rather than
# having one slow moment. Five, because a working sweep's failures are scattered
# - the 10 Aug cloud run had none at all in 350 searches - while a throttled one
# fails everything from the moment it starts.
THROTTLE_STREAK = 5

# How long to wait out a refusal, escalating. Backing off is the cheap move: a
# 2-minute pause costs less than a single timed-out search, and the whole run is
# abandoned only if the site is still refusing after the longest one.
#
# Sized against the measurement that prompted it: the 11 Aug local sweep spent
# 4.5 hours to finish 245 of 615 searches with 125 timeouts, 93% of its worker
# time waiting on nothing. With this it reaches the same verdict in ~25 minutes.
#
# The lengths are also what the site has been seen to want. On 20 Aug a local
# run died at 120 answered, and one started **15 minutes later** answered a
# full 120 more - so a quarter of an hour of genuine quiet is enough to be
# forgiven. `_Breaker` did not deliver genuine quiet until 21 Aug, which is why
# these numbers looked useless without being wrong.
BACKOFF_S = (120.0, 300.0, 900.0)

ProgressFn = Callable[[int, int, str], None]

# What a sweep must clear before its best total may be plotted against another
# sweep's. Measured, not guessed: of the first four sweeps committed here, the
# `standard` one averaged 2.9 legs per search with error_count 0 and reported a
# best total 7% *worse* than a `quick` sweep at 9.7 that ran half as many
# searches. Comparing them as a price series charts scraper health.
#
# 6.0 sits between the two clusters (2.9/3.7 against 7.6/9.7) rather than on
# either, so neither a marginal pass nor a marginal fail turns on it.
MIN_COMPARABLE_LEGS_PER_SEARCH = 6.0


def legs_per_search_of(status: dict) -> float | None:
    """This sweep's legs per search, recorded or derived.

    Sweeps written before the field existed still carry `legs_found` and
    `total`, and the figure is exactly their quotient - so deriving it
    classifies committed history honestly instead of discarding all of it for
    lacking a field it could not have had.
    """
    recorded = status.get("legs_per_search")
    if recorded is not None:
        return float(recorded)
    total = status.get("total")
    found = status.get("legs_found")
    if not total or found is None:
        return None
    return round(found / total, 2)


def focus_of(status: dict) -> list | None:
    """The focus this sweep searched under, as `[start, end]`, or None.

    Normalised to a list because it is compared across a JSON round trip, where
    a tuple and a list are the same two strings.
    """
    focus = status.get("focus")
    return list(focus) if focus else None


def is_comparable(
    status: dict, routes_covered: int, routes_planned: int, focus=None
) -> bool:
    """Whether this sweep's best total may be plotted beside another's.

    Four independent ways for a sweep to be incomparable:

    - **Starved.** Enough searches failed or came back thin that the cheapest
      trip was simply never seen. `error_count` does not catch this; legs per
      search does.
    - **Narrower than the trip.** Either a route went dark, or the sweep
      predates a widening of the trip and searched a smaller one. Measuring
      coverage against what the trip plans *now* handles both, and correctly
      retires old sweeps when the trip is edited.
    - **Not a sweep at all.** An exploration pass covers every route and can
      post a perfectly healthy legs-per-search while pricing three dates out of
      seventy. Its cheapest total is a reconnaissance figure, and plotting it as
      a price would put a spike in the chart that no fare ever made. Stopped
      sweeps fall out through `state` for the same reason, and a watch - which
      prices even fewer days, and on purpose - falls out with them.
    - **Focused differently.** A focused sweep prices a handful of departure
      dates out of the window, so its cheapest is the cheapest *of those dates*
      rather than of the trip. Plotting it beside a broad sweep draws a step no
      fare ever made - the same mistake as charting an exploration pass, and
      caught the same way. A sweep is only comparable with others carrying the
      focus the trip has now.
    """
    if status.get("state") != "done":
        return False
    if status.get("mode") in {"explore", "watch"}:
        return False
    if focus_of(status) != (list(focus) if focus else None):
        return False
    if not routes_planned or routes_covered < routes_planned:
        return False
    legs_per_search = legs_per_search_of(status)
    return legs_per_search is not None and legs_per_search >= MIN_COMPARABLE_LEGS_PER_SEARCH


class LegProvider(Protocol):
    NAME: str

    def search_leg(self, page, origin: str, destination: str, depart, ret=None, adults: int = 1) -> list[Leg]:
        ...


@dataclass
class SweepResult:
    scenario_id: str
    directory: Path
    total: int
    completed: int = 0
    # Searches the site actually replied to, with offers or with its own "no
    # flights" message. The figure `legs_found` and `error_count` between them
    # could never give: a sweep that answered 460 of its 483 has 23 holes in the
    # date grid, and no per-route number shows them, because the routes involved
    # answered perfectly well on their other dates.
    answered: int = 0
    legs: list[Leg] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    # True when the run ended because it was asked to, rather than because it
    # ran out of searches.
    stopped: bool = False
    # True when it ended because the site kept refusing. Not the same as broken.
    throttled: bool = False
    # Searches attempted and legs found, per "ORIGIN->DEST". A route that was
    # searched on many dates and yielded nothing on all of them is breakage,
    # not a quiet market - see routes_with_no_results.
    route_searches: dict[str, int] = field(default_factory=dict)
    route_legs: dict[str, int] = field(default_factory=dict)
    # Failures per route. `errors` keeps only the last 20 messages, and a
    # starved sweep produces hundreds - so without a count, a route that timed
    # out on every attempt cannot be told from one the site answered with an
    # empty page. Those two mean opposite things: broken, and no inventory.
    route_errors: dict[str, int] = field(default_factory=dict)

    @property
    def routes_with_no_results(self) -> list[str]:
        """Routes that were searched but never returned a single offer.

        This is the signal that was missing when MNL->VIE, CEB->PRG and
        CEB->FRA all came back empty while the sweep reported error_count: 0.
        """
        return sorted(
            route
            for route, attempts in self.route_searches.items()
            if attempts and not self.route_legs.get(route)
        )

    @property
    def legs_per_search(self) -> float:
        """The honest health metric: flights found per search run.

        ~10 is healthy; 2.9 was 70% silent failure. Recorded rather than
        derived on demand because it is what makes two sweeps comparable, and
        the one figure that separates a broken sweep from a quiet market -
        `error_count` cannot, having read 0 on the sweep that was failing most.
        """
        if not self.total:
            return 0.0
        return round(len(self.legs) / self.total, 2)

    @property
    def coverage(self) -> float:
        """Share of the planned searches that came back with an answer.

        This is what "complete" means, and the only figure that can be checked
        afterwards. `state: done` says the run finished; it has never said the
        run asked everything it set out to ask.
        """
        if not self.total:
            return 0.0
        return round(self.answered / self.total, 4)

    @property
    def route_coverage(self) -> float:
        """Share of searched routes that returned at least one offer.

        A sweep missing the leg home still reports a healthy leg count while
        being unable to produce a single complete trip, so coverage is tracked
        separately from volume.
        """
        if not self.route_searches:
            return 0.0
        found = sum(1 for route in self.route_searches if self.route_legs.get(route))
        return round(found / len(self.route_searches), 3)

    @property
    def is_healthy(self) -> bool:
        """False when the sweep looks broken rather than merely unlucky.

        A sweep that finds nothing, or fails more than half its searches, is
        almost certainly scraper breakage. This distinction exists because the
        previous automation died silently and went unnoticed for 8 months:
        "no results" must never be indistinguishable from "no cheap flights".
        """
        if self.total == 0:
            return False
        if not self.legs:
            return False
        return len(self.errors) <= self.total / 2


def _search(provider, page, search: LegSearch, adults: int):
    return provider.search_leg(
        page,
        search.origin,
        search.destination,
        search.depart_date,
        search.ret_date,
        adults,
    )


class _Breaker:
    """Decides when the site is refusing, and how long to wait it out.

    Shared by every worker, because a throttle is applied to the client and not
    to a thread: two workers each failing three times in a row is the same wall
    as one worker failing six times.

    The distinction it exists to draw is between a bad patch and a refusal. A
    working sweep's timeouts are scattered - the clean cloud run of 11 Aug had
    none at all in 350 searches - so a *streak* is the signal, and a single
    success anywhere resets it. Only still failing after the longest pause is
    taken as evidence to abandon the run.

    The pause is a **deadline every worker honours**, not a sleep handed to
    whichever worker happened to record the fifth timeout. That is what it was,
    and it meant the ladder was never once run as designed: one worker slept
    while the other kept searching, so the client was never quiet, the site had
    nothing to forgive, and the two workers spent rungs between them faster
    than any of the pauses lasted. Measured 21 Aug - three local runs reached
    the end of the ladder without pelikan.cz ever having had two minutes off.
    """

    def __init__(self, backoff_s, on_backoff=None):
        self._backoff = list(backoff_s)
        self._on_backoff = on_backoff
        self._lock = threading.Lock()
        self._streak = 0
        self._level = 0
        self._paused_until = 0.0
        self.tripped = False

    def _left(self) -> float:
        """Seconds still owed on the open pause. Caller holds the lock."""
        return max(0.0, self._paused_until - time.monotonic())

    def record_success(self) -> None:
        """The site answered, so there is nothing left to wait out."""
        with self._lock:
            self._streak = 0
            self._level = 0
            self._paused_until = 0.0

    def record_timeout(self) -> None:
        """Count a refusal, and open a pause if this is a streak of them."""
        with self._lock:
            if self._left():
                # A search that was already in flight when the pause opened.
                # It is evidence about the moment before the quiet rather than
                # about the quiet, and counting it is exactly how two workers
                # used to spend two rungs of the ladder on one refusal.
                return
            self._streak += 1
            if self._streak < THROTTLE_STREAK:
                return
            self._streak = 0
            if self._level >= len(self._backoff):
                self.tripped = True
                return
            seconds = self._backoff[self._level]
            self._level += 1
            self._paused_until = time.monotonic() + seconds
        # Outside the lock: this writes status.json, which takes another.
        if self._on_backoff:
            self._on_backoff(seconds)

    def hold(self, stop: threading.Event | None = None) -> None:
        """Wait out whatever is left of the pause, whoever opened it.

        Interruptible, because the longest pause is fifteen minutes. Pressing
        Stop during one did nothing for a quarter of an hour while the page
        said "finishing the search in flight" - with no search in flight and
        none due.
        """
        while True:
            with self._lock:
                left = self._left()
            if left <= 0:
                return
            if stop is None:
                time.sleep(left)
            elif stop.wait(left):
                return


def _chunk(searches: list[LegSearch], workers: int) -> list[list[LegSearch]]:
    """Split into contiguous runs, one per worker.

    Two bugs lived in the one-liner this replaces, `searches[i::workers]`:

    - `workers=0` produced an empty list, so a sweep reported itself complete
      having run nothing at all.
    - Striding interleaves. The planner emits searches grouped by route, so
      every worker started on the *same* origin-destination pair at the same
      moment - the worst possible pattern against a per-route throttle, and a
      plausible contributor to the timeouts that made 58 of 93 searches fail.
      Contiguous runs spread the workers across different routes instead.
    """
    workers = max(1, workers)
    if not searches:
        return []
    size, extra = divmod(len(searches), workers)
    chunks: list[list[LegSearch]] = []
    start = 0
    for index in range(workers):
        end = start + size + (1 if index < extra else 0)
        if start < end:
            chunks.append(searches[start:end])
        start = end
    return _stagger(chunks)


def _stagger(chunks: list[list[LegSearch]]) -> list[list[LegSearch]]:
    """Start each worker on a different route, without changing its share.

    The plan is dealt across routes - one date from each in turn, so that a run
    cut short still holds a grid that chains - which means contiguous chunks of
    it advance through the routes in step with one another. Whenever the chunk
    size divides by the route count, two workers then sit on the *same* route at
    the same instant for the entire run. Measured on the real trip before this:
    33 of 33 steps in lockstep, which is precisely the pattern the contiguous
    split above exists to avoid.

    Rotating a chunk changes where a worker starts, never what it searches, so
    coverage and the shard arithmetic are untouched. A worker with no unused
    route left - more workers than routes - keeps its chunk as it is: the
    collision is unavoidable at that point and pretending otherwise would only
    move it.
    """
    taken: set[tuple[str, str]] = set()
    staggered = []
    for chunk in chunks:
        offset = next(
            (
                index
                for index, search in enumerate(chunk)
                if (search.origin, search.destination) not in taken
            ),
            0,
        )
        first = chunk[offset]
        taken.add((first.origin, first.destination))
        staggered.append(chunk[offset:] + chunk[:offset])
    return staggered


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _new_sweep_directory(root: Path) -> Path:
    """A directory no other sweep is using, named for the current second.

    Two runs of one trip starting within the same second used to land in the
    same directory. That was survivable while legs were written once at the end
    - the loser's file was simply replaced - but now that they are appended as
    they are found, the second run truncates a file the first is still writing.

    The next free second is taken rather than a suffix, because the stamp is
    also a URL segment: `src/web/app.py` validates it against a strict
    `YYYY-MM-DDTHH-MM-SSZ` pattern, so a sweep named anything else could be run
    but never opened.
    """
    started = datetime.now(UTC)
    for offset in range(60):
        stamp = (started + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H-%M-%SZ")
        directory = root / stamp
        try:
            directory.mkdir(parents=True, exist_ok=False)
            return directory
        except FileExistsError:
            continue
    raise RuntimeError(f"no free sweep directory under {root} within a minute of {started}")


def _write_status(directory: Path, payload: dict) -> None:
    (directory / "status.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_scenario(directory: Path, scenario: Scenario) -> None:
    """Record the trip this sweep is about to search.

    Legs alone do not say what was asked for, so a trip edited afterwards used
    to be read back over old results - listing airports that were never
    searched and silently discarding the ones that were. Written before the
    first search, because a run that is stopped or killed is exactly the one
    whose contents most need explaining.
    """
    (directory / "scenario.json").write_text(
        json.dumps(scenario.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _in(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def run_sweep(
    scenario: Scenario,
    provider: LegProvider | None = None,
    data_dir: Path | str = "data",
    workers: int = DEFAULT_WORKERS,
    on_progress: ProgressFn | None = None,
    depth: str | None = None,
    delay_s: float = SEARCH_DELAY_S,
    mode: str = "sweep",
    stop: threading.Event | None = None,
    backoff_s=BACKOFF_S,
    on_backoff: Callable[[float], None] | None = None,
    shard: tuple[int, int] | None = None,
    recycle_after: int = PAGE_RECYCLE_EVERY,
    resume_from: Path | str | None = None,
) -> SweepResult:
    """Run every search `scenario` implies and persist the legs found.

    `mode="explore"` runs the reconnaissance plan instead: every route on a
    handful of dates, to find out which airports are worth pricing at all.
    `mode="watch"` runs the pinned candidates of `scenario.watches` and writes
    to `data/watch/` rather than `data/sweeps/`.
    Deliberately a separate argument from `depth` rather than a fourth depth -
    depth is saved on the scenario and read by the nightly cloud workflow, so an
    "explore" depth could be persisted and quietly turn the daily sweep into a
    probe for good.

    `stop`, once set, ends the run after the searches already in flight. The
    sweep keeps everything it found and records itself as stopped.

    `backoff_s` is how long to wait out a run of timeouts before giving up on
    the site entirely; tests pass zeroes so they need not sleep.

    `recycle_after` searches, the browser is replaced. 0 keeps one for the whole
    run, which is what every sweep did until the site started refusing a session
    after about sixty searches.

    `shard=(index, count)` runs only this machine's share of the plan, so a deep
    sweep can be split across several cloud runners and merged afterwards. Each
    runner is a separate VM with its own address, so three of them at the same
    two workers and four-second delay put exactly the per-address load that has
    measured zero timeouts - while finishing in a third of the wall clock.

    `resume_from` is a directory whose run ended short - refused, stopped, or
    out of budget. Its answers are inherited and only the rest of the plan is
    asked for. The site answers about 120 searches from one client, so a probe
    refused at 80 of 126 cannot afford to buy its remaining 46 by re-asking the
    80 it already has.

    Deliberately not a shard, though `merge_shards` would stitch the two
    directories. `_merged_status` sums `total` across shards, so a 126-search run
    resumed with 46 would report a plan of 172 while `planned` - taken with
    `max` - stayed 126. The inconsistency would be quiet, and a number that
    disagrees with itself about how much was searched is the one thing this
    file exists to prevent. The resumed run inherits the rows instead and is a
    complete sweep directory on its own.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if depth:
        from dataclasses import replace

        scenario = replace(scenario, depth=depth)
    scenario.validate()

    if provider is None:
        from ..providers.pelikan import PelikanProvider

        provider = PelikanProvider()

    plans = {"explore": plan_exploration, "watch": plan_watch, "sweep": plan_searches}
    searches = plans[mode](scenario)
    # Counted before any narrowing, so a shard and a resumed run both report what
    # they are a part of rather than the part they ran.
    planned = len(searches)
    if shard is not None:
        searches = shard_of(searches, *shard)
    inherited = Path(resume_from) if resume_from else None
    if inherited is not None:
        already = answered_searches(inherited)
        searches = [
            search for search in searches
            if (search.origin, search.destination, search.depart_date.isoformat())
            not in already
        ]
    directory = _new_sweep_directory(Path(data_dir) / MODE_ROOTS[mode] / scenario.id)
    _write_scenario(directory, scenario)
    if inherited is not None:
        # Copied before the logs open: they open for appending, onto these.
        for name in ("legs.jsonl", "searches.jsonl"):
            source = inherited / name
            if source.exists():
                shutil.copyfile(source, directory / name)

    result = SweepResult(
        scenario_id=scenario.id,
        directory=directory,
        total=len(searches),
        started_at=_now(),
    )
    if inherited is not None:
        # The inherited rows are already on disk; the counters have to agree with
        # them from the first status write, or a resumed run opens at 0 of the
        # plan and reads as though the earlier answers were thrown away.
        earlier = _read_status_file(inherited)
        result.completed = earlier.get("completed") or 0
        result.answered = earlier.get("answered") or 0
        result.total = result.completed + len(searches)
        result.route_searches = dict(earlier.get("route_searches") or {})
        result.route_legs = dict(earlier.get("route_legs") or {})
        result.route_errors = dict(earlier.get("route_errors") or {})
        result.legs = load_legs(directory)

    lock = threading.Lock()
    # Transient, unlike everything on `result`: it describes what the run is
    # doing right now, and is cleared the moment a search answers.
    backoff = {"seconds": 0.0, "until": ""}

    def note_backoff(seconds: float) -> None:
        """Write the wait down before sleeping through it.

        Status is otherwise written every fifth search, so a run that stops
        making searches stops updating its status - the one situation where the
        page most needs to hear something.
        """
        backoff["seconds"] = seconds
        backoff["until"] = _in(seconds) if seconds else ""
        with lock:
            _write_status(directory, status_payload("running"))
        if on_backoff:
            on_backoff(seconds)

    breaker = _Breaker(backoff_s, note_backoff)

    def status_payload(state: str, current: str = "") -> dict:
        return {
            "scenario_id": scenario.id,
            "state": state,
            "mode": mode,
            "total": result.total,
            "completed": result.completed,
            "current": current,
            "legs_found": len(result.legs),
            "errors": result.errors[-20:],
            "error_count": len(result.errors),
            # Written into every sweep so later ones can be compared against
            # earlier ones without re-reading and re-combining legs.jsonl.
            "legs_per_search": result.legs_per_search,
            "route_coverage": result.route_coverage,
            "routes_planned": len(result.route_searches),
            "routes_with_legs": sum(1 for r in result.route_searches if result.route_legs.get(r)),
            "routes_with_no_results": result.routes_with_no_results,
            # Attempts and offers per route. `viability.route_stats` has always
            # read these two out of status.json; until they were written here it
            # counted zero attempts for every route, so no route could ever be
            # judged dead however many times it came back empty. The exploration
            # report needs them for the same distinction: asked and answered
            # with nothing is a finding, never asked is not.
            "route_searches": dict(result.route_searches),
            "route_legs": dict(result.route_legs),
            "route_errors": dict(result.route_errors),
            # What "complete" means, and the figure to read before any price.
            # `planned` is the whole trip's plan even when this process ran one
            # shard of it, so a shard's status says what it is a share of.
            "answered": result.answered,
            "planned": planned,
            "coverage": result.coverage,
            # Stated as its own number because `error_count` cannot carry it. A
            # run the circuit breaker abandons never attempts what is left, so
            # nothing is recorded as an error and the count reads 0 - which is
            # precisely the reading that hid 70% failure once before. The 20 Aug
            # sharded run posted `error_count: 0` beside 48 searches it never
            # made.
            "unanswered": max(0, planned - result.answered),
            "shard": list(shard) if shard is not None else None,
            # The run this one carried on from, so a directory holding more
            # answers than it made searches says where the rest came from.
            "resumed_from": inherited.name if inherited is not None else None,
            # Why nothing is happening. The breaker waits out a refusal for up
            # to fifteen minutes, and until this was written down the page had
            # no way to tell that from a hung run: same green dot, same
            # countdown, computed from a constant that knows nothing about it.
            # Both cleared by the next search that answers, so the notice can
            # never outlive the wait it describes.
            "backoff_seconds": backoff["seconds"],
            "backoff_until": backoff["until"],
            # The focus this sweep searched under, so a narrowed run is never
            # charted as though it had priced the whole window.
            "focus": (
                [scenario.focus_start.isoformat(), scenario.focus_end.isoformat()]
                if scenario.focus_start and scenario.focus_end
                else None
            ),
            # The candidates this run was following, so a watch directory says
            # what it is a watch *of* without needing the trip beside it - the
            # trip is edited, and the run is not.
            "watches": [
                [d.isoformat() for d in watch.depart_dates] for watch in scenario.watches
            ],
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "depth": scenario.depth,
        }

    _write_status(directory, status_payload("running"))

    # Each chunk is handled start-to-finish by one worker, so one browser is
    # launched per worker rather than per search.
    chunks = _chunk(searches, workers)

    mode_ = "a" if inherited is not None else "w"
    log = _JsonlLog(directory / "legs.jsonl", mode_)
    asked = _JsonlLog(directory / "searches.jsonl", mode_)

    def record(search: LegSearch, legs: list[Leg], error: str | None) -> None:
        label = f"{search.origin}→{search.destination} {search.depart_date}"
        route = f"{search.origin}->{search.destination}"
        with lock:
            result.completed += 1
            result.route_searches[route] = result.route_searches.get(route, 0) + 1
            result.route_legs.setdefault(route, 0)
            result.route_errors.setdefault(route, 0)
            if error:
                result.errors.append(f"{label}: {error}")
                result.route_errors[route] += 1
            else:
                result.answered += 1
                # The wait is over, demonstrably. Leaving this set would keep a
                # "waiting for the site" banner on screen while the run works.
                backoff["seconds"] = 0.0
                backoff["until"] = ""
                result.legs.extend(legs)
                result.route_legs[route] += len(legs)
                log.add([leg.to_dict() for leg in legs])
            # One line per search, whatever the outcome. Legs alone cannot say
            # which dates were asked about, so a hole in the grid was
            # indistinguishable from a date the site had nothing on.
            asked.add([{
                "origin": search.origin,
                "destination": search.destination,
                "depart_date": search.depart_date.isoformat(),
                "leg_index": search.leg_index,
                "answered": error is None,
                "legs": 0 if error else len(legs),
            }])
            if result.completed % 5 == 0 or result.completed == result.total:
                _write_status(directory, status_payload("running", label))
            if on_progress:
                on_progress(result.completed, result.total, label)

    def worker(chunk: list[LegSearch]) -> None:
        if not chunk:
            return
        from ..providers.pelikan import SearchTimeout

        def run_pass(searches: list[LegSearch], last: bool) -> list[LegSearch]:
            """Work through `searches`; return the ones still unanswered.

            A failure is only *recorded* on the last pass. Until then the search
            is simply still outstanding, so `completed` counts each search once
            and `route_errors` ends up counting searches that were never
            answered at all - which is exactly what the exploration report needs
            to tell "asked and answered with nothing" from "never asked".

            Retrying on the spot, which is what this used to do, doubles the load
            at the one moment the site is least willing and doubles what a
            failure costs from ~124s to ~248s. A transient timeout still
            recovers; it just waits until the rest of the chunk is done.
            """
            outstanding: list[LegSearch] = []
            for index, search in enumerate(searches):
                # Wait out any pause the breaker has opened, whichever worker
                # opened it. The throttle is on the client, so one worker
                # sleeping while another searches is not a pause at all.
                breaker.hold(stop)
                # Checked between searches rather than during one: a search
                # already in flight has to finish or time out, which is why the
                # UI says "stopping" rather than pretending this is instant.
                if (stop is not None and stop.is_set()) or breaker.tripped:
                    # Everything not yet attempted stays unanswered, and says so
                    # by being absent from searches.jsonl. Coverage is what
                    # reports the shortfall.
                    return outstanding + list(searches[index:])
                try:
                    legs = _search(provider, session.page(), search, scenario.adults)
                except SearchTimeout as exc:
                    breaker.record_timeout()
                    outstanding.append(search)
                    if last:
                        record(search, [], str(exc))
                except Exception as exc:  # one bad search must not kill the sweep
                    # Retried like a timeout. These used to be recorded and
                    # dropped on the first attempt, so a single transient
                    # navigation error left a permanent hole in the grid.
                    outstanding.append(search)
                    if last:
                        record(search, [], str(exc))
                else:
                    breaker.record_success()
                    record(search, legs, None)
                if delay_s:
                    time.sleep(delay_s)
            return outstanding

        session = _Session(provider, recycle_after)
        try:
            pending = chunk
            for attempt in range(1, MAX_FILL_PASSES + 1):
                pending = run_pass(pending, last=attempt == MAX_FILL_PASSES)
                if not pending or (stop is not None and stop.is_set()) or breaker.tripped:
                    break
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(worker, chunks))
    finally:
        log.close()
        asked.close()

    result.throttled = breaker.tripped
    result.stopped = stop is not None and stop.is_set()
    result.finished_at = _now()
    if result.throttled:
        # Reported ahead of "stopped" even when both are true, because it is the
        # finding: the run ended because the site refused, and knowing that is
        # what stops you debugging a scraper that is working. Distinct from
        # "unhealthy" for the same reason - a throttled sweep needs running
        # later or from somewhere else, not a fix.
        state = "throttled"
    elif result.stopped:
        # Not "unhealthy": a sweep you stopped at search 40 of 600 is not
        # broken, and calling it broken would hide the real thing that state is
        # for. It is simply incomplete, and `is_comparable` already refuses
        # anything that is not "done".
        state = "stopped"
    else:
        state = "done" if result.is_healthy else "unhealthy"
    backoff["seconds"] = 0.0
    backoff["until"] = ""
    _write_status(directory, status_payload(state))
    return result


class _JsonlLog:
    """Appends rows to a `.jsonl` file as they are produced.

    Legs used to be a single write at the end of the run. A deep sweep takes
    around two hours, so anything that interrupted it - a stop, a crash, a
    restart to pick up new code - threw away every flight it had found. Callers
    hold the result lock, so no extra synchronisation is needed here.

    Two files are written this way now. `legs.jsonl` is what was found;
    `searches.jsonl` is what was asked, which is the only record that can later
    prove the sweep asked everything it planned to.
    """

    def __init__(self, path: Path, mode: str = "w"):
        # "a" when a resumed run has inherited the earlier run's rows. Opening
        # for writing would empty the file it was just handed - the flights are
        # copied in before this runs.
        self._handle = path.open(mode, encoding="utf-8")

    def add(self, rows: list[dict]) -> None:
        for row in rows:
            self._handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        # Flushed per search, not per row: the point is that a reader looking at
        # a running sweep sees complete lines, and that a kill -9 costs at most
        # the search in flight.
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def _stamp_to_iso(name: str) -> str | None:
    """"2026-08-10T11-57-06Z" -> "2026-08-10T11:57:06+00:00", or None."""
    try:
        return (
            datetime.strptime(name, "%Y-%m-%dT%H-%M-%SZ")
            .replace(tzinfo=UTC)
            .isoformat(timespec="seconds")
        )
    except ValueError:
        return None


def answered_searches(directory: Path) -> set[tuple[str, str, str]]:
    """`(origin, destination, depart_date)` this run got an answer for.

    Read from `searches.jsonl`, which records one row per search whatever the
    outcome. Only rows that answered count: a row with `answered: false` is a
    timeout, and a timeout is exactly what a later run should ask again. Skipping
    every recorded row would make a refusal permanent.
    """
    path = Path(directory) / "searches.jsonl"
    if not path.exists():
        return set()
    answered = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # A row half-written when the process died. It was not answered.
            continue
        if row.get("answered"):
            answered.add((row["origin"], row["destination"], row["depart_date"]))
    return answered


def load_legs(directory: Path) -> list[Leg]:
    directory = Path(directory)
    path = directory / "legs.jsonl"
    if not path.exists():
        return []
    legs = [
        Leg.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    # Sweeps committed before legs carried their own timestamp still deserve an
    # answer to "when was this true", and the directory name is the honest one:
    # accurate to the start of the sweep rather than to the search. Never
    # applied over a leg that knows its own time.
    fallback = _stamp_to_iso(directory.name)
    if fallback:
        for leg in legs:
            if leg.observed_at is None:
                leg.observed_at = fallback
    return legs


class _Session:
    """A browser page that throws itself away every `recycle_after` searches.

    One page was reused for a whole worker's chunk, which is what makes a sweep
    affordable - a browser launch per search would dominate the cost. But it
    also means one session carries the whole sweep, and pelikan.cz stops
    answering a session after about sixty searches (see PAGE_RECYCLE_EVERY).

    So the page is still reused, just not forever. Everything about the browser
    is replaced together - context and all - because a fresh context is what
    drops the cookies, and keeping the cookies is the thing being tested.
    """

    def __init__(self, provider, recycle_after: int = PAGE_RECYCLE_EVERY):
        self._provider = provider
        self._recycle_after = recycle_after
        self._browser = None
        self._page = None
        self._used = 0
        self.recycles = 0

    def page(self):
        """The page to run the next search on, replaced when it is due."""
        if self._page is None or (self._recycle_after and self._used >= self._recycle_after):
            if self._page is not None:
                self.recycles += 1
            self.close()
            self._browser = _browser_page(self._provider)
            self._page = self._browser.__enter__()
            self._used = 0
        self._used += 1
        return self._page

    def close(self) -> None:
        if self._browser is not None:
            self._browser.__exit__(None, None, None)
        self._browser = self._page = None


class _NullPage:
    """Stand-in page for providers that do not need a browser (tests, demo)."""


class _browser_page:
    """Context manager yielding a Playwright page, or a null page for fakes."""

    def __init__(self, provider: LegProvider):
        self.provider = provider
        self._pw = None
        self._browser = None
        self._context = None

    def __enter__(self):
        # Providers that never touch `page` (the fake in tests, DemoStatic) do
        # not justify launching Chromium.
        if getattr(self.provider, "NAME", "") in {"FAKE", "DEMO_STATIC"}:
            return _NullPage()
        from playwright.sync_api import sync_playwright

        # If launch() raises after start() succeeds, __exit__ never runs and the
        # driver subprocess is left behind - once per worker, every sweep.
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                locale="cs-CZ", viewport={"width": 1600, "height": 1000}
            )
            return self._context.new_page()
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc_info):
        # Each step is independent: a context that refuses to close must not
        # strand the browser, and a browser that refuses to close must not
        # strand the driver process.
        for close in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if close is None:
                continue
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - teardown, nothing left to do
                print(f"[sweep] browser teardown: {type(exc).__name__}: {exc}")
        self._context = self._browser = self._pw = None
        return False


# ------------------------------------------------------------- merging shards
#
# A deep sweep is split across several cloud runners so it finishes inside its
# budget without asking pelikan.cz for anything faster than the rate that has
# measured zero timeouts. What comes back is N part-sweeps, and everything
# downstream - the combiner, the charts, the report, `is_comparable` - reads one
# sweep directory. So the shards are stitched into one before anything sees them.


class ShardMismatch(RuntimeError):
    """The shards did not search the same trip, so their legs cannot be summed.

    Possible whenever a run is dispatched while `scenarios/` is being edited: two
    runners check out different commits and the merge silently produces a sweep
    of a trip that never existed. Refusing is the only honest answer - this is
    the same failure as reading a sweep back against a trip edited since, which
    twice presented a probe of Prague and Frankfurt as the answer for Katowice.
    """


def _sum_into(target: dict, source: dict) -> None:
    for key, value in (source or {}).items():
        target[key] = target.get(key, 0) + value


# Worst first. A merged sweep is only as good as its unhappiest shard: one
# runner that was throttled means the merged result has holes, and calling the
# whole thing "done" because two shards finished is how a starved sweep gets
# charted as a price.
_STATE_ORDER = ("unhealthy", "throttled", "stopped", "running", "unknown", "done")


def merge_shards(shard_dirs: list[Path], destination: Path) -> dict:
    """Stitch shard directories into one sweep directory. Returns its status.

    Shards are written by separate runners and carry a `scenario.json` snapshot
    each; they must agree, or the merge refuses.
    """
    shard_dirs = [Path(d) for d in shard_dirs]
    if not shard_dirs:
        raise ShardMismatch("no shards to merge")
    # A shard is a directory holding a scenario.json snapshot. Anything else
    # under the download is not one - the first sharded run uploaded the whole
    # of `data/sweeps/<id>/`, so the merge was handed 24 "shards", seven of them
    # committed sweeps from a fortnight earlier.
    missing = [d.name for d in shard_dirs if not (d / "scenario.json").exists()]
    if missing:
        raise ShardMismatch(
            "these are not shards of one run - no scenario.json in: " + ", ".join(missing)
        )

    snapshots = {}
    for directory in shard_dirs:
        snapshot = (directory / "scenario.json").read_text(encoding="utf-8")
        snapshots.setdefault(json.dumps(json.loads(snapshot), sort_keys=True), []).append(
            directory.name
        )
    if len(snapshots) > 1:
        raise ShardMismatch(
            "the shards searched different trips and cannot be merged: "
            + "; ".join(", ".join(names) for names in snapshots.values())
        )

    destination.mkdir(parents=True, exist_ok=True)
    (destination / "scenario.json").write_text(
        (shard_dirs[0] / "scenario.json").read_text(encoding="utf-8"), encoding="utf-8"
    )

    for name in ("legs.jsonl", "searches.jsonl"):
        lines: list[str] = []
        for directory in shard_dirs:
            path = directory / name
            if path.exists():
                lines += [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        (destination / name).write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8"
        )

    statuses = [_read_status_file(d) for d in shard_dirs]
    merged = _merged_status(statuses, destination)
    _write_status(destination, merged)
    return merged


def _read_status_file(directory: Path) -> dict:
    try:
        return json.loads((directory / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A shard whose job died before writing one is not a reason to lose the
        # others, but it must not read as a clean shard either.
        return {"state": "unknown"}


def _merged_status(statuses: list[dict], destination: Path) -> dict:
    route_searches: dict[str, int] = {}
    route_legs: dict[str, int] = {}
    route_errors: dict[str, int] = {}
    errors: list[str] = []
    for status in statuses:
        _sum_into(route_searches, status.get("route_searches"))
        _sum_into(route_legs, status.get("route_legs"))
        _sum_into(route_errors, status.get("route_errors"))
        errors += status.get("errors") or []

    first = statuses[0]
    # `planned` is the whole trip's plan, which every shard records identically;
    # `total` sums what the shards were each handed. They agree when no shard was
    # lost, and the difference is itself the finding when one was.
    planned = max((s.get("planned") or 0) for s in statuses) or sum(
        s.get("total") or 0 for s in statuses
    )
    total = sum(s.get("total") or 0 for s in statuses)
    completed = sum(s.get("completed") or 0 for s in statuses)
    answered = sum(s.get("answered") or 0 for s in statuses)
    legs = sum(
        1
        for line in (destination / "legs.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )

    return {
        "scenario_id": first.get("scenario_id"),
        "state": min(
            (s.get("state", "unknown") for s in statuses),
            key=lambda name: _STATE_ORDER.index(name) if name in _STATE_ORDER else 0,
        ),
        "mode": first.get("mode", "sweep"),
        "total": total,
        "completed": completed,
        "current": "",
        "legs_found": legs,
        "errors": errors[-20:],
        "error_count": sum(s.get("error_count") or 0 for s in statuses),
        "legs_per_search": round(legs / total, 2) if total else 0.0,
        "route_coverage": (
            round(sum(1 for r in route_searches if route_legs.get(r)) / len(route_searches), 3)
            if route_searches
            else 0.0
        ),
        "routes_planned": len(route_searches),
        "routes_with_legs": sum(1 for r in route_searches if route_legs.get(r)),
        "routes_with_no_results": sorted(
            route for route, tries in route_searches.items() if tries and not route_legs.get(route)
        ),
        "route_searches": route_searches,
        "route_legs": route_legs,
        "route_errors": route_errors,
        "answered": answered,
        "planned": planned,
        "coverage": round(answered / planned, 4) if planned else 0.0,
        "unanswered": max(0, planned - answered),
        # Merged, so the shard field is spent. What it becomes is the roll call:
        # which shards were merged, so a run that lost one says so rather than
        # reporting a smaller sweep that looks complete.
        "shard": None,
        "shards": [s.get("shard") for s in statuses],
        "focus": first.get("focus"),
        "started_at": min((s.get("started_at") or "" for s in statuses), default=""),
        "finished_at": max((s.get("finished_at") or "" for s in statuses), default=""),
        "depth": first.get("depth"),
    }
