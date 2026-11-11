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


def test_matched_auc_compares_each_listed_vessel_with_its_own_group_only():
    # Group A scores run high and group B low. Pooled, the listed vessel in B looks
    # bad; within its own group it is clearly on top.
    labels = np.array([1, 0, 0, 1, 0, 0], dtype=bool)
    scores = np.array([90.0, 80, 70, 20, 10, 10])
    groups = np.array(["a", "a", "a", "b", "b", "b"])
    assert evaluation.matched_auc(labels, scores, groups) == 1.0
    assert evaluation.roc_auc(labels, scores) == pytest.approx(0.75)
    # A tie with a comparison vessel counts as half a win.
    assert evaluation.matched_auc(labels, np.array([5.0, 5, 1, 9, 1, 1]), groups) == pytest.approx(0.875)


def test_group_bootstrap_is_reproducible_and_brackets_the_estimate():
    rng = np.random.default_rng(0)
    groups = np.repeat(np.arange(25), 11)
    labels = np.tile(np.array([True] + [False] * 10), 25)
    scores = np.where(labels, rng.normal(1.0, 1.0, 275), rng.normal(0.0, 1.0, 275))
    first = evaluation.group_bootstrap_interval(evaluation.roc_auc, labels, scores, groups, 300, seed=1)
    again = evaluation.group_bootstrap_interval(evaluation.roc_auc, labels, scores, groups, 300, seed=1)
    assert first == again
    assert first[0] < evaluation.roc_auc(labels, scores) < first[1]
    matched = evaluation.matched_auc_interval(labels, scores, groups, 300, seed=1)
    assert matched == evaluation.matched_auc_interval(labels, scores, groups, 300, seed=1)
    assert matched[0] < evaluation.matched_auc(labels, scores, groups) < matched[1]
    assert np.isnan(evaluation.matched_auc_interval(labels[:22], scores[:22], groups[:22], 300, 1)[0])
    # Too few groups to say anything: no interval rather than a misleading one.
    few = evaluation.group_bootstrap_interval(evaluation.roc_auc, labels[:22], scores[:22], groups[:22], 300, 1)
    assert np.isnan(few[0])


def _tables(settings):
    main_end = settings.window.end
    old_start, old_end = pd.Timestamp("2021-03-01", tz="UTC"), pd.Timestamp("2022-03-01", tz="UTC")
    ids = ["listed", "control_1", "control_2", "patrol_only"]
    scores = schemas.conform(pd.DataFrame({
        "risk_score_id": ["listed|a", "control_1|a", "control_2|a", "control_1|main", "patrol_only|main"],
        "vessel_id": ["listed", "control_1", "control_2", "control_1", "patrol_only"],
        "as_of_date": [old_end, old_end, old_end, main_end, main_end],
        "risk_score": [60.0, 5.0, 0.0, 99.0, 50.0],
    }), schemas.RISK_SCORES)
    cohort = schemas.conform(pd.DataFrame({
        "cohort_member_id": [f"evaluation|{v}" for v in ids[:3]] + ["patrol_area|control_1", "patrol_area|patrol_only"],
        "cohort": ["evaluation"] * 3 + ["patrol_area"] * 2,
        "vessel_id": ids[:3] + ["control_1", "patrol_only"],
        "selection_reason": ["iuu_listed_vessel", "matched_control", "matched_control",
                             "active_in_patrol_area", "active_in_patrol_area"],
        "window_start": [old_start] * 3 + [settings.window.start] * 2,
        "window_end": [old_end] * 3 + [main_end] * 2,
        "match_group": ["listed"] * 3 + [None, None],
        "vessel_class": ["fishing"] * 3 + [None, None],
    }), schemas.COHORT_MEMBERS)
    listings = schemas.conform(pd.DataFrame({
        "listing_id": ["l1"], "vessel_id": ["listed"],
        "listed_from": [pd.Timestamp("2021-09-01", tz="UTC")],
        "listed_to": [pd.Timestamp("2024-01-01", tz="UTC")],
    }), schemas.IUU_LISTINGS)
    return scores, listings, cohort


def test_evaluation_table_uses_each_vessels_own_window(settings):
    table = evaluation.build_evaluation_vessels(*_tables(settings))
    schemas.validate(table, schemas.EVALUATION_VESSELS)
    by_id = table.set_index("vessel_id")
    assert set(by_id.index) == {"listed", "control_1", "control_2"}
    # control_1 is also a patrol-area vessel with a different, higher score for the
    # main window; the evaluation must use the score for the group's window.
    assert by_id.loc["control_1", "risk_score"] == 5.0
    assert by_id["iuu_listed"].to_dict() == {"listed": True, "control_1": False, "control_2": False}
    assert by_id.loc["listed", "listing_timing"] == "listed during window"
    assert by_id.loc["listed", "rank"] == 1


@pytest.mark.parametrize("first, last, expected", [
    ("2023-01-01", "2024-01-01", "listed later"),
    ("2018-01-01", "2020-06-01", "listed earlier"),
    ("2018-01-01", "2021-06-01", "listed during window"),
    (None, None, "listing dates unknown"),
])
def test_listing_timing_relative_to_the_scored_window(first, last, expected):
    start, end = pd.Timestamp("2021-03-01", tz="UTC"), pd.Timestamp("2022-03-01", tz="UTC")
    stamp = lambda text: pd.Timestamp(text, tz="UTC") if text else pd.NaT
    assert evaluation._timing(True, start, end, stamp(first), stamp(last)) == expected
    assert evaluation._timing(False, start, end, stamp(first), stamp(last)) is None


def test_metrics_cover_the_score_baselines_and_each_signal(sample_store, settings):
    metrics = sample_store.read("evaluation_metrics")
    schemas.validate(metrics, schemas.EVALUATION_METRICS)
    scorers = set(metrics["scorer"])
    assert {"risk_score", "baseline:activity", "baseline:random"} <= scorers
    assert {f"signal:{s}" for s in settings.scoring.weights} <= scorers
    assert set(metrics["subset"]) == {"all", "fishing", "support"}
    everyone = metrics[metrics["subset"] == "all"]
    for metric in ("roc_auc", "matched_auc"):
        auc = everyone[(everyone["scorer"] == "risk_score") & (everyone["metric"] == metric)].iloc[0]
        assert auc["ci_low"] <= auc["value"] <= auc["ci_high"]
        assert auc["n_positive"] > 0 and auc["n_vessels"] > auc["n_positive"]
    random_auc = everyone[(everyone["scorer"] == "baseline:random") & (everyone["metric"] == "roc_auc")]
    assert random_auc["value"].iloc[0] == 0.5
    counts = metrics.drop_duplicates("subset").set_index("subset")["n_vessels"]
    assert counts["fishing"] + counts["support"] == counts["all"]


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

    # With no vessel confirmed as listed the evaluation groups disappear, so the
    # comparison is made on the patrol-area vessels, which are unaffected.
    original = sample_store.read("risk_scores").set_index("risk_score_id")["risk_score"]
    rebuilt = other.read("risk_scores").set_index("risk_score_id")["risk_score"]
    shared = rebuilt.index.intersection(original.index)
    assert len(shared) > 100
    pd.testing.assert_series_equal(original.loc[shared].sort_index(), rebuilt.loc[shared].sort_index())
