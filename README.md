# Flight scenario watcher

Finds the cheapest way to fly a **multi-leg trip** — Europe → Japan → Philippines → Europe —
and watches the price until you book.

- **Scenarios** are committed JSON files: which airports, which date window, how long to stay where
- **GitHub Actions does the searching** on a schedule and commits results back, so it works whether or not your machine is on
- **Discord** pings you only when the best total actually improves
- **A local web UI** (`make ui`) to define scenarios, launch sweeps and read results
- No database — everything is files under `data/` and `scenarios/`

> Personal, non-commercial use. Keep the politeness delays in place.

---

## Quick start

```bash
make install     # creates a Python 3.12 venv and installs deps
make pw-install  # installs the Chromium Playwright needs
make ui          # http://localhost:8000
```

Python **3.12+** is required — `str | None` annotations are evaluated at runtime by pydantic and
will crash on 3.9 (which is what macOS ships).

```bash
make test                                                  # run the suite
python -m src.cli sweep --scenario japan-philippines --dry-run   # cost, no browser
python -m src.cli sweep --scenario japan-philippines --depth quick
```

---

## How a trip is modelled

The three legs are searched **independently** and then chained, rather than searching whole
itineraries. That keeps cost additive rather than multiplicative — each leg is searched once and
reused across every itinerary built from it.

| Stage | What it does | Where |
|---|---|---|
| Planner | Scenario → list of one-way searches | [src/sweep/planner.py](src/sweep/planner.py) |
| Runner | Runs them across 4 browsers, writes `legs.jsonl` | [src/sweep/runner.py](src/sweep/runner.py) |
| Combiner | Chains legs into valid itineraries | [src/combine.py](src/combine.py) |
| Notifier | Posts to Discord when the best total improves | [src/notify_discord.py](src/notify_discord.py) |

Stay lengths are computed from the dates **on the returned flights**, never the requested ones —
pelikan.cz substitutes nearby dates, so asking for 22 January can return the 23rd.

### Baggage and ranking

Itineraries are ranked on the **bag-inclusive** total, not the headline fare. A leg that does not
confirm an included checked bag is charged `bag_estimate_czk` (default 1,500) for ranking purposes.
Without this, a low-cost carrier's bagless fare is compared directly against a legacy carrier's
bag-inclusive one — which systematically flatters exactly the cheap leg the total depends on. The
estimate is always shown as an estimate; the site only quotes the real fee after "POKRAČOVAT".

### Depth

| Depth | Date step | Searches | Time (2 workers) |
|---|---|---:|---:|
| `quick` | every 7 days | 93 | ~15 min |
| `standard` | every 3 days | 210 | ~33 min |
| `deep` | every day | 615 | ~97 min |

---

## Airports

Every airport below was checked live against pelikan.cz on a sample January 2027 date
(one-way to NRT, and one-way back from MNL, 1 adult, CZK). Failures are kept in the list rather
than deleted so nobody re-litigates them later.

| Airport | →NRT | MNL→ | Combined | Status |
|---|---:|---:|---:|---|
| **VIE** Vienna | 15,057 | 8,567 | **23,624** | on by default |
| **FRA** Frankfurt | 13,546 | 13,608 | **27,154** | on by default — 45% under Prague on a live run |
| **PRG** Prague | 14,480 | 16,255 | **30,735** | on by default |
| BER Berlin | 14,785 | 16,269 | 31,054 | available, off |
| MUC Munich | 16,963 | 14,832 | 31,795 | available, off |
| KRK Krakow | 16,723 | 17,105 | 33,828 | available, off |
| KTW Katowice | 16,860 | 21,313 | 38,173 | available, off |
| BTS Bratislava | 25,706 | — | — | **unavailable** — no return inventory |
| BRQ Brno | — | — | — | **unavailable** — no long-haul inventory at all |

The Japan→Philippines leg is cheap and well served: NRT→MNL from 3,863 Kč, HND→MNL 4,261,
KIX→CEB 4,669.

---

## The deep-link URL grammar

