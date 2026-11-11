"""Settings: loaded once from ``config/settings.toml`` into plain, immutable objects."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.toml"

SIGNALS = (
    "going_dark",
    "encounters",
    "protected_areas",
    "unauthorized_fishing",
    "identity_changes",
)


@dataclass(frozen=True)
class Window:
    start: pd.Timestamp
    end: pd.Timestamp

    @property
    def start_date(self) -> str:
        return self.start.strftime("%Y-%m-%d")

    @property
    def end_date(self) -> str:
        return self.end.strftime("%Y-%m-%d")


@dataclass(frozen=True)
class PatrolArea:
    name: str
    bbox: tuple[float, float, float, float]  # west, south, east, north
    min_fishing_hours: float
    watched_protected_areas: tuple[str, ...]
    buffer_nm: float

    def contains(self, lat: float, lon: float) -> bool:
        west, south, east, north = self.bbox
        return south <= lat <= north and west <= lon <= east

    def geojson(self) -> dict:
        west, south, east, north = self.bbox
        ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
        return {"type": "Polygon", "coordinates": [ring]}


@dataclass(frozen=True)
class ScoringSettings:
    model_version: str
    weights: dict[str, float]
    half_points: dict[str, float]
    gap_min_hours: float
    gap_min_distance_from_shore_km: float
    tier_high: float
    tier_elevated: float

    @property
    def version_id(self) -> str:
        """Model version plus a short fingerprint of every number that affects a score."""
        payload = json.dumps(
            [
                self.weights,
                self.half_points,
                self.gap_min_hours,
                self.gap_min_distance_from_shore_km,
                self.tier_high,
                self.tier_elevated,
            ],
            sort_keys=True,
        )
        return f"{self.model_version}-{hashlib.sha1(payload.encode()).hexdigest()[:8]}"


@dataclass(frozen=True)
class EvaluationSettings:
    control_vessels: int
    random_seed: int
    bootstrap_samples: int
    top_k: tuple[int, ...]


@dataclass(frozen=True)
class Settings:
    window: Window
    patrol_area: PatrolArea
    scoring: ScoringSettings
    evaluation: EvaluationSettings
    patrol_boats: tuple[dict, ...]


class SettingsError(ValueError):
    pass


def load_settings(path: Path | str | None = None) -> Settings:
    path = Path(path or os.environ.get("PATROL_PLANNER_SETTINGS") or DEFAULT_SETTINGS_PATH)
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    return parse_settings(raw)


def parse_settings(raw: dict) -> Settings:
    end = pd.Timestamp(raw["window"]["end"], tz="UTC")
    start = end - pd.DateOffset(months=int(raw["window"]["months"]))

    area = raw["patrol_area"]
    bbox = tuple(float(v) for v in area["bbox"])
    if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise SettingsError("patrol_area.bbox must be [west, south, east, north]")

    scoring = raw["scoring"]
    weights = {k: float(v) for k, v in scoring["weights"].items()}
    half_points = {k: float(v) for k, v in scoring["half_points"].items()}
    if set(weights) != set(SIGNALS) or set(half_points) != set(SIGNALS):
        raise SettingsError(f"scoring weights and half_points must cover exactly {SIGNALS}")
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise SettingsError("scoring.weights must add up to 1")
    if any(v <= 0 for v in half_points.values()):
        raise SettingsError("scoring.half_points must be positive")

    evaluation = raw["evaluation"]
    return Settings(
        window=Window(start=start, end=end),
        patrol_area=PatrolArea(
            name=str(area["name"]),
            bbox=bbox,  # type: ignore[arg-type]
            min_fishing_hours=float(area["min_fishing_hours"]),
            watched_protected_areas=tuple(area["watched_protected_areas"]),
            buffer_nm=float(area["buffer_nm"]),
        ),
        scoring=ScoringSettings(
            model_version=str(scoring["model_version"]),
            weights=weights,
            half_points=half_points,
            gap_min_hours=float(scoring["gap_rule"]["min_hours"]),
            gap_min_distance_from_shore_km=float(
                scoring["gap_rule"]["min_distance_from_shore_km"]
            ),
            tier_high=float(scoring["tiers"]["high"]),
            tier_elevated=float(scoring["tiers"]["elevated"]),
        ),
        evaluation=EvaluationSettings(
            control_vessels=int(evaluation["control_vessels"]),
            random_seed=int(evaluation["random_seed"]),
            bootstrap_samples=int(evaluation["bootstrap_samples"]),
            top_k=tuple(int(k) for k in evaluation["top_k"]),
        ),
        patrol_boats=tuple(dict(b) for b in raw.get("patrol_boats", [])),
    )
