"""Tests for tracking a handful of pinned candidate trips over time.

A watch answers a narrower question than a sweep: not "what is this window
worth" but "is *this* trip, on *these* days, moving". So the things under test
are the ones that make a series honest - that a run which found nothing records
a gap rather than a zero, that a starved run is marked rather than plotted, and
that a ping means a real fall and not the site rounding by six crowns.
"""
from __future__ import annotations

import json
from datetime import date

from src.models import Leg
from src.scenario import LegWatch, Preference, Scenario, Stop
from tests.conftest import make_scenario

CANDIDATE = [date(2027, 1, 10), date(2027, 1, 20), date(2027, 1, 30)]
OTHER = [date(2027, 1, 12), date(2027, 1, 22), date(2027, 2, 1)]


def trip(*candidates, slack=0, **overrides) -> Scenario:
    defaults = dict(
        id="jp-ph",
        origins=["PRG"],
        stops=[
            Stop(airports=["NRT"], stay_days=(9, 11), label="Japan"),
            Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
        ],
        preferences=[
            # No slack unless a test asks for it: most of these are about what a
            # pinned trip records, and the slack has its own tests.
            Preference(depart_dates=list(c), slack_days=slack)
            for c in (candidates or [CANDIDATE])
        ],
        bag_estimate=0,
    )
    defaults.update(overrides)
    return make_scenario(**defaults)


def leg(origin, destination, depart, price) -> Leg:
    return Leg(
        provider="TEST", origin=origin, destination=destination, depart_date=depart,
        airline="QR", flight_number=None, stops=1, price_currency="CZK",
        price_amount=price, url="", checked_bag=True,
    )


def legs_for(dates, prices=(12000.0, 4000.0, 14000.0)):
    return [
        leg("PRG", "NRT", dates[0], prices[0]),
        leg("NRT", "MNL", dates[1], prices[1]),
        leg("MNL", "PRG", dates[2], prices[2]),
    ]


def status(coverage=1.0, legs_per_search=9.5):
    return {"coverage": coverage, "legs_per_search": legs_per_search, "state": "done"}


# ------------------------------------------------------------- observations


def test_an_observation_records_what_the_candidate_costs_now(tmp_path):
    from src.watch import record_observations

    rows = record_observations(legs_for(CANDIDATE), trip(), status(), tmp_path)
    assert len(rows) == 1
    assert rows[0]["depart_date"] == "2027-01-10"
    assert rows[0]["total"] == 30000
    assert rows[0]["route"] == "PRG → NRT → MNL → PRG"
    assert rows[0]["pinned_dates"] == ["2027-01-10", "2027-01-20", "2027-01-30"]


def test_a_candidate_that_found_nothing_records_no_price_rather_than_zero(tmp_path):
    """A run that came back empty is breakage, not a free flight.

    Averaging a 0 into the series would be nonsense, and drawing it would put
    the cheapest trip you ever saw at the bottom of the chart.
    """
    from src.watch import record_observations

    rows = record_observations([], trip(), status(), tmp_path)
    assert rows[0]["total"] is None


