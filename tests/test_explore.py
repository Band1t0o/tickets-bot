"""Tests for the exploration report.

The report exists to answer one question per airport: is this worth including
when the real sweep runs? Everything here is about not answering it wrongly -
in particular, never calling an airport dead when the truth is that the site
never replied.
"""
from __future__ import annotations

from src.scenario import Stop
from src.sweep.explore import explore_report
from tests.conftest import make_round_trip, make_scenario


def trip(**overrides):
    """Two origins, two Japanese airports, one Philippine one - readable rows."""
    defaults = dict(
        id="jp-ph",
        origins=["PRG", "KTW"],
        stops=[
            Stop(airports=["NRT", "KIX"], stay_days=(9, 11), label="Japan"),
            Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
        ],
    )
    defaults.update(overrides)
    return make_scenario(**defaults)


def status_for(searches: dict[str, int], errors: dict[str, int] | None = None) -> dict:
    errors = errors or {}
    return {
        "mode": "explore",
        "state": "done",
        "route_searches": dict(searches),
        "route_errors": {route: errors.get(route, 0) for route in searches},
    }


def all_routes_searched(scenario, times: int = 3) -> dict[str, int]:
    from src.sweep.planner import planned_routes

    return {f"{origin}->{destination}": times for origin, destination in planned_routes(scenario)}


def rows(report, pool_index):
    return {row["iata"]: row for row in report["pools"][pool_index]["airports"]}


def priced(make_leg, scenario, prices: dict[str, float], stops: dict[str, int] | None = None):
    """One leg per route in `prices`, keyed "PRG->NRT"; the rest come back empty."""
    stops = stops or {}
    legs = []
    for route, amount in prices.items():
        origin, _, destination = route.partition("->")
        legs.append(
            make_leg(
                origin=origin,
                destination=destination,
                price_amount=amount,
                stops=stops.get(route, 1),
            )
        )
    return legs


# ------------------------------------------------------------------- shape


def test_every_pool_of_the_trip_gets_a_block(make_leg):
    scenario = trip()
    report = explore_report([], scenario, status_for({}))
    assert len(report["pools"]) == len(scenario.airport_pools)


def test_every_airport_in_a_pool_gets_a_row(make_leg):
    report = explore_report([], trip(), status_for({}))
    assert set(rows(report, 0)) == {"PRG", "KTW"}
    assert set(rows(report, 1)) == {"NRT", "KIX"}


def test_a_pool_says_which_part_of_the_trip_it_is(make_leg):
    """The Remove button has to know which list of airports it is editing."""
    report = explore_report([], trip(), status_for({}))
    assert report["pools"][0]["role"] == "origins"
    assert report["pools"][1]["role"] == "stop"
    assert report["pools"][1]["stop_index"] == 0
    assert report["pools"][1]["label"] == "Japan"


def test_the_way_home_is_labelled_as_the_origins_it_actually_edits(make_leg):
    """`return_to: null` means the last pool *is* the origins list."""
    report = explore_report([], trip(), status_for({}))
    assert report["pools"][-1]["role"] == "origins"


def test_an_open_jaw_names_its_own_return_airports(make_leg):
    report = explore_report([], trip(return_to=["BER"]), status_for({}))
    assert report["pools"][-1]["role"] == "return_to"
    assert set(rows(report, -1)) == {"BER"}


# ------------------------------------------------------------------ verdicts


def test_the_cheapest_origin_in_a_pool_is_the_benchmark(make_leg):
    scenario = trip()
    legs = priced(make_leg, scenario, {"PRG->NRT": 11000, "KTW->NRT": 20000})
    report = explore_report(legs, scenario, status_for(all_routes_searched(scenario)))
    assert rows(report, 0)["PRG"]["verdict"] == "best"
    assert rows(report, 0)["PRG"]["vs_best_pct"] == 0.0


def test_an_origin_far_above_the_best_is_called_poor(make_leg):
    """Katowice at 20k against Prague at 11k is not going to come good in March."""
    scenario = trip()
    legs = priced(make_leg, scenario, {"PRG->NRT": 11000, "KTW->NRT": 20000})
    report = explore_report(legs, scenario, status_for(all_routes_searched(scenario)))
    katowice = rows(report, 0)["KTW"]
    assert katowice["verdict"] == "poor"
    assert round(katowice["vs_best_pct"]) == 82


def test_an_origin_a_little_dearer_is_close_rather_than_poor(make_leg):
    scenario = trip()
    legs = priced(make_leg, scenario, {"PRG->NRT": 11000, "KTW->NRT": 12000})
    report = explore_report(legs, scenario, status_for(all_routes_searched(scenario)))
    assert rows(report, 0)["KTW"]["verdict"] == "close"


