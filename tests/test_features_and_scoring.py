import dataclasses

import pandas as pd
import pytest
from conftest import features_frame, raw_event

from patrol_planner import schemas
from patrol_planner.config import SIGNALS, SettingsError, parse_settings
from patrol_planner.transforms import clean, features, scoring


def build(raw_events, settings, vessel_ids=("v1",), insights=None, activity=None):
    vessels = schemas.conform(pd.DataFrame({"vessel_id": list(vessel_ids)}), schemas.VESSELS)
    return features.build_vessel_features(
        vessels=vessels,
        events=clean.clean_events(raw_events),
        protected_area_activity=activity if activity is not None else schemas.PROTECTED_AREA_ACTIVITY.empty(),
        vessel_insights=insights if insights is not None else schemas.VESSEL_INSIGHTS.empty(),
        cohort_members=schemas.COHORT_MEMBERS.empty(),
        window=settings.window,
        scoring=settings.scoring,
    ).set_index("vessel_id")


def test_only_long_offshore_gaps_inside_the_window_count(settings):
    raw = [
        raw_event("long_far", start="2026-03-01T00:00:00Z", end="2026-03-03T00:00:00Z", shore_km=300),
        raw_event("short", start="2026-03-05T00:00:00Z", end="2026-03-05T08:00:00Z", shore_km=300),
        raw_event("near_shore", start="2026-03-07T00:00:00Z", end="2026-03-09T00:00:00Z", shore_km=40),
        raw_event("before_window", start="2024-01-01T00:00:00Z", end="2024-01-05T00:00:00Z", shore_km=300),
        raw_event("in_mpa", start="2026-04-01T00:00:00Z", end="2026-04-01T12:00:00Z", shore_km=100,
                  regions={"mpa": ["11753"]}),
        raw_event("laid_up", start="2026-05-01T00:00:00Z", end="2026-08-01T00:00:00Z", shore_km=300),
        raw_event("unintentional", start="2026-09-01T00:00:00Z", end="2026-09-02T00:00:00Z",
                  shore_km=300, detail={"intentionalDisabling": False}),
    ]
    row = build(raw, settings).loc["v1"]
    assert row["gap_events"] == 2
    assert row["gap_hours"] == pytest.approx(60.0)
    assert row["longest_gap_hours"] == pytest.approx(48.0)
    assert row["gaps_in_protected_areas"] == 1


def test_encounter_features(settings):
    carrier = {"vessel": {"id": "c", "type": "carrier"}, "type": "fishing-carrier", "potentialRisk": True}
    fishing = {"vessel": {"id": "f", "type": "fishing"}, "type": "fishing-fishing", "potentialRisk": False}
    raw = [
        raw_event("a", "encounter", detail=carrier, regions={"highSeas": ["1"]}),
        raw_event("b", "encounter", detail=fishing, regions={"eez": ["8403"]}),
    ]
    row = build(raw, settings).loc["v1"]
    assert (row["encounter_events"], row["encounters_with_carrier"]) == (2, 1)
    assert (row["encounters_potential_risk"], row["encounters_high_seas"]) == (1, 1)


def test_vessel_without_events_gets_zeros_not_nulls(settings):
    row = build([], settings, vessel_ids=("quiet",)).loc["quiet"]
    assert row["gap_events"] == 0 and row["encounter_events"] == 0
    assert pd.isna(row["last_lat"]) and pd.isna(row["ais_coverage_pct"])


def test_last_position_is_the_most_recent_event(settings):
    raw = [
        raw_event("old", "fishing", start="2026-01-01T00:00:00Z", end="2026-01-01T03:00:00Z"),
        {**raw_event("new", "fishing", start="2026-08-01T00:00:00Z", end="2026-08-01T03:00:00Z"),
         "position": {"lat": 2.5, "lon": -88.0}},
    ]
    row = build(raw, settings).loc["v1"]
    assert (row["last_lat"], row["last_lon"]) == (2.5, -88.0)


def test_ground_truth_is_not_a_feature():
    assert not [c for c in schemas.VESSEL_FEATURES.columns if "iuu" in c or "listed" in c]
    assert not [c for c in schemas.VESSEL_INSIGHTS.columns if "iuu" in c or "listed" in c]