def test_observations_append_rather_than_replace(tmp_path):
    from src.watch import record_observations

    record_observations(legs_for(CANDIDATE), trip(), status(), tmp_path)
    record_observations(
        legs_for(CANDIDATE, (11000.0, 4000.0, 14000.0)), trip(), status(), tmp_path
    )
    lines = (tmp_path / "observations.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["total"] for line in lines] == [30000, 29000]


def test_each_candidate_gets_its_own_row(tmp_path):
    from src.watch import record_observations

    rows = record_observations(
        legs_for(CANDIDATE) + legs_for(OTHER, (9000.0, 4000.0, 14000.0)),
        trip(CANDIDATE, OTHER),
        status(),
        tmp_path,
    )
    assert {r["depart_date"]: r["total"] for r in rows} == {
        "2027-01-10": 30000,
        "2027-01-12": 27000,
    }


def test_an_observation_carries_the_health_of_the_run_that_made_it(tmp_path):
    from src.watch import record_observations

    rows = record_observations(legs_for(CANDIDATE), trip(), status(0.5, 2.9), tmp_path)
    assert rows[0]["coverage"] == 0.5
    assert rows[0]["comparable"] is False


# ------------------------------------------------------------------ report


def test_the_report_gives_each_candidate_its_own_series(tmp_path):
    from src.watch import record_observations, watch_report

    record_observations(legs_for(CANDIDATE), trip(), status(), tmp_path)
    record_observations(
        legs_for(CANDIDATE, (11000.0, 4000.0, 14000.0)), trip(), status(), tmp_path
    )

    report = watch_report(tmp_path)
    candidate = report["candidates"]["2027-01-10"]
    assert [point["total"] for point in candidate["series"]] == [30000, 29000]
    assert candidate["latest"] == 29000
    assert candidate["net_change"] == -1000


def test_the_report_states_the_move_as_a_percentage(tmp_path):
    from src.watch import record_observations, watch_report

    record_observations(legs_for(CANDIDATE), trip(), status(), tmp_path)
    record_observations(
        legs_for(CANDIDATE, (15000.0, 4000.0, 14000.0)), trip(), status(), tmp_path
    )
    candidate = watch_report(tmp_path)["candidates"]["2027-01-10"]
    assert candidate["net_change_pct"] == 10.0


def test_a_starved_run_is_kept_but_not_counted(tmp_path):
    """The gap in the record is worth seeing; the price in it is not a price.

    A run the site refused half of reports a cheapest total that says more
    about the scraper than the market, and averaging it into the move would
    chart scraper health - the mistake the history chart made for four sweeps.
    """
    from src.watch import record_observations, watch_report

    record_observations(legs_for(CANDIDATE), trip(), status(), tmp_path)
    record_observations(
        legs_for(CANDIDATE, (99000.0, 4000.0, 14000.0)), trip(), status(0.3, 2.1), tmp_path
    )

    candidate = watch_report(tmp_path)["candidates"]["2027-01-10"]
    assert len(candidate["series"]) == 2
    assert candidate["series"][1]["comparable"] is False
    assert candidate["latest"] == 30000  # the last trustworthy one


def test_an_empty_directory_reports_nothing_rather_than_failing(tmp_path):
    from src.watch import watch_report

    assert watch_report(tmp_path)["candidates"] == {}


# ------------------------------------------------------------------- drops


def test_a_real_fall_is_reported(tmp_path):
    from src.watch import drops, record_observations, watch_report

    record_observations(legs_for(CANDIDATE), trip(), status(), tmp_path)
    assert drops(watch_report(tmp_path), trip(), tmp_path) == []

    record_observations(
        legs_for(CANDIDATE, (8000.0, 4000.0, 14000.0)), trip(), status(), tmp_path
    )
    found = drops(watch_report(tmp_path), trip(), tmp_path)
    assert len(found) == 1
    assert found[0]["depart_date"] == "2027-01-10"
    assert found[0]["total"] == 26000
    assert found[0]["previous_best"] == 30000


def test_the_site_rounding_by_a_few_crowns_is_not_news(tmp_path):
    """Sub-1% moves are the site rounding, not the market.

    Counting every non-zero change once made the steadiest probe route report
    the highest volatility of the three, on six-crown twitches.
    """
    from src.watch import drops, record_observations, watch_report

    record_observations(legs_for(CANDIDATE), trip(), status(), tmp_path)
    drops(watch_report(tmp_path), trip(), tmp_path)
    record_observations(
        legs_for(CANDIDATE, (11950.0, 4000.0, 14000.0)), trip(), status(), tmp_path
    )
    assert drops(watch_report(tmp_path), trip(), tmp_path) == []


def test_a_drip_of_small_falls_eventually_adds_up_to_news(tmp_path):
    """Each step is below the floor; together they are a real fall.

    The level a drop is measured against only moves when something is actually
    reported, so a slow slide cannot stay silent forever by never taking a big
    enough step.
    """
    from src.watch import drops, record_observations, watch_report

    found = []
    for fare in (12000.0, 11950.0, 11900.0, 11850.0, 11500.0):
        record_observations(
            legs_for(CANDIDATE, (fare, 4000.0, 14000.0)), trip(), status(), tmp_path
        )
        found = drops(watch_report(tmp_path), trip(), tmp_path)
    assert len(found) == 1
    assert found[0]["total"] == 29500


def test_the_same_fall_is_not_reported_twice(tmp_path):
    from src.watch import drops, record_observations, watch_report

    record_observations(legs_for(CANDIDATE), trip(), status(), tmp_path)
    drops(watch_report(tmp_path), trip(), tmp_path)
    record_observations(
        legs_for(CANDIDATE, (8000.0, 4000.0, 14000.0)), trip(), status(), tmp_path
    )
    assert len(drops(watch_report(tmp_path), trip(), tmp_path)) == 1
    assert drops(watch_report(tmp_path), trip(), tmp_path) == []


def test_a_starved_run_never_raises_an_alert(tmp_path):
    from src.watch import drops, record_observations, watch_report

    record_observations(legs_for(CANDIDATE), trip(), status(), tmp_path)
    drops(watch_report(tmp_path), trip(), tmp_path)
    record_observations(
        legs_for(CANDIDATE, (1000.0, 1000.0, 1000.0)), trip(), status(0.2, 1.5), tmp_path
    )
    assert drops(watch_report(tmp_path), trip(), tmp_path) == []


def test_the_price_you_picked_it_at_is_what_the_first_run_is_judged_against(tmp_path):
    """Otherwise the first observation can never be news, however far it fell.

    You add a candidate having just seen it at 30,000; the watch runs an hour
    later and it is 26,000. That is exactly the message worth having.
    """
    from src.watch import drops, record_observations, watch_report

    watched = trip()
    watched.preferences[0] = Preference(depart_dates=list(CANDIDATE), added_price=30000.0)
    record_observations(
        legs_for(CANDIDATE, (8000.0, 4000.0, 14000.0)), watched, status(), tmp_path
    )
    found = drops(watch_report(tmp_path), watched, tmp_path)
    assert len(found) == 1
    assert found[0]["previous_best"] == 30000.0


# ------------------------------------------------------------- the command


def _watched_repo(tmp_path, monkeypatch, **overrides):
    """A scratch repo with one watched trip, since the CLI reads scenarios/."""
    from src.scenario import save_scenario

    monkeypatch.chdir(tmp_path)
    (tmp_path / "scenarios").mkdir()
    save_scenario(trip(**overrides), tmp_path / "scenarios")
    return tmp_path


class OneLegProvider:
    """Prices whatever it is asked for, so a watch run finds a whole trip."""

    NAME = "FAKE"

    def __init__(self, price=4000.0):
        self.price = price
        self.calls = []

    def search_leg(self, page, origin, destination, depart, ret=None, adults=1):
        self.calls.append((origin, destination, depart))
        return [leg(origin, destination, depart, self.price)]


def test_the_command_writes_an_observation_per_candidate(tmp_path, monkeypatch):
    from src.cli import run_watch_command

    _watched_repo(tmp_path, monkeypatch)
    run_watch_command(
        "jp-ph", provider=OneLegProvider(), data_dir=tmp_path / "data",
        delay_s=0, notify=False,
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "data" / "watch" / "jp-ph" / "observations.jsonl")
        .read_text(encoding="utf-8").strip().splitlines()
    ]
    assert [row["depart_date"] for row in rows] == ["2027-01-10"]
    assert rows[0]["total"] == 12000


