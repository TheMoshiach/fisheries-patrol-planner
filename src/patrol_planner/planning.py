"""Suggests which patrol boat should go after which vessel.

A suggestion is only that: the officer makes and records the decision (see ``actions``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

EARTH_RADIUS_NM = 3440.065


def distance_nm(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles. Works on scalars or arrays."""
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(v, dtype=float)) for v in (lat1, lon1, lat2, lon2))
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_NM * np.arcsin(np.sqrt(a))


def rank_vessels(risk_scores: pd.DataFrame) -> pd.DataFrame:
    """Highest risk first, with a 1-based rank. Ties are ordered by vessel id for stability."""
    ranked = risk_scores.sort_values(
        ["risk_score", "vessel_id"], ascending=[False, True]
    ).reset_index(drop=True)
    ranked["rank"] = np.arange(1, len(ranked) + 1)
    return ranked


def suggest_assignments(
    ranked_vessels: pd.DataFrame,
    patrol_boats: pd.DataFrame,
    unavailable_boat_ids: set[str] | frozenset[str] = frozenset(),
    skip_vessel_ids: set[str] | frozenset[str] = frozenset(),
) -> pd.DataFrame:
    """Greedy plan: walk down the ranking and give each vessel the nearest free boat.

    A boat is eligible when it is available, not already assigned, and the vessel's
    last known position is within half the boat's range (so it can get back).
    Each boat gets at most one vessel.
    """
    columns = [
        "vessel_id",
        "patrol_boat_id",
        "rank",
        "risk_score",
        "distance_nm",
        "transit_hours",
    ]
    free = patrol_boats[
        (patrol_boats["status"] == "available")
        & ~patrol_boats["patrol_boat_id"].isin(unavailable_boat_ids)
    ].copy()
    rows = []
    for vessel in ranked_vessels.itertuples(index=False):
        if free.empty:
            break
        if vessel.vessel_id in skip_vessel_ids or pd.isna(vessel.last_lat) or pd.isna(vessel.last_lon):
            continue
        distances = pd.Series(
            distance_nm(vessel.last_lat, vessel.last_lon, free["home_lat"], free["home_lon"]),
            index=free.index,
        )
        reachable = distances[distances <= free["range_nm"] / 2]
        if reachable.empty:
            continue
        chosen = reachable.idxmin()
        boat = free.loc[chosen]
        rows.append(
            {
                "vessel_id": vessel.vessel_id,
                "patrol_boat_id": boat["patrol_boat_id"],
                "rank": vessel.rank,
                "risk_score": vessel.risk_score,
                "distance_nm": round(float(reachable[chosen]), 1),
                "transit_hours": round(float(reachable[chosen] / boat["max_speed_knots"]), 1),
            }
        )
        free = free.drop(index=chosen)
    return pd.DataFrame(rows, columns=columns)
