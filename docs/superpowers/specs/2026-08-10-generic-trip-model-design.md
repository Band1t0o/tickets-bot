# Generic trip model — design

*2026-08-10*

## Why

Two things hold the watcher back.

**It fails silently.** A search whose results never render returns `[]`, and the runner books that as
a success (`pelikan.py` result poll → `runner.py` `record(search, legs, None)`). The fix exists on the
unmerged branch `fix/silent-timeouts-and-baggage`, whose commit message measures the damage: 58 of 93
searches timing out, and the *previous* "error_count: 0" sweep averaged 2.9 legs per search where a
healthy search returns ~10. Roughly 70% of it was already failing invisibly. This is the same failure
mode the README records as having killed the workflow in December 2025 unnoticed for eight months.

**It only knows one trip.** `Scenario` carries `japan_airports`, `ph_airports`, `japan_stay_days` and
`ph_stay_days` as literal field names, restated in a Pydantic mirror, an HTML form and a JS mapper.
The planner emits three hardcoded leg blocks; the combiner is a triple-nested loop. Another trip means
editing six files.

**Outcome:** any trip — `origins → stop₁ → … → home` — is a JSON file and a form; the scraper reports
failure as failure; CI runs the tests.

## Trip model

A trip is an ordered chain of stops. Nothing names a country.

```python
@dataclass(frozen=True)
class Stop:
    airports: list[str]            # candidate arrival airports
    stay_days: tuple[int, int]     # how long before flying on
    label: str = ""                # display only

@dataclass
class Scenario:
    id: str
    name: str
    origins: list[str]
    stops: list[Stop]                    # 1..N
    window_start: date
    window_end: date
    return_to: list[str] | None = None   # None => back to origins
    one_way: bool = False
    adults: int = 1
    depth: str = "standard"
    alert_threshold: int | None = None
    currency: str = "CZK"
    enabled: bool = True
    notes: str = ""
```

`trip_type` disappears. A round trip is one stop whose `stay_days` is the old `trip_length_days`;
EU→JP→PH is two stops; a fourth country costs nothing.

**This deletes a bug rather than fixing it.** Round-trip scenarios currently cannot produce a single
itinerary: the planner searches only the outbound direction while the combiner requires a return leg.
Only `"enabled": false` hides it, and no test connects planner to combiner. In the generic model the
final leg is emitted by the same loop as every other leg, so the shape cannot recur.

Old-format scenario files are translated by a shim in `from_dict`. Existing `legs.jsonl` needs no
migration — it was always trip-agnostic.

## Planning and combining

**Planner.** One loop over `pools = [origins] + [stop.airports …] + [return_to or origins]`. Leg *i*
departs no earlier than `window_start + Σ(stay minimums of preceding stops)`; the final leg extends
`RETURN_SLACK_DAYS` past `window_end` because the site substitutes nearby dates. An
`origin == destination` guard is new — a real hazard once pools may overlap.

The additive-not-multiplicative design is preserved: each leg is searched once and reused across every
itinerary built from it. That is what makes a sweep affordable at all.

**Combiner.** A recursive chainer over the same pools, with two changes that N stops force:

- *Bounded search.* Today every itinerary is built and fully sorted to slice 50. At 3+ stops that
  explodes. Carry a running total through the DFS and prune any branch already ≥ the worst entry of a
  full top-K heap. Prices are non-negative, so the bound is admissible — no cheap itinerary is lost.
- *One pass, not three.* `limit=None` exists only because the price chart needs every itinerary.
  Return a `CombineResult` carrying `top`, `best_by_date`, `best_same_airport` and `best_open_jaw`
  from a single traversal. This also removes the N+1 where the Prices tab re-reads and re-combines
  every sweep ever committed.

## Airports

`scripts/build_airports.py` filters the OurAirports CSV (public domain, no key) to entries with an
IATA code and scheduled service, and writes `data/airports.json`. The output is committed, so runtime
never touches the network. `GET /api/airports/search?q=` matches IATA, city or name.