def test_the_command_searches_only_the_pinned_dates(tmp_path, monkeypatch):
    from src.cli import run_watch_command

    _watched_repo(tmp_path, monkeypatch)
    provider = OneLegProvider()
    run_watch_command(
        "jp-ph", provider=provider, data_dir=tmp_path / "data", delay_s=0, notify=False
    )
    assert {call[2] for call in provider.calls} == set(CANDIDATE)


def test_watching_nothing_is_refused_rather_than_run_empty(tmp_path, monkeypatch):
    """A run of no searches would write a directory, report coverage 0.0 and
    look like a watch that failed, rather than a trip nobody asked to watch."""
    import pytest

    from src.cli import run_watch_command

    _watched_repo(tmp_path, monkeypatch, preferences=[])
    with pytest.raises(SystemExit):
        run_watch_command(
            "jp-ph", provider=OneLegProvider(), data_dir=tmp_path / "data", notify=False
        )


# ------------------------------------------------------------------- slack
#
# A preference prices the days either side of the ones it pinned, and the whole
# discipline of it is that the extra days must never leak into the series. The
# line on the chart is the trip that was chosen; the cheaper neighbour is
# reported beside it, by name, as an offer to move.


def test_the_series_stays_on_the_pinned_trip_when_a_neighbour_is_cheaper(tmp_path):
    """The one thing a followed price must never do is move.

    Without the positional date filter, `best_by_date` hands back the cheapest
    chain leaving on the pinned morning - which, once the slack has priced the
    later legs on other days, is a different trip. It would read as your trip
    falling by four thousand, and there would be nothing on screen to say the
    dates had changed underneath you.
    """
    from src.watch import record_observations

    legs = legs_for(CANDIDATE)
    # Same departure day, second and third legs two days later and far cheaper.
    legs += [
        leg("NRT", "MNL", date(2027, 1, 22), 1000.0),
        leg("MNL", "PRG", date(2027, 2, 1), 1000.0),
    ]

    rows = record_observations(legs, trip(slack=2), status(), tmp_path)
    assert rows[0]["total"] == 30000
    assert rows[0]["found_dates"] == ["2027-01-10", "2027-01-20", "2027-01-30"]


