"""Tests for narrowing a trip after a broad sweep has mapped it out.

A focus says when you leave. These are the other two halves of the same
decision - when you fly home, and how long you are away - and the three have to
agree with each other and with the per-stop stays. What they encode is mostly
that: a narrowing nobody can satisfy is refused by name rather than swept, and
one that can be satisfied narrows the plan without ever excluding a date that a
valid trip could have used.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.combine import combine_all
from src.models import Leg
from src.scenario import Scenario, Stop
from src.sweep.planner import plan_final, plan_searches
from tests.conftest import WINDOW_END, make_scenario


def scenario(**overrides) -> Scenario:
    defaults = dict(
        id="jp-ph",
        origins=["VIE"],
        stops=[
            Stop(airports=["HND"], stay_days=(10, 13), label="Japan"),
            Stop(airports=["MNL"], stay_days=(8, 13), label="Philippines"),
        ],
        depth="deep",
    )
    defaults.update(overrides)
    return make_scenario(**defaults)


def leg(origin, destination, depart, price=10000.0) -> Leg:
    return Leg(
        provider="TEST",
        origin=origin,
        destination=destination,
        depart_date=depart,
        airline="XX",
        flight_number=None,
        stops=1,
        price_currency="CZK",
        price_amount=price,
        url="https://example.test/leg",
    )


def chain(out: date, on: date, home: date, price=10000.0) -> list[Leg]:
    """One complete trip's worth of legs, on the three dates given."""
    return [
        leg("VIE", "HND", out, price),
        leg("HND", "MNL", on, price),
        leg("MNL", "VIE", home, price),
    ]


# ------------------------------------------------------------------ the schema


def test_span_bounds_come_from_the_stays_between_first_and_final_leg():
    sc = scenario()
    assert (sc.min_span_days, sc.max_span_days) == (18, 26)


def test_min_trip_days_is_not_the_span_on_a_one_way_chain():
    """The two agree on a round trip and must not be confused on a one-way.

    `min_trip_days` sums every stop; the span stops one short, because nothing
    has to be waited out after the final leg departs. Using the first as the
    floor for `total_days` would reject bands that are perfectly reachable.
    """
    one_way = scenario(one_way=True)
    assert one_way.min_trip_days == 18
    assert one_way.min_span_days == 10  # the Japan stay only
    assert one_way.max_span_days == 13


def test_the_narrowing_round_trips_through_json():
    sc = scenario(
        return_focus_start=date(2027, 2, 4),
        return_focus_end=date(2027, 2, 8),
        total_days=(22, 26),
    )
    restored = Scenario.from_dict(sc.to_dict())
    assert restored.return_focus_start == date(2027, 2, 4)
    assert restored.return_focus_end == date(2027, 2, 8)
    assert restored.total_days == (22, 26)
    assert restored.to_dict() == sc.to_dict()


def test_a_trip_saved_before_the_narrowing_existed_still_loads():
    data = scenario().to_dict()
    for key in ("return_focus_start", "return_focus_end", "total_days"):
        del data[key]
    restored = Scenario.from_dict(data)
    assert restored.return_focus_start is None
    assert restored.total_days is None


# ------------------------------------------------------------------ validation


def test_a_return_window_needs_both_ends():
    with pytest.raises(ValueError, match="both a first and a last date"):
        scenario(return_focus_start=date(2027, 2, 4)).validate()


def test_a_return_window_outside_the_window_says_widen_the_window():
    with pytest.raises(ValueError, match="widen the window first"):
        scenario(
            return_focus_start=date(2027, 2, 6),
            return_focus_end=WINDOW_END + timedelta(days=3),
        ).validate()


def test_a_backwards_return_window_is_refused():
    with pytest.raises(ValueError, match="must not precede"):
        scenario(
            return_focus_start=date(2027, 2, 8), return_focus_end=date(2027, 2, 4)
        ).validate()