Viability stops being a hand-written list and becomes derived: `src/viability.py` reads sweep history
for per-route attempts, legs found and minimum price, using `route_searches` / `route_legs` from the
merged branch. The measured findings in the old catalogue were valuable *because they were measured* —
they are seeded from the committed sweeps so the conclusions survive the deletion.

## Correctness work

Grouped by what breaks:

- **Crashes.** `Leg.content_hash()` and `to_dict()` raise on `depart_date is None`, which the pelikan
  parser produces on three paths; `_dedupe` reaches the crash before the existing `None` fallback, so
  that fallback is dead code and any markup tweak turns every search into an exception.
- **Currency.** `"CZK" if "Kč" in text else "EUR"` makes EUR the fallback for anything unrecognised.
  Unknown must fail loudly. `Itinerary.total_price` sums across legs regardless of currency; guard it
  and drop mixed-currency chains in the combiner.
- **Alerts.** `save_best` ratchets backwards — with a threshold set, an alert fires and overwrites
  `best.json` even when the total got worse, destroying the "only on genuine improvement" guarantee.
  The health alert hardcodes `legs_found=0`, so it claims "returned no flights" even when hundreds
  were found but none chained, sending you to debug the scraper when the combiner is at fault.
- **Web layer.** Sweep threads swallow exceptions so the UI polls forever; `PUT /api/scenarios/{id}`
  ignores the path id and writes wherever the body points; ids and timestamps are interpolated into
  paths unvalidated; two render paths `await` without `try/catch`; scraped `airline` and `url` reach
  `innerHTML` unescaped.
- **Runner.** `_browser_page` leaks the Playwright driver if `launch()` raises and never closes the
  context; `workers=0` silently runs nothing; `searches[i::workers]` interleaves so every worker hits
  the same route simultaneously — the worst pattern against a per-route throttle, and a plausible
  contributor to the timeouts.

The legacy Vietnam-era pipeline is deleted: `config.py`, `cli scrape`/`watch`, `storage.py` (whose
bare-except recovery path *overwrites* run history), `scheduler.py`, `Offer`, `skyscanner_api.py`,
`test_discord.sh`, and the stale Docker files.

## Second source

**Spiked, and the answer was no.** letuska.cz has no deep-link grammar: `/letenky/PRG/NRT/<date>`,
`?from=&to=&date=` and a hash route all return 404, and the search is an Angular form whose results
render in place. Pelikan's deep link took a search from ~150 s to ~14 s and is what makes sweeping
affordable at all, so letuska stays out of `run_sweep` and becomes `src.cli check-price` — a second
opinion on one fare, run by hand.

It still had to move off `Offer` for the legacy model to be deletable, and it carried the same
"failure looks like no flights" bug being fixed elsewhere: four separate handlers all ended in
`return offers` with an empty list. It now raises `LetuskaSearchFailed`, parses card dates instead of
storing raw Czech text in a field documented as `YYYY-MM-DD`, and no longer skips filling the origin
when it happens to be Prague.

robots.txt was checked while there: `/searchform`, `/assets/` and `/api/` are disallowed and none of
them is touched — the search runs on the public homepage.

## Verification

`pytest` green in CI. A 1-stop scenario must yield itineraries through a real planner→combiner round
trip, not hand-built legs. Dry-run search counts for 1-, 2- and 3-stop scenarios. A live `quick` sweep
compared against the Phase 0 baseline — **legs per search is the honest metric**; ~10 is healthy, 2.9
was 70% silent failure. And a route with no inventory must report as *empty* while a timeout reports
as *error*: they must never look alike.

## Out of scope

- **Round-trip through-fares as extra candidates.** Measured as real but not universal — PRG↔NRT 33%
  cheaper as a round trip, FRA↔NRT + NRT↔MNL 19% cheaper as one-ways. Worth doing separately; it needs
  a `Leg` whose price covers two directions, which would tangle the schema work.
- **Non-CZK markets.** Every provider is a Czech OTA. Origins can be anywhere; pricing stays CZ-market
  until a non-CZ provider exists.
- Restyling.
