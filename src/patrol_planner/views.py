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
    patrol = cohort.loc[cohort["cohort"] == "patrol_area", ["vessel_id", "window_end"]]
    patrol = patrol.rename(columns={"window_end": "as_of_date"})
    # A vessel may also have been scored over another period for the evaluation;
    # the ranking uses only the score for the patrol window.
    ranked = rank_vessels(store.read("risk_scores").merge(patrol, on=["vessel_id", "as_of_date"]))

    vessels = store.read("vessels")[["vessel_id", "name", "flag", "gear_type", "mmsi", "imo"]]
    features = store.read("vessel_features")[
        ["vessel_id", "window_end", "patrol_area_fishing_hours", "ais_coverage_pct", "fishing_events"]
    ].rename(columns={"window_end": "as_of_date"})
    table = ranked.merge(vessels, on="vessel_id", how="left").merge(
        features, on=["vessel_id", "as_of_date"], how="left"
    )
    table["name"] = table["name"].fillna("(name not reported)")

    decisions = decision_log(store).read()
    status =actions.latest_vessel_status(decisions)[["vessel_id", "decision_type", "patrol_boat_id"]]
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


def vessel_reasons(store: DataStore, risk_score_id: str) -> pd.DataFrame:
    """Why a score is what it is: one row per signal, strongest first."""
    signals = store.read("risk_signals")
    mine = signals[signals["risk_score_id"] == risk_score_id]
    return mine.sort_values("points", ascending=False).reset_index(drop=True)


def vessel_trail(store: DataStore, vessel_id: str, settings: Settings) -> pd.DataFrame:
    """Where a vessel apparently fished over the trail period, oldest first.

    This is a sequence of fishing locations, not the vessel's actual track: the
    source does not release tracks to public users.
    """
    events = store.read("events")
    since = settings.window.end - pd.Timedelta(days=settings.patrol_area.trail_days)
    mine = events[
        (events["vessel_id"] == vessel_id)
        & (events["event_type"] == "fishing")
        & (events["start_time"] >= since)
        & (events["start_time"] < settings.window.end)
    ].dropna(subset=["lat", "lon"])
    return mine.sort_values("start_time")[["start_time", "lat", "lon", "duration_hours"]].reset_index(drop=True)


def protected_area_shapes(store: DataStore) -> list[dict]:
    """Outlines of the watched protected areas, when they have been downloaded."""
    return store.read_raw("protected_area_shapes")


def vessel_events(
    store: DataStore, vessel_id: str, settings: Settings, include_routine_fishing: bool = False
) -> pd.DataFrame:
    """The individual events behind a vessel's score, newest first, described in words.

    Ordinary apparent fishing events are left out unless asked for: there are
    hundreds per vessel and they do not affect the score.
    """
    events = store.read("events")
    in_window = (events["start_time"] >= settings.window.start) & (events["start_time"] < settings.window.end)
    mine = events[(events["vessel_id"] == vessel_id) & in_window].copy()
    if not include_routine_fishing:
        routine = (
            (mine["event_type"] == "fishing")
            & ~mine["in_no_take_area"].fillna(False)
            & ~mine["potential_risk"].fillna(False)
        )
        mine = mine[~routine]
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
                text += " (not counted: too short, too long, too near shore, or judged unintentional)"
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
    vessels = store.read("vessels")[["vessel_id", "name", "flag", "imo"]]
    # Where each listed vessel stands among the comparison vessels of its own group.
    group_size = table.groupby("match_group")["vessel_id"].transform("size")
    table["rank_in_group"] = (
        table.groupby("match_group")["risk_score"].rank(method="average", ascending=False)
    )
    table["group_size"] = group_size
    listed = table[table["iuu_listed"].fillna(False)].merge(vessels, on="vessel_id", how="left")
    listed["percentile"] = (listed["rank"] / len(table) * 100).round(1)
    return listed.sort_values("rank").reset_index(drop=True)