def test_the_cheaper_trip_inside_the_slack_is_reported_beside_it(tmp_path):
    """The whole reason the slack is paid for.

    "The same trip two days later is 16,000 cheaper" is the answer a decision
    is waiting on, and no pinned watch could ever give it.
    """
    from src.watch import record_observations

    legs = legs_for(CANDIDATE) + legs_for(
        [date(2027, 1, 12), date(2027, 1, 22), date(2027, 2, 1)],
        (2000.0, 2000.0, 2000.0),
    )

    row = record_observations(legs, trip(slack=2), status(), tmp_path)[0]
    assert row["total"] == 30000
    assert row["nearby_total"] == 6000
    assert row["nearby_dates"] == ["2027-01-12", "2027-01-22", "2027-02-01"]


def test_a_preference_already_on_the_cheapest_day_reports_no_saving(tmp_path):
    """`nearby` includes the pinned days themselves, on purpose.

    Excluding them would make the field "the best of the days you are not on",
    so a preference sitting exactly where it should be would still be offered a
    move - to something dearer than what it already has.
    """
    from src.watch import record_observations

    legs = legs_for(CANDIDATE) + legs_for(
        [date(2027, 1, 12), date(2027, 1, 22), date(2027, 2, 1)],
        (20000.0, 20000.0, 20000.0),
    )

    row = record_observations(legs, trip(slack=2), status(), tmp_path)[0]
    assert row["nearby_total"] == row["total"] == 30000


def test_no_slack_asks_no_second_question(tmp_path):
    """At zero slack there is no neighbourhood, so there is nothing to report.

    None rather than the pinned total, which would read as "nothing nearby is
    cheaper" - a claim this run did not make and did not pay to find out.
    """
    from src.watch import record_observations

    row = record_observations(legs_for(CANDIDATE), trip(slack=0), status(), tmp_path)[0]
    assert row["nearby_total"] is None


def test_a_pinned_leg_that_went_unpriced_records_a_gap_and_never_a_zero(tmp_path):
    """A chain that cannot be built is breakage, not a free flight.

    Sharper with slack than without it: the run did find legs, and plenty of
    them, so "we have prices" is true while "we have a price for your trip" is
    not. Filling the hole from a neighbouring day would be the series moving.
    """
    from src.watch import record_observations

    legs = [
        leg("PRG", "NRT", CANDIDATE[0], 12000.0),
        leg("NRT", "MNL", CANDIDATE[1], 4000.0),
        # Home priced on a day of the slack, but not on the one that was pinned.
        # 29 Jan is 9 nights after the second leg, so it chains; 28 Jan would be
        # 8 and the stay ranges would refuse it, which is a different failure.
        leg("MNL", "PRG", date(2027, 1, 29), 14000.0),
    ]

    row = record_observations(legs, trip(slack=2), status(), tmp_path)[0]
    assert row["total"] is None
    assert row["nearby_total"] == 30000


