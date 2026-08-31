# Flight scenario watcher

Finds the cheapest way to fly a **multi-leg trip** — anywhere to anywhere, via as many stops as you
like — and watches the price until you book.

- **Scenarios** are committed JSON files: which airports, which date window, how long to stay where
- **GitHub Actions does the searching** on a schedule and commits results back, so it works whether or not your machine is on
- **Discord** sends the cheapest trip and the cheapest from airports you'd rather use, with the difference between them — and only when one of them actually improves
- **A local web UI** (`make ui`, or the desktop shortcut) to build trips, launch sweeps and read results
- **Every price says when it was measured**, and sweeps too incomplete to compare are never plotted as a trend
- **The scraper's selectors are editable in the UI**, with a button that proves them against a real search
- No database — everything is files under `data/` and `scenarios/`

> Personal, non-commercial use. Keep the politeness delays in place.

---

## Quick start

```bash
make install     # creates a venv and installs deps
make pw-install  # installs the Chromium Playwright needs
make ui          # http://localhost:8000
```

Python **3.10+** is required — models use `str | None`, which pydantic evaluates at runtime and 3.9
cannot parse. CI pins 3.12.

```bash
make test
python -m src.cli sweep --scenario japan-philippines --dry-run   # cost, no browser
python -m src.cli sweep --scenario japan-philippines --depth quick
```

---

## How a trip is modelled

A trip is an **ordered chain of stops**. Nothing in the schema names a country:

```json
{
  "origins": ["PRG", "VIE", "FRA"],
  "stops": [
    { "label": "Japan",       "airports": ["NRT", "HND", "KIX"], "stay_days": [8, 13] },
    { "label": "Philippines", "airports": ["MNL", "CEB"],        "stay_days": [8, 13] }
  ],
  "window_start": "2027-01-05",
  "window_end": "2027-02-08"
}
```

One stop is a round trip. Two are the trip above. A fourth country costs nothing but search time.
`return_to` flies you home somewhere other than where you left; `one_way` drops the leg home
entirely.

Legs are searched **independently** and then chained, rather than searching whole itineraries. That
keeps cost additive rather than multiplicative — each leg is searched once and reused across every
itinerary built from it. A three-leg trip is ~200 searches this way and tens of thousands the other.

| Stage | What it does | Where |
|---|---|---|
| Planner | Scenario → list of one-way searches | [src/sweep/planner.py](src/sweep/planner.py) |
| Runner | Runs them across 2 browsers, writes `legs.jsonl` | [src/sweep/runner.py](src/sweep/runner.py) |
| Explorer | Scores each airport from a cheap probe | [src/sweep/explore.py](src/sweep/explore.py) |
| Combiner | Chains legs into valid itineraries | [src/combine.py](src/combine.py) |
| Selector | Picks which itineraries are worth reporting | [src/alerts.py](src/alerts.py) |
| Notifier | Posts to Discord when a pick improves | [src/notify_discord.py](src/notify_discord.py) |

Both halves walk `scenario.airport_pools`, so they cannot disagree about the shape of a trip. They
used to: the planner's round-trip branch emitted outbound searches only, while the combiner branch
required a leg departing the destination, so **a round-trip scenario could never produce a single
itinerary**. No test connected the two, which is why it went unnoticed. One now does.

Stay lengths are computed from the dates **on the returned flights**, never the requested ones —
pelikan.cz substitutes nearby dates, so asking for 22 January can return the 23rd.

### When a price was true

Every leg carries `observed_at`, the moment its card was read off the page, and every itinerary
reports the **oldest** of its legs' stamps plus how far apart they were. A deep sweep runs ~97
minutes and the probe caught FRA→NRT moving 21% inside a single two-hour window, so a leg from
minute 3 and one from minute 95 are not the same measurement. Legs written before the field existed
fall back to the sweep's own timestamp on load.

It is deliberately outside `content_hash()`. Unlike `checked_bag`, which merely *could* split one
flight into two hashes, a per-search timestamp is unique by construction — hashing it would give
every leg a distinct digest and turn the parser's `_dedupe` into a no-op.

### Comparing sweeps

Two sweeps' best totals may only be plotted against each other when both are complete enough to
mean anything. A sweep is **comparable** when it finished, averaged at least 6 legs per search, and
returned at least one offer on every route the trip plans *today*.

The threshold is measured, not guessed. Of the first four sweeps committed here:

| Sweep | Depth | Searches | Legs/search | Routes | Best total | Comparable |
|---|---|---:|---:|---:|---:|:--:|
| 06 Aug 18:08 | quick | 20 | 7.6 | 5/21 | 30,188 | no |
| 06 Aug 20:22 | **standard** | 204 | **2.9** | 15/21 | 23,017 | no |
| 07 Aug 13:17 | quick | 93 | 3.7 | 15/21 | 31,302 | no |
| 10 Aug 11:57 | quick | 93 | **9.7** | **21/21** | **21,324** | **yes** |

The *quick* sweep that worked beat the *standard* sweep by 7%, at 2.9 legs per search and
`error_count: 0`. Drawn as one line, that chart plotted scraper health rather than prices. 6.0 sits
between the two clusters (2.9/3.7 against 7.6/9.7) so nothing marginal turns on it.

Incomparable sweeps are still **drawn** — hollow, dash-joined and never labelled as "the best". The
gaps in the record are worth seeing; a chart that silently dropped them would be its own kind of
lie. Measuring coverage against what the trip plans *now* also retires old sweeps automatically when
you widen a trip, which is why the 5-route smoke test is excluded.

**Depth is about the date axis, not the price level.** It sets the resolution of *cheapest total by
departure date*, and that curve is steep — the cheapest day sampled is 29% below the dearest. At
`quick` the grid is 7 days, so the best day is only known to ±3 days. The chart says so under
itself rather than drawing a smooth line through gaps it never searched.

### Ranking

Itineraries are ranked on the **bag-inclusive** total. The cheapest headline fare is usually a
low-cost carrier whose checked bag costs extra, and comparing that against a bag-inclusive fare is
not like-for-like. The headline fare stays visible next to it. Where the site will not say until you
click through, the bag is assumed *not* included — assuming otherwise flatters exactly the fares
whose real price is a bag fee higher.

### Depth

Counts are for the `japan-philippines` scenario; they scale with airports × dates.

| Depth | Date step | Searches | Time |
|---|---|---:|---:|
| `quick` | every 7 days | 84 | ~21 min |
| `standard` | every 3 days | 168 | ~41 min |
| `deep` | every day | 483 | ~119 min |

Deep was 615 until every leg started reserving the minimum stays that still have to happen after
it. The first leg was being searched twelve days past the last departure that can reach a searched
final leg, so 132 of those searches could not produce an itinerary however good the fares were --
proven on the committed data, where the quick sweep of 10 August searched 2 February and yielded
nothing from it.

### The probe

Depth decides how finely a trip is priced. It does nothing about the other multiplier — the number
of airports — and that is usually where a sweep's cost goes. Three origins, three Japanese airports
and two Philippine ones is 21 routes, and a deep sweep prices every one of them on every date
whether or not it could ever win.

**Probe** (`--mode explore`) searches every route on three spread-out dates: **63 searches,
~16 min**, against 483 and ~119. It will not find you a trip. It tells you, per airport, what the
cheapest flight in and out of it costs against the alternatives standing in the same place — so the
real sweep can leave the hopeless ones out. On the trip above it says Frankfurt is the benchmark and
Prague is 57% dearer to leave from, and that Cebu costs 67% more than Manila once you count getting
home again.

Verdicts distinguish **measured** from **unmeasured**, which is the only part that matters:

| Verdict | Means |
|---|---|
| `best` / `close` / `worse` / `poor` | Priced, and this far above the cheapest airport in the same pool |
| `no_offers` | Asked at least three times, answered every time, nothing sold |
| `unproven` | The site never answered — **not** a verdict on the airport |

