import json

import pandas as pd
import pytest

from patrol_planner import actions, config, geo, jobs, planning, schemas


@pytest.fixture
def boat():
    return {"patrol_boat_id": "PB-01", "name": "Boat 1", "home_port": "Port A", "home_lat": 0.0,
            "home_lon": -90.0, "max_speed_knots": 20.0, "range_nm": 400.0, "status": "available"}


@pytest.fixture
def ranked():
    scores = pd.DataFrame({
        "vessel_id": ["near", "far", "middle", "unlocated", "too_far"],
        "name": ["NEAR", "FAR", "MIDDLE", "UNLOCATED", "TOO FAR"],
        "risk_score_id": ["near|x", "far|x", "middle|x", "unlocated|x", "too_far|x"],
        "risk_score": [10.0, 90.0, 50.0, 95.0, 80.0],
        "last_lat": [0.0, 0.0, 0.0, None, 0.0],
        "last_lon": [-89.0, -87.0, -88.0, None, -60.0],
    })
    return planning.rank_vessels(scores)


def test_bearings_point_the_right_way():
    assert geo.bearing_deg(0, 0, 1, 0) == pytest.approx(0, abs=0.01)     # due north
    assert geo.bearing_deg(0, 0, 0, 1) == pytest.approx(90, abs=0.01)    # due east
    assert geo.bearing_deg(0, 0, -1, 0) == pytest.approx(180, abs=0.01)  # due south
    assert geo.bearing_deg(0, 0, 0, -1) == pytest.approx(270, abs=0.01)  # due west


def test_route_visits_nearest_first_and_returns_home(boat, ranked):
    stops = ranked[ranked["vessel_id"].isin(["far", "near", "middle", "unlocated"])]
    legs = planning.plan_route(boat, stops)
    # The vessel with no known position cannot be routed to and is left out.
    assert legs["to"].tolist() == ["NEAR", "MIDDLE", "FAR", "Port A"]
    assert legs["from"].tolist() == ["Port A", "NEAR", "MIDDLE", "FAR"]
    assert legs["distance_nm"].tolist() == pytest.approx([60, 60, 60, 180], rel=0.01)
    assert legs["bearing_deg"].tolist() == [90, 90, 90, 270]
    assert legs["hours"].tolist() == pytest.approx([3, 3, 3, 9], abs=0.1)
    assert pd.isna(legs["vessel_id"].iloc[-1])

    summary = planning.route_summary(legs, boat)
    assert summary == {"stops": 3, "total_nm": pytest.approx(360, rel=0.01), "total_hours": pytest.approx(18, abs=0.2),
                       "range_nm": 400.0, "within_range": True}
    assert planning.plan_route(boat, stops.iloc[0:0]).empty


def test_route_beyond_range_is_flagged(boat, ranked):
    legs = planning.plan_route(boat, ranked[ranked["vessel_id"] == "too_far"])
    assert not planning.route_summary(legs, boat)["within_range"]


def test_suggested_route_takes_the_riskiest_vessels_the_boat_can_reach(boat, ranked):
    # "unlocated" ranks first but has no position; "too_far" is out of range.
    assert planning.suggest_route(boat, ranked) == ["far", "middle", "near"]
    assert planning.suggest_route(boat, ranked, max_stops=2) == ["far", "middle"]
    assert planning.suggest_route(boat, ranked, skip_vessel_ids={"far"}) == ["middle", "near"]
    short_range = {**boat, "range_nm": 130.0}
    assert planning.suggest_route(short_range, ranked) == ["near"]


def test_a_patrol_of_several_vessels_is_one_patrol_in_the_log(tmp_path, boat, ranked):
    log = actions.DecisionLog(tmp_path / "patrol_decisions.csv")
    boats = schemas.conform(pd.DataFrame([boat, {**boat, "patrol_boat_id": "PB-02", "name": "Boat 2"}]),
                            schemas.PATROL_BOATS)
    t0 = pd.Timestamp("2026-11-01T08:00:00Z")
    rows = actions.assign_route(
        log, officer="Officer Reyes", patrol_boat_id="PB-01", vessel_ids=["near", "middle", "near"],
        rationale="Sweep east", ranked_vessels=ranked, patrol_boats=boats, data_mode="real", now=t0)
    assert [r["vessel_id"] for r in rows] == ["near", "middle"]  # a repeated vessel is listed once
    saved = log.read()
    schemas.validate(saved, schemas.PATROL_DECISIONS)
    assert saved["patrol_id"].nunique() == 1 and saved["route_stop"].tolist() == [1, 2]
    assert actions.open_assignments(saved)["vessel_id"].tolist() == ["near", "middle"]

    with pytest.raises(actions.ActionError, match="already on a patrol"):
        actions.assign_route(log, officer="x", patrol_boat_id="PB-01", vessel_ids=["far"], rationale="",
                             ranked_vessels=ranked, patrol_boats=boats, data_mode="real")
    with pytest.raises(actions.ActionError, match="already has PB-01"):
        actions.assign_route(log, officer="x", patrol_boat_id="PB-02", vessel_ids=["far", "middle"], rationale="",
                             ranked_vessels=ranked, patrol_boats=boats, data_mode="real")
    with pytest.raises(actions.ActionError, match="at least one vessel"):
        actions.assign_route(log, officer="x", patrol_boat_id="PB-02", vessel_ids=[], rationale="",
                             ranked_vessels=ranked, patrol_boats=boats, data_mode="real")
    assert len(log.read()) == 2

    actions.release_boat(log, officer="Officer Reyes", patrol_boat_id="PB-01", rationale="Done",
                         data_mode="real", now=t0 + pd.Timedelta(hours=20))
    after = log.read()
    assert actions.open_assignments(after).empty
    assert len(after) == 4  # two assignments and two releases; nothing was overwritten
    status = actions.latest_vessel_status(after).set_index("vessel_id")["decision_type"]
    assert status.to_dict() == {"near": actions.RELEASE_BOAT, "middle": actions.RELEASE_BOAT}