def test_a_preference_carries_a_name_it_can_be_told_apart_by(tmp_path):
    from src.watch import record_observations

    row = record_observations(legs_for(CANDIDATE), trip(), status(), tmp_path)[0]
    # 10 nights in Japan, 10 in the Philippines, leaving on 10 January.
    assert row["label"] == "10+10 from 10 Jan"


# --------------------------------------------------------------- leg watches
#
# A trip watch answers "is this trip moving" for 21 searches; a leg watch
# answers "is this ticket moving" for one. The tests below are about the same
# honesty properties as the trip ones - a gap is not a zero, a starved run is
# marked not plotted, a ping is a real fall - plus the one thing only a leg
# watch has to get right: this site substitutes nearby dates, and a price for
# the 23rd is not a price for the 22nd.

WATCHED_LEG = LegWatch(origin="PRG", destination="NRT", depart_date=date(2027, 1, 10))


def leg_trip(*watched, **overrides) -> Scenario:
    defaults = dict(
        id="jp-ph",
        origins=["PRG"],
        stops=[
            Stop(airports=["NRT"], stay_days=(9, 11), label="Japan"),
            Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
        ],
        preferences=[],
        leg_watches=list(watched or [WATCHED_LEG]),
        bag_estimate=0,
    )
    defaults.update(overrides)
    return make_scenario(**defaults)


def test_a_watched_leg_records_what_that_ticket_costs_now(tmp_path):
    from src.watch import record_leg_observations

    rows = record_leg_observations(
        [leg("PRG", "NRT", date(2027, 1, 10), 12000.0)], leg_trip(), status(), tmp_path
    )
    assert len(rows) == 1
    assert rows[0]["key"] == "PRG-NRT@2027-01-10"
    assert rows[0]["route"] == "PRG→NRT"
    assert rows[0]["price"] == 12000
    assert rows[0]["exact"] is True
    assert rows[0]["found_date"] == "2027-01-10"


def test_a_leg_that_found_nothing_records_a_gap_and_never_a_zero(tmp_path):
    from src.watch import record_leg_observations

    rows = record_leg_observations([], leg_trip(), status(), tmp_path)
    assert rows[0]["price"] is None


def test_a_substituted_date_is_recorded_as_the_day_it_really_is(tmp_path):
    """The site answers 22 January with the 23rd.

    Recording that as the 22nd prices a flight you cannot buy on the day you
    asked about.
    """
    from src.watch import record_leg_observations

    rows = record_leg_observations(
        [leg("PRG", "NRT", date(2027, 1, 11), 11000.0)], leg_trip(), status(), tmp_path
    )
    assert rows[0]["price"] == 11000
    assert rows[0]["found_date"] == "2027-01-11"
    assert rows[0]["exact"] is False


def test_the_day_asked_about_wins_even_when_a_neighbour_is_cheaper(tmp_path):
    """A watch asks what *this* day costs.

    Quietly answering about another one is how a series stops meaning anything.
    """
    from src.watch import record_leg_observations

    rows = record_leg_observations(
        [
            leg("PRG", "NRT", date(2027, 1, 10), 12000.0),
            leg("PRG", "NRT", date(2027, 1, 11), 8000.0),
        ],
        leg_trip(),
        status(),
        tmp_path,
    )
    assert rows[0]["price"] == 12000
    assert rows[0]["exact"] is True


def test_a_date_far_from_the_one_asked_about_is_not_used_at_all(tmp_path):
    from src.watch import record_leg_observations

    rows = record_leg_observations(
        [leg("PRG", "NRT", date(2027, 1, 25), 5000.0)], leg_trip(), status(), tmp_path
    )
    assert rows[0]["price"] is None


def test_leg_observations_go_in_their_own_file(tmp_path):
    """Two workflows appending to different files never conflict on a rebase."""
    from src.watch import record_leg_observations, record_observations

    record_observations(legs_for(CANDIDATE), trip(), status(), tmp_path)
    record_leg_observations(
        [leg("PRG", "NRT", date(2027, 1, 10), 12000.0)], leg_trip(), status(), tmp_path
    )
    assert (tmp_path / "observations.jsonl").exists()
    assert (tmp_path / "leg-observations.jsonl").exists()