The probe never edits a trip. Removing an airport is a button under **Probe results** that drops the
chip in the route editor and gathers it in a pending bar for you to confirm. Three sampled dates is
enough to show you an airport is hopeless and nowhere near enough for a tool to narrow your trip on
its own.

#### Keeping an airport probed after you stop searching it

Acting on the probe used to destroy the probe. The verdict table filtered to the trip's own
airports, so the row you narrowed *by* — "Cebu is 65% dearer" — vanished the moment you took Cebu
out, and the next probe never asked about it again either. The one measurement that justified the
decision could not be checked afterwards.

So the probe has a list of its own, `probe_extra` on the trip, keyed by role — `origins`,
`stop:0`, `return_to` — rather than by position, because adding a stop renumbers every pool after
it. **Remove from trip** and **Keep probing it** are offered side by side on the row, and the second
does both things in one pending edit: the airport leaves the trip and joins the probe list, so the
sweep stops pricing it and the probe goes on doing so.

Nothing lands there implicitly. A list that grew whenever you edited a route would walk a 50-search
probe up toward the cost of the sweep it exists to avoid, so keeping an airport is always a button
someone presses, and both figures on the Run row move before anything is saved. Keeping one that was
already in the trip makes the **sweep** cheaper and leaves the probe where it was — it was pricing
that airport either way; adding one the trip has never held is what makes the probe dearer.

A probe-only airport shows greyed, reading *not in this trip · still probed*, with **Stop probing**
and **Put it back** beside it. Each group also has an **Also probe** row for adding an airport the
trip has never had — a nearby one you want measured before you would consider it. An entry naming a
stop the trip no longer has is kept on disk and reported as kept-but-unused, because a stop removed
for an afternoon should not cost a year of list.

### Then focus on the dates that won

The probe narrows a trip by airport. A **focus** narrows it by date, and it is the other half of the
same idea: once a broad sweep has drawn the price-by-date curve, most of the window is not worth
pricing again tonight.

It is the **Leave between** pair in *When you actually want to go*, on **Narrow it down**. Type the
two dates and press **Save**. That writes `focus_start` and `focus_end` onto the trip, and the planner bounds the *first*
leg to them -- the later legs follow through the stay ranges, so the three can never contradict each
other and a focused sweep can still complete a whole trip. Five departure days on the trip above is
**195 searches, ~48 min**, against 483 and ~119.

The chart is in the panel rather than beside it because a click and a keystroke were setting one
field through two controls. It used to be a panel of its own, with its own badge (*watching 2027-01-08
to 2027-01-12*), its own Save (*Watch these dates*) and its own words for the range the boxes two
panels above already called *out 01-08--01-12*. Neither control knew what the other had done. The
chart fills the boxes now, and the narrowing panel's one Save writes them.

For a trip of several legs this curve is the weakest of the three constraints, and the panel says so:
the total is a sum of legs priced on their own days, and the flight home usually moves it more than
the flight out. *Every flight, priced on its own* is where a trip is actually picked.

A narrowed sweep is never charted beside a broad one -- its cheapest is the cheapest *of those days*,
and joining the two would draw a step no fare ever made, which is the same mistake `is_comparable`
already refuses for an exploration pass. They are two lines on one chart, never one line.

**The broad sweep stays broad.** It did not until 24 August, and the bug was structural: `plan_searches`
read the narrowing off the trip file, so saving one silently narrowed *every* sweep -- the 02:00 run
included -- and the broad picture simply stopped being refreshed. Nothing on the page said so, because
a status recorded only the focus, and this trip was narrowed by a return window. Measured that morning
on `japan-philippines`: two nightly runs of **48 searches against a window of 85**, both filed as broad
sweeps.

The narrowing is now the setting of a **separate sweep**, with its own mode, its own tab and its own
line on the trend chart. `plan_searches` prices the window and nothing on the trip may shrink it;
`plan_final` prices the narrowing and refuses a trip that has none. Same arithmetic, one flag apart --
`_leg_window(scenario, leg_index, narrowed)` in [src/sweep/planner.py](src/sweep/planner.py) is still
the one place the four constraints meet.

### Narrow sweeps

The reason narrowing is worth doing more than once. Once the decision has settled, what you want is
not one more price but the *same* price on Monday, Tuesday and Wednesday, so a drop is visible as a
drop. On the real trip that is **31 searches against 85** -- cheap enough to run three times a day
against a site that answers about 120 per address.

| | prices | when |
|---|---|---|
| broad sweep (`--mode sweep`) | the whole window | 02:00 UTC, and on demand from *Map it out* |
| narrow sweep (`--mode final`) | only what you narrowed to | 13:00 and 20:00 UTC when the trip asks for it, and on demand from *Narrow it down* |

**Asking for it is a tick, not a side effect of narrowing.** `sweep_narrowing` on the trip decides
whether the two afternoon slots run at all; `plan_sweep --final` selects on it as well as on there
being a narrowing to run. Narrowing a trip in order to *read* the window through it is an ordinary
thing to do, and it used to be indistinguishable from asking for two more runs a day. The boxes
filter the charts either way; the searches are the part worth choosing about. It defaults to true,
which is what every trip committed before the field existed was already getting.

The mode is still called `final` on disk, in `status.json` and in `--mode`. Renaming it would make
every run ever committed look like a run of something else, which is a worse lie than an old name.

Both land in `data/sweeps/<id>/<stamp>/` and are told apart by `status.json`, which now records all
three constraints rather than the focus alone:

```json
"narrowing": {"focus": ["2027-01-08", "2027-01-12"],
              "return_focus": ["2027-02-01", "2027-02-12"],
              "total_days": [24, 28]}
```

A broad run writes three `null`s **even on a narrowed trip**, because that is what it searched: the
field says what happened, not what the trip said at the time. Reading it the other way round is the
whole of the bug above. Runs committed before the split carry `focus` and no `narrowing`, and
`narrowing_of` reads them by it -- honest about what they knew, rather than retired or silently
promoted onto the broad line.

Each population keeps its own alert high-water mark in `best.json` — `cheapest` for a broad sweep,
`final:cheapest` for a narrowed one. One shared entry was harmless only while every sweep was
narrowed: after the split a final run recorded 25,967 over the broad runs' 21,445, and the next broad
sweep would have been announced as a 4,500 CZK drop that no fare ever made. With `notify_quiet` off,
every sweep posts whatever it found, so three slots is three Discord messages a day; turn it on and
each population reports only a genuine improvement against its own mark.

The 13:00 and 20:00 slots skip a trip narrowed to nothing rather than sweeping it twice, and unlike the
old focused slot they carry **no health gate**. That gate existed because the afternoon used to re-run
the whole window at a site the morning had just shown to be refusing. Thirty-one searches is not that
run, and the days it prices are the ones a booking decision is waiting on.

### And when you already know roughly when

A focus says when you leave. By the time a broad sweep has been read a few times the decision has
usually narrowed by things no sweep knows -- the day work starts again, someone else's leave, a flat
that is let from the 9th -- and what is left is not one date range but three constraints at once:

| Field | Bounds | Example |
|---|---|---|
| `focus_start` / `focus_end` | when the **first** leg departs | leave 8-12 January |
| `return_focus_start` / `return_focus_end` | when the **final** leg departs | home 4-8 February |
| `total_days` | nights from the first leg's departure to the final leg's | 22-26 |

The planner intersects all three with the window rather than choosing between them, in the one place
that already decides when a leg may depart (`_leg_window`), and `combine.py` applies all three when
reading a sweep back. Two of the three is not a smaller version of this — with only the return window
and the nights band applied, *the cheapest trip that fits your narrowing* meant a trip leaving two
days outside the window typed into the box above it. On the trip above, a deep sweep of the
whole window is **198 searches, ~49 min**; with those three set it is **44, ~11**.

