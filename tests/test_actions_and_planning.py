import pandas as pd
import pytest

from patrol_planner import actions, planning, schemas


@pytest.fixture
def boats():
    return schemas.conform(pd.DataFrame({
        "patrol_boat_id": ["PB-01", "PB-02", "PB-03"],
        "name": ["Boat 1", "Boat 2", "Boat 3"],
        "home_port": ["A", "B", "C"],
        "home_lat": [0.0, 0.0, 0.0],
        "home_lon": [-90.0, -80.0, -90.0],
        "max_speed_knots": [20.0, 20.0, 20.0],
        "range_nm": [600.0, 600.0, 600.0],
        "status": ["available", "available", "maintenance"],
    }), schemas.PATROL_BOATS)


@pytest.fixture
def ranked():
    scores = pd.DataFrame({
        "vessel_id": ["low", "top", "mid", "far", "unlocated"],
        "risk_score_id": ["low|x", "top|x", "mid|x", "far|x", "unlocated|x"],
        "risk_score": [5.0, 80.0, 40.0, 70.0, 75.0],
        "last_lat": [0.0, 0.0, 0.0, 40.0, None],
        "last_lon": [-89.0, -89.5, -81.0, 150.0, None],
    })
    return planning.rank_vessels(scores)


@pytest.fixture
def log(tmp_path):
    return actions.DecisionLog(tmp_path / "decisions" / "patrol_decisions.csv")


def assign(log, ranked, boats, vessel="top", boat="PB-01", officer="Officer Reyes", **extra):
    return actions.assign_patrol(
        log, officer=officer, vessel_id=vessel, patrol_boat_id=boat, rationale="Check it",
        ranked_vessels=ranked, patrol_boats=boats, data_mode="sample", **extra)


def test_ranking_is_highest_score_first(ranked):
    assert ranked["vessel_id"].tolist() == ["top", "unlocated", "far", "mid", "low"]
    assert ranked["rank"].tolist() == [1, 2, 3, 4, 5]


def test_assignment_is_saved_with_the_score_and_rank_the_officer_saw(log, ranked, boats):
    row = assign(log, ranked, boats)
    saved = log.read()
    schemas.validate(saved, schemas.PATROL_DECISIONS)
    assert len(saved) == 1
    record = saved.iloc[0]
    assert record["decision_id"] == row["decision_id"]
    assert record["decision_type"] == actions.ASSIGN_PATROL
    assert (record["vessel_id"], record["patrol_boat_id"]) == ("top", "PB-01")
    assert record["risk_score_id"] == "top|x"
    assert record["risk_score_at_decision"] == 80.0 and record["rank_at_decision"] == 1
    assert record["officer"] == "Officer Reyes" and record["data_mode"] == "sample"


@pytest.mark.parametrize("change, message", [
    (dict(officer="  "), "officer's name"),
    (dict(vessel="ghost"), "not in the current ranking"),
    (dict(boat="PB-99"), "Unknown patrol boat"),
    (dict(boat="PB-03"), "not available"),
])
def test_invalid_assignments_are_rejected_and_nothing_is_saved(log, ranked, boats, change, message):
    with pytest.raises(actions.ActionError, match=message):
        assign(log, ranked, boats, **change)
    assert log.read().empty


def test_a_boat_or_vessel_cannot_be_double_booked(log, ranked, boats):
    assign(log, ranked, boats, vessel="top", boat="PB-01")
    with pytest.raises(actions.ActionError, match="already assigned"):
        assign(log, ranked, boats, vessel="mid", boat="PB-01")
    with pytest.raises(actions.ActionError, match="already has"):
        assign(log, ranked, boats, vessel="top", boat="PB-02")
    assert len(log.read()) == 1


def test_release_frees_the_boat_and_history_is_kept(log, ranked, boats):
    t0 = pd.Timestamp("2026-11-01T08:00:00Z")
    assign(log, ranked, boats, now=t0)
    actions.release_boat(log, officer="Officer Reyes", patrol_boat_id="PB-01",
                         rationale="Inspected", data_mode="sample", now=t0 + pd.Timedelta(hours=6))
    assert actions.open_assignments(log.read()).empty
    assign(log, ranked, boats, vessel="mid", boat="PB-01", now=t0 + pd.Timedelta(hours=7))
    decisions = log.read()
    assert decisions["decision_type"].tolist() == ["ASSIGN_PATROL", "RELEASE_BOAT", "ASSIGN_PATROL"]
    assert actions.open_assignments(decisions)["vessel_id"].tolist() == ["mid"]
    with pytest.raises(actions.ActionError, match="no open assignment"):
        actions.release_boat(log, officer="x", patrol_boat_id="PB-02", rationale="", data_mode="sample")


def test_dismiss_needs_a_reason_and_watchlist_does_not(log, ranked):
    common = dict(officer="Officer Reyes", vessel_id="low", ranked_vessels=ranked, data_mode="sample")
    with pytest.raises(actions.ActionError, match="reason"):
        actions.dismiss_vessel(log, rationale=" ", **common)
    actions.add_to_watchlist(log, rationale="", now=pd.Timestamp("2026-11-01T08:00:00Z"), **common)
    actions.dismiss_vessel(log, rationale="Licensed, inspected last week",
                           now=pd.Timestamp("2026-11-01T09:00:00Z"), **common)
    status = actions.latest_vessel_status(log.read())
    assert status.set_index("vessel_id").loc["low", "decision_type"] == actions.DISMISS


def test_decision_text_with_commas_quotes_and_newlines_round_trips(log, ranked):
    reason = 'Seen near "the line", twice;\nfollow up'
    actions.add_to_watchlist(log, officer="Officer Reyes", vessel_id="low", rationale=reason,
                             ranked_vessels=ranked, data_mode="sample")
    assert log.read()["rationale"].iloc[0] == reason


def test_distance_between_known_points():
    # One degree of longitude on the equator is 60 nautical miles.
    assert planning.distance_nm(0, 0, 0, 1) == pytest.approx(60.0, rel=0.002)
    assert planning.distance_nm(10, 20, 10, 20) == pytest.approx(0.0, abs=1e-6)


def test_suggestions_follow_the_ranking_and_respect_range_and_availability(ranked, boats):
    plan = planning.suggest_assignments(ranked, boats)
    # "unlocated" has no position, "far" is out of range, PB-03 is in maintenance.
    assert plan["vessel_id"].tolist() == ["top", "mid"]
    assert plan["patrol_boat_id"].tolist() == ["PB-01", "PB-02"]
    assert plan["distance_nm"].iloc[0] == pytest.approx(30.0, rel=0.01)
    assert plan["transit_hours"].iloc[0] == pytest.approx(1.5, abs=0.1)


def test_suggestions_skip_busy_boats_and_handled_vessels(ranked, boats):
    plan = planning.suggest_assignments(ranked, boats, unavailable_boat_ids={"PB-01"},
                                        skip_vessel_ids={"top"})
    assert plan["vessel_id"].tolist() == ["mid"]
    assert plan["patrol_boat_id"].tolist() == ["PB-02"]
    assert planning.suggest_assignments(ranked, boats, unavailable_boat_ids={"PB-01", "PB-02"}).empty