def test_a_nights_band_the_stays_cannot_reach_names_the_stays():
    """The error has to say which stays make it impossible.

    Refusing with "unreachable" alone leaves the reader to work out for
    themselves which of two ranges to change, on a screen showing both.
    """
    with pytest.raises(ValueError) as exc:
        scenario(total_days=(30, 35)).validate()
    message = str(exc.value)
    assert "30-35 nights away is unreachable" in message
    assert "10-13 at Japan" in message
    assert "8-13 at Philippines" in message


def test_a_nights_band_inside_the_stays_is_accepted():
    scenario(total_days=(22, 26)).validate()


def test_two_windows_that_cannot_both_be_met_are_refused():
    """Leave 5-6 January, be home 6-8 February: 31 nights at least, 26 possible."""
    with pytest.raises(ValueError, match="nights away, but"):
        scenario(
            focus_start=date(2027, 1, 5),
            focus_end=date(2027, 1, 6),
            return_focus_start=date(2027, 2, 6),
            return_focus_end=date(2027, 2, 8),
        ).validate()


def test_two_windows_that_overlap_the_band_are_accepted():
    scenario(
        focus_start=date(2027, 1, 8),
        focus_end=date(2027, 1, 12),
        return_focus_start=date(2027, 2, 4),
        return_focus_end=date(2027, 2, 8),
        total_days=(22, 26),
    ).validate()


# --------------------------------------------------------------------- planner


def narrowed_trip() -> Scenario:
    return scenario(
        focus_start=date(2027, 1, 8),
        focus_end=date(2027, 1, 12),
        return_focus_start=date(2027, 2, 4),
        return_focus_end=date(2027, 2, 8),
        total_days=(22, 26),
    )


def test_narrowing_cuts_the_plan():
    assert len(plan_final(narrowed_trip())) < len(plan_searches(scenario())) / 3


def test_narrowing_does_not_cut_the_broad_plan():
    """The other half of the same statement, and the one that was untrue.

    A narrowing is a decision about which trip to take. Until 24 Aug it was also
    a decision to stop pricing every other one, taken silently by saving it.
    """
    assert plan_searches(narrowed_trip()) == plan_searches(scenario())


def test_the_final_leg_is_searched_only_inside_the_return_window():
    dates = {s.depart_date for s in plan_final(narrowed_trip()) if s.leg_index == 2}
    assert min(dates) >= date(2027, 2, 4)
    assert max(dates) <= date(2027, 2, 8)


def test_the_first_leg_drops_dates_that_cannot_reach_the_return_window():
    """8 January is inside the focus and still unreachable.

    At the longest stays it puts you home on 3 February, a day before the
    return window opens. Searching it would buy legs no itinerary could use -
    the same orphan-search bug `_leg_window` already fixed for the horizon.
    """
    dates = {s.depart_date for s in plan_final(narrowed_trip()) if s.leg_index == 0}
    assert date(2027, 1, 8) not in dates
    assert min(dates) == date(2027, 1, 9)


def test_no_planned_first_leg_date_is_an_orphan():
    """Every date searched for the first leg must reach a searched final leg."""
    sc = narrowed_trip()
    searches = plan_final(sc)
    finals = {s.depart_date for s in searches if s.leg_index == sc.leg_count - 1}
    for start in {s.depart_date for s in searches if s.leg_index == 0}:
        reachable = {
            start + timedelta(days=n)
            for n in range(sc.min_span_days, sc.max_span_days + 1)
        }
        assert reachable & finals, f"{start} reaches no searched final leg"


def test_a_nights_band_alone_does_not_narrow_the_plan():
    """A band is relative, so on its own it cannot bound an absolute date.

    Worth stating rather than leaving to be rediscovered as a bug. With no end
    pinned, the first leg may still depart anywhere in the window, so every date
    of every later leg is reachable from *some* first-leg date and none can be
    dropped. The band constrains which chains are valid, not which searches are
    worth running, and the combiner is where it bites. Pin either end and it
    starts narrowing the plan too - which is what the tests above measure.
    """
    assert len(plan_final(scenario(total_days=(18, 19)))) == len(
        plan_searches(scenario())
    )