**None of it is guessed at the edges.** 8 January is inside the departure window and still never
searched: at the longest stays it puts you home on the 3rd, a day before the return window opens, so
every fare found on it would belong to an itinerary that cannot be built. That is the same
orphan-search bug `_leg_window` was written for, arriving from the other end.

A nights band on its own does **not** shrink the plan, and that is not an oversight. It is a
relative constraint -- with neither end pinned the first leg may still depart anywhere in the window,
so every date of every later leg is reachable from some first-leg date and none can be dropped. It
constrains which chains are valid, not which searches are worth running, and `combine.py` is where
it bites. Pin either end and it starts narrowing the plan as well.

**A narrowing nothing can satisfy is refused by name**, because the alternative is a sweep that
spends every search, chains nothing, and reports the same emptiness the site gives when it has no
seats:

```
30-35 nights away is unreachable: the stays allow 18-26
(10-13 at Japan + 8-13 at Philippines); change the nights or the stays
```

**It is applied inside the traversal, never to the finished list.** Measured on the 21 August sweep:
with the return window at 4-8 February the unnarrowed traversal kept a 3 February trip and pruned an
identically priced 6 February one, so filtering afterwards would have reported nothing available at
all. This is the same reason `from_airport` is applied inside `combine_all` rather than to its
result.

Reading is separable from searching, on the page as well as in the API. **Ignore my narrowing** in
the results filter row -- `?window=all` on `/results`, `/by-date` and `/candidates` -- shows every
trip the sweep can build, including the ones you said you did not want, which is what you gave up by
narrowing. The tick appears only when there is a narrowing to ignore. A broad sweep committed months
before any of this existed therefore stays fully readable. It is a lens over legs already on disk
and never runs a
search. The narrowing -- all three parts of it, focus included -- is read from the **live** trip
rather than the sweep's snapshot: the sweeps worth narrowing are precisely the ones that ran before
you narrowed. What a run actually searched under is in its `status.json`, which is what
`is_comparable` reads, so nothing that needed the snapshot's copy lost it.

### How complete was it?

`legs_found` and `error_count` cannot answer that. A route that answered on nine dates and never on
the tenth reports perfect health on every per-route figure, and the hole is invisible.

So every search writes a line to `searches.jsonl` whatever the outcome, and `status.json` carries
`answered`, `planned` and `coverage`. A search that fails is retried across three fill passes, each
smaller than the last, rather than dropped on the second try. Coverage below 1.0 is stated in the
Discord message beside the price, because a price you might book on has to come with how much of the
trip was priced to find it.

### A run searches the trip on screen

The route editor keeps its edits in the browser, and a run reads the trip from disk. Nothing joined
those two facts up, so two probes were spent searching the *previous day's* airports and the
verdict table reported them as the answer for a trip that no longer contained them — Prague, Vienna
and Frankfurt priced in detail, Katowice and Kraków not mentioned at all.

Two changes, because either alone still leaves a way to be misled:

- **Run saves first.** Pressing *Probe*, *Quick sweep*, *Deep sweep* or *Sweep narrowed* writes the trip
  before starting, so what is on screen is what gets searched. A trip that will not save does not
  run, and the reason appears on whichever tab you pressed the button from. An `Unsaved changes`
  marker sits beside *Save trip* in the meantime, and each button's cost prices the edited trip
  rather than the last saved one.
- **Every sweep records its trip.** `scenario.json` is written into the sweep directory before the
  first search, and every panel reads a run against *that* — so an old sweep keeps
  showing its flights after you change airports, and Probe results says
  `BER, KRK, KTW, MUC never searched in this run` instead of leaving them out. Runs searched under
  something the trip no longer says are marked in every picker before they are opened, naming which
  part differs: `· ⚠ different airports · different stays`, or `· ⚠ a different trip` when all of it
  does. Airports, stay ranges and the window are each checked, because all three drift and a run of
  one trip read as a run of another is how a headline price ends up thousands out.
  Bag estimate, preferred origins and the alert threshold are still read from the current trip:
  those are how a result is read, not what was searched.

### Stopping a run

A sweep in progress can be stopped from the status strip. It stops after the searches already in
flight — one can be sitting on the site's timeout — and keeps everything it found: legs are appended
to `legs.jsonl` as they arrive rather than written once at the end, so a stop, a crash or a restart
no longer costs the whole run. A stopped sweep records `state: "stopped"`, is labelled as such in
the sweep picker, and is never plotted on the price chart.

`--max-minutes` is the same mechanism on a timer: the sweep stops itself inside a budget and its
results reach disk, instead of a job timeout killing it with everything still in memory.

---

## Where sweeping works, and where it does not

**Sweep from GitHub Actions. Do not sweep from home.** Measured on 11 August, same code, same trip:

| | searches | wall clock | timeouts |
|---|---|---|---:|
| Cloud runner, 03:24 | 350 | 5,156 s | **0** |
| This machine, 14:31 | 240 | 4.5 h | **120** |

pelikan.cz throttles *this connection*. It is not the scraper, not concurrency, and not the width of
the trip — a local run fails about half its searches whatever you do, while the cloud sweeps the
same routes without a single timeout at 14.7 s/search.

Local runs are for spot checks with `make run`. Everything the app starts goes to Actions,
the probe included.

### What a throttled run does now

A timed-out search used to cost 248 s: the full timeout, then an immediate retry into the same
throttle. The local sweep of 11 August spent **93% of its worker time** on searches that returned
nothing, and would have carried on for hours. Three things changed:

- **The retry waits its turn.** A timed-out search is re-run at the end of the worker's chunk, not
  on the spot — same recovery for a transient timeout, without doubling the load at the one moment
  the site is least willing.
- **The wait is set from measurement.** Successful searches record how long they took; once there
  are ten to go on, a search is abandoned at three times the recent median, floored at 60 s and
  capped at the configured timeout. A flat cutoff would not do: the healthy cloud median is ~25–30 s,
  so 45 s would start failing good searches.
- **It gives up.** Five consecutive timeouts pauses every worker for 2, then 5, then 15 minutes.
  Still failing after the longest pause ends the run as `state: "throttled"` — deliberately not
  `unhealthy`, which means the scraper is broken and someone must fix a selector. A throttled sweep
  needs no fix; it needs running later, or from the cloud.

There is no IP, token or user-agent rotation here and there will not be. Changing identity to get
around a limit a site has applied is evasion whatever the mechanism, and it is moot: the cloud path
works without pretending to be anyone.

---

## Airports

