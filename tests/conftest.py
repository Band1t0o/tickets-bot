"""Shared test fixtures.

Tests here must never touch the network or launch a browser: the scrapers are
slow (~15s per real search) and the sites are third-party. Provider behaviour is
tested against saved HTML fixtures instead.

That promise was not being kept. `run_sweep_command` takes `notify=True` by
default and two tests here call it without saying otherwise, so every run of the
suite posted fake flights - 10,000 CZK, airline "XX", a URL on example.test - to
whatever real Discord channel `.secrets/discord.json` named. It went unnoticed
because it looks like a message from the app. `no_real_webhook` below closes it
for every test at once rather than at the two call sites, because the next test
to forget is the one nobody will think to check.

The scenario builders live here rather than in each test module because they
used to be copied into four files, and every schema change meant finding all
four. A test that wants a different shape passes overrides.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.scenario import Scenario, Stop

WINDOW_START = date(2027, 1, 5)
WINDOW_END = date(2027, 2, 8)


@pytest.fixture(autouse=True)
def no_real_webhook(tmp_path_factory, monkeypatch):
    """Point every webhook lookup at an empty directory, for every test.

    `SECRETS_DIR` is the relative `Path(".secrets")`, so it resolves against the
    working directory - which, running the suite from the repo root, is the real
    one. Both routes in are closed here: the environment variable the cloud sets,
    and the file the Sources tab writes.

    A test that wants to prove notification behaviour still monkeypatches `post`
    and its own secrets directory, as `test_notify.py` does; those run after this
    and win.
    """
    from src import notify_discord

    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(
        notify_discord, "SECRETS_DIR", tmp_path_factory.mktemp("no-webhook")
    )


@pytest.fixture(autouse=True)
def no_real_gh(monkeypatch):
    """No test may shell out to `gh`, for every test at once.

    Same reasoning as `no_real_webhook` above: closed here rather than at the
    call sites, because the next test to forget is the one nobody will think to
    check. Two ways in, and the second is the quiet one - `cloud_runs.enqueue`
    starts a daemon thread that polls `gh run list` until the lane clears, so a
    test that merely queues something would poll a real GitHub in the background
    for as long as the suite ran.

    A test proving cloud behaviour monkeypatches `_gh` itself; those run after
    this and win.
    """
    from src.web import cloud_runs

    def refuse(*args, **kwargs):
        raise cloud_runs.CloudError("gh is not available in tests")

    monkeypatch.setattr(cloud_runs, "_gh", refuse)
    monkeypatch.setattr(cloud_runs, "_start_worker", lambda: None)


def make_scenario(**overrides) -> Scenario:
    """A two-stop trip: origins -> stop 1 -> stop 2 -> home."""
    defaults = dict(
        id="japan-philippines",
        name="Japan then Philippines",
        origins=["PRG", "VIE", "FRA"],
        stops=[
            Stop(airports=["NRT", "HND", "KIX"], stay_days=(9, 11), label="Japan"),
            Stop(airports=["MNL", "CEB"], stay_days=(9, 11), label="Philippines"),
        ],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    defaults.update(overrides)
    return Scenario(**defaults)


def make_round_trip(**overrides) -> Scenario:
    """One stop and back: the shape that could never produce an itinerary."""
    defaults = dict(
        id="tokyo",
        name="Tokyo return",
        origins=["PRG"],
        stops=[Stop(airports=["NRT"], stay_days=(18, 20), label="Japan")],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        depth="quick",
    )
    defaults.update(overrides)
    return Scenario(**defaults)


def make_three_stop(**overrides) -> Scenario:
    """A shape the old three-block planner could not express at all."""
    defaults = dict(
        id="grand-tour",
        name="Three stops",
        origins=["PRG"],
        stops=[
            Stop(airports=["NRT"], stay_days=(7, 9), label="Japan"),
            Stop(airports=["MNL"], stay_days=(7, 9), label="Philippines"),
            Stop(airports=["BKK"], stay_days=(5, 7), label="Thailand"),
        ],
        window_start=WINDOW_START,
        window_end=date(2027, 3, 15),
        depth="quick",
    )
    defaults.update(overrides)
    return Scenario(**defaults)


@pytest.fixture
def make_leg():
    """Build a Leg with sensible defaults; override only what a test cares about."""
    # Imported lazily so test collection works before src.models defines Leg.
    from src.models import Leg

    def _make(
        origin: str = "PRG",
        destination: str = "NRT",
        depart_date: date = date(2027, 1, 12),
        airline: str = "Qatar Airways",
        flight_number: str = "QR8100",
        stops: int = 1,
        price_amount: float = 14480.0,
        price_currency: str = "CZK",
        url: str = "https://www.pelikan.cz/cs/letenky/example/",
        provider: str = "PELIKAN",
        checked_bag: bool | None = None,
        observed_at: str | None = None,
    ) -> Leg:
        return Leg(
            provider=provider,
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            airline=airline,
            flight_number=flight_number,
            stops=stops,
            price_currency=price_currency,
            price_amount=price_amount,
            url=url,
            checked_bag=checked_bag,
            observed_at=observed_at,
        )

    return _make
