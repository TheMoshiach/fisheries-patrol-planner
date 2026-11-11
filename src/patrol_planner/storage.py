"""The only module that touches the disk for pipeline tables.

Layout under a data root (``data/real`` or ``data/sample``)::

    raw/         JSON-lines files exactly as received from each source
    clean/       typed tables with stable ids
    features/    one row per vessel
    scores/      risk scores and their signals
    evaluation/  accuracy metrics
    decisions/   append-only officer decision log (CSV)

Each stage is a folder of Parquet files, one per table, mirroring one dataset per
table in a hosted data platform.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path

import pandas as pd

from . import schemas
from .config import PROJECT_ROOT

DATA_ROOT = PROJECT_ROOT / "data"

STAGE_OF = {
    "vessels": "clean",
    "events": "clean",
    "event_protected_areas": "clean",
    "protected_areas": "clean",
    "protected_area_activity": "clean",
    "vessel_insights": "clean",
    "iuu_listings": "clean",
    "cohort_members": "clean",
    "patrol_boats": "clean",
    "vessel_features": "features",
    "risk_scores": "scores",
    "risk_signals": "scores",
    "evaluation_metrics": "evaluation",
    "evaluation_vessels": "evaluation",
}

RAW_DATASETS = (
    "protected_area_list",
    "patrol_area_report",
    "protected_area_reports",
    "vessel_identities",
    "events",
    "insights",
    "cohort",
)


class DataStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    # --- typed tables -----------------------------------------------------
    def path(self, table: str) -> Path:
        return self.root / STAGE_OF[table] / f"{table}.parquet"

    def exists(self, table: str) -> bool:
        return self.path(table).exists()

    def read(self, table: str) -> pd.DataFrame:
        schema = schemas.BY_NAME[table]
        return schemas.conform(pd.read_parquet(self.path(table)), schema)

    def write(self, table: str, df: pd.DataFrame) -> Path:
        schema = schemas.BY_NAME[table]
        df = schemas.validate(schemas.conform(df, schema), schema)
        path = self.path(table)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
        return path

    def read_all(self, tables: Iterable[str]) -> dict[str, pd.DataFrame]:
        return {t: self.read(t) for t in tables}

    # --- raw landing zone -------------------------------------------------
    def raw_path(self, dataset: str) -> Path:
        return self.root / "raw" / f"{dataset}.jsonl"

    def write_raw(self, dataset: str, records: Iterable[dict]) -> Path:
        path = self.raw_path(dataset)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        return path

    def read_raw(self, dataset: str) -> list[dict]:
        return list(self.iter_raw(dataset))

    def iter_raw(self, dataset: str) -> Iterator[dict]:
        path = self.raw_path(dataset)
        if not path.exists():
            return
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    # --- metadata ---------------------------------------------------------
    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def write_manifest(self, manifest: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))


def data_mode() -> str:
    """Which dataset the app and CLI use: ``real`` when it has been built, else ``sample``.

    Set PATROL_PLANNER_DATA=sample or =real to force one.
    """
    forced = os.environ.get("PATROL_PLANNER_DATA", "").strip().lower()
    if forced in ("real", "sample"):
        return forced
    return "real" if DataStore(DATA_ROOT / "real").exists("risk_scores") else "sample"


def open_store(mode: str | None = None) -> DataStore:
    return DataStore(DATA_ROOT / (mode or data_mode()))
