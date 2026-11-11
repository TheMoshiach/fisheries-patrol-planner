"""Smoke test for the app: it must render on the sample data and save a decision."""

from pathlib import Path

import pytest

from patrol_planner import pipeline, views
from patrol_planner.sources import sample
from patrol_planner.storage import DataStore

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = str(Path(__file__).resolve().parents[1] / "app" / "streamlit_app.py")


@pytest.fixture
def app_store(tmp_path, monkeypatch, settings):
    store = DataStore(tmp_path / "sample")
    sample.generate_sample(store, settings, seed=7)
    pipeline.build(store, settings)
    monkeypatch.setenv("PATROL_PLANNER_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("PATROL_PLANNER_DATA", "sample")
    return store


def test_app_renders_and_labels_sample_data(app_store):
    app = AppTest.from_file(APP, default_timeout=60).run()
    assert not app.exception
    assert app.title[0].value == "Fisheries Patrol Planner"
    assert any("Synthetic sample data" in warning.value for warning in app.warning)
    assert [tab.label for tab in app.tabs] == [
        "Ranking", "Patrol plan", "Decision log", "Accuracy", "About the data"]


def test_accepting_a_suggestion_saves_a_decision(app_store):
    app = AppTest.from_file(APP, default_timeout=60).run()
    app.text_input(key="officer").set_value("Officer Reyes").run()
    accept = next(button for button in app.button if (button.key or "").startswith("accept_"))
    accept.click().run()
    assert not app.exception
    decisions = views.decision_log(app_store).read()
    assert len(decisions) == 1
    assert decisions.iloc[0]["decision_type"] == "ASSIGN_PATROL"
    assert decisions.iloc[0]["officer"] == "Officer Reyes"


def test_a_decision_without_an_officer_name_is_refused(app_store):
    app = AppTest.from_file(APP, default_timeout=60).run()
    accept = next(button for button in app.button if (button.key or "").startswith("accept_"))
    accept.click().run()
    assert any("officer's name" in error.value for error in app.error)
    assert views.decision_log(app_store).read().empty
