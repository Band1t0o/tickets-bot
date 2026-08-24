# Trip narrowing, and every leg priced on its own — design

*2026-08-23*

## Why

The watcher maps a trip well and cannot help you decide one.

**A focus only says when you leave.** `focus_start`/`focus_end` bounds the first leg and
`planner._leg_window` derives the rest through the stay ranges. By the time a broad sweep has been
read a few times the decision has narrowed by things no sweep knows — when leave starts, when work
starts again — and what is left is three constraints at once: leave 8-12 January, be home 4-8
February, about 24 nights. Two of those three cannot be said.

**Nothing constrains the total.** `combine._stay_ok` checks each stop in isolation, so against Japan
10-13 and Philippines 8-13 an 18-night trip and a 26-night one are equally valid. "About 24, and I
do not much mind how it splits" is a single constraint that neither range expresses and both
together still cannot, and it is usually the one actually being held. It is also what lets 14+10 be
compared against 12+12 instead of one of them being ruled out before it is priced.

**Every chart is about the total.** That is the right shape for choosing a departure date and the
wrong one for choosing a trip. A total cannot show that the flight out is flat all January while the
one home has a single cheap Thursday, and that reading is what lets a person pick a combination the
ranking would never surface — because the ranking may only surface combinations obeying the stays.

**Seven tabs, two of them the same panel twice.** Prices and Watch both drew a price history and
neither said which question it answered.

**Outcome:** the trip is narrowed once, in the schema, so the nightly sweep spends its searches
where the decision already is; the per-leg charts let a combination be picked by hand, including one
the rules forbid; the navigation reads as the three things you actually do.

## The narrowing

Three fields on `Scenario`, intersected rather than chosen between:

| Field | Bounds |
|---|---|
| `focus_start` / `focus_end` | when the **first** leg departs (existing) |
| `return_focus_start` / `return_focus_end` | when the **final** leg departs |
| `total_days` | nights from the first leg's departure to the final leg's |

`total_days` is measured to the final leg's *departure*, because that is the date on a ticket. Its
bounds are `min_span_days` / `max_span_days` — new properties that slice `stops[:leg_count - 1]`.
Deliberately **not** `min_trip_days`, which sums every stop and so over-counts a one-way chain by
the stay at its last one; using it as the floor would reject reachable bands.

`planner._leg_window` gains the two new sources. Backward propagation needs `max_stay_after` for the
early bound and `remaining_min_stay` for the late one; getting them the wrong way round widens the
plan silently instead of narrowing it, which is why both are named rather than inlined.

Measured on `japan-philippines` with the window widened to 8 February, at `deep`:

| | Searches | Time |
|---|---:|---:|
| whole window | 198 | ~49 min |
| leave 8-12 Jan, home 4-8 Feb, 22-26 nights | **44** | **~11 min** |

Nothing is guessed at the edges. 8 January is inside the departure window and never searched: at the
longest stays it puts you home on the 3rd, a day before the return window opens. That is the same
orphan-search bug `_leg_window` was written for, arriving from the other end.

**A band alone does not narrow the plan, and that is not an oversight.** It is a relative
constraint — with neither end pinned the first leg may depart anywhere in the window, so every date
of every later leg is reachable from some first-leg date. It constrains which chains are valid, not
which searches are worth running. Pin either end and it narrows the plan too.

**An unsatisfiable narrowing is refused by name**, because the alternative is a sweep that spends
every search, chains nothing, and reports the emptiness the site gives when it has no seats:

```
30-35 nights away is unreachable: the stays allow 18-26
(10-13 at Japan + 8-13 at Philippines); change the nights or the stays
```

## Applied inside the traversal, never to the result

`combine_all(..., narrowed=True)`, and all three parts of the narrowing, not two. Applying the
return window and the nights band without the focus was caught by driving the real app: *Snap to the
cheapest that fits* landed on a 14 January departure while the departure window said 8-12 January,
and the readout called it "fits every rule you set".

Measured on the committed 21 August sweep: with the return window at 4-8 February the unnarrowed
traversal kept a 3 February trip and pruned an **identically priced** 6 February one, so filtering
the finished list would have reported nothing available at all. This is
the same reason `from_airport` is applied inside the traversal, and the reason `_combination`'s memo
key holds the narrowing rather than a shared result.

That memo key also had to grow to hold everything `_sweep_scenario` takes from the *live* trip.
Editing a trip does not touch its sweeps, so a key made of the legs file alone handed back the
previous answer: setting a nights band and then widening it returned the narrower result forever,
and read as the sweep having found nothing.