Any airport with scheduled service — 4,161 of them, filtered from the
[OurAirports](https://ourairports.com/data/) public-domain dataset into `data/airports.json` by
[scripts/build_airports.py](scripts/build_airports.py). The output is committed, so runtime never
touches the network and there is no key or quota to acquire.

Search matches **code, city, alias or country**, in that order of strength, and three of those were
added because the obvious query returned nothing:

| Type this | What it used to do | What fixed it |
|---|---|---|
| `Japan` | nothing at all | the dataset stores `JP`; `countries.csv` supplies the name |
| `Tokyo` | Haneda only | Narita's municipality is *Narita*; `keywords` carries "Tokyo" |
| `Bali` | **Krakow** | Kraków's municipality really is *Balice*, and Denpasar says "Bali" only in `keywords` — so an exact alias has to outrank a city prefix |

Within a band, airports are ordered by **longest runway**. It is a proxy, but it is the only size
signal in the dataset: `type` alone tags 28 Japanese airports `large_airport`, and ordering those
alphabetically put Narita 22nd, behind Aomori and Saga. By runway, NRT/KIX/NGO/HND come first.

Airports you already use appear as one-click chips beside the picker, split by direction — derived
from your saved trips and `airport_notes.json`, not hardcoded, so it follows you somewhere new.

### Your airports

**How easy an airport is to get to is a third axis, and nothing knew it.** Brno is a tram ride,
Prague is a morning, Vienna is a coach — and frequency cannot discover that, because the usual
reason a convenient airport goes unused is that it has no long-haul inventory. (`BRQ` is exactly
that: convenient, and flagged *no inventory* by `viability.py` in the same chip.)

So `data/home_airports.json` is one ranked list, **in tiers**, edited under ⚙ Setup:

```json
{ "tiers": [["BRQ"], ["PRG", "VIE"], ["FRA"]] }
```

Tiers because two airports can be equally awkward — Prague and Vienna are both a morning — and
forcing a strict order on them invents a preference nobody holds. Flattened, it replaces the
frequency-derived chips under **Depart from** and **Back to**, in your order; a row of buttons has
no way to show two airports at one rank. Destinations are untouched — there is no convenient end to
a trip to Japan. Shaped like `sources.py`: the defaults live in code and the file only overrides, so
a missing or hand-mangled one costs you the ordering of some chips rather than the ability to build
a trip. `{"airports": [...]}` is still read, as one airport per tier, so nothing needs migrating.

**This is also what Discord reports by.** It used to be a second list: `preferred_origins` on each
trip, on a different panel, in a different shape — a list of tiers against a flat one — and each
panel spent a paragraph explaining that it was not the other one. Two controls that can only be
described by their difference from each other belong next to each other.

Tiering the global list made them the same kind of object, so a trip that says nothing inherits it
verbatim (`Scenario.reporting_tiers`). Inherited at read time, never written into the trip file: the
point is that a trip which never expressed a preference follows the list *as the list changes*, and
copying today's answer in would both freeze it and make every trip claim a preference nobody chose
for it. A trip that does want its own order still has one, behind a disclosure on the same panel.

**Whether an airport is worth using is derived from your own sweeps**, not hand-written:
[src/viability.py](src/viability.py) reads sweep history and flags routes that were searched
repeatedly and never returned a single offer. That is breakage or genuinely absent inventory, and
either way it belongs on the airport in the picker.

Findings that no sweep can reproduce — airports checked by hand and then never swept, because
nothing sweeps an airport it cannot use — live in `data/airport_notes.json`:

| Airport | Finding |
|---|---|
| **VIE** Vienna | 23,624 combined — cheapest measured |
| **FRA** Frankfurt | 27,154 combined — 45% under Prague on a live run |
| **PRG** Prague | 30,735 combined — home base |
| BER / MUC / KRK / KTW | 31,054 / 31,795 / 33,828 / 38,173 combined |
| BTS Bratislava | **no return inventory**, and 78% over Prague outbound |
| BRQ Brno | **no long-haul inventory in either direction** |

("Combined" is the cheapest one-way to NRT plus the cheapest one-way back from MNL, 1 adult, CZK,
measured on a sample January 2027 date.)

---

## The deep-link URL grammar

Searches navigate straight to a constructed URL instead of driving the search form. This took a
search from ~150 s to **~14 s**, and is what makes a multi-leg sweep possible at all.

```
/cs/letenky/T:{type},P:{adults}000E_0_0,CDF:{FROM}{FROM},CDT:A{TO},DD:{y}_{m}_{d}[,DR:{y}_{m}_{d}]/
```

- `T:1` round trip (needs `DR`), `T:2` one-way. `T:0` and `R:0` return nothing — never generate them.
- Dates are bare integers: `2027_2_3`, never `2027_02_03`.
- `CDF` repeats the origin code; `CDT` prefixes the destination with `A`. The asymmetry is the site's.
- Prices are **per person**. The site's label switches from "Celková cena pro všechny osoby" at
  1 passenger to "Průměrná cena na osobu" at 2 — same number either way for one traveller.
- Flight numbers are **not** in the DOM (they sit behind a collapsed "Detaily letů" panel), so legs
  are identified by carrier + times + duration + stops.
- The deep link **cannot express an open jaw**: `CDF`/`CDT` are last-pair-wins and always build a
  round trip, and `T:3` returns nothing. Open jaws come from chaining one-ways, not from the URL.

### Sources

Everything about a site that changes without its behaviour changing — base URL, deep-link template,
the six selectors, the "no flights" marker — lives in [src/sources.py](src/sources.py), overridable
from `data/sources.json` and editable in the **Sources** tab. **Test this source** runs one real
search and reports what those selectors parsed, which answers the only question that matters when
the sweep goes quiet: *is it the URL or the markup?*

Telling those apart needs more than an HTTP status. Measured live, pelikan answers **200** for a
path it does not recognise and quietly bounces to `/cs`, dropping the search — clean status, real
page, zero cards, indistinguishable from a renamed class unless you notice you were moved. So the
check compares the *final* address against the one asked for. A working search redirects too (it
appends `,LOAD`), so the test is the prefix, not equality.

### Where results get sent

The same tab holds the Discord webhook, with a **Send a test message** button — because "saved" and
"reaches the channel" are different claims, and the gap between them otherwise shows up as silence
from a 02:00 run nobody watched.

A webhook URL is a bearer token wearing a URL's clothes: whoever holds it can post to the channel.
So it is stored in **`.secrets/discord.json`**, which `.gitignore` excludes as a directory —
deliberately not under `data/`, which the scheduled workflow commits, and deliberately not in a
scenario file, which is committed on purpose. It is never sent back to the page either; the field
shows the id with the token replaced by dots, which is enough to tell one channel from another and
not enough to post with.

`DISCORD_WEBHOOK_URL` wins over the file whenever it is set. Actions sets it from the repo secret
and has no `.secrets/` at all, so saving one locally can never change where a cloud run posts. The
file exists so a local `python -m src.cli sweep` notifies without exporting anything first — the UI
disables the field and says so when the variable is overriding it.

**pelikan.cz is the only sweep source**, and a [spike](docs/superpowers/specs/2026-08-11-second-source-spike.md)
concluded it should stay that way: kiwi.com's `robots.txt` disallows `/search` for `User-Agent: *`,
and Skyscanner's API is partner-only.

**letuska.cz is the second opinion, at the scale where it is affordable.** It has no deep-link
grammar — `/letenky/PRG/NRT/<date>`, `?from=&to=&date=` and a hash route all 404 — so a search means
driving an Angular form through a cookie banner, autocomplete typing and a Czech-month calendar
behind two nested shadow roots: ~60–90s against pelikan's ~14s. Unaffordable for 615 searches,
perfectly affordable for the five or six the shortlist rests on.

```bash
python -m src.cli verify --scenario japan-philippines --top 3
python -m src.cli check-price --from PRG --to NRT --depart 2027-01-12 --return 2027-01-30
```

The comparison has to be like-for-like, and two ways of failing that were found by running it:

| Mistake | What it looked like |
|---|---|
| `ret=None` searched `ret or depart` — a same-day **round trip** | Every leg read ~2.2× dearer, and the report said the two sites *agreed* |
| Taking letuska's cheapest quote | It offers neighbouring days, so a 3 February leg was compared against their 2 February one |

Both are fixed: `ret=None` now drives the form's *Jednosměrná* toggle, and only quotes for the exact
date requested are compared. A leg the other site cannot price on that date is reported as
**unpriced**, never as agreement — silence from a second source is not confirmation from it. On the
10 August sweep this reads: FRA→NRT 16% cheaper on pelikan, NRT→MNL 44% cheaper, MNL→FRA 3% cheaper
on letuska.

Through-fares are real but not universal, which is why the leg chain stays: PRG↔NRT on 6/28 Jan is
33% cheaper as a round trip than as two one-ways, while FRA↔NRT + NRT↔MNL on 23 Jan/10 Feb is 19%
cheaper as one-ways. Re-pricing everything as through-fares would have made results worse.

Prices are CZK because every source is a Czech OTA. Origins can be anywhere; *pricing* stays
CZ-market until a non-CZ provider exists.

---

## Automation

The repo is **public**, so Actions minutes are free and depth is no longer rationed by budget.

| Job | Schedule | Runtime |
|---|---|---:|
| Broad sweep ([scrape.yml](.github/workflows/scrape.yml)) | daily 02:00 UTC, `deep` | ~119 min |
| Narrow sweep (same workflow) | 13:00 and 20:00 UTC, only for trips that asked | ~8 min |
| Preference check ([watch.yml](.github/workflows/watch.yml)) | every 4 h | ~4 min |
| Volatility probe ([probe.yml](.github/workflows/probe.yml)) | every 2 h | ~2 min |
| Tests ([test.yml](.github/workflows/test.yml)) | every push | ~1 min |

**What stops a sweep is a browser session.** Not an address, and not a rate -- both of which the
first two readings were taken for. Three cloud runs settle it:

| Run | Shape | Answered |
|---|---|---:|
| 11 Aug | 1 runner, 2 workers | 350, zero timeouts |
| 20 Aug | 3 runners, 2 workers | 360 -- **120 per runner** |
| 20 Aug | 1 runner, 2 workers | **120** |

One runner and three runners both answered 120 *per runner*, at a steady ten seconds a search with
no slowdown at all before a hard cliff. Each worker holds its own browser, so that is **60 searches
per session**, every time -- and three runners only looked faster because six sessions beat two.

`PAGE_RECYCLE_EVERY = 40` in [runner.py](src/sweep/runner.py) replaces the browser before a session
is spent. A context restart costs ~1-2 s against ~10 s per search, so recycling a whole deep sweep
costs under a minute.

**Recycling did not turn out to buy searches, though, and sharding is not optional.** Re-measured on
20 August: one runner answered 120 whether or not it recycled, and five runners answered 483 with
coverage 1.0. What is constant within a runner and differs between runners is the *address*. So the
shard count is now derived rather than chosen -- `planner.shards_for(planned)` splits a plan at
`SEARCHES_PER_RUNNER = 100`, and `scripts/plan_sweep.py` sizes **each trip separately** and emits the
matrix. It gives 5 for the 483-search shape that has finished whole every night since 20 August, and
1 for a 66-search one. The `DEFAULT_SHARDS` env var it replaced was a bare number applied to every
trip in the run, and it stayed 5 after pinning a crossing took the main trip to 66 searches -- five
runners splitting thirteen apiece.

Shards are dealt per route (`shard_of`) so a lost shard thins the date grid rather than deleting
routes, and **the plan itself is dealt the same way** (`_deal`). That is not cosmetic: emitted leg by
leg, a run cut short left the legs on date bands that could not reach each other, and a local run of
37 of 66 searches produced 357 real flights and *no complete trip at all*. `_chunk` then staggers the
workers so two of them do not advance through the routes in step.

**`timeout-minutes` is a ceiling above the budget, never the budget.** It used to be the budget, set
from an estimate that was wrong by half, and thirteen consecutive nightly runs -- 8 to 20 August --
swept cleanly for 90 minutes, were cancelled, and committed nothing at all. Neither the runs nor the
data ever said so; `gh run list` did.

**What still constrains the sweep is pelikan.cz, not minutes.** This client has already been
throttled into 58 of 93 timeouts in one sweep, and a starved sweep is worse than no sweep: it spends
time, commits thin results, and its price is not comparable with anything. So `DEFAULT_WORKERS = 2`
and `SEARCH_DELAY_S = 4.0` stay exactly where they are — free minutes are not a reason to hammer the
site harder — and a second daily run is gated:

```bash
python -m src.cli health-gate --scenario japan-philippines --min-legs-per-search 6
```

It reads the newest `status.json` and exits non-zero when the last sweep was starved, so the
afternoon run skips rather than following a failing sweep with another. It lives in the CLI rather
than in workflow YAML so it can be tested.

**A second slot at 13:00 UTC is written into [scrape.yml](.github/workflows/scrape.yml) but
commented out.** Enable it only once the 02:00 deep runs hold ≥8 legs/search at full route coverage
for a few days. The two times come from the probe, detrended against each route-day's median:
01:00–03:00 UTC runs ~2.2% below the day's median and 18:00–22:00 ~0.6% above, so a true evening
slot would systematically sample the day's high. 02:00 lands results before you wake; 13:00 (~15:00
local) is near-median and actionable while you are awake.

