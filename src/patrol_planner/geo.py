"""Small geographic helpers: distances, bearings and outline simplification."""

from __future__ import annotations

import numpy as np

EARTH_RADIUS_NM = 3440.065


def distance_nm(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles. Works on scalars or arrays."""
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(v, dtype=float)) for v in (lat1, lon1, lat2, lon2))
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_NM * np.arcsin(np.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing from the first point to the second, 0-360 degrees."""
    lat1, lon1, lat2, lon2 = (float(np.radians(v)) for v in (lat1, lon1, lat2, lon2))
    x = np.sin(lon2 - lon1) * np.cos(lat2)
    y = np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(lon2 - lon1)
    return float((np.degrees(np.arctan2(x, y)) + 360) % 360)


def simplify_geometry(geometry: dict | None, precision: int = 2, min_points: int = 4) -> dict | None:
    """Thin a GeoJSON Polygon or MultiPolygon so it is light enough to draw.

    Coordinates are rounded (two decimals is about one kilometre) and points that
    repeat after rounding are dropped. Rings left with too few points to enclose
    an area, such as tiny islets, disappear.
    """
    if not geometry or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    kept = []
    for polygon in polygons:
        rings = [ring for ring in (_thin(ring, precision) for ring in polygon) if len(ring) >= min_points]
        # A polygon needs its outer ring; holes without one are meaningless.
        if rings and len(_thin(polygon[0], precision)) >= min_points:
            kept.append(rings)
    if not kept:
        return None
    return {"type": "MultiPolygon", "coordinates": kept}


def _thin(ring: list, precision: int) -> list:
    out: list[list[float]] = []
    for point in ring:
        rounded = [round(float(point[0]), precision), round(float(point[1]), precision)]
        if not out or rounded != out[-1]:
            out.append(rounded)
    if len(out) > 1 and out[0] != out[-1]:
        out.append(out[0])
    return out