def test_a_log_from_before_patrol_routes_existed_still_works(tmp_path, boat, ranked):
    path = tmp_path / "patrol_decisions.csv"
    old_columns = [c for c in schemas.PATROL_DECISIONS.columns if c not in ("patrol_id", "route_stop")]
    old_row = {"decision_id": "d1", "decided_at": "2026-10-01T08:00:00+00:00", "officer": "Officer Reyes",
               "decision_type": "WATCHLIST", "vessel_id": "near", "risk_score_id": "near|x",
               "risk_score_at_decision": "10.0", "rank_at_decision": "5", "rationale": "old entry",
               "data_mode": "real"}
    pd.DataFrame([old_row], columns=old_columns).to_csv(path, index=False)
    log = actions.DecisionLog(path)
    actions.add_to_watchlist(log, officer="Officer Reyes", vessel_id="far", rationale="new entry",
                             ranked_vessels=ranked, data_mode="real")
    saved = log.read()
    assert saved["rationale"].tolist() == ["old entry", "new entry"]
    assert saved["decision_id"].iloc[0] == "d1"


def test_outlines_are_thinned_but_keep_their_shape():
    square = [[0, 0], [0.001, 0.001], [1, 0], [1, 1], [0.5004, 1.0001], [0, 1], [0, 0]]
    speck = [[5, 5], [5.001, 5], [5.001, 5.001], [5, 5]]
    out = geo.simplify_geometry({"type": "MultiPolygon", "coordinates": [[square], [speck]]})
    assert out["type"] == "MultiPolygon" and len(out["coordinates"]) == 1  # the speck vanished
    ring = out["coordinates"][0][0]
    assert ring[0] == ring[-1] and [0.5, 1.0] in ring and len(ring) == 6
    assert geo.simplify_geometry({"type": "Polygon", "coordinates": [square]})["coordinates"][0][0] == ring
    assert geo.simplify_geometry(None) is None and geo.simplify_geometry({"type": "Point"}) is None


def test_saved_overrides_are_laid_over_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("PATROL_PLANNER_OVERRIDES", str(tmp_path / "overrides.json"))
    defaults = config.load_settings(window_end="2026-10-01")
    weights = dict(defaults.scoring.weights, going_dark=0.40, encounters=0.15)
    config.write_overrides({
        "scoring": {"weights": weights},
        "patrol_boats": [{"patrol_boat_id": "X1", "name": "My boat", "home_port": "Home", "home_lat": 1.0,
                          "home_lon": 2.0, "max_speed_knots": 18.0, "range_nm": 300.0, "status": "available"}],
    })
    changed = config.load_settings(window_end="2026-10-01")
    assert changed.scoring.weights["going_dark"] == 0.40
    assert changed.scoring.half_points == defaults.scoring.half_points  # untouched settings survive
    assert [b["name"] for b in changed.patrol_boats] == ["My boat"]
    assert changed.scoring.version_id != defaults.scoring.version_id
    assert config.load_settings(window_end="2026-10-01", use_overrides=False).scoring.weights == defaults.scoring.weights


def test_invalid_overrides_are_refused_and_nothing_is_written(tmp_path, monkeypatch):
    path = tmp_path / "overrides.json"
    monkeypatch.setenv("PATROL_PLANNER_OVERRIDES", str(path))
    with pytest.raises(config.SettingsError):
        config.write_overrides({"patrol_area": {"bbox": [10, 0, -10, 5]}})  # west is east of east
    with pytest.raises(config.SettingsError):
        config.write_overrides({"scoring": {"weights": {"going_dark": 0.9}}})
    assert not path.exists()


def test_automatic_window_ends_a_few_days_ago_and_can_be_pinned():
    automatic = config.load_settings(use_overrides=False)
    age = pd.Timestamp.now(tz="UTC") - automatic.window.end
    assert pd.Timedelta(days=5) <= age < pd.Timedelta(days=6)
    assert automatic.window.end - automatic.window.start >= pd.Timedelta(days=365)
    pinned = config.load_settings(window_end="2026-10-01", use_overrides=False)
    assert pinned.window.end_date == "2026-10-01" and pinned.window.start_date == "2025-10-01"


def test_refresh_status_is_reported_through_files(tmp_path, monkeypatch):
    monkeypatch.setenv("PATROL_PLANNER_DATA_ROOT", str(tmp_path))
    assert jobs.read_status() == {"state": "idle"}
    jobs.write_status("running", "Downloading")
    jobs.log_path().write_text("1/8 Protected-area list\n\n2/8 Fishing vessels\n", encoding="utf-8")
    status = jobs.read_status()
    assert status["state"] == "running" and status["started"]
    assert jobs.progress_lines(1) == ["2/8 Fishing vessels"]
    assert jobs.start_refresh() is False  # one refresh at a time
    jobs.write_status("failed", "No token")
    assert jobs.read_status()["state"] == "failed" and jobs.read_status()["message"] == "No token"
    assert json.loads(jobs.status_path().read_text())["started"] == status["started"]