def test_clean_vessel_scores_zero_with_a_plain_summary(settings):
    scores = scoring.score_vessels(features_frame(), settings.scoring)
    schemas.validate(scores, schemas.RISK_SCORES)
    assert scores["risk_score"].iloc[0] == 0
    assert scores["risk_tier"].iloc[0] == "Low"
    assert scores["summary"].iloc[0] == "No risk signals in the analysis window."


def test_score_is_the_sum_of_its_signal_points(settings):
    frame = features_frame(
        gap_events=[3, 0], gap_hours=[90.0, 0.0], longest_gap_hours=[50.0, 0.0],
        encounter_events=[2, 1], encounters_with_carrier=[1, 0],
        no_take_fishing_events=[1, 0], watched_area_hours_near=[40.0, 0.0],
        unauthorized_fishing_events=[2, 0], flag_changes=[1, 0],
    )
    scores = scoring.score_vessels(frame, settings.scoring).set_index("vessel_id")
    signals = scoring.explain_signals(frame, settings.scoring)
    schemas.validate(signals, schemas.RISK_SIGNALS)
    assert len(signals) == len(frame) * len(SIGNALS)
    totals = signals.groupby("vessel_id")["points"].sum()
    for vessel_id in frame["vessel_id"]:
        assert scores.loc[vessel_id, "risk_score"] == pytest.approx(totals[vessel_id], abs=0.05)
    assert scores.loc["v1", "risk_score"] > scores.loc["v2", "risk_score"] > 0
    assert set(signals["risk_score_id"]) == set(scores["risk_score_id"])


@pytest.mark.parametrize("column", [
    "gap_events", "gap_hours", "encounter_events", "encounters_with_carrier",
    "encounters_potential_risk", "no_take_fishing_events", "watched_area_hours_inside",
    "watched_area_hours_near", "gaps_in_protected_areas", "unauthorized_fishing_events",
    "flag_changes",
])
def test_more_of_any_risky_behaviour_never_lowers_the_score(settings, column):
    frame = features_frame(**{column: [0, 1, 5, 50]})
    scores = scoring.score_vessels(frame, settings.scoring)["risk_score"].tolist()
    assert scores == sorted(scores) and scores[0] < scores[-1]


def test_score_never_exceeds_100(settings):
    extreme = {c: [10_000] for c, t in schemas.VESSEL_FEATURES.columns.items()
               if t in (schemas.INT, schemas.FLOAT) and c not in ("last_lat", "last_lon", "ais_coverage_pct")}
    score = scoring.score_vessels(features_frame(**extreme), settings.scoring)["risk_score"].iloc[0]
    assert 99 < score <= 100


def test_explanations_describe_the_behaviour_in_words(settings):
    frame = features_frame(gap_events=[2], gap_hours=[60.0], longest_gap_hours=[40.0],
                           encounter_events=[1], encounters_with_carrier=[1])
    signals = scoring.explain_signals(frame, settings.scoring).set_index("signal")
    assert "2 times" in signals.loc["going_dark", "explanation"]
    assert "60 hours" in signals.loc["going_dark", "explanation"]
    assert "carrier" in signals.loc["encounters", "explanation"]
    assert signals.loc["identity_changes", "explanation"] == "No change of flag."
    summary = scoring.score_vessels(frame, settings.scoring)["summary"].iloc[0]
    assert "Stopped transmitting AIS" in summary


def test_changing_a_weight_changes_the_model_version(settings):
    weights = dict(settings.scoring.weights, going_dark=0.35, encounters=0.20)
    changed = dataclasses.replace(settings.scoring, weights=weights)
    assert changed.version_id != settings.scoring.version_id
    assert changed.version_id.startswith(settings.scoring.model_version)


def test_settings_reject_weights_that_do_not_sum_to_one():
    raw = {
        "window": {"end": "2026-10-01", "months": 12},
        "patrol_area": {"name": "x", "bbox": [-1, -1, 1, 1], "min_fishing_hours": 1,
                        "watched_protected_areas": [], "buffer_nm": 12},
        "scoring": {"model_version": "t", "weights": {s: 0.3 for s in SIGNALS},
                    "half_points": {s: 1 for s in SIGNALS},
                    "gap_rule": {"min_hours": 12, "min_distance_from_shore_km": 92.6, "max_hours": 720},
                    "tiers": {"high": 40, "elevated": 20}},
        "evaluation": {"control_vessels": 1, "random_seed": 1, "bootstrap_samples": 1, "top_k": [1]},
    }
    with pytest.raises(SettingsError):
        parse_settings(raw)
