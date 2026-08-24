# Per-leg charts on Final sweeps, and one spacing scale — design

*2026-08-24*

## Why

### The freshest prices in the app were the ones with no chart

*Final sweeps* shipped with a plan panel and a ranking table. The per-leg charts — three curves on
one date axis, a draggable marker on each, and under them **Snap to the cheapest that fits**,
**Follow this trip** and **Find it in the table** — existed only on *Narrow it down*, reading only
broad runs.

There was an argument for that, written into `app.js` at the time:

> This step is where a trip is *picked*, and picking needs the whole window on the axis; a narrowed
> run draws three charts a few days wide, on which every marker position already obeys the narrowing
> and the chart can only ever agree with it.

It is right about picking a *narrowing* and wrong about picking a *flight*, which is a second
question the app had no view for. The consequence was backwards for the phase the tool exists to
serve: the final sweeps run **twice a day** and the broad sweep **once a night**, so the most recent
per-leg pricing in the app was the only pricing with no chart, and the button that follows a trip
before booking it was wired to the stalest data on disk.

The chart is also not degenerate. Measured on the live run `2026-08-24T09-55-02Z`: leg 0 got 5 dates,
leg 1 got 7, leg 2 got 9, and they do not overlap — a 21-column axis of three tight clusters, against
the broad run's ~33 columns of everything against everything. Denser and easier to read.

### Nothing on the page began at the same distance from anything else

The second half is a levelling pass, and the inconsistencies were countable rather than a matter of
taste:

- **31 inline `margin` declarations across 12 distinct values** (0, 6, 8, 10, 12, 14, 16, 18, 20,
  22 …), chosen a panel at a time.
- **Two sub-heading treatments.** Eight `<h3 class="muted small">` with a hand-set `margin-top`,
  against `.panel__section > h3` — 13px, uppercase, ruled — used only by the exploration report.
- **One depth control spelled two ways**: opposite option order, different capitalisation,
  `every 3rd day` against `every 3 days`, and a `quick` option on one tab that declined to say what
  its sampling step was.
- **Two names for one action**: `Run locally` / `Run in cloud` on *Map it out*, `Run it here` /
  `Run it in the cloud` on *Final sweeps*, and `Stop` against `Stop this run`.
- **A `.row` rule that mis-aligned every select against every button beside it** (below).

## What was built

### Two leg-chart views

`legView({prefix, stampKey, modes, picker, mate, table, results, emptyText})`, the same move
`resultsView` already makes for the tables: one set of renderers addressed by an id prefix.
`NARROW_CHARTS` (prefix `''`, `state.stamp`, broad runs) and `FINAL_CHARTS` (prefix `final-`,
`state.finalStamp`, final runs) each own their own `{data, domain, cursor, expanded}`.

`loadLegCharts`, `drawLegCharts`, `cursorTrip`, `renderCursor`, `placeCursor`, `snapCursor` and
`watchCursor` all take the view; the picker and the three buttons are bound in a loop over
`LEG_VIEWS`, so two copies cannot drift into two panels answering one question differently.

**The cursors are separate on purpose.** A drag on one moving the other would be exactly the
broad/final toggle these two steps exist instead of. A browser test pins it.

Three couplings stay broad-only rather than being copied: `renderNarrowFields` / `refreshStayDerived`
(they write the narrowing boxes, which the final step does not have), the "pull the step onto a
drawable run" dance (every final run is drawable — no probe is ever `mode: final`), and the by-date
chart, which exists to fill *Leave between* and would say nothing over a five-day window.

**Nothing in the readout is special-cased for a narrowed run.** Everything a final sweep priced
already obeys the narrowing, so no badge can fire on its own — but a marker dragged past a stay range
still trips one, which is the case worth naming.

### `charts: true|false` retired for `legs`

`resultsView` carried a boolean meaning "may a row offer *Put on the charts*", false for the final
view. It was false only because the final step had no charts. It is now `view.legs`, the leg view a
row lifts onto, and `FINAL_VIEW.legs = FINAL_CHARTS` — so a row of the narrowed ranking lands on the
narrowed curves and a broad row on the broad ones.

### The cloud-sync notice, third copy

The step the cloud files two runs a day into was the one step that could not say any had arrived.
`renderCloudSync` already loops over `(box, text, get, take)` id tuples and skips absent ones; the
markup and a third tuple were the whole change.

### One spacing scale

The default was inverted. `.panel__hint` used to be `margin: -6px 0 14px` — a tuck up under the panel
header — with four adjacency rules undoing that tuck everywhere else, and inline margins on the
paragraphs those four missed. Stated the other way round it is one rule:

```css
.panel__hint { margin: 8px 0 14px; }
.panel__header + .panel__hint { margin-top: -6px; }
```

The rest is adjacency rather than per-element: `.panel > * + .row`, `+ .disclosure`, `+ .stat-row`,
`+ .table-scroll` at 16px; `.disclosure > .row` at 12px both sides; `.panel__line` at 12px for the
lines a renderer writes under the controls they describe, collapsing to 0 when empty so a gap under
nothing does not read as a panel that failed to draw something.

`tests/test_web_contract.py` fails on any inline `margin` in `index.html`, so the scale cannot leak
back one panel at a time. It also pins the two depth selects to identical option text, and pins every
depth option to naming its sampling step.

### One baseline

```css
.row:has(label.field) { align-items: flex-end; }
```

`label.field` is a two-line column — a 12px label stacked over its control — and `.row` centred it,
so a ~49px column against ~35px buttons put their bottom edges about seven pixels apart. In every run
row and every filter row on every tab. Two parametrised browser tests assert the select and the
button beside it share a bottom edge to within a pixel.

### One control size, one vocabulary

`.small` is for a control inside something else — a table row, a notice, a disclosure. A panel's own
action row is full size, which moved the narrowing's Save/Clear, all six cursor buttons and the final
run buttons onto one size.

`Run it here` / `Run it in the cloud` / `Stop this run` everywhere. One action, one name, the whole
way through the flow.

## What was measured, not assumed

A test written to catch "the same depth select renders at 12px on one tab and 14px on the other"
**passed on the first run**. `label.field` sets the size itself and the extra `.small` on one of them
changes nothing, so the two already agreed. The test was kept — as a guard rather than a fix — and
its docstring corrected, rather than shipping a false history in a comment.

## Not built

- No by-date chart on *Final sweeps*.
- No broad/final toggle on *Narrow it down*. Its boxes, its by-date chart and its stay ranges all
  assume a broad axis; reading a final run there would let you narrow from the narrowing.
- No palette, typeface or layout change.
- No `API_CONTRACT` bump — no endpoint changed shape.
