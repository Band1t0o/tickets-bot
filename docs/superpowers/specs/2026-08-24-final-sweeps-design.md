# Final sweeps: the narrowing as a sweep of its own — design

*2026-08-24*

## Why

**The broad sweep stopped being broad the moment you decided anything.** The narrowing lived on the
trip file and `planner._leg_window` read it unconditionally, so `plan_searches` — the plan behind the
02:00 cloud run, the *Run locally* button and the exploration probe alike — searched only what the
trip had been narrowed to. Saving a departure window was therefore also a decision to stop pricing
every other week, taken silently, with nothing on screen saying so.

Measured on `japan-philippines` the morning this was written: the two committed nightly runs planned
**48 searches against a window of 85**. Both were filed as ordinary broad sweeps and both were joined
to the broad trend line, because `status.json` recorded `focus` and nothing else — and this trip has
no focus. It is narrowed by a return window and a nights band, neither of which the status could
express. The reading that hid the problem was the cheerful one: a nightly sweep quoting a fraction of
its usual cost looks like a saving.

**The narrowing had no Run button anywhere.** *Narrow it down* writes to the trip file and reads a
sweep three ways, and every control on it is either a date box or a chart. So it reads as a filter
view — which is how it was described when this work was asked for, along with the reasonable question
of where the sweep with those settings had gone. There was not one.

**Both facts are the same fact.** There was one sweep doing two jobs badly. The window needs pricing
broadly and rarely; the decision needs pricing narrowly and often, for the days before a booking, so
that a drop is visible as a drop rather than as one number with nothing to compare it to.

**Outcome:** two sweeps, told apart on disk and on screen. The broad one prices the window and
nothing on the trip may shrink it. The narrowed one is a mode, a tab and a scheduled slot of its own,
and it refuses a trip with nothing to narrow to.

## What was built

### One flag, two plans

`_leg_window(scenario, leg_index, narrowed)` stays the single place the four constraints meet — the
property that stopped a probe and a sweep disagreeing about when a leg may leave. `narrowed=False`
keeps the horizon and the stays; `narrowed=True` adds the three narrowing bounds, which is what the
function always did.

| | plan | on the real trip |
|---|---|---|
| `plan_searches` | the whole window | 85 searches, ~21 min |
| `plan_final` | only the narrowing | 31 searches, ~8 min |
| `plan_exploration` | broad, three dates a leg | unchanged |

The probe goes broad with the sweep. It judges which airports are worth pricing and belongs to *Map
it out*; sampled inside the narrowing it would rank airports on the handful of days already chosen,
which is a verdict about those days wearing an airport's name.

`plan_final` **raises** on a trip with no narrowing rather than returning the broad plan. A run filed
as `final` that had in fact priced the whole window would sit on the narrowed trend line, on the
wrong axis, for as long as it stayed on disk — and it would have spent an hour reaching the answer
the 02:00 sweep already had.

### A fourth mode, and one dispatch table

`MODES` gains `final`; `MODE_ROOTS` files it under `sweeps/` beside the broad runs, so the history,
merge-shards and branch-sync paths need no change. The two are told apart by `status.mode`.

The plan dispatch table `{"explore": …, "watch": …, "sweep": …}` existed in **three** identical
copies — `web/app.py`, `sweep/runner.py`, `cli.py` — which is a mode added to two of them and priced
one way while running another. It is now `planner.PLANS`, imported by all three. `cli.py`'s
`--mode` choices read `MODES` instead of a hardcoded pair.

### The status records what it searched

```json
"narrowing": {"focus": [...] | null, "return_focus": [...] | null, "total_days": [lo, hi] | null}
```

`focus` is kept beside it untouched, because every sweep committed before the split carries that key
and nothing may retire them.

A **broad run writes three `null`s even on a narrowed trip.** The field says what happened, not what
the trip said at the time; reading it the other way round is the whole of the bug. `narrowing_of`
falls back to synthesising from `focus` for older runs — honest about what they knew, rather than
promoted onto the broad line or discarded.

`is_comparable` now turns on the whole narrowing rather than the focus alone, and **does not exclude
`final`**: two narrowed runs of the same narrowing are the same measurement twice, which is the
series a booking decision is waiting on. What keeps a narrowed run off the broad line is the
narrowing rule, not its mode.

### Two lines, each read on its own terms

`/api/history` tags every point `series: broad | final` and reads each run with `in_window` matching
its own series. A broad run's best total is now the cheapest of the whole window; it used to be read
through today's narrowing, which made the broad line a second copy of the narrowed one drawn from
older data. Comparability is judged within a line — a trip gaining a focus no longer retires the
broad sweeps behind it, because they priced the window and the window has not moved.

**Expected consequence:** runs committed before this change ran narrowed and now dim out of the broad
line. They were narrowed. No data is touched; the chart stops claiming otherwise.