Searches navigate straight to a constructed URL instead of driving the search form. This took a
search from ~150 s to **~14 s**, and is what makes a full 3-leg sweep possible at all.

```
/cs/letenky/T:{type},P:{adults}000E_0_0,CDF:{FROM}{FROM},CDT:A{TO},DD:{y}_{m}_{d}[,DR:{y}_{m}_{d}]/
```

- `T:1` round trip (needs `DR`), `T:2` one-way. `T:0`, `T:3` and `R:0` return nothing — never
  generate them.
- Dates are bare integers: `2027_2_3`, never `2027_02_03`.
- `CDF` repeats the origin code; `CDT` prefixes the destination with `A`. The asymmetry is the site's.
- **`CDF` and `CDT` are index-paired lists, not an open-jaw pair.** `CDF:PRGVIE,CDT:ANRT` searches
  PRG↔NRT; `CDF:PRGVIE,CDT:ANRTAMNL` searches VIE↔MNL. Each index is its own round trip, so this
  cannot express "out from Prague, back to Vienna" — do not try to build an open jaw this way.
- Prices are **per person**. The site's label switches from "Celková cena pro všechny osoby" at
  1 passenger to "Průměrná cena na osobu" at 2 — same number either way for one traveller.
- Flight numbers are **not** in the DOM (they sit behind a collapsed "Detaily letů" panel), so legs
  are identified by carrier + times + duration + stops.
- Checked baggage **is** in the DOM, per offer: `img.baggage-img` resolves to
  `checked-baggage-include.svg` or `-exclude.svg`, with `Odbavené zavazadlo: Ano/Ne` beside it.
  Low-cost carriers show "Pro více info o zavazadlech klikněte na POKRAČOVAT" instead, which is
  recorded as **unknown** (`None`) — never as included.

### What pelikan cannot do

Its search panel offers exactly three modes: **ZPÁTEČNÍ** (round trip), **JEDNOSMĚRNÁ** (one-way)
and **NÁVRAT Z JINÉHO MĚSTA** (open jaw — return to a different city). There is **no 3-leg
multi-city search**, so a Europe→Japan→Philippines→Europe trip cannot be priced as one ticket
here. Metasearch sites (Skyscanner and friends) do offer it, and a hand-built quote for this trip
came in below the sum of three pelikan one-ways — 27,971 vs 29,813 Kč on identical dates. Part of
that gap is through-fare pricing and part is simply that a metasearch aggregates more sellers than
one OTA; the measurement does not separate the two.

The open jaw is reachable only by driving the form: the deep link is **last-pair-wins**, so
`CDF:PRGVIE,CDT:ANRTAMNL` searches VIE↔MNL and `CDF:PRGMNL,CDT:ANRTAVIE` searches MNL↔VIE — always
a round trip, never an open jaw.

### Through-fares are real but not universal — do not assume either way

Measured on pelikan, same dates, one adult:

| Journey | Through-fare | Two one-ways | Winner |
|---|---:|---:|---|
| PRG↔NRT, 6 Jan / 28 Jan | 19,961 | 29,938 | **round trip, −33%** |
| FRA↔NRT + NRT↔MNL, 23 Jan / 10 Feb | 28,276 | **23,009** | **one-ways, −19%** |

So "always buy a through-fare" is as wrong as "always sum one-ways". A round trip is worth adding
as an **extra candidate** to compare against the leg chain, never as a replacement for it — which
is why the sweep still searches legs and ranks the chain.

---

## Automation and the Actions budget

The repo is **private**, so GitHub Actions gives 2,000 free minutes a month.

| Job | Schedule | Cost |
|---|---|---:|
| Standard sweep ([scrape.yml](.github/workflows/scrape.yml)) | daily 02:00 UTC | ~1,000 min/mo |
| Volatility probe ([probe.yml](.github/workflows/probe.yml)) | every 2 h, **7 days only** | ~168 min once |

That is ~50% of the tier, leaving room for manual runs. Prefer **Run locally** in the UI for
exploring; a cloud run spends real budget.