**Failure modes worth knowing about, all now guarded:**

1. The workflow died in December 2025 and went unnoticed for **8 months**, because a broken scraper
   and a quiet day look identical. A sweep that finds nothing now posts a red health alert.
2. A blanket `data/` rule in `.gitignore` silently discarded every result — `git add` matched only
   ignored paths and `|| true` swallowed the error.
3. **A timed-out search returned `[]` and was booked as a success.** The first sweep that could
   report failures reported 58 of 93 searches timing out — and that rate was not new: the previous
   "error_count: 0" sweep averaged 2.9 legs per search where a healthy one returns ~10. Roughly 70%
   had been failing invisibly. A timeout now raises, and is distinguished from the site's own "no
   flights" message.
4. The sweep pushed without rebasing while the probe pushed to the same branch on an overlapping
   schedule, so a rejected push threw away ~40 minutes of spent budget.

**Legs per search is the honest health metric.** ~10 is healthy; 2.9 was 70% silent failure. It is
now written into every `status.json` alongside route coverage, and the UI refuses to compare sweeps
that fall short of either — see *Comparing sweeps* below.

Secrets do not transfer between repositories. Only one is needed, and cloud runs read *this*, not
the local `.secrets/discord.json`:

```bash
gh secret set DISCORD_WEBHOOK_URL -R Band1t0o/tickets-bot
```

---

## Volatility probe

Three fixed routes, sampled every two hours, so the sweep cadence comes from data.

```bash
python -m src.cli probe          # one sample of all three routes
python -m src.cli probe-report   # how much prices actually moved
```

**Net move leads.** It is the figure that says whether a fare is running away from you, and it was
the one missing. Over the first four days:

| Route | Any step moved | Moved >1% | Net | High to low |
|---|---:|---:|---:|---:|
| FRA→NRT | 17% | 6% | **+25.1%** | 25.2% |
| PRG→NRT | 37% | 20% | +12.6% | 14.6% |
| NRT→MNL | **56%** | 11% | −0.1% | 2.3% |

Read the first two columns together. NRT→MNL moved at 56% of steps — the highest of the three — while
living inside a 2.3% band all week, because a 6 Kč twitch on a 3,850 Kč fare counted the same as a
2,400 Kč step. Counting changes measures the site's rounding; magnitude measures the market. The
panel used to lead on `median move` and `biggest drop`, and reported **24** and **20** for FRA→NRT
during the four days it climbed 2,724 Kč — `largest_drop` reads only negative steps.

Two conclusions the data supports, both of which contradict the intuition that prompted the probe:

- **Movement is day-over-day, not intraday.** FRA→NRT stepped 21% on 8 August and then sat perfectly
  flat for two entire days. A second daily sweep catches a step ~12 h sooner and little else.
- **Fares are rising, not falling.** Every route measured went up. Whatever the general shape of a
  booking curve, the only evidence in this repo says waiting has cost money so far.

---

## Preferences

A **preference** is one of the two or three trips you are actually deciding between: the leg dates
you picked off the charts, a name, and a couple of days' slack either side of each. They are ranked
in the order you would take them, they are what *Follow it* draws, and they replaced the pinned
watch -- a watch was a preference with no slack.

A sweep answers "what is this window worth?" once a day at hundreds of searches. Once it has shown
which days are cheap, the question changes to "are *those* moving?", and re-sweeping the window to
answer it is the load pelikan.cz throttles this client for. A preference prices the days it pinned
instead: on `japan-philippines` that is **15 searches at the default slack** against 483 for a deep
sweep, which is what lets it run every four hours.

**Two numbers come back, and keeping them apart is the whole discipline of it.**

| | is | drawn as |
|---|---|---|
| `total` | the chain on the dates you pinned | the line on the chart |
| `nearby_total` | the cheapest chain anywhere inside the slack | a sentence under the row, with a button |

