# Spike: a second price source

**Date:** 2026-08-11
**Question:** pelikan.cz is the only sweep source. Is a second one worth adding, and is it feasible?
**Outcome:** No new sweep source. Verify the shortlist against letuska instead.

## Why ask

One source is one point of failure and one opinion. If pelikan renames a class the whole
watcher goes quiet, and if pelikan simply prices a route badly there is nothing to notice it
against. The candidates considered were kiwi.com, Skyscanner and letuska.cz.

Note that the *breakage* half of that worry is now largely answered by
[`src/sources.py`](../../../src/sources.py) and the Sources tab: a renamed selector is a string
you fix yourself in about a minute. What a second source adds is the second *opinion*.

## kiwi.com — no

`https://www.kiwi.com/en/search/results/PRG/NRT/2027-01-12` looked like a usable deep-link
grammar, which is the thing that makes pelikan affordable to sweep at all.

**It is disallowed.** `https://www.kiwi.com/robots.txt`, in the `User-Agent: *` block that
applies to a personal script, contains 1,291 `Disallow` rules including:

```
Disallow: /search        (and /en/search, /cz/search, … for every locale)
Disallow: /api
Disallow: /flights
Disallow: /deep
```

That is the exact path a search would use. The decision needed no request to a search page and
none was made.

The file does contain `User-Agent: ClaudeBot / Allow: /` near the top, alongside GPTBot and
others. That is a permission granted to a named crawler, not to this project, and reading it as
cover for a personal scraper would be choosing the interpretation that suits us. The wildcard
block is the one that applies.

## Skyscanner — no

Ruled out before the spike, and it has been ruled out here once before. The official API is
partner-only. The RapidAPI mirror was already in this repo and was **deleted rather than left
commented out**: unreachable since its registry entry was disabled, gated behind a 100-call
monthly free tier no sweep could live inside, and it divided prices by 1000 to undo "cents",
turning a genuine 1,200 EUR fare into 1.20. Nothing about that has changed.

## letuska.cz — already the answer, at the right scale

Spiked and rejected as a *sweep* source in a previous session, for reasons that still hold: no
deep-link grammar (`/letenky/PRG/NRT/<date>`, `?from=&to=&date=` and a hash route all 404), so a
search means driving an Angular form through a cookie banner, autocomplete typing and a
Czech-month calendar behind two nested shadow roots. That is ~60–90s per search against
pelikan's ~14s, and a 615-search deep sweep is not affordable at that rate.

But `LetuskaProvider.check_price` works today, and the question worth asking is not "what does
every one of 900 legs cost elsewhere" — it is **"is the one trip I am about to book real?"**
That is 3–6 legs, not 900, and letuska answers it in a few minutes.

Its robots.txt disallows `/searchform`, `/assets/` and `/api/`. The search runs on the public
homepage and results render into it; none of those paths are touched.

## Decision

1. **No new sweep source.** Coverage stays pelikan, and the Sources tab covers the breakage risk
   that motivated most of the ask.
2. **Verify the shortlist on letuska.** After a sweep, re-price the distinct legs of the top few
   itineraries and report where the two sites disagree by more than a few percent. Opt-in per
   trip, never on the sweep's critical path.
3. **Revisit if a source appears with a deep link and permission to use it.** The bar is both, in
   that order.