def test_a_leg_series_reports_how_far_it_has_moved(tmp_path):
    from src.watch import leg_report, record_leg_observations

    for price in (12000.0, 11500.0, 10800.0):
        record_leg_observations(
            [leg("PRG", "NRT", date(2027, 1, 10), price)], leg_trip(), status(), tmp_path
        )
    summary = leg_report(tmp_path)["legs"]["PRG-NRT@2027-01-10"]
    assert summary["observations"] == 3
    assert summary["first"] == 12000
    assert summary["latest"] == 10800
    assert summary["low"] == 10800
    assert summary["net_change"] == -1200
    assert summary["net_change_pct"] == -10.0


def test_a_starved_run_is_kept_in_the_leg_series_but_not_counted(tmp_path):
    from src.watch import leg_report, record_leg_observations

    record_leg_observations(
        [leg("PRG", "NRT", date(2027, 1, 10), 12000.0)], leg_trip(), status(), tmp_path
    )
    record_leg_observations(
        [leg("PRG", "NRT", date(2027, 1, 10), 3000.0)],
        leg_trip(),
        status(legs_per_search=2.9),
        tmp_path,
    )
    summary = leg_report(tmp_path)["legs"]["PRG-NRT@2027-01-10"]
    assert len(summary["series"]) == 2
    assert summary["series"][-1]["comparable"] is False
    assert summary["latest"] == 12000, "a refused run must not become the headline"


def test_a_leg_that_falls_far_enough_is_reported(tmp_path):
    from src.watch import leg_drops, leg_report, record_leg_observations

    scenario = leg_trip()
    record_leg_observations(
        [leg("PRG", "NRT", date(2027, 1, 10), 12000.0)], scenario, status(), tmp_path
    )
    assert leg_drops(leg_report(tmp_path), scenario, tmp_path) == [], "first run sets the level"

    record_leg_observations(
        [leg("PRG", "NRT", date(2027, 1, 10), 10000.0)], scenario, status(), tmp_path
    )
    fell = leg_drops(leg_report(tmp_path), scenario, tmp_path)
    assert len(fell) == 1
    assert fell[0]["route"] == "PRG→NRT"
    assert fell[0]["drop"] == 2000
    assert fell[0]["drop_pct"] == 16.7


def test_a_few_crowns_off_a_leg_is_not_worth_a_message(tmp_path):
    from src.watch import leg_drops, leg_report, record_leg_observations

    scenario = leg_trip()
    for price in (12000.0, 11950.0):
        record_leg_observations(
            [leg("PRG", "NRT", date(2027, 1, 10), price)], scenario, status(), tmp_path
        )
    assert leg_drops(leg_report(tmp_path), scenario, tmp_path) == []


def test_reporting_a_leg_drop_twice_says_it_once(tmp_path):
    from src.watch import leg_drops, leg_report, record_leg_observations

    scenario = leg_trip()
    record_leg_observations(
        [leg("PRG", "NRT", date(2027, 1, 10), 12000.0)], scenario, status(), tmp_path
    )
    # Sets the level and says nothing, exactly as `drops` does: with no price it
    # was picked at, there is nothing yet to have fallen from.
    assert leg_drops(leg_report(tmp_path), scenario, tmp_path) == []

    record_leg_observations(
        [leg("PRG", "NRT", date(2027, 1, 10), 10000.0)], scenario, status(), tmp_path
    )
    assert len(leg_drops(leg_report(tmp_path), scenario, tmp_path)) == 1
    # Idempotent: the level is recorded on a report, so a second call has
    # nothing new to say about the same observation.
    assert leg_drops(leg_report(tmp_path), scenario, tmp_path) == []


