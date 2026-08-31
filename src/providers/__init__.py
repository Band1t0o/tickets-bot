"""Providers that a sweep can drive.

letuska.cz is deliberately absent. It has no deep-link grammar, so a search
means driving an Angular form through two nested shadow roots and waiting for
results, where pelikan answers in ~14s from a constructed URL. Sweeping it is
not affordable; it is reachable as an on-demand second opinion through
`python -m src.cli check-price`.

Skyscanner-via-RapidAPI was removed rather than left commented out: unreachable
since its registry entry was disabled, gated behind a 100-call monthly free
tier no sweep could live inside, and it divided prices by 1000 to undo "cents",
turning a genuine 1,200 EUR fare into 1.20.

DemoStaticProvider went the same way, and a `REGISTRY` mapping names to classes
with it. Both were reachable only through `src.cli scrape --provider`, which was
deleted with the legacy pipeline, so nothing had constructed a provider by name
for months. `run_sweep` takes the provider as an argument and defaults to
pelikan; tests pass their own fakes. A registry with one entry is a lookup
table for a choice nobody makes.
"""
from __future__ import annotations

from .base import BaseProvider

__all__ = ["BaseProvider"]
