"""clean -> features: one row of behavioural features per vessel.

The ground-truth table (``iuu_listings``) is not an input, by construction.
"""

from __future__ import annotations

import pandas as pd

from .. import schemas
from ..config import ScoringSettings, Window

TRANSSHIPMENT_CAPABLE = ("carrier", "bunker", "tanker")


def build_vessel_features(
    vessels: pd.DataFrame,
    events: pd.DataFrame,
    protected_area_activity: pd.DataFrame,
    vessel_insights: pd.DataFrame,
    cohort_members: pd.DataFrame,
    window: Window,
    scoring: ScoringSettings,
) -> pd.DataFrame:
    base = pd.DataFrame({"vessel_id": vessels["vessel_id"].astype("string")})
    in_window = events[(events["start_time"] >= window.start) & (events["start_time"] < window.end)]

    parts = [
        _gap_features(in_window, scoring),
        _encounter_features(in_window),
        _watched_area_features(protected_area_activity),
        _insight_features(vessel_insights),
        _patrol_area_hours(cohort_members),
        _last_position(in_window),
    ]
    out = base
    for part in parts:
        out = out.merge(part, on="vessel_id", how="left")

    counts = [
        "fishing_events",
        "gap_events",
        "gaps_in_protected_areas",
        "encounter_events",
        "encounters_with_carrier",
        "encounters_potential_risk",
        "encounters_high_seas",
        "no_take_fishing_events",
        "unauthorized_fishing_events",
        "flag_changes",
    ]
    amounts = [
        "patrol_area_fishing_hours",
        "gap_hours",
        "longest_gap_hours",
        "watched_area_hours_inside",
        "watched_area_hours_near",
    ]
    for column in counts + amounts:
        out[column] = pd.to_numeric(out[column], errors="coerce").fillna(0)

    out["window_start"] = window.start
    out["window_end"] = window.end
    out["feature_id"] = out["vessel_id"] + "|" + window.end_date
    return schemas.conform(out, schemas.VESSEL_FEATURES)


def build_cohort_features(
    vessels: pd.DataFrame,
    events: pd.DataFrame,
    protected_area_activity: pd.DataFrame,
    vessel_insights: pd.DataFrame,
    cohort_members: pd.DataFrame,
    scoring: ScoringSettings,
) -> pd.DataFrame:
    """Features for every cohort member over that member's own analysis window.

    Patrol-area vessels share the main window; each evaluation group has its own.
    The same single-window function is applied to each window in turn.
    """
    frames = []
    windows = cohort_members[["window_start", "window_end"]].drop_duplicates()
    for start, end in windows.itertuples(index=False):
        members = cohort_members[
            (cohort_members["window_start"] == start) & (cohort_members["window_end"] == end)
        ]
        ids = set(members["vessel_id"])
        frames.append(
            build_vessel_features(
                vessels=vessels[vessels["vessel_id"].isin(ids)],
                events=events[events["vessel_id"].isin(ids)],
                protected_area_activity=protected_area_activity[
                    protected_area_activity["window_end"] == end
                ],
                vessel_insights=vessel_insights[vessel_insights["window_end"] == end],
                cohort_members=members,
                window=Window(start=start, end=end),
                scoring=scoring,
            )
        )
    if not frames:
        return schemas.VESSEL_FEATURES.empty()
    combined = pd.concat(frames, ignore_index=True).drop_duplicates("feature_id")
    return schemas.conform(combined, schemas.VESSEL_FEATURES)


def qualifying_gaps(events: pd.DataFrame, scoring: ScoringSettings) -> pd.DataFrame:
    """AIS gaps that can be read as "going dark".

    A gap counts when it is long enough to rule out satellite timing, short enough
    to rule out a lay-up, starts far enough offshore to rule out coastal reception
    problems, and the source has not judged it unintentional.
    """
    gaps = events[events["event_type"] == "gap"]
    long_enough = gaps["duration_hours"] >= scoring.gap_min_hours
    not_too_long = gaps["duration_hours"] <= scoring.gap_max_hours
    offshore = gaps["start_distance_from_shore_km"] >= scoring.gap_min_distance_from_shore_km
    deliberate = gaps["intentional_disabling"].fillna(True).astype(bool)
    return gaps[(long_enough & not_too_long & offshore).fillna(False) & deliberate]


def _gap_features(events: pd.DataFrame, scoring: ScoringSettings) -> pd.DataFrame:
    gaps = qualifying_gaps(events, scoring)
    grouped = gaps.groupby("vessel_id")
    return pd.DataFrame(
        {
            "gap_events": grouped.size(),
            "gap_hours": grouped["duration_hours"].sum(),
            "longest_gap_hours": grouped["duration_hours"].max(),
            "gaps_in_protected_areas": grouped["in_protected_area"].sum(),
        }
    ).reset_index()


def is_transshipment_capable(encounters: pd.DataFrame) -> pd.Series:
    """True where the other vessel in an encounter can take fish or deliver fuel."""
    described = (
        encounters["other_vessel_type"].fillna("") + " " + encounters["encounter_type"].fillna("")
    )
    return described.str.contains("|".join(TRANSSHIPMENT_CAPABLE), case=False)


def _encounter_features(events: pd.DataFrame) -> pd.DataFrame:
    encounters = events[events["event_type"] == "encounter"].copy()
    encounters["with_carrier"] = is_transshipment_capable(encounters)
    grouped = encounters.groupby("vessel_id")
    return pd.DataFrame(
        {
            "encounter_events": grouped.size(),
            "encounters_with_carrier": grouped["with_carrier"].sum(),
            "encounters_potential_risk": grouped["potential_risk"].sum(),
            "encounters_high_seas": grouped["on_high_seas"].sum(),
        }
    ).reset_index()


def _watched_area_features(activity: pd.DataFrame) -> pd.DataFrame:
    near = (activity["fishing_hours_within_buffer"] - activity["fishing_hours_inside"]).clip(lower=0)
    frame = pd.DataFrame(
        {
            "vessel_id": activity["vessel_id"],
            "watched_area_hours_inside": activity["fishing_hours_inside"],
            "watched_area_hours_near": near,
        }
    )
    return frame.groupby("vessel_id").sum().reset_index()


def _insight_features(insights: pd.DataFrame) -> pd.DataFrame:
    return insights.drop_duplicates("vessel_id")[
        [
            "vessel_id",
            "fishing_events",
            "no_take_fishing_events",
            "unauthorized_fishing_events",
            "flag_changes",
            "ais_coverage_pct",
        ]
    ]


def _patrol_area_hours(cohort_members: pd.DataFrame) -> pd.DataFrame:
    patrol = cohort_members[cohort_members["cohort"] == "patrol_area"]
    return patrol[["vessel_id", "patrol_area_fishing_hours"]].drop_duplicates("vessel_id")


def _last_position(events: pd.DataFrame) -> pd.DataFrame:
    located = events.dropna(subset=["lat", "lon", "end_time"]).sort_values("end_time")
    last = located.groupby("vessel_id").tail(1)
    return last.rename(columns={"lat": "last_lat", "lon": "last_lon", "end_time": "last_seen"})[
        ["vessel_id", "last_lat", "last_lon", "last_seen"]
    ]
