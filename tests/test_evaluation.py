import numpy as np
import pandas as pd
import pytest

from patrol_planner import evaluation, schemas


def test_auc_known_values():
    labels = np.array([1, 1, 0, 0], dtype=bool)
    assert evaluation.roc_auc(labels, np.array([4, 3, 2, 1])) == 1.0
    assert evaluation.roc_auc(labels, np.array([1, 2, 3, 4])) == 0.0
    assert evaluation.roc_auc(labels, np.array([5, 5, 5, 5])) == 0.5  # all tied: no information
    assert evaluation.roc_auc(labels, np.array([4, 2, 3, 1])) == 0.75
    assert np.isnan(evaluation.roc_auc(np.zeros(4, dtype=bool), np.arange(4)))


def test_average_precision_known_values():
    labels = np.array([1, 0, 1, 0], dtype=bool)
    assert evaluation.average_precision(labels, np.array([4, 3, 2, 1])) == pytest.approx((1 + 2 / 3) / 2)
    # With every score tied, average precision falls back to the base rate.
    assert evaluation.average_precision(labels, np.zeros(4)) == pytest.approx(0.5)


def test_top_k_treats_ties_as_a_fair_draw():
    # One clear leader, then four vessels tied on zero of which one is listed.
    labels = np.array([1, 1, 0, 0, 0], dtype=bool)
    scores = np.array([9.0, 0, 0, 0, 0])
    assert evaluation.expected_hits_at_k(labels, scores, 1) == 1.0
    # Two more slots are drawn from the four tied vessels: 2/4 of one listed vessel.
    assert evaluation.expected_hits_at_k(labels, scores, 3) == pytest.approx(1.5)
    assert evaluation.precision_at_k(labels, scores, 3) == pytest.approx(0.5)
    assert evaluation.recall_at_k(labels, scores, 5) == 1.0


def test_bootstrap_interval_is_reproducible_and_brackets_the_estimate():
    rng = np.random.default_rng(0)
    labels = np.array([True] * 20 + [False] * 200)
    scores = np.where(labels, rng.normal(1.0, 1.0, 220), rng.normal(0.0, 1.0, 220))
    first = evaluation.bootstrap_interval(evaluation.roc_auc, labels, scores, 300, seed=1)
    again = evaluation.bootstrap_interval(evaluation.roc_auc, labels, scores, 300, seed=1)
    assert first == again
    assert first[0] < evaluation.roc_auc(labels, scores) < first[1]


def _tables(settings):
    window = settings.window
    ids = ["listed_a", "listed_b_old_identity", "listed_b", "clean_1", "clean_2", "not_in_cohort"]
    vessels = schemas.conform(pd.DataFrame({
        "vessel_id": ids,
        "imo": [None, "9139610", "9139610", None, None, None],
        "last_transmission": ["2026-09-01", "2025-11-01", "2026-09-01", "2026-09-01", "2026-09-01", "2026-09-01"],
    }), schemas.VESSELS)
    scores = schemas.conform(pd.DataFrame({
        "risk_score_id": [f"{v}|x" for v in ids], "vessel_id": ids,
        "risk_score": [60.0, 10.0, 45.0, 5.0, 0.0, 99.0],
    }), schemas.RISK_SCORES)
    cohort = schemas.conform(pd.DataFrame({
        "cohort_member_id": [f"evaluation|{v}" for v in ids[:5]], "cohort": "evaluation",
        "vessel_id": ids[:5], "selection_reason": ["iuu_list_seed"] * 3 + ["random_control"] * 2,
    }), schemas.COHORT_MEMBERS)
    listings = schemas.conform(pd.DataFrame({
        "listing_id": ["l1", "l2", "l3", "l4"],
        "vessel_id": ["listed_a", "listed_b_old_identity", "listed_b", "clean_1"],
        "listed_from": [window.start, window.start, window.start, pd.Timestamp("2015-01-01", tz="UTC")],
        # clean_1 was delisted long before the window, so it does not count as listed.
        "listed_to": [window.end, window.end, window.end, pd.Timestamp("2018-01-01", tz="UTC")],
    }), schemas.IUU_LISTINGS)
    return scores, listings, cohort, vessels


def test_evaluation_table_labels_by_window_and_counts_each_hull_once(settings):
    table = evaluation.build_evaluation_vessels(*_tables(settings), settings.window)
    schemas.validate(table, schemas.EVALUATION_VESSELS)
    by_id = table.set_index("vessel_id")
    assert "not_in_cohort" not in by_id.index
    # Two identities share one IMO number: only the one still transmitting is kept.
    assert "listed_b" in by_id.index and "listed_b_old_identity" not in by_id.index
    assert by_id["iuu_listed"].to_dict() == {
        "listed_a": True, "listed_b": True, "clean_1": False, "clean_2": False}
    assert by_id.loc["listed_a", "rank"] == 1 and by_id.loc["clean_2", "rank"] == 4


def test_metrics_cover_the_score_baselines_and_each_signal(sample_store, settings):
    metrics = sample_store.read("evaluation_metrics")
    schemas.validate(metrics, schemas.EVALUATION_METRICS)
    scorers = set(metrics["scorer"])
    assert {"risk_score", "baseline:activity", "baseline:random"} <= scorers
    assert {f"signal:{s}" for s in settings.scoring.weights} <= scorers
    auc = metrics[(metrics["scorer"] == "risk_score") & (metrics["metric"] == "roc_auc")].iloc[0]
    assert auc["ci_low"] <= auc["value"] <= auc["ci_high"]
    assert auc["n_positive"] > 0 and auc["n_vessels"] > auc["n_positive"]
    random_auc = metrics[(metrics["scorer"] == "baseline:random") & (metrics["metric"] == "roc_auc")]
    assert random_auc["value"].iloc[0] == 0.5


def test_scores_do_not_depend_on_the_ground_truth(sample_store, settings, tmp_path):
    """Relabel every vessel as unlisted and rebuild: features and scores must not move."""
    import json

    from patrol_planner import pipeline
    from patrol_planner.storage import DataStore

    other = DataStore(tmp_path)
    for dataset in ("protected_area_list", "patrol_area_report", "protected_area_reports",
                    "vessel_identities", "events", "cohort"):
        other.write_raw(dataset, sample_store.read_raw(dataset))
    relabelled = json.loads(json.dumps(sample_store.read_raw("insights")))
    for record in relabelled:
        record["vesselIdentity"]["iuuVesselList"] = {
            "valuesInThePeriod": [], "totalTimesListed": 0, "totalTimesListedInThePeriod": 0}
    other.write_raw("insights", relabelled)
    pipeline.run_clean(other, settings)
    pipeline.run_features(other, settings)
    pipeline.run_scores(other, settings)

    original = sample_store.read("risk_scores").set_index("vessel_id")["risk_score"]
    rebuilt = other.read("risk_scores").set_index("vessel_id")["risk_score"]
    shared = rebuilt.index.intersection(original.index)
    assert len(shared) > 100
    pd.testing.assert_series_equal(original.loc[shared].sort_index(), rebuilt.loc[shared].sort_index())
