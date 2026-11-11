from __future__ import annotations

import pandas as pd
import pytest

from patrol_planner import pipeline, schemas
from patrol_planner.config import load_settings
from patrol_planner.sources import sample
from patrol_planner.storage import DataStore


@pytest.fixture(scope="session")
def settings():
    return load_settings()


@pytest.fixture(scope="session")
def sample_store(tmp_path_factory, settings) -> DataStore:
    """The synthetic sample, generated and run through the whole pipeline once."""
    store = DataStore(tmp_path_factory.mktemp("sample"))
    sample.generate_sample(store, settings, seed=7)
    pipeline.build(store, settings)
    return store


def raw_event(event_id="e1", kind="gap", vessel="v1", start="2026-03-01T00:00:00Z",
              end="2026-03-02T00:00:00Z", shore_km=200.0, regions=None, detail=None, **extra):
    """A raw event in the shape the Global Fishing Watch API returns."""
    return {
        "id": event_id,
        "type": kind,
        "start": start,
        "end": end,
        "position": {"lat": -1.0, "lon": -91.0},
        "regions": {"mpa": [], "eez": [], "rfmo": [], "highSeas": [], "mpaNoTake": [],
                    "mpaNoTakePartial": [], **(regions or {})},
        "distances": {"startDistanceFromShoreKm": shore_km, "endDistanceFromShoreKm": shore_km},
        "vessel": {"id": vessel, "name": f"vessel {vessel}", "ssvid": "123456789"},
        kind: detail or {},
        **extra,
    }


def features_frame(**columns) -> pd.DataFrame:
    """A vessel_features table with one row per value given; unspecified columns are zero."""
    length = max((len(v) for v in columns.values() if isinstance(v, list)), default=1)
    data = {"vessel_id": [f"v{i + 1}" for i in range(length)]}
    data.update(columns)
    frame = pd.DataFrame(data)
    frame["window_start"] = pd.Timestamp("2025-10-01", tz="UTC")
    frame["window_end"] = pd.Timestamp("2026-10-01", tz="UTC")
    frame["feature_id"] = frame["vessel_id"] + "|2026-10-01"
    frame = schemas.conform(frame, schemas.VESSEL_FEATURES)
    numeric = [c for c, t in schemas.VESSEL_FEATURES.columns.items()
               if t in (schemas.INT, schemas.FLOAT) and c not in ("last_lat", "last_lon", "ais_coverage_pct")]
    frame[numeric] = frame[numeric].fillna(0)
    return frame
