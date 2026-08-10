# Flight scenario watcher

Finds the cheapest way to fly a **multi-leg trip** — anywhere to anywhere, via as many stops as you
like — and watches the price until you book.

- **Scenarios** are committed JSON files: which airports, which date window, how long to stay where
- **GitHub Actions does the searching** on a schedule and commits results back, so it works whether or not your machine is on
- **Discord** pings you only when the best total actually improves
- **A local web UI** (`make ui`) to define scenarios, launch sweeps and read results
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
touches the network and there is no key or quota to acquire. The UI searches it by code, city or
name.

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

## Automation and the Actions budget

The repo is **private**, so GitHub Actions gives 2,000 free minutes a month.

| Job | Schedule | Cost |
|---|---|---:|
| Sweep ([scrape.yml](.github/workflows/scrape.yml)) | daily 02:00 UTC, `standard` | ~1,000 min/mo |
| Volatility probe ([probe.yml](.github/workflows/probe.yml)) | every 2 h, **temporary** | ~720 min/mo if left on |
| Tests ([test.yml](.github/workflows/test.yml)) | every push | ~1 min |

Scheduled runs sweep at `standard`, not `deep`: at 2 workers a deep sweep is ~97 min, past both the
90-minute job timeout and the monthly tier. A standard sweep that mostly succeeds is worth more than
a deep one that does not.

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

**Legs per search is the honest health metric.** ~10 is healthy; 2.9 was 70% silent failure.

Secrets do not transfer between repositories. Only one is needed:

```bash
gh secret set DISCORD_WEBHOOK_URL -R Band1t0o/tickets-bot
```

---

## Volatility probe

The daily cadence is an assumption: at five months out, fares normally move over days, not hours.
The probe measures whether that holds, sampling three fixed routes every two hours.

```bash
python -m src.cli probe          # one sample of all three routes
python -m src.cli probe-report   # how much prices actually moved
```

**It is temporary.** Read the report and turn it off — left running it costs ~720 min/month and will
break the budget:

```bash
gh workflow disable probe.yml -R Band1t0o/tickets-bot
```

Reading it: a change rate under ~20% with moves under ~2% means daily sweeping loses nothing worth
chasing. Frequent moves above ~5% argue for sweeping more often.

---

## The UI

Three tabs: **Search** (stops you can add, remove, reorder and label, each with a typeahead airport
picker and a stay range; cost estimate before you commit), **Results** (cheapest same-airport and
cheapest open-jaw side by side, then every itinerary, expandable into legs), **Prices** (cheapest
total by departure date — *when* to fly; best total over time — *whether to book now*; and the probe
table).

The design system is **ported from the Finance-planner project** (`src/styles/palette.css` and
`theme.css` copied verbatim). It is a copy, not a shared dependency, so the two will drift. Charts
are hand-rolled inline SVG in [chart.js](src/web/static/chart.js); there is deliberately no Node
toolchain here.

---

## Project layout

```
scenarios/*.json          saved searches (committed)
scripts/build_airports.py regenerates data/airports.json from OurAirports
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
data/airports.json        4,161 airports with scheduled service
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
