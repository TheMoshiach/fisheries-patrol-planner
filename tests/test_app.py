"""Smoke tests for the app: it must render, plan a route and save decisions."""

from pathlib import Path

import pytest

from patrol_planner import pipeline, views
from patrol_planner.sources import sample
from patrol_planner.storage import DataStore

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
pytest.importorskip("streamlit_folium")
APP = str(Path(__file__).resolve().parents[1] / "app" / "streamlit_app.py")


@pytest.fixture
def app_store(tmp_path, monkeypatch, settings):
    """A built dataset in the place the app looks for one (generated, and labelled as such)."""
    store = DataStore(tmp_path / "real")
    sample.generate_sample(store, settings, seed=7)
    pipeline.build(store, settings)
    monkeypatch.setenv("PATROL_PLANNER_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("PATROL_PLANNER_OVERRIDES", str(tmp_path / "overrides.json"))
    return store


def button(app, label):
    return next(b for b in app.button if b.label == label)


def test_app_renders_the_map_screen_and_labels_generated_data(app_store):
    app = AppTest.from_file(APP, default_timeout=90).run()
    assert not app.exception
    assert app.title[0].value == "Fisheries Patrol Planner"
    assert any("Synthetic sample data" in warning.value for warning in app.warning)
    assert [tab.label for tab in app.tabs] == ["Map", "Decision log", "How reliable is this?", "Settings"]


def test_first_run_without_data_offers_a_refresh_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.setenv("PATROL_PLANNER_DATA_ROOT", str(tmp_path))
    app = AppTest.from_file(APP, default_timeout=90).run()
    assert not app.exception
    assert any("no data yet" in info.value for info in app.info)
    assert button(app, "Refresh data from Global Fishing Watch")


def test_searching_for_a_vessel_opens_its_reasons(app_store):
    app = AppTest.from_file(APP, default_timeout=90).run()
    top = views.patrol_area_ranking(app_store).iloc[0]
    app.selectbox(key="picker").set_value(top["vessel_id"]).run()
    assert not app.exception
    assert any(top["name"] == heading.value for heading in app.subheader)
    assert any("Why it ranks here" in text.value for text in app.markdown)


def test_a_suggested_route_can_be_saved_as_one_patrol(app_store):
    app = AppTest.from_file(APP, default_timeout=90).run()
    app.text_input(key="officer").set_value("Officer Reyes").run()
    button(app, "Suggest a route").click().run()
    assert not app.exception
    stops = app.session_state["route_stops"]
    assert 1 <= len(stops) <= 3
    button(app, "Save patrol").click().run()
    assert not app.exception
    decisions = views.decision_log(app_store).read()
    assert decisions["decision_type"].unique().tolist() == ["ASSIGN_PATROL"]
    assert sorted(decisions["vessel_id"]) == sorted(stops)
    assert decisions["patrol_id"].nunique() == 1
    assert decisions["officer"].unique().tolist() == ["Officer Reyes"]
    assert app.session_state["route_stops"] == []


def test_a_patrol_without_an_officer_name_is_refused(app_store):
    app = AppTest.from_file(APP, default_timeout=90).run()
    button(app, "Suggest a route").click().run()
    button(app, "Save patrol").click().run()
    assert any("officer's name" in error.value for error in app.error)
    assert views.decision_log(app_store).read().empty


def test_saving_weights_in_settings_rebuilds_the_ranking(app_store, tmp_path):
    before = app_store.read("risk_scores")["model_version"].iloc[0]
    app = AppTest.from_file(APP, default_timeout=120).run()
    app.number_input(key="weight_going_dark").set_value(60).run()
    button(app, "Save weights").click().run()
    assert not app.exception
    saved = (tmp_path / "overrides.json").read_text(encoding="utf-8")
    assert "going_dark" in saved
    after = app_store.read("risk_scores")
    assert after["model_version"].nunique() == 1 and after["model_version"].iloc[0] != before
    # 60 out of a total of 130 points of importance.
    signals = app_store.read("risk_signals")
    assert signals.loc[signals["signal"] == "going_dark", "weight"].iloc[0] == pytest.approx(60 / 130)