def test_an_origin_between_the_two_is_merely_worse(make_leg):
    scenario = trip()
    legs = priced(make_leg, scenario, {"PRG->NRT": 11000, "KTW->NRT": 14500})
    report = explore_report(legs, scenario, status_for(all_routes_searched(scenario)))
    assert rows(report, 0)["KTW"]["verdict"] == "worse"


def test_the_cheapest_offers_stop_count_is_reported(make_leg):
    """Two transfers for 20k is the whole story, and one number does not tell it."""
    scenario = trip()
    legs = priced(
        make_leg, scenario,
        {"PRG->NRT": 11000, "KTW->NRT": 20000},
        stops={"PRG->NRT": 1, "KTW->NRT": 2},
    )
    report = explore_report(legs, scenario, status_for(all_routes_searched(scenario)))
    assert rows(report, 0)["KTW"]["out_min_stops"] == 2
    assert rows(report, 0)["PRG"]["out_min_stops"] == 1


def test_airports_are_compared_within_their_own_pool_not_across_legs(make_leg):
    """A 3,000 Kc hop to Manila must not make every European origin look poor."""
    scenario = trip()
    legs = priced(
        make_leg, scenario,
        {"PRG->NRT": 11000, "KTW->NRT": 11500, "NRT->MNL": 3000, "KIX->MNL": 3200},
    )
    report = explore_report(legs, scenario, status_for(all_routes_searched(scenario)))
    assert rows(report, 0)["KTW"]["verdict"] == "close"


def test_a_pool_with_one_airport_is_the_best_by_definition(make_leg):
    scenario = trip()
    legs = priced(make_leg, scenario, {"NRT->MNL": 3000, "MNL->PRG": 9000})
    report = explore_report(legs, scenario, status_for(all_routes_searched(scenario)))
    assert rows(report, 2)["MNL"]["verdict"] == "best"


# ----------------------------------------------------- measured vs unmeasured
#
# The distinction the whole report turns on. The sweep running when this was
# written managed 1.9 legs per search: an empty result is far more likely to be
# the site refusing than the route being empty.


def test_a_route_asked_three_times_and_answered_empty_is_reported_as_dead(make_leg):
    scenario = trip()
    legs = priced(make_leg, scenario, {"PRG->NRT": 11000})
    status = status_for(all_routes_searched(scenario, times=3))
    report = explore_report(legs, scenario, status)
    assert rows(report, 0)["KTW"]["verdict"] == "no_offers"


def test_a_route_that_failed_every_time_is_unproven_never_dead(make_leg):
    scenario = trip()
    searched = all_routes_searched(scenario, times=3)
    failures = {route: 3 for route in searched if route.startswith("KTW")}
    legs = priced(make_leg, scenario, {"PRG->NRT": 11000})
    report = explore_report(legs, scenario, status_for(searched, errors=failures))
    katowice = rows(report, 0)["KTW"]
    assert katowice["verdict"] == "unproven"
    assert katowice["errors"] == 6  # KTW->NRT and KTW->KIX, three failures each


def test_an_airport_nobody_asked_about_is_unproven(make_leg):
    report = explore_report([], trip(), status_for({}))
    assert rows(report, 0)["KTW"]["verdict"] == "unproven"


def test_one_empty_answer_is_not_enough_to_call_a_route_dead(make_leg):
    scenario = trip()
    legs = priced(make_leg, scenario, {"PRG->NRT": 11000})
    report = explore_report(legs, scenario, status_for(all_routes_searched(scenario, times=1)))
    assert rows(report, 0)["KTW"]["verdict"] == "unproven"


def test_an_airport_you_can_reach_but_not_leave_is_not_usable(make_leg):
    """NRT priced inbound only. Half a connection is no connection."""
    scenario = trip()
    legs = priced(make_leg, scenario, {"PRG->NRT": 11000, "KIX->MNL": 3000, "PRG->KIX": 12000})
    report = explore_report(legs, scenario, status_for(all_routes_searched(scenario)))
    tokyo = rows(report, 1)["NRT"]
    assert tokyo["in_min_price"] == 11000
    assert tokyo["out_min_price"] is None
    assert tokyo["verdict"] == "no_offers"