The line must never move. `record_observations` filters the run's legs to the pinned dates *at their
own position in the chain* before combining -- positionally, via `leg_pools`, because a preference
leaving on the 12th and flying on the 25th admits both dates and a set-membership test would chain a
VIE-HND leg on the 25th into a trip nobody pinned. Without that filter `best_by_date` hands back the
cheapest chain leaving on the pinned morning, which once the slack has priced the later legs is a
*different trip*: it would draw as your trip falling by four thousand, with nothing on screen saying
the dates had changed underneath you.

The neighbour is the return on the slack, and measured on the 30 August sweep it is real: the
committed preference costs **24,187 CZK** on 12 Jan → 25 Jan → 6 Feb, and **22,770** for the same
trip flying home on the 8th instead. That is the answer a decision is waiting on, and no pinned
watch could ever have given it. *Move it to those dates* is a `PATCH`, not a new preference -- the
series belongs to the decision, and starting a fresh one on every shift would leave you unable to
see that the trip has fallen since you began looking.

**A substituted day is a hole, not a price.** pelikan.cz answers the 25th with the 26th when it has
nothing on the 25th; `record_leg_observations` allows that slack, because a leg watch asks about one
ticket. Here it would mean quietly pricing a trip you did not pick, so `total` is `None` for that
check and `nearby_total` reports the 26th by name, with an offer to move to it.

`nearby` deliberately includes the pinned days themselves, so a preference already sitting on the
cheapest day in its window reports no saving rather than being offered a move to something dearer.
At zero slack it is `null` and not the pinned total: "nothing nearby is cheaper" is a claim that run
did not pay to make.

**Discord hears about `total` only.** A cheaper neighbouring day would need a high-water mark of its
own or it would be re-reported every four hours, and it would mix two populations in one alert the
way `best.json` once mixed broad and narrowed sweeps. The page says it instead.

### Following a preference follows its flights

Saving one from the charts also follows each of its legs, one search each -- and they cost nothing,
because `plan_watch` dedupes them against the preference's own searches. They are tagged with the
preference in the flights table and are dropped by dropping it; a route you picked by hand carries
an empty `source` and survives.

They are **stored** rather than derived at read time. A preference pins dates and not airports --
deliberately, so a check can still find that Frankfurt undercut Vienna overnight -- so a derived row
would have no stable key on a leg with several airport pairs, and `leg_report` keys an entire series
on that string. A key that moved would start a new series and abandon the old one, silently.

### What the cap is really on

`MAX_PREFERENCES = 4` is the cheap guard that needs no planner. The one that binds is
`WATCH_SEARCH_CAP = 110` in `web/app.py`, applied to the whole planned run -- preferences and
followed flights together -- because what a preference costs depends on its slack and on how many
airport pairs each leg has, neither of which a row count can see. Measured: three preferences at ±2
days is **45 searches** on a trip whose crossing is pinned, and **315** on one with three departure
airports and two in Japan. The count is the same; the run is not.

---

## The UI

`make ui`, or the **Flight watcher** desktop shortcut ([start-ui.bat](start-ui.bat)).

Three steps and a gear, in the order the work is actually done:

| Step | Holds | Answers |
|---|---|---|
| **Map it out** | *Search options* (When, Route, Run), *Probe results*, *Search results* | what trip is this, which airports are worth pricing, and what a sweep of it found |
| **Narrow it down** | the trip read-only, the dates and stays you actually want, then the narrowed runs — per leg, then ranked | *which days, and which trip* |
| **Follow it** | your preferences, the flights you are following, best total over time, the volatility probe | *is it moving, should I book now* |
| ⚙ **Setup** | Discord, your airports, scheduled sweeping, the cloud, sources | none of the above |

**Map it out has a second nav row**, because it holds a form and the two kinds of answer a run gives
back. *Probe results* is which airports are worth keeping; *Search results* is what a sweep found —
every leg priced on its own, then the itineraries ranked. Both used to be somewhere else: the probe's
verdicts on a tab of their own, and the sweep's results on **Narrow it down**, behind a switch.

### The gear holds five panels, and each subject once

It held nine, and two subjects were configured in two places each: a Discord webhook was set on one
panel while what Discord is told was ticked on another, three panels apart, and the airports near
home were ranked on one panel while which of them Discord should report about was ranked on another.
Each of those four carried a paragraph whose only job was to say it was not the other one. **That
paragraph is the tell** — two controls that can only be described by their difference from each
other belong next to each other, where the difference is visible instead of asserted.

| Panel | Was | Holds |
|---|---|---|
| **Discord** | two panels | the webhook, then what gets sent |
| **Your airports** | two panels, two shapes | one tiered list, with a per-trip override behind a click |
| **Scheduled sweeping** | one panel of controls | what fires tonight, read-only, plus *Edit the schedule* |
| **The cloud** | four panels | the schedule and any warning; the queue and the run list behind disclosures |
| **Where the prices come from** | one panel | unchanged, and folded shut — you need it when a site breaks and never otherwise |

Four of the nine were the cloud, which is one subject in four boxes. What that panel is *read* for
is the schedule and the "your checkout is behind" warning; what it is *consulted* for is the queue
and the run list, and those are long. So the first two stay open and the last two are a click.

### The schedule is one editor with three ways in

**Scheduled sweeping** appears on the Run row, on the narrowing step, and behind the gear. All three
used to call `showTab('night')`, which opened the whole gear — nine panels, one of them about
scheduling. A button naming one subject should open that subject and nothing else.

It is a `<dialog>`, not an expander under each button, because the same four controls are wanted
from three places and three copies would be three sets of the same ids: `#enabled` existing twice is
a checkbox that reads whichever the DOM found first and saves the other. The gear's panel keeps the
part you go there to read — the list of what fires tonight — and an **Edit the schedule** button
that opens the same dialog. It prices both scheduled runs live as you tick them, so the trade
between a nightly deep sweep and a twice-daily narrow one is a number rather than a guess.

### One population per step

There was a `[the whole window | just what I chose]` switch on the narrowing step, and four panels
existed twice over because of it — once per side. It is gone. The broad runs answer under *Search
results*, beside the buttons that start them; **Narrow it down** draws the narrowed runs and nothing
else. Nothing about how the two populations are stored or planned changed: `status.mode` still tells
them apart, and `legView`/`resultsView` are still one set of renderers behind an id prefix. What
changed is that the results of a sweep over the whole window are no longer read on the tab named for
cutting the window down.

It was seven tabs, one per panel, which read as a list of screens rather than as an order. Two of
them were the same panel twice: **Prices** and **Watch** both drew a price history and neither said
which question it was answering. They are split by question now rather than by data — the by-date
chart says which days and sits with the narrowing, the history chart and the probe say whether to
book now and sit with what you are following, and neither step shows both.

**Final sweeps used to be a fourth step, and was the same mistake again.** It held a copy of Narrow
it down's two panels drawn over the narrowed runs instead of the broad ones, which is one step read
two ways. It became a switch on that step, and then stopped being a switch at all: see *One
population per step* above.

### Starting a run

The Run row is one button per kind of run, each carrying its own cost:

| Button | Mode | Where it runs |
|---|---|---|
| **Probe** | `explore` | the cloud |
| **Quick sweep** | `sweep --depth quick` | the cloud |
| **Deep sweep** | `sweep --depth deep` | the cloud |
| **Sweep narrowed** (on *Narrow it down*) | `final` | the cloud |
| **Scheduled sweeping** | — | opens the schedule editor |

Depth was a select and the destination two more buttons: three controls for one decision, and the
two with their own controls were the two that mattered least. Depth is now which button you press.

**Every run goes to the cloud**, and it is not about convenience. pelikan.cz answers about 120
searches from one address before it stops answering at all. A deep sweep of a three-leg trip is over
three hundred, so a sweep started on this machine half-fails every time, and a sweep that half-fails
is worth less than a shallow one that works — only the cloud shards the plan. The probe went up for
the other half of the same arithmetic: it is ~50 searches, which is not too many to run here but is
nearly half of what this machine gets in a day, spent on the cheapest question the app asks.