def test_the_price_a_leg_was_picked_at_seeds_the_level(tmp_path):
    """Add a leg at 12,000 and find it at 10,000 an hour later.

    That is precisely the message worth having, and waiting a run to say it
    wastes it.
    """
    from src.watch import leg_drops, leg_report, record_leg_observations

    scenario = leg_trip(
        LegWatch(
            origin="PRG",
            destination="NRT",
            depart_date=date(2027, 1, 10),
            added_price=12000.0,
        )
    )
    record_leg_observations(
        [leg("PRG", "NRT", date(2027, 1, 10), 10000.0)], scenario, status(), tmp_path
    )
    assert len(leg_drops(leg_report(tmp_path), scenario, tmp_path)) == 1


def test_a_leg_and_a_trip_cannot_overwrite_each_others_recorded_best(tmp_path):
    """They share one best.json, and a collision silences one of them.

    A leg watched on 10 January and a trip departing 10 January are the case:
    both would key on that date if the names were not namespaced apart.
    """
    from src.watch import (
        drops,
        leg_drops,
        leg_report,
        record_leg_observations,
        record_observations,
        watch_report,
    )

    combined = trip()
    combined.leg_watches = [WATCHED_LEG]

    record_observations(legs_for(CANDIDATE), combined, status(), tmp_path)
    record_leg_observations(
        [leg("PRG", "NRT", date(2027, 1, 10), 12000.0)], combined, status(), tmp_path
    )
    drops(watch_report(tmp_path), combined, tmp_path)
    leg_drops(leg_report(tmp_path), combined, tmp_path)

    recorded = json.loads((tmp_path / "best.json").read_text(encoding="utf-8"))
    assert "watch:2027-01-10" in recorded
    assert "legwatch:PRG-NRT@2027-01-10" in recorded
    assert recorded["watch:2027-01-10"]["best_total"] == 30000
    assert recorded["legwatch:PRG-NRT@2027-01-10"]["best_total"] == 12000

# --------------------------------------------------- hand-picked candidates
#
# The per-leg charts let a trip be assembled by dragging, including one the stay
# ranges forbid. Saving such a watch is only half of it: if the run then refused
# to chain it, the series would read "nothing found" every four hours, which
# looks exactly like the site having no seats. These are the other half.

HAND_PICKED = [date(2027, 1, 10), date(2027, 1, 14), date(2027, 1, 24)]


def test_a_watch_outside_the_stay_ranges_is_still_priced(tmp_path):
    """4 nights in Japan against a 9-11 stay: chained anyway, and totalled."""
    from src.watch import record_observations

    rows = record_observations(
        legs_for(HAND_PICKED), trip(HAND_PICKED), status(), tmp_path
    )
    assert rows[0]["total"] == 30000
    assert rows[0]["found_dates"] == [d.isoformat() for d in HAND_PICKED]


def test_widening_for_one_candidate_does_not_reprice_another(tmp_path):
    """Each candidate is chained against its own widened trip, not a shared one.

    One traversal widened by every watch at once would let this candidate's
    four-night Japan stay become available to the legal one, and the legal one
    would start reporting a trip its owner never picked.
    """
    from src.watch import record_observations

    legs = legs_for(CANDIDATE) + legs_for(HAND_PICKED, prices=(1000.0, 1000.0, 1000.0))
    rows = record_observations(legs, trip(CANDIDATE, HAND_PICKED), status(), tmp_path)
    by_key = {row["depart_date"]: row for row in rows}

    # Both pinned 10 January, so both could reach the cheap legs if the stays
    # allowed. Only the candidate that pinned those dates may have them.
    assert by_key["2027-01-10"]["total"] == 3000
    assert by_key["2027-01-10"]["found_dates"] == [d.isoformat() for d in HAND_PICKED]


def test_a_watch_is_priced_even_when_it_falls_outside_the_narrowing(tmp_path):
    """The narrowing decides what to search, never whether a pick may be priced.

    Without this, moving the return window would silently blank every watch
    already being followed outside it - a series that stops for a reason nothing
    on screen mentions.
    """
    from src.watch import record_observations

    narrowed = trip(CANDIDATE, total_days=(30, 40), return_focus_start=date(2027, 2, 5),
                    return_focus_end=date(2027, 2, 8))
    rows = record_observations(legs_for(CANDIDATE), narrowed, status(), tmp_path)
    assert rows[0]["total"] == 30000