### Scheduling

02:00 UTC prices the window. 13:00 and 20:00 UTC re-price the narrowing — the second slot added
because the point of a narrowed sweep is repetition, and the probe's measured time-of-day bias for
18:00–22:00 UTC is ~0.6% above the day median, small and consistent enough to shift the level
slightly and not the trend.

`plan_sweep.py --focused` becomes `--final` and selects on **any** of the three constraints, not the
focus alone — the old rule would have skipped exactly the trip whose nightly runs prompted this. The
**health gate is dropped**, on the watch slot's reasoning rather than the old focused slot's: that
gate existed because the afternoon used to re-run the whole window at a site the morning had shown to
be refusing. Thirty-one searches is not that run, and if the site is still refusing, coverage records
it honestly.

### The frontend

A fourth step, `Final sweeps`, third in the bar. It holds what a narrowed sweep would search (read
from the trip, not restated — one field, one control), its cost at the depth the buttons will use,
Run here / Run in cloud, a picker of narrowed runs, and their ranking.

*Narrow it down* and *Final sweeps* draw the same ranking from **one set of renderers**, parameterised
by a `resultsView` naming the ids it owns, the run it has selected and the modes its picker may offer.
The ids differ only by a `final-` prefix. Copying six hundred lines per tab was the alternative.

**Every step that lists runs owns its selection**: `state.stamp`, `state.finalStamp`,
`state.watchStamp`, `state.exploreStamp`. One shared stamp is how the watch picker came to show
whichever run another step last chose — and binding it to the broad step's stamp only moved that bug,
because it could then never land on a narrowed run.

Which runs each picker offers:

| picker | offers | why |
|---|---|---|
| *Narrow it down* | broad only | picking a trip needs the whole window on the axis |
| *Final sweeps* | narrowed only | reading the wrong one puts a narrowed cheapest under "the trip's cheapest" |
| *Follow it* | both, prefixed `final ·` | by then the freshest pricing of the days you care about is the narrowed run; defaults to it |

At the same depth on the same day the two are indistinguishable in a list, which is why the word is
in front of the depth rather than instead of it.

The narrowing panel's cost line was quietly broken by the split: it compared two estimates that
differed by the *body* — the same trip with the narrowing nulled out — which stopped meaning anything
once a broad sweep stopped reading the narrowing. Both sides returned the identical number, so the
line reported that narrowing to five days out of thirty-five saved nothing. They differ by `mode` now.

### The alert's high-water mark, per population

`best.json` held one `cheapest` entry, which was harmless only while every sweep was narrowed.
Immediately after the split a live final run recorded **25,967** over the broad runs' **21,445**, and
the next broad sweep would have been reported as a 4,500 CZK drop that no fare ever made — the
trend-chart mistake one layer down, caught the same way.

`alert_key(name, mode)` keeps them apart: `cheapest` for a broad sweep, `final:cheapest` for a
narrowed one. The broad key is deliberately unchanged, because `best.json` is committed and every
entry ever written to it is a broad sweep's; renaming it would restart the mark from nothing and
report the next ordinary sweep as an all-time low. `SweepResult` gained `mode` so nothing downstream
has to re-read `status.json` to find out which population it is in.

**Message volume.** With `notify_quiet` **off** — as `japan-philippines` has it — every sweep posts
whatever it found, so the third slot takes Discord from two messages a day to three. Turning
`notify_quiet` on makes both populations report only on a genuine improvement against their own mark,
which is what the separate keys now make possible.

### Folded in: taking cloud results without merging

`branch_sync.pull` fast-forwards or refuses, and refuses correctly when the checkout has commits of
its own — but the runs it was refusing were **directories the working tree does not have at all**.
Two finished cloud sweeps sat unreachable behind a sentence with no button under it.

`take` checks out only those paths from `CLOUD_REF` and never moves HEAD. It is a deliberate
carve-out from the module's "no `checkout --`" rule, and narrow by construction: it only ever names
paths that do not exist, and re-checks that immediately before each one. A checkout restricted to a
path with nothing at it cannot discard anything.

## What was not done

- **The broad sweep's cost went up**, from 48 searches a night to 85 on this trip — 1 runner to 1,
  since both are under the ~100 the sharder deals per runner. That is the intended cost of the fix.
- **The final sweep has no per-leg charts or cursor.** Picking a trip by hand stays on *Narrow it
  down*, which draws the whole window; a narrowed run draws three charts a few days wide on which
  every marker position already obeys the narrowing.
- **Nothing snapshots the narrowing.** Editing the boxes mid-week changes what the scheduled final
  sweeps run and correctly marks the trend before it incomparable. A committed copy separate from the
  scratchpad was considered and dropped as a schema field earning too little.
