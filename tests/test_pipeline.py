import re
from pathlib import Path

import pandas as pd
import pytest

import patrol_planner
from patrol_planner import pipeline, schemas, views
from patrol_planner.sources import sample
from patrol_planner.storage import STAGE_OF, DataStore


def test_every_table_is_written_and_satisfies_its_contract(sample_store):
    tables = {}
    for name in STAGE_OF:
        assert sample_store.exists(name), name
        tables[name] = schemas.validate(sample_store.read(name), schemas.BY_NAME[name])
    assert schemas.check_links(tables) == []
    assert len(tables["vessels"]) > 300 and len(tables["events"]) > 500


def test_every_vessel_gets_exactly_one_score_and_one_row_per_signal(sample_store, settings):
    vessels = sample_store.read("vessels")
    scores = sample_store.read("risk_scores")
    signals = sample_store.read("risk_signals")
    assert set(scores["vessel_id"]) == set(vessels["vessel_id"])
    assert scores["risk_score"].between(0, 100).all()
    assert signals.groupby("risk_score_id").size().eq(len(settings.scoring.weights)).all()
    assert set(signals["risk_score_id"]) == set(scores["risk_score_id"])
    assert scores["model_version"].nunique() == 1


def test_pipeline_is_deterministic(sample_store, settings, tmp_path):
    again = DataStore(tmp_path)
    sample.generate_sample(again, settings, seed=7)
    pipeline.build(again, settings)
    for name in ("risk_scores", "vessel_features", "evaluation_metrics"):
        pd.testing.assert_frame_equal(sample_store.read(name), again.read(name))


def test_sample_data_is_labelled_as_synthetic(sample_store):
    manifest = sample_store.read_manifest()
    assert manifest["data_mode"] == "sample" and "SYNTHETIC" in manifest["label"]
    vessels = sample_store.read("vessels")
    assert vessels["vessel_id"].str.startswith("sample-").all()
    assert vessels["name"].str.contains("SAMPLE").all()


def test_ranking_view_lists_patrol_area_vessels_with_status(sample_store, settings):
    ranking = views.patrol_area_ranking(sample_store)
    cohort = sample_store.read("cohort_members")
    assert len(ranking) == (cohort["cohort"] == "patrol_area").sum()
    assert ranking["risk_score"].is_monotonic_decreasing
    assert ranking["rank"].tolist() == list(range(1, len(ranking) + 1))
    assert (ranking["status"] == "Not reviewed").all()

    top = ranking.iloc[0]
    reasons = views.vessel_reasons(sample_store, top["risk_score_id"])
    assert reasons["points"].sum() == pytest.approx(top["risk_score"], abs=0.05)
    events = views.vessel_events(sample_store, top["vessel_id"], settings)
    assert not events.empty and events["description"].str.len().gt(10).all()
    assert events["start_time"].is_monotonic_decreasing


def test_a_bad_table_is_refused(tmp_path):
    store = DataStore(tmp_path)
    duplicated = pd.DataFrame({"vessel_id": ["v1", "v1"]})
    with pytest.raises(schemas.SchemaError, match="duplicate"):
        store.write("vessels", duplicated)
    assert not store.exists("vessels")


def test_the_package_contains_no_user_interface_code():
    package = Path(patrol_planner.__file__).parent
    for module in package.rglob("*.py"):
        source = module.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+(streamlit|pydeck)\b", source, re.M), module