Scheduled runs use **standard** depth, not deep. At 2 workers a deep sweep is ~97 min, over both
the job timeout and the monthly tier — and a standard sweep that mostly succeeds beats a deep one
where most searches time out.

| Depth | Searches | Estimate (2 workers) |
|---|---:|---:|
| `quick` | 93 | ~15 min |
| `standard` | 210 | ~33 min |
| `deep` | 615 | ~97 min (manual only) |

### Concurrency is deliberately low

2 workers and a 4 s delay, down from 4 and 1.5 s. The first sweep that could report failures showed
**58 of 93 searches timing out**, and the same rate was there before and invisible: the previous
"0 errors" sweep averaged 2.9 legs per search where a healthy one returns ~10. Whether the cause is
concurrency or IP-level throttling is unresolved — after a day of probing, sequential single
searches from one machine went from ~14 s to over 6 minutes, which looks like client throttling
rather than load. Both readings argue for going gentler, so these settings are below measured need.
**Validate them from the scheduled run's `error_count`, not by hammering the site.**

Making the repo public would give unlimited minutes on standard runners. The reason not to is
privacy, not cost: scenario files record which airports you leave from and exactly when you are
abroad.

**Three failure modes worth knowing about, all now guarded:**

1. The previous workflow died in December 2025 and went unnoticed for **8 months**, because a
   broken scraper and a quiet day look identical. A sweep that finds nothing now posts a red
   health alert to Discord.
2. A blanket `data/` rule in `.gitignore` silently discarded every result — `git add` matched only
   ignored paths and `|| true` swallowed the error. Results are now committed, and `git add` no
   longer hides failures.
3. A search whose results never rendered returned `[]`, which the runner recorded as a *successful*
   empty route. On the 2026-08-06 sweep this lost `MNL→VIE`, `CEB→PRG` and `CEB→FRA` entirely while
   `status.json` reported `error_count: 0` — and `MNL→VIE` was the return leg of the cheapest real
   itinerary. `search_leg` now races the offer cards against the site's own "Nenašli jsme žádn"
   message and raises `SearchTimeout` if neither appears; `status.json` lists
   `routes_with_no_results`, and a dark route triggers the health alert.

Secrets do **not** transfer between repositories. Only one is needed:

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

**It is temporary.** After about a week, read the report and turn it off — left running it costs
~720 min/month and will break the budget:

```bash
gh workflow disable probe.yml -R Band1t0o/tickets-bot
```

Reading it: a change rate under ~20% with moves under ~2% means daily sweeping loses nothing worth
chasing. Frequent moves above ~5% argue for sweeping more often.

---

## The UI

Three tabs: **Search** (scenario definition, airport picker, cost estimate before you commit),
**Results** (cheapest same-airport and cheapest open-jaw side by side, then every itinerary,
expandable into legs), **Prices** (cheapest total by departure date — *when* to fly; best total
over time — *whether to book now*; and the probe table).

The design system is **ported from the Finance-planner project**
(`src/styles/palette.css` and `theme.css` copied verbatim). It is a copy, not a shared dependency,
so the two will drift — style fixes worth keeping should be made in both. Charts are hand-rolled
inline SVG in [chart.js](src/web/static/chart.js); there is deliberately no Node toolchain here.

---

## Project layout

```
scenarios/*.json          saved searches (committed)
src/
  scenario.py             schema, validation, load/save
  sweep/planner.py        scenario -> searches
  sweep/runner.py         concurrent execution
  combine.py              legs -> itineraries
  probe.py                volatility sampling and report
  notify_discord.py       price + health alerts
  providers/
    pelikan_url.py        deep-link builder
    pelikan.py            search + parser
    letuska.py            second source, legacy interface
  web/app.py              JSON API, serves the UI
  web/static/             the UI
data/sweeps/<id>/<ts>/    legs.jsonl, status.json, best.json
data/probe/               observations.jsonl
```

---

## Legal

Respect each site's terms and robots.txt. Verified: pelikan.cz disallows only `/gf3/` and
`/services/`; letuska.cz disallows `/searchform`, `/api/` and `/assets/`. Nothing used here is
disallowed. Keep request rates low.