def test_a_band_narrows_the_plan_once_an_end_is_pinned():
    focused = scenario(focus_start=date(2027, 1, 8), focus_end=date(2027, 1, 12))
    assert len(plan_final(replace(focused, total_days=(18, 19)))) < len(
        plan_final(focused)
    )


# -------------------------------------------------------------------- combiner


def test_a_trip_outside_the_return_window_is_not_built():
    sc = scenario(return_focus_start=date(2027, 2, 4), return_focus_end=date(2027, 2, 8))
    legs = chain(date(2027, 1, 8), date(2027, 1, 20), date(2027, 2, 1))
    assert combine_all(legs, sc, limit=None).top == []
    assert len(combine_all(legs, sc, limit=None, narrowed=False).top) == 1


def test_a_trip_longer_than_the_band_is_not_built():
    sc = scenario(total_days=(18, 22))
    # 13 + 13 = 26 nights: both stays legal, the total is not.
    legs = chain(date(2027, 1, 8), date(2027, 1, 21), date(2027, 2, 3))
    assert combine_all(legs, sc, limit=None).top == []
    assert len(combine_all(legs, sc, limit=None, narrowed=False).top) == 1


def test_a_trip_shorter_than_the_band_is_not_built():
    sc = scenario(total_days=(24, 26))
    legs = chain(date(2027, 1, 8), date(2027, 1, 18), date(2027, 1, 26))  # 18 nights
    assert combine_all(legs, sc, limit=None).top == []


def test_a_split_the_band_allows_is_built_whichever_way_it_falls():
    """12+12 and 13+11 are both 24 nights and both must survive."""
    sc = scenario(total_days=(23, 25))
    even = chain(date(2027, 1, 8), date(2027, 1, 20), date(2027, 2, 1))
    assert len(combine_all(even, sc, limit=None).top) == 1
    lopsided = chain(date(2027, 1, 8), date(2027, 1, 21), date(2027, 2, 1), price=9000.0)
    assert len(combine_all(lopsided, sc, limit=None).top) == 1


def test_narrowing_is_applied_during_the_traversal_not_after():
    """A cheaper out-of-window trip must not prune an in-window one away.

    Measured on the committed 21 August sweep before this was inside the
    traversal: with the return window at 4-8 February the unnarrowed traversal
    kept a 3 February trip and pruned an identically priced 6 February one, so
    filtering the finished result would have reported nothing available at all.
    """
    sc = scenario(return_focus_start=date(2027, 2, 4), return_focus_end=date(2027, 2, 8))
    legs = [
        # One departure day, two legal ways home: 10 nights in the Philippines
        # for 8,000, or 13 for 9,000. The cheaper one lands outside the window.
        *chain(date(2027, 1, 10), date(2027, 1, 23), date(2027, 2, 2), price=8000.0),
        leg("MNL", "VIE", date(2027, 2, 5), 9000.0),
    ]
    assert [i.legs[-1].depart_date for i in combine_all(legs, sc, limit=None).top] == [
        date(2027, 2, 5)
    ]
    wide = combine_all(legs, sc, limit=None, narrowed=False).top
    assert [i.legs[-1].depart_date for i in wide] == [date(2027, 2, 2)]

def test_a_trip_leaving_outside_the_departure_window_is_not_built():
    """The focus is a third of the same sentence, not a separate feature.

    Applying the return window and the nights band without it let "the cheapest
    trip that fits your narrowing" mean a trip leaving two days outside the
    window typed into the box above it — found by driving the real app against
    the committed 21 August sweep.
    """
    sc = scenario(focus_start=date(2027, 1, 8), focus_end=date(2027, 1, 12))
    legs = chain(date(2027, 1, 14), date(2027, 1, 26), date(2027, 2, 5))
    assert combine_all(legs, sc, limit=None).top == []
    assert len(combine_all(legs, sc, limit=None, narrowed=False).top) == 1


def test_a_trip_leaving_inside_the_departure_window_survives():
    sc = scenario(focus_start=date(2027, 1, 8), focus_end=date(2027, 1, 12))
    legs = chain(date(2027, 1, 10), date(2027, 1, 22), date(2027, 2, 1))
    assert len(combine_all(legs, sc, limit=None).top) == 1
