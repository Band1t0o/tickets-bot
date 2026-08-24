# Reading the watcher: explanations on demand, one date vocabulary, one decision centre — design

*2026-08-23*

## Why

The narrowing work landed and the mechanics were right. What was wrong was the reading of it.

**Every panel taught before it told.** Thirty-four `.panel__hint` blocks of static prose sat above the
things they describe. The prose is good — most of it records a failure that cost a day — and it was
also the first paragraph on every panel of a screen already understood, which pushed the numbers
below the fold.

**Two controls wrote one field.** `focus_start`/`focus_end` was set by the *Leave between* boxes in
*When you actually want to go* and by clicking two points on *Cheapest total by departure date*, two
panels below. Two badges in two vocabularies — `out 01-08–01-12` against `watching 2027-01-08 to
2027-01-12` — two Saves, one field, and neither control knew what the other had done.

**That panel also stated something false.** It said the morning sweep keeps covering the whole window
so a better date outside your pick is still found. The README had already recorded that this is not
true: `--focused` picks which *trips* run in the afternoon, both slots then run the same command, and
`plan_searches` reads the narrowing off the file. The UI was the last place still claiming it.

**The decision had no centre.** *Every flight, priced on its own* is where a trip is chosen, and it
was the one panel with no way to buy anything and no connection to the ranking under it. The booking
URLs were already in the `by-leg` payload it fetches, and nothing read them.

**Follow it read as five unrelated panels**, one pointing at a *Results* tab that stopped existing
when seven tabs became three steps, and nothing on it answered the question it is arrived with: if I
change the trip, does any of this move?

**Explore opened on a timestamp.** Answering "is Vienna worth keeping" began with choosing which of
twenty-two runs to ask — and whichever you chose, a route it never reached read as an airport with
nothing to say for itself.

## Hide the teaching, keep the telling

The rule the whole first change rests on. `.panel__hint` is prose about how a mechanic works and
collapses. `.panel__hint--live` is written by a renderer — a cost, a sampling resolution, which stays
a run was priced under — and never collapses, because it is an answer about the run in front of you
rather than a lesson. Notices and badges are neither and are untouched.

It held on the first pass: no test in the suite asserted on static prose, and all three hint elements
the tests do read (`#history-note`, `#by-date-note`, `#narrow-cost`) are derived.

One hint had to be split rather than classified. `#explore-note` was a paragraph explaining what a
probe is with the measured cost of *this* probe inside it, so collapsing it took the number away. The
number is now its own live line above the lesson.

Per-panel state lives in `localStorage` under `hintsOpen`, keyed `data-panel:index` — not by heading,
which is prose someone may reword, and not by DOM order alone, which changes whenever a panel moves.
Flipping the global switch clears the overrides outright: a choice made against the old default means
the opposite under the new one.

Collapsing is a class on the panel, never `hidden` on the paragraph, so the prose stays in the DOM and
find-in-page still reaches it. A panel whose only hints are live gets no button, because a control
that does nothing is worse than none.

## One field gets one control

The by-date chart moved into the narrowing panel. Clicking two points fills *Leave between*; the
panel's own Save writes them. `focusPick`, `focusRange`, `renderFocusControls` and `saveFocus` are
gone, and with them the `dates` panel and its `STEP_OF` entry.

Three things fell out of doing it:

- **`pickFocusDate` must not call `renderNarrowFields`.** That function fills the two boxes from the
  *saved* trip, so calling it on the way to redrawing the band wiped the click that had just happened,
  and the chart showed nothing at all.
- **The band reads the boxes, not the scenario.** One state drawn twice, so a typed date and a clicked
  one behave identically and a half-open pick still shows the day it has.
- **Clear had to grow.** It cleared the return window and the nights band and deliberately left the
  departure window alone, because that had its own *Watch the whole window* button. With that panel
  gone, a narrowing would have been left that nothing on screen could undo.

The copy now says what actually happens: both scheduled sweeps read these boxes off the trip file, so
a date outside them stops being priced and a cheaper day out there will not be found until the range
is widened.

## The panel you decide from

