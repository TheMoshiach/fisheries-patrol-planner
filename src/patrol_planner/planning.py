"""Suggests which patrol boat should go after which vessel.

A suggestion is only that: the officer makes and records the decision (see ``actions``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .geo import bearing_deg, distance_nm  # noqa: F401  (distance_nm is part of this module's interface)


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



LEG_COLUMNS = ["leg", "from", "to", "vessel_id", "to_lat", "to_lon", "distance_nm", "bearing_deg", "hours"]


def plan_route(boat, stops: pd.DataFrame) -> pd.DataFrame:
    """A patrol route from the boat's home port past each vessel and back.

    ``stops`` needs ``vessel_id``, ``name``, ``last_lat`` and ``last_lon``. Vessels
    are visited nearest-first from wherever the boat is, which is simple to follow
    and close to the shortest tour for a handful of stops. Legs are straight lines:
    no allowance is made for weather, currents or land in the way.
    """
    remaining = stops.dropna(subset=["last_lat", "last_lon"]).to_dict("records")
    lat, lon, here = float(boat["home_lat"]), float(boat["home_lon"]), str(boat["home_port"])
    speed = float(boat["max_speed_knots"])
    legs = []

    def add_leg(to_label, vessel_id, to_lat, to_lon):
        distance = float(distance_nm(lat, lon, to_lat, to_lon))
        legs.append(
            {
                "leg": len(legs) + 1,
                "from": here,
                "to": to_label,
                "vessel_id": vessel_id,
                "to_lat": float(to_lat),
                "to_lon": float(to_lon),
                "distance_nm": round(distance, 1),
                "bearing_deg": round(bearing_deg(lat, lon, to_lat, to_lon)),
                "hours": round(distance / speed, 1) if speed > 0 else float("nan"),
            }
        )

    while remaining:
        nearest = min(
            remaining, key=lambda s: float(distance_nm(lat, lon, s["last_lat"], s["last_lon"]))
        )
        remaining.remove(nearest)
        add_leg(str(nearest["name"]), nearest["vessel_id"], nearest["last_lat"], nearest["last_lon"])
        lat, lon, here = float(nearest["last_lat"]), float(nearest["last_lon"]), str(nearest["name"])
    if legs:
        add_leg(str(boat["home_port"]), None, boat["home_lat"], boat["home_lon"])
    return pd.DataFrame(legs, columns=LEG_COLUMNS)


def route_summary(legs: pd.DataFrame, boat) -> dict:
    """Total distance and time for a route, and whether the boat's range covers it."""
    total = float(legs["distance_nm"].sum())
    return {
        "stops": int(legs["vessel_id"].notna().sum()),
        "total_nm": round(total, 1),
        "total_hours": round(float(legs["hours"].sum()), 1),
        "range_nm": float(boat["range_nm"]),
        "within_range": total <= float(boat["range_nm"]),
    }


def suggest_route(
    boat,
    ranked_vessels: pd.DataFrame,
    skip_vessel_ids: set[str] | frozenset[str] = frozenset(),
    max_stops: int = 3,
) -> list[str]:
    """Highest-risk vessels this boat can visit in one patrol and still get home.

    Walks down the ranking and adds a vessel whenever the route including it stays
    within the boat's range, up to ``max_stops`` vessels.
    """
    chosen: list[str] = []
    candidates = ranked_vessels.dropna(subset=["last_lat", "last_lon"])
    for vessel in candidates.itertuples(index=False):
        if len(chosen) >= max_stops:
            break
        if vessel.vessel_id in skip_vessel_ids:
            continue
        trial = candidates[candidates["vessel_id"].isin(chosen + [vessel.vessel_id])]
        if route_summary(plan_route(boat, trial), boat)["within_range"]:
            chosen.append(vessel.vessel_id)
    return chosen

