# Flight scenario watcher

Finds the cheapest way to fly a **multi-leg trip** — anywhere to anywhere, via as many stops as you
like — and watches the price until you book.

- **Scenarios** are committed JSON files: which airports, which date window, how long to stay where
- **GitHub Actions does the searching** on a schedule and commits results back, so it works whether or not your machine is on
- **Discord** pings you only when the best total actually improves
- **A local web UI** (`make ui`, or the desktop shortcut) to build trips, launch sweeps and read results
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
| Combiner | Chains legs into valid itineraries | [src/combine.py](src/combine.py) |
| Notifier | Posts to Discord when the best total improves | [src/notify_discord.py](src/notify_discord.py) |

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
| `quick` | every 7 days | 93 | ~15 min |
| `standard` | every 3 days | 210 | ~33 min |
| `deep` | every day | 615 | ~97 min |

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

**pelikan.cz** is the only sweep source. **letuska.cz** was spiked and rejected for sweeping: it has
no deep-link grammar — `/letenky/PRG/NRT/<date>`, `?from=&to=&date=` and a hash route all 404 — and
its search is an Angular form whose results render in place, reached through a cookie banner,
autocomplete typing and a Czech-month calendar behind two nested shadow roots. It survives as a
second opinion on a single fare:

```bash
python -m src.cli check-price --from PRG --to NRT --depart 2027-01-12 --return 2027-01-30
```

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
| Sweep ([scrape.yml](.github/workflows/scrape.yml)) | daily 02:00 UTC, `deep` | ~97 min |
| Volatility probe ([probe.yml](.github/workflows/probe.yml)) | every 2 h | ~2 min |
| Tests ([test.yml](.github/workflows/test.yml)) | every push | ~1 min |

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

Secrets do not transfer between repositories. Only one is needed:

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

## The UI

`make ui`, or the **Flight watcher** desktop shortcut ([start-ui.bat](start-ui.bat)).

Three tabs: **Search** (build a trip), **Results** (cheapest same-airport and cheapest open-jaw side
by side, then every itinerary, expandable into legs), **Prices** (cheapest total by departure date —
*when* to fly; best total over time — *whether to book now*; and the probe table).

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
  viability.py            what sweep history says about a route or airport
  sweep/planner.py        scenario -> searches
  sweep/runner.py         concurrent execution
  combine.py              legs -> itineraries
  probe.py                volatility sampling and report
  notify_discord.py       price + health alerts
  providers/
    pelikan_url.py        deep-link builder
    pelikan.py            search + parser (the only sweep source)
    letuska.py            on-demand second opinion, not sweepable
  web/app.py              JSON API, serves the UI
  web/static/             the UI
data/airports.json        4,161 airports: code, city, country, size, aliases
data/countries.json       ISO code -> country name, so "Japan" is searchable
data/airport_notes.json   hand-measured findings no sweep can reproduce
data/sweeps/<id>/<ts>/    legs.jsonl, status.json, best.json
data/probe/               observations.jsonl
docs/superpowers/specs/   design documents
```

---

## Legal

Respect each site's terms and robots.txt. Verified: pelikan.cz disallows only `/gf3/` and
`/services/`; letuska.cz disallows `/searchform`, `/assets/` and `/api/`. Nothing used here is
disallowed — searches run on public pages and results render into them. Keep request rates low; the
sweep runs 2 workers with a 4-second delay deliberately, which is gentler than measured need.