Each leg of the cursor's pick is a row carrying its date, route, airline, price, a **Book** link and
**Follow**. The link is the offer's own `url` from `by-leg`, assigned to `.href` on a node and never
interpolated, like every other link this page draws; a leg the sweep saved no URL for says so, since
an absent link between two present ones reads as a page that failed rather than as a sweep that
recorded a price without one.

`place()` came out of `snapCursor` as `placeCursor`, because the ranking uses it too. A table row can
now be put on the charts, and a pick can be found in the table — matched on leg dates, which is what a
row is keyed by, and never on price, which two different trips can share. A pick the ranking does not
contain says so rather than failing silently: the markers move freely and very often land on a trip
the narrowed, capped ranking never offered.

**The readout grew two lines, and that broke every drag test.** `drag_marker` used `page.mouse`, which
takes viewport coordinates and does not scroll, so the third chart fell below a 1400px viewport and
every drag test asserted against a cursor that had not moved. `chart_point` already scrolled first and
said why in a comment; the helper now does the same.

## Follow it

One line above both panels carries the shared budget and the sentence the tab never said: nothing here
moves when you change the trip or the narrowing. That is true — `watch._admitting` widens the stays to
whatever a candidate pinned and drops the narrowing entirely, per candidate — and it is the question
people arrive with. Both panels used to carry the *whole run's* figure in a badge each, so a step
costing five searches announced five twice; the badges are plain counts now.

*Days you are watching* became *Trips you are following*: each is a whole trip and not a day, and
"days" is what the previous step now calls the departure window. *Pick a day from the last sweep* is a
`<details>` inside that panel rather than a panel of its own — it is the fallback path, since the
ranking only offers combinations that obeyed the stay ranges and the charts offer every combination
there is. It opens itself once when nothing is followed yet, because a folded list is wrong when the
panel above it is empty and this is the only way to fill it.

A followed flight gained a **Book** link built server-side. The watch records a price and never the
offer's URL, so it is a search rebuilt from the three things that define the watch — which is also the
honest link, since what was cheapest four hours ago need not still be.

Its sweep picker turned out never to have been populated by anything. It was labelled, it had an
`onchange` that moved the whole tab, and it silently showed whichever run another step last selected.

## Airports before timestamps

`GET /api/scenarios/{id}/airport-verdicts` walks every run on disk and reports each airport of the
live trip judged by the run that measured it best.

**Chosen per pool, never per airport.** `_rank` scores each airport against the cheapest of its own
pool, so rows taken from different runs would be percentages against different baselines, printed in
one table as though they were comparable. A whole pool comes from one run — the newest that priced the
most of that pool's airports, walking newest-first with a strict `>` so recency breaks ties. Different
pools may come from different runs, which is safe, because pools are ranked independently anyway. Each
block names its run.

Runs of a differently shaped trip are skipped outright, for the reason `_sweep_scenario` exists: pools
are positional, and lining up pool 2 of a two-stop trip with pool 2 of a three-stop one is how a probe
of Prague and Vienna came to be presented as the verdict for a trip flying out of Katowice.

**`never_measured` had to be renamed `not_searched`.** A run with no snapshot is read as having
searched the trip as it stands, so an airport added since gets an ordinary `unproven` row saying
nothing was measured — which is the honest answer, and one the tab already knows how to draw. The
field means only "this pool's airports the chosen run has no row for at all", which is exactly what
the single-run report's field of that name has always meant.

Reading one run on its own is folded shut below. It keeps every notice it had, because two questions
need it: how much of *that* run's plan came back, and which way round it found cheaper.

## What was not done

- **The volatility probe still samples three hardcoded routes** on fixed 2027 dates that need have
  nothing to do with the trip on screen. Left by decision.
- **Non-contiguous departure dates.** `focus_start`/`focus_end` stays a range. The hand-picked case is
  the cursor on the leg charts, and it is now reachable from where the decision is made.
- **The broad sweep still does not stay broad.** This stops the UI claiming otherwise and states the
  cost plainly instead. The fix is a `--ignore-narrowing` flag on the 02:00 command and belongs to
  that feature.
