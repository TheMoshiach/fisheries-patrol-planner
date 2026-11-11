"""Read-only views over the tables, shaped for whatever front end displays them.

These are plain table joins with no display code, so the same views can back a
local app, a notebook, or an application built on an object layer.
"""

from __future__ import annotations

import pandas as pd

from . import actions
from .config import Settings
from .planning import rank_vessels
from .storage import DataStore
from .transforms.features import is_transshipment_capable, qualifying_gaps


def decision_log(store: DataStore) -> actions.DecisionLog:
    return actions.DecisionLog(store.root / "decisions" / "patrol_decisions.csv")


def patrol_area_ranking(store: DataStore) -> pd.DataFrame:
    """Vessels active in the patrol area, highest risk first, with identity and status."""
    cohort = store.read("cohort_members")
    patrol_ids = set(cohort.loc[cohort["cohort"] == "patrol_area", "vessel_id"])
    scores = store.read("risk_scores")
    ranked = rank_vessels(scores[scores["vessel_id"].isin(patrol_ids)])

    vessels = store.read("vessels")[["vessel_id", "name", "flag", "gear_type", "mmsi", "imo"]]
    features = store.read("vessel_features")[
        ["vessel_id", "patrol_area_fishing_hours", "ais_coverage_pct", "fishing_events"]
    ]
    table = ranked.merge(vessels, on="vessel_id", how="left").merge(
        features, on="vessel_id", how="left"
    )
    table["name"] = table["name"].fillna("(name not reported)")

    decisions = decision_log(store).read()
    status = actions.latest_vessel_status(decisions)[["vessel_id", "decision_type", "patrol_boat_id"]]
    status = status.rename(columns={"decision_type": "status", "patrol_boat_id": "assigned_boat"})
    table = table.merge(status, on="vessel_id", how="left")
    table["status"] = table["status"].map(STATUS_LABELS).fillna("Not reviewed")
    return table


STATUS_LABELS = {
    actions.ASSIGN_PATROL: "Patrol assigned",
    actions.RELEASE_BOAT: "Patrol completed",
    actions.WATCHLIST: "On watchlist",
    actions.DISMISS: "Dismissed",
}


def vessel_reasons(store: DataStore, vessel_id: str) -> pd.DataFrame:
    """Why a vessel has its score: one row per signal, strongest first."""
    signals = store.read("risk_signals")
    mine = signals[signals["vessel_id"] == vessel_id]
    return mine.sort_values("points", ascending=False).reset_index(drop=True)


def vessel_events(store: DataStore, vessel_id: str, settings: Settings) -> pd.DataFrame:
    """The individual events behind a vessel's score, newest first, described in words."""
    events = store.read("events")
    mine = events[events["vessel_id"] == vessel_id].copy()
    if mine.empty:
        return mine.assign(description=pd.Series(dtype="string"), counted=pd.Series(dtype="boolean"))
    counted_gaps = set(qualifying_gaps(mine, settings.scoring)["event_id"])
    with_carrier = is_transshipment_capable(mine)
    descriptions, counted = [], []
    for row, carrier in zip(mine.itertuples(index=False), with_carrier):
        if row.event_type == "gap":
            is_counted = row.event_id in counted_gaps
            text = f"AIS off for {row.duration_hours:.0f} h, {row.start_distance_from_shore_km:.0f} km from shore"
            if not is_counted:
                text += " (too short or too close to shore to count)"
        elif row.event_type == "encounter":
            is_counted = True
            other = row.other_vessel_name if pd.notna(row.other_vessel_name) else "unidentified vessel"
            kind = "carrier/bunker vessel" if carrier else "vessel"
            text = f"Met {kind} {other} for {row.duration_hours:.1f} h"
            if _yes(row.potential_risk):
                text += ", no matching public authorisation"
        else:
            is_counted = bool(_yes(row.in_no_take_area) or _yes(row.potential_risk))
            text = f"Apparent fishing for {row.duration_hours:.1f} h"
            if _yes(row.in_no_take_area):
                text += " inside a no-take area"
            if _yes(row.potential_risk):
                text += " with no known RFMO authorisation"
        if _yes(row.in_protected_area) and "no-take" not in text:
            text += " (inside a protected area)"
        descriptions.append(text)
        counted.append(is_counted)
    mine["description"] = descriptions
    mine["counted"] = counted
    return mine.sort_values("start_time", ascending=False).reset_index(drop=True)


def _yes(value) -> bool:
    """True only for a real True: missing values count as no."""
    return bool(pd.notna(value) and value)


def evaluation_listed_vessels(store: DataStore) -> pd.DataFrame:
    """Where each IUU-listed vessel landed in the evaluation ranking."""
    table = store.read("evaluation_vessels")
    vessels = store.read("vessels")[["vessel_id", "name", "flag", "gear_type", "imo"]]
    listed = table[table["iuu_listed"].fillna(False)].merge(vessels, on="vessel_id", how="left")
    listed["percentile"] = (listed["rank"] / len(table) * 100).round(1)
    return listed.sort_values("rank").reset_index(drop=True)