Reading is separable from searching. `?window=all` on `/results`, `/by-date` and `/candidates`
ignores the narrowing, so a sweep committed months before it existed stays readable and what the
narrowing costs stays visible. The narrowing comes from the **live** trip, not the sweep's snapshot:
the sweeps worth narrowing are precisely the ones that ran before you narrowed.

## Every leg on its own

`GET /api/sweeps/{id}/{stamp}/by-leg` returns, per leg, the cheapest offer per date across the whole
pool (naming the pair that won it) and the same broken out per airport pair. Pools come from
`leg_pools` — the property the planner and combiner both walk — so a route that sold nothing still
appears, as nothing.

Built from `searches.jsonl` as well as `legs.jsonl`, because **a date that sold nothing and a date
never asked about are opposite facts** that would otherwise draw as the same blank column. The first
gets a tick on the floor of the plot; the second gets nothing at all.

`lineChart` grew three things rather than a third chart type: `opts.domain` forces the x categories
so several charts can be stacked and read down a vertical slice; a `null` value is a gap that breaks
the line rather than being bridged across it; and `opts.marker` is a draggable date cursor.

## The cursor, and why it does not enforce

Markers move independently and nothing stops one landing where the stay ranges forbid. The stay
ranges exist so a sweep knows what to price; a range typed a month ago is not evidence that a stay
is wrong. If fifteen nights in Japan is four thousand cheaper, that is worth knowing, and the tool
that shows you the saving must not be the one that hides it.

*Snap to the cheapest that fits* takes its answer from `/results?window=narrow` rather than working
one out on the chart. Two pieces of code answering "which is cheapest" is two answers, and the one
on the chart would be the one nobody could check.

The one pick the panel refuses is a leg departing before the one before it — not a preference, and
nothing downstream could price it. It is refused on the page rather than by the server a click
later.

**Following a rule-breaking pick had to be made possible.** `_validate_watches` checked the stay
ranges, on grounds that were sound until now: the combiner could never close such a chain, so the
watch would report nothing every four hours, which looks exactly like the site having no seats.
`watch._admitting` now widens the stays to whatever a candidate pinned before pricing it — **per
candidate**, so one watch's widening cannot decide another's price — and drops the narrowing
entirely, since a watch's dates are already chosen and leaving it on would blank every followed trip
the moment the window moved. With that in place the validation was protecting against nothing, and
it was deleted. Ordering is still checked.

## Navigation

| Step | Holds | Answers |
|---|---|---|
| **Map it out** | Search, Explore | what trip is this, which airports are worth pricing |
| **Narrow it down** | narrowing, per-leg charts, by-date chart, itinerary table | *which days* |
| **Follow it** | followed flights and trips, best total over time, probe | *is it moving* |
| ⚙ **Setup** | Discord, night sweep, Cloud, Sources | none of the above |

Split by question rather than by data: that is what resolves Prices and Watch both drawing a
history. Sections carry `data-step` and several share one; `data-panel` stays as each panel's own
name because that is what every renderer, test and error box already addresses them by. `showTab`
accepts either.

`renderPrices` split into `renderByDate` and `renderHistory` — the two charts now live in different
steps, and a chart drawn into a hidden section measures its container at zero width.

## What was not done

- No duration, layover or departure-time constraints. Still absent from the whole domain model.
- `total_days` is nights between departures, not between wheels-down and wheels-up. Close enough to
  "24 days of leave" to be useful and honest about which it measures.
- The narrowing does not reach `alerts.py` explicitly; it inherits the combiner default, which is
  correct — Discord should not report trips you cannot take — but is untested there.
- **The broad nightly sweep does not stay broad, and this makes that worse.** The README claimed
  02:00 covers the whole window while 13:00 runs the focus. It does not: `--focused` picks which
  *trips* run in the afternoon slot, both slots then run the same `src.cli sweep`, and
  `plan_searches` reads the focus off the file. That was already true of `focus` alone (198 → 80
  searches); with a return window and a nights band it is 198 → 44 on **both** runs, so a narrowed
  trip would never discover that leaving a week earlier is thousands cheaper. Left alone because it
  is a pre-existing defect in a different feature and fixing it means deciding how the two slots
  should differ — most likely a `--ignore-narrowing` flag on the 02:00 command. The README now
  records the gap instead of the claim.
