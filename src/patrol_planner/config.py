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
# Changes made on the app's settings page are stored here and laid over the defaults.
DEFAULT_OVERRIDES_PATH = PROJECT_ROOT / "config" / "overrides.json"

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
    trail_days: int = 90

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
    gap_max_hours: float
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
                self.gap_max_hours,
                self.tier_high,
                self.tier_elevated,
            ],
            sort_keys=True,
        )
        return f"{self.model_version}-{hashlib.sha1(payload.encode()).hexdigest()[:8]}"


@dataclass(frozen=True)
class EvaluationSettings:
    controls_per_case: int
    history_start: pd.Timestamp
    min_positions: int
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


def overrides_path() -> Path:
    return Path(os.environ.get("PATROL_PLANNER_OVERRIDES") or DEFAULT_OVERRIDES_PATH)


def read_overrides() -> dict:
    path = overrides_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_overrides(overrides: dict) -> None:
    """Save settings changed in the app. They are validated before being written."""
    parse_settings(_merge(_read_defaults(None), overrides), window_end="2020-01-01")
    path = overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, indent=2), encoding="utf-8")


def _read_defaults(path: Path | str | None) -> dict:
    path = Path(path or os.environ.get("PATROL_PLANNER_SETTINGS") or DEFAULT_SETTINGS_PATH)
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def _merge(base: dict, extra: dict) -> dict:
    """Lay ``extra`` over ``base``: nested tables are merged, everything else is replaced."""
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


merge_settings = _merge


def load_settings(
    path: Path | str | None = None,
    window_end: str | pd.Timestamp | None = None,
    use_overrides: bool = True,
) -> Settings:
    """Read the settings file, lay any saved overrides on top, and fix the analysis window.

    ``window_end`` pins the window to a dataset that has already been built. Without
    it, a window end of "auto" means the most recent day the source is likely to
    have complete data for.
    """
    raw = _read_defaults(path)
    if use_overrides:
        raw = _merge(raw, read_overrides())
    return parse_settings(raw, window_end)


def parse_settings(raw: dict, window_end: str | pd.Timestamp | None = None) -> Settings:
    configured = window_end if window_end is not None else raw["window"]["end"]
    if str(configured).lower() == "auto":
        lag = int(raw["window"].get("lag_days", 5))
        end = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=lag)
    else:
        end = pd.Timestamp(configured)
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
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
    if abs(sum(weights.values()) - 1.0) > 1e-6:
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
            trail_days=int(area.get("trail_days", 90)),
        ),
        scoring=ScoringSettings(
            model_version=str(scoring["model_version"]),
            weights=weights,
            half_points=half_points,
            gap_min_hours=float(scoring["gap_rule"]["min_hours"]),
            gap_min_distance_from_shore_km=float(
                scoring["gap_rule"]["min_distance_from_shore_km"]
            ),
            gap_max_hours=float(scoring["gap_rule"]["max_hours"]),
            tier_high=float(scoring["tiers"]["high"]),
            tier_elevated=float(scoring["tiers"]["elevated"]),
        ),
        evaluation=EvaluationSettings(
            controls_per_case=int(evaluation["controls_per_case"]),
            history_start=pd.Timestamp(evaluation["history_start"], tz="UTC"),
            min_positions=int(evaluation["min_positions"]),
            random_seed=int(evaluation["random_seed"]),
            bootstrap_samples=int(evaluation["bootstrap_samples"]),
            top_k=tuple(int(k) for k in evaluation["top_k"]),
        ),
        patrol_boats=tuple(dict(b) for b in raw.get("patrol_boats", [])),
    )
