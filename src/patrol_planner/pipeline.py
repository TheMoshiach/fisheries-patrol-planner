"""Runs the pipeline stages in order: raw -> clean -> features -> scores -> evaluation.

Each stage reads its input tables from the store, calls pure transform functions,
and writes its output tables. All logic lives in the transforms; this module only
wires datasets to functions, like a pipeline definition.
"""

from __future__ import annotations

import pandas as pd

from . import evaluation as evaluation_module
from . import schemas
from .config import Settings
from .storage import DataStore
from .transforms import clean, features, scoring

CLEAN_TABLES = (
    "vessels",
    "events",
    "event_protected_areas",
    "protected_areas",
    "protected_area_activity",
    "vessel_insights",
    "iuu_listings",
    "cohort_members",
    "patrol_boats",
)


def run_clean(store: DataStore, settings: Settings) -> dict[str, pd.DataFrame]:
    raw_events = store.read_raw("events")
    raw_insights = store.read_raw("insights")
    raw_area_report = store.read_raw("patrol_area_report")
    raw_area_list = store.read_raw("protected_area_list")
    raw_area_reports = store.read_raw("protected_area_reports")

    event_areas = clean.clean_event_protected_areas(raw_events)
    area_activity = clean.clean_protected_area_activity(raw_area_reports)
    cohort = clean.clean_cohort_members(
        store.read_raw("cohort"), raw_area_report, settings.patrol_area.min_fishing_hours
    )
    vessels = clean.clean_vessels(
        store.read_raw("vessel_identities"), raw_area_report + raw_area_reports, raw_events
    )
    # Only vessels that belong to a cohort are kept: counterpart vessels seen in
    # encounters stay as ids on the event rows.
    vessels = vessels[vessels["vessel_id"].isin(set(cohort["vessel_id"]))].reset_index(drop=True)
    members = set(vessels["vessel_id"])
    events = clean.clean_events(raw_events)
    events = events[events["vessel_id"].isin(members)].reset_index(drop=True)
    event_areas = event_areas[event_areas["event_id"].isin(set(events["event_id"]))]
    area_activity = area_activity[area_activity["vessel_id"].isin(members)]
    insights = clean.clean_vessel_insights(raw_insights)
    listings = clean.clean_iuu_listings(raw_insights)

    tables = {
        "vessels": vessels,
        "events": events,
        "event_protected_areas": event_areas.reset_index(drop=True),
        "protected_area_activity": area_activity.reset_index(drop=True),
        "protected_areas": clean.clean_protected_areas(
            raw_area_list, event_areas, area_activity, settings.patrol_area.watched_protected_areas
        ),
        "vessel_insights": insights[insights["vessel_id"].isin(members)].reset_index(drop=True),
        "iuu_listings": listings[listings["vessel_id"].isin(members)].reset_index(drop=True),
        "cohort_members": cohort[cohort["vessel_id"].isin(members)].reset_index(drop=True),
        "patrol_boats": clean.clean_patrol_boats(settings.patrol_boats),
    }
    problems = schemas.check_links(tables)
    if problems:
        raise schemas.SchemaError("broken links after cleaning: " + "; ".join(problems))
    for name, table in tables.items():
        store.write(name, table)
    return tables


def run_features(store: DataStore, settings: Settings) -> pd.DataFrame:
    table = features.build_vessel_features(
        vessels=store.read("vessels"),
        events=store.read("events"),
        protected_area_activity=store.read("protected_area_activity"),
        vessel_insights=store.read("vessel_insights"),
        cohort_members=store.read("cohort_members"),
        window=settings.window,
        scoring=settings.scoring,
    )
    store.write("vessel_features", table)
    return table


def run_scores(store: DataStore, settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame]:
    vessel_features = store.read("vessel_features")
    scores = scoring.score_vessels(vessel_features, settings.scoring)
    signals = scoring.explain_signals(vessel_features, settings.scoring)
    store.write("risk_scores", scores)
    store.write("risk_signals", signals)
    return scores, signals


def run_evaluation(store: DataStore, settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame]:
    evaluation_vessels = evaluation_module.build_evaluation_vessels(
        risk_scores=store.read("risk_scores"),
        iuu_listings=store.read("iuu_listings"),
        cohort_members=store.read("cohort_members"),
        vessels=store.read("vessels"),
        window=settings.window,
    )
    metrics = evaluation_module.evaluate(
        evaluation_vessels=evaluation_vessels,
        vessel_features=store.read("vessel_features"),
        scoring=settings.scoring,
        settings=settings.evaluation,
        window=settings.window,
    )
    store.write("evaluation_vessels", evaluation_vessels)
    store.write("evaluation_metrics", metrics)
    return evaluation_vessels, metrics


def build(store: DataStore, settings: Settings) -> dict:
    """Run every stage and return a short summary of what was produced."""
    tables = run_clean(store, settings)
    vessel_features = run_features(store, settings)
    scores, _ = run_scores(store, settings)
    evaluation_vessels, metrics = run_evaluation(store, settings)
    cohort = tables["cohort_members"]
    return {
        "vessels": len(tables["vessels"]),
        "events": len(tables["events"]),
        "patrol_area_vessels": int((cohort["cohort"] == "patrol_area").sum()),
        "evaluation_vessels": len(evaluation_vessels),
        "iuu_listed_in_evaluation": int(evaluation_vessels["iuu_listed"].sum()),
        "features": len(vessel_features),
        "scores": len(scores),
        "metrics": len(metrics),
    }