The cloud searches the trip **committed to the branch**, so all four buttons refuse, by name, a trip
that is not on it or differs from it — commit and push first. Running a sweep on this machine is
still what `make run` is for.

Sizing follows the mode: `scripts/plan_sweep.py --mode explore` shards from the probe's own plan.
Without it a probe of `grand-tour` would be sized off that trip's 660-search sweep and dealt 36
searches across seven runners.

**Save trip** sits at the end of the Route panel, where the thing being saved is. In the Run row it
read as a fourth way to start a search.

### Which trip opens

The picker used to open `scenarios[0]`, which is alphabetical by id and so is nobody's answer. On a
machine holding both `eu-phil-japan-eu` ("EU - Phil - Japan - EU", switched off, never swept) and
`japan-philippines` ("Europe → Japan → Philippines → Europe", the trip actually being decided), it
opened the first — two names a word apart, and the one that opened drew empty charts, which is
indistinguishable from a trip whose data has gone.

It opens the **last trip you had open** (remembered in `localStorage`, wrapped in try/catch because
a private window throws on access), then **the first trip that is switched on**, then alphabetical
order for a fresh checkout where neither applies.

Switching trips goes through `openScenario`, and the order in it is the point: `loadScenario` refills
the route editor, `pollStatus` refreshes `state.sweeps`, and only then does `showTab` redraw the
step you are standing on. Redrawing earlier painted the new trip's boxes over the *previous* trip's
list of runs and left "no broad sweep with flights on disk yet" above a trip with thirteen of them.
Not redrawing at all — which is what it did — left the charts, the ranking and the whole preference
list describing the trip you had just left, changing only the name in the picker.

### Saying it where it can be read

Explanations collapse by default — `.explain` is prose about how a mechanic works, and it is
hidden until *What this is for* is pressed. That is right for teaching and it is a trap for anything
the step is unusable without. Narrow it down fell into it exactly: every word about what the date
boxes *do* was collapsible prose, so the default view of the step was a pile of date boxes, a
checkbox and four buttons, with nothing on screen saying whether the boxes filter what you read, or
buy searches, or both — or whether the nightly sweep still covers the window once you narrow.

So the answer is a **live** line (`#narrow-role`), written by the renderer from the trip in front of
you and never collapsed, under the boxes it is about:

> These dates filter the charts and the ranking on this step, and they are what the narrow sweep
> prices at 13:00 and 20:00. The broad sweep is untouched either way: it goes on pricing every date
> in the window at 02:00, and it is the only thing that can still find a cheaper week outside what
> you chose.

The rule this follows: **anything a reader must know to use a control belongs in a heading, a
label, a button's name or a live line — never in prose that hides.** So each run button states its
own cost on its face, `#narrow-role` says in a live line what the dates on that step actually do,
and the buttons are named for what they run — *Sweep narrowed* rather than a second *Run it in the
cloud*. Two identically named buttons running different modes was the single most confusing thing
on the page.

Each step keeps its own selected run (`state.stamp` and `state.finalStamp`) — going to the narrowed
runs and back must not lose the broad run you were reading. A hidden section measures its container
at zero width, so a step is re-drawn when it is opened and not merely unhidden; the same applies to
Map it out's sub-tabs.

Sections carry a `data-step`; several share one, and Map it out's also carry a `data-sub` naming
which of its three views they belong to. `data-panel` stays as each panel's own name, since that is
what every renderer, test and error box already addresses them by, and `showTab` accepts either —
asked for a panel it opens that panel's step, opens the sub-tab that panel lives on, and scrolls to
it, which is what a finished sweep opening Results and a save error opening Search both need.

The broad panels and the narrowed ones draw the same ranking *and* the same leg charts from one set
of renderers, pointed at different runs. Each is a `resultsView` and a `legView` naming the ids it owns, the run it has
selected and the modes its picker may offer; the ids differ only by a `final-` prefix, so a panel
added to one and forgotten in the other shows up as a missing element rather than as the two quietly
sharing a control. The cursors are deliberately separate: a drag on one moving the other would
collapse the distinction the switch exists to keep.

**Every step that lists runs owns its selection** — `state.stamp`, `state.finalStamp`,
`state.watchStamp`, and the probe's `state.exploreStamp`. One shared stamp is how the watch picker
came to show whichever run another step had last chosen. The broad population offers only broad runs
and the narrow one only narrowed runs, because reading the wrong one puts a narrowed run's cheapest
under a heading that says it is the trip's cheapest — and at the same depth on the same day the two
are indistinguishable in a list. **Follow it** deliberately offers both, prefixed `final ·`, and
defaults to the newest narrowed run: by the time you are pinning days, that is the freshest pricing
of the days you care about.

### The explanations are on demand

Every panel here explains itself, and most of those explanations record a failure that cost a day.
They were also the first paragraph on every panel of a screen already understood, which pushed the
actual numbers below the fold.

So each panel has a **What this is for** button, and the strip at the top has one switch that sets
the default for all of them. It starts off. A per-panel choice is remembered; flipping the switch
clears them, because a choice made against the old default means the opposite under the new one.

The line it holds is between **teaching and telling**, and it is one class each:

| | Collapses | Example |
|---|---|---|
| `.explain` | yes | *"A sweep of the dates you settled on under Narrow it down, and only those…"* |
| `.answer` | **no** | *"Next 31 Aug, 22:00 (only what you narrowed to), then 1 Sept, 04:00"* |
| `.notice`, `.badge` | **no** | *"1 cloud run is on the branch but not on this machine"* |

It was `.panel__hint` with a `--live` modifier that opted out, and that shape had the switch only
half working. A modifier naming the thing it exempts invites reaching for it: every renderer writing
grey prose used the exempt class, so turning explanations off left most of the teaching on screen —
including two paragraphs that were pure lesson. Two names cannot be reached for by accident. Walking
all four steps with the switch off now leaves exactly one grey paragraph standing, and it is a date.

**A fact must never live only in prose the switch can hide.** The nights-away band was the case that
proved it: set before the narrowing panel had a box for it, it went on filtering every sweep, and
the only thing naming it was a sentence — which had to be exempted from the switch to stay visible,
which is how one live fact kept a paragraph of teaching permanently on screen. It has a **chip with
an ×** now, beside the date fields, and the panel badge states it. A button is not prose, so it
survives the switch by not being the kind of thing the switch is about.

The default is set by a class on `<body>`, not on each panel, and a panel may then disagree with it
in either direction (`is-loud` / `is-quiet`). Scoping it to panels is what left the *Follow it*
summary — a notice sitting directly in its section — beyond the switch entirely.

The prose is hidden by a class rather than `hidden` on the paragraph, so it stays in the DOM and
find-in-page still reaches it.

### Every flight, priced on its own

Every other chart in this app is about the *total*. That is the right shape for choosing a departure
date and the wrong one for choosing a trip: a total cannot show you that the flight out is flat all
January while the one home has a single cheap Thursday, and that reading is exactly what lets a
person pick a combination the ranking would never offer — because the ranking may only offer
combinations that obey the stay ranges.

So **Narrow it down** draws one chart per leg, stacked on one shared date axis, with a draggable
marker on each. A vertical slice down the stack is one trip; the readout above says what it costs,
what split it implies, and how many nights it is.

**The narrow population draws them too, over its own runs.** The two ask different questions of the
same picture: which week, against which exact flights at today's price. For a while only the broad
one had charts, on the argument that picking needs the whole window on the axis — true for picking a
narrowing, and false for picking a flight. It left the app's freshest per-leg prices as the only ones
with no chart, since a narrow sweep runs twice a day and the broad sweep once a night, and it pointed
**Save as preference** at the stalest data in the app.