def test_a_middle_airport_is_judged_on_both_sides_together(make_leg):
    """Cheap to fly into and dear to leave is not a cheap airport."""
    scenario = trip()
    legs = priced(
        make_leg, scenario,
        {
            "PRG->NRT": 11000, "NRT->MNL": 3000,
            "PRG->KIX": 11500, "KIX->MNL": 14000,
        },
    )
    report = explore_report(legs, scenario, status_for(all_routes_searched(scenario)))
    # KIX is the cheaper airport to reach and the dearer trip: 25,500 to NRT's
    # 14,000. Scoring it on the inbound leg alone would have called it close.
    assert rows(report, 1)["NRT"]["total_min"] == 14000
    assert rows(report, 1)["NRT"]["verdict"] == "best"
    assert rows(report, 1)["KIX"]["total_min"] == 25500
    assert rows(report, 1)["KIX"]["verdict"] == "poor"


# -------------------------------------------------------------------- routes


def test_every_route_is_listed_with_what_was_asked_and_what_came_back(make_leg):
    scenario = make_round_trip()
    legs = priced(make_leg, scenario, {"PRG->NRT": 11000})
    searched = all_routes_searched(scenario, times=3)
    report = explore_report(legs, scenario, status_for(searched, errors={"NRT->PRG": 2}))
    routes = {row["route"]: row for row in report["routes"]}
    assert routes["PRG->NRT"]["searches"] == 3
    assert routes["PRG->NRT"]["legs"] == 1
    assert routes["PRG->NRT"]["min_price"] == 11000
    assert routes["NRT->PRG"]["errors"] == 2
    assert routes["NRT->PRG"]["min_price"] is None


def test_the_report_carries_the_currency_it_is_quoting(make_leg):
    assert explore_report([], trip(), status_for({}))["currency"] == "CZK"


def test_legs_from_routes_the_trip_no_longer_contains_are_ignored(make_leg):
    """A trip edited between the probe and the report must not grow rows."""
    scenario = trip()
    legs = priced(make_leg, scenario, {"BRQ->NRT": 9000})
    report = explore_report(legs, scenario, status_for(all_routes_searched(scenario)))
    assert "BRQ" not in rows(report, 0)


# -------------------------------------------- the trip searched vs the trip now
#
# The report describes one run, and a run searched one trip. Reading it against
# whatever the trip happens to be now is how two probes came to report on
# Prague, Vienna and Frankfurt for a trip that flies from Katowice - listing
# airports that were never searched and throwing away the ones that were.


def test_a_report_of_an_older_trip_keeps_the_airports_that_run_actually_searched(make_leg):
    searched = trip()
    current = trip(origins=["BER", "MUC"])
    legs = priced(make_leg, searched, {"PRG->NRT": 11000})
    report = explore_report(
        legs, searched, status_for(all_routes_searched(searched)), current=current
    )
    assert set(rows(report, 0)) == {"PRG", "KTW"}
    assert rows(report, 0)["PRG"]["out_min_price"] == 11000


def test_the_airports_of_the_current_trip_that_this_run_never_priced_are_named(make_leg):
    """The question the screen could not answer: "what about Katowice?"."""
    searched = trip(origins=["PRG", "VIE"])
    current = trip(origins=["BER", "KTW", "VIE"])
    report = explore_report([], searched, status_for({}), current=current)
    assert report["pools"][0]["not_searched"] == ["BER", "KTW"]
    assert report["matches_current_trip"] is False


def test_a_run_of_the_trip_you_are_looking_at_has_nothing_to_warn_about(make_leg):
    scenario = trip()
    report = explore_report([], scenario, status_for({}), current=scenario)
    assert report["matches_current_trip"] is True
    assert all(pool["not_searched"] == [] for pool in report["pools"])


def test_a_trip_that_has_gained_a_stop_is_not_compared_pool_by_pool(make_leg):
    """Pools are matched by position, so a trip of a different length cannot be
    lined up against this run at all. Say the shape changed rather than accuse
    Manila of never having been searched because it moved along one."""
    searched = trip()
    current = trip(
        stops=[
            Stop(airports=["NRT", "KIX"], stay_days=(9, 11), label="Japan"),
            Stop(airports=["BKK"], stay_days=(4, 6), label="Thailand"),
            Stop(airports=["MNL"], stay_days=(9, 11), label="Philippines"),
        ],
    )
    report = explore_report([], searched, status_for({}), current=current)
    assert report["shape_changed"] is True
    assert report["matches_current_trip"] is False
    assert all(pool["not_searched"] == [] for pool in report["pools"])


def test_with_no_other_trip_to_compare_against_the_report_is_about_itself(make_leg):
    report = explore_report([], trip(), status_for({}))
    assert report["matches_current_trip"] is True
    assert report["shape_changed"] is False