A narrowed run's legs barely overlap — measured on `japan-philippines`, five departures, seven
middles and nine returns, a 21-column axis of three tight clusters against the broad run's ~33
columns of everything against everything. Denser and easier to read, not degenerate. Nothing about
the readout is special-cased for it: everything a narrow sweep priced already obeys the narrowing, so
no badge can fire on its own, but a marker dragged past a stay range still trips one — which is
exactly the case worth naming.

**Nothing is enforced.** Drag a marker into a fifteen-night Japan stay against a 10-13 range and an
amber badge names the rule; the price is still totalled, and **Save as preference** still works. The
stay ranges exist so a sweep knows what to price, and a range typed a month ago is not evidence that
a stay is wrong — it is only a statement about where to look. *Snap to the cheapest that fits* is
there to argue with, and takes its answer from `/results?window=narrow` rather than working one out
here, so it always lands on exactly the itinerary the table calls cheapest.

The one pick the panel refuses is a leg departing before the one before it has arrived. That is not
a preference, and no sweep, watch or airline could price it.

Following a rule-breaking pick used to be refused at the save. `_validate_preferences` checked the
stay ranges, on the sound-until-it-wasn't grounds that the combiner could never close such a chain
and the check would report nothing every four hours. `watch._admitting` now widens the stays to
whatever a preference pinned before pricing it — per preference, so one preference's widening cannot
decide another's price — and drops the narrowing entirely, since a preference's dates are already
chosen and leaving it on would blank every followed trip the moment the window moved.

A leg's name expands into one line per airport pair, capped at six because that is how many chart
colours were validated in both themes; the rest are counted rather than drawn in a colour that lies.

**A date that sold nothing and a date never asked about do not draw the same.** The first is a fact
about the route and gets a tick on the floor of the plot; the second is a hole in the sweep and gets
nothing at all. `/by-leg` reads `searches.jsonl` alongside `legs.jsonl` for exactly this, and the
line breaks across a gap rather than bridging it — joining the two sides would draw a price for
every day in between.

**Probe results** reads verdicts from any run that still has its legs on disk, not only from probes — a
full sweep priced the same routes on far more dates, so its verdict is the better one when you have
one. Airports you drop gather in a bar at the top and are written in one save, so deciding about six
airports is one pass rather than six. A run whose legs never reached disk is not offered at all,
however many flights its status claims: the 11 August local sweep reports 1,167 and has none.

A run of a trip you have since edited says so at the top of the tab, names the airports it never
priced, and offers to probe the current trip — before any table is read. Rows are struck through
only for airports *you* dropped in this session; one that was simply never in your trip reads
`not in this trip`, because a whole table struck through as "dropped" looks like six decisions you
made rather than a report of the wrong thing.

Results points at Probe results when a probe is selected rather than drawing an empty itinerary
table. The status strip carries a **Stop** button while a run is going.

Every total on Results says when it was measured and how long ago, on the headline cards and on each
leg — a figure from three days ago otherwise reads exactly like one from ten minutes ago. Both charts
on Prices caption their own limits: what the departure-date grid is, and how many sweeps were too
incomplete to compare.

**+ New trip** creates one; the dropdown beside it is a list of saved trips, not a mode selector. A
trip's name is derived from its route unless you give it one, and its id is the slug of that name —
so `POST /api/scenarios` answers **409** rather than overwriting a trip you named similarly. New
trips start out of the nightly cloud sweep until you tick them in.

The route reads top to bottom: **Depart from → stops → Return to**. There is no one-way or open-jaw
checkbox; leave the Return row matching the origins for a round trip, change it for an open jaw,
empty it for a one-way. The stored `one_way` / `return_to` fields are derived from the row.

Type a city, code or country and press **Enter**. That sentence is the whole point of
[tests/test_ui_flow.py](tests/test_ui_flow.py): Enter used to be a no-op at human typing speed,
because the key handler began `if (menu.hidden) return` while the menu was still waiting on a 160 ms
debounce and a round trip. Typing three letters takes about 200 ms, so Enter always landed in the
gap — no chip, no message, nothing in the console. Those tests are marked `slow` and run a real
browser (`make test-ui`); CI runs `pytest -m "not slow"`.

### An empty page must never mean "your data is gone"

`make ui` serves static files from disk on every request while the Python stays frozen at import
time. A `uvicorn` left running from an older commit therefore hands you the *newest* page and then
404s the endpoints it asks for. That happened: two stale processes, a page that rendered an empty
trip picker and empty charts, and nothing whatsoever wrong on disk.

Two guards, because emptiness is the one failure mode this app cannot afford to render silently:

- **A contract number.** `API_CONTRACT` in [src/web/app.py](src/web/app.py) and
  `EXPECTED_CONTRACT` in [app.js](src/web/static/app.js) must match; `tests/test_web_contract.py`
  fails if they drift. The page asks `/api/version` before anything else and, on a mismatch — or on
  a 404, which is what a server older than the endpoint answers — replaces itself with *restart the
  server*, rather than drawing a convincing nothing.
- **Per-file scenario loading.** `read_scenarios` returns the trips that parsed alongside a named
  reason for each one that did not, so a single typo costs you that trip instead of the whole list.
  `load_scenarios` still raises, because a sweep told to run a specific trip should fail loudly.

The design system is **ported from the Finance-planner project** (`src/styles/palette.css` and
`theme.css` copied verbatim). It is a copy, not a shared dependency, so the two will drift. Charts
are hand-rolled inline SVG in [chart.js](src/web/static/chart.js); there is deliberately no Node
toolchain here.

---

## Project layout

```
scenarios/*.json          saved trips (committed)
start-ui.bat              what the desktop shortcut runs
scripts/build_airports.py regenerates the airport catalogue from OurAirports
src/
  scenario.py             schema, validation, load/save, migration
  airports.py             catalogue lookup and search
  home_airports.py        your airports, ranked by how easy each is to reach
  viability.py            what sweep history says about a route or airport
  sources.py              per-site URL grammar and selectors, with the defaults
  sweep/planner.py        scenario -> searches, and the routes a trip requires
  sweep/runner.py         concurrent execution, sweep quality, comparability
  sweep/explore.py        one probe -> a verdict per airport, and what went unsearched
  combine.py              legs -> itineraries, and the narrowing applied to them
  alerts.py               which itineraries are worth reporting
  verify.py               re-price the shortlist on a second site
  probe.py                volatility sampling and report
  watch.py                preferences: what they cost now, and what is cheaper nearby
  notify_discord.py       price + health alerts
  webhook_store.py        the Discord webhook, kept where `git add` cannot reach
  providers/
    pelikan_url.py        deep-link builder
    pelikan.py            search + parser (the only sweep source)
    letuska.py            on-demand second opinion, not sweepable
  web/app.py              JSON API, serves the UI
  web/static/             the UI
data/airports.json        4,161 airports: code, city, country, size, aliases
data/countries.json       ISO code -> country name, so "Japan" is searchable
data/airport_notes.json   hand-measured findings no sweep can reproduce
data/sources.json         overrides for src/sources.py; delete to restore defaults
data/home_airports.json   your airport ranking; delete to fall back to your trips
.secrets/discord.json     the webhook URL — gitignored, never committed
data/sweeps/<id>/<ts>/    legs.jsonl, status.json, scenario.json (the trip it
                          searched), best.json, verify.json
data/probe/               observations.jsonl
data/watch/<id>/          observations.jsonl (preferences), leg-observations.jsonl
docs/superpowers/specs/   design documents
```

---

## Legal

Respect each site's terms and robots.txt. Verified: pelikan.cz disallows only `/gf3/` and
`/services/`; letuska.cz disallows `/searchform`, `/assets/` and `/api/`. Nothing used here is
disallowed — searches run on public pages and results render into them. Keep request rates low; the
sweep runs 2 workers with a 4-second delay deliberately, which is gentler than measured need.
