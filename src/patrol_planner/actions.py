"""Officer decisions.

Each function here is one *action*: it takes named parameters, checks them against
the current tables, and appends exactly one row to the ``patrol_decisions`` log.
Nothing is ever updated or deleted, so the log is a complete audit trail and the
current state (which boat is on which vessel) is always derived from it.
"""

from __future__ import annotations

import csv
import uuid
from pathlib import Path

import pandas as pd

from . import schemas

ASSIGN_PATROL = "ASSIGN_PATROL"
RELEASE_BOAT = "RELEASE_BOAT"
WATCHLIST = "WATCHLIST"
DISMISS = "DISMISS"
DECISION_TYPES = (ASSIGN_PATROL, RELEASE_BOAT, WATCHLIST, DISMISS)


class ActionError(ValueError):
    """The action was rejected; the message says why in plain words."""


class DecisionLog:
    """Append-only CSV holding the ``patrol_decisions`` table."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def read(self) -> pd.DataFrame:
        schema = schemas.PATROL_DECISIONS
        if not self.path.exists():
            return schema.empty()
        frame = pd.read_csv(self.path, dtype="string", keep_default_na=False, na_values=[""])
        return schemas.conform(frame, schema)

    def append(self, row: dict) -> None:
        columns = list(schemas.PATROL_DECISIONS.columns)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.path.exists()
        with open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            if is_new:
                writer.writeheader()
            writer.writerow({c: "" if row.get(c) is None else row[c] for c in columns})


# --- state derived from the log ---------------------------------------------------
def open_assignments(decisions: pd.DataFrame) -> pd.DataFrame:
    """Assignments that have not been released: at most one per boat, the latest."""
    relevant = decisions[decisions["decision_type"].isin([ASSIGN_PATROL, RELEASE_BOAT])]
    latest = relevant.sort_values("decided_at", kind="stable").groupby("patrol_boat_id").tail(1)
    return latest[latest["decision_type"] == ASSIGN_PATROL].reset_index(drop=True)


def latest_vessel_status(decisions: pd.DataFrame) -> pd.DataFrame:
    """The most recent decision about each vessel."""
    about_vessels = decisions.dropna(subset=["vessel_id"])
    return (
        about_vessels.sort_values("decided_at", kind="stable")
        .groupby("vessel_id")
        .tail(1)
        .reset_index(drop=True)
    )


# --- actions ------------------------------------------------------------------------
def assign_patrol(
    log: DecisionLog,
    *,
    officer: str,
    vessel_id: str,
    patrol_boat_id: str,
    rationale: str,
    ranked_vessels: pd.DataFrame,
    patrol_boats: pd.DataFrame,
    data_mode: str,
    now: pd.Timestamp | None = None,
) -> dict:
    """Send a patrol boat after a vessel."""
    officer = _require_officer(officer)
    vessel = _require_vessel(vessel_id, ranked_vessels)
    boats = patrol_boats[patrol_boats["patrol_boat_id"] == patrol_boat_id]
    if boats.empty:
        raise ActionError(f"Unknown patrol boat: {patrol_boat_id}")
    boat = boats.iloc[0]
    if boat["status"] != "available":
        raise ActionError(f"{boat['name']} is not available (status: {boat['status']}).")

    active = open_assignments(log.read())
    busy = active[active["patrol_boat_id"] == patrol_boat_id]
    if not busy.empty:
        raise ActionError(
            f"{boat['name']} is already assigned to vessel {busy.iloc[0]['vessel_id']}. "
            "Release it first."
        )
    covered = active[active["vessel_id"] == vessel_id]
    if not covered.empty:
        raise ActionError(
            f"This vessel already has {covered.iloc[0]['patrol_boat_id']} assigned to it."
        )
    return _record(log, ASSIGN_PATROL, officer, rationale, data_mode, now, vessel, patrol_boat_id)


def release_boat(
    log: DecisionLog,
    *,
    officer: str,
    patrol_boat_id: str,
    rationale: str,
    data_mode: str,
    now: pd.Timestamp | None = None,
) -> dict:
    """End a boat's current assignment so it can be used again."""
    officer = _require_officer(officer)
    active = open_assignments(log.read())
    current = active[active["patrol_boat_id"] == patrol_boat_id]
    if current.empty:
        raise ActionError(f"{patrol_boat_id} has no open assignment to release.")
    previous = current.iloc[0]
    vessel = {
        "vessel_id": previous["vessel_id"],
        "risk_score_id": previous["risk_score_id"],
        "risk_score": previous["risk_score_at_decision"],
        "rank": previous["rank_at_decision"],
    }
    return _record(log, RELEASE_BOAT, officer, rationale, data_mode, now, vessel, patrol_boat_id)


def add_to_watchlist(
    log: DecisionLog,
    *,
    officer: str,
    vessel_id: str,
    rationale: str,
    ranked_vessels: pd.DataFrame,
    data_mode: str,
    now: pd.Timestamp | None = None,
) -> dict:
    """Keep an eye on a vessel without sending a boat."""
    officer = _require_officer(officer)
    vessel = _require_vessel(vessel_id, ranked_vessels)
    return _record(log, WATCHLIST, officer, rationale, data_mode, now, vessel, None)


def dismiss_vessel(
    log: DecisionLog,
    *,
    officer: str,
    vessel_id: str,
    rationale: str,
    ranked_vessels: pd.DataFrame,
    data_mode: str,
    now: pd.Timestamp | None = None,
) -> dict:
    """Record that a vessel was reviewed and needs no action. A reason is required."""
    officer = _require_officer(officer)
    if not (rationale or "").strip():
        raise ActionError("Give a reason for dismissing this vessel.")
    vessel = _require_vessel(vessel_id, ranked_vessels)
    return _record(log, DISMISS, officer, rationale, data_mode, now, vessel, None)


# --- internals ----------------------------------------------------------------------
def _require_officer(officer: str) -> str:
    officer = (officer or "").strip()
    if not officer:
        raise ActionError("Enter the officer's name before recording a decision.")
    return officer


def _require_vessel(vessel_id: str, ranked_vessels: pd.DataFrame) -> dict:
    match = ranked_vessels[ranked_vessels["vessel_id"] == vessel_id]
    if match.empty:
        raise ActionError(f"Vessel {vessel_id} is not in the current ranking.")
    return match.iloc[0].to_dict()


def _record(
    log: DecisionLog,
    decision_type: str,
    officer: str,
    rationale: str,
    data_mode: str,
    now: pd.Timestamp | None,
    vessel: dict,
    patrol_boat_id: str | None,
) -> dict:
    decided_at = now if now is not None else pd.Timestamp.now(tz="UTC")
    score = vessel.get("risk_score")
    rank = vessel.get("rank")
    row = {
        "decision_id": str(uuid.uuid4()),
        "decided_at": decided_at.isoformat(),
        "officer": officer,
        "decision_type": decision_type,
        "vessel_id": vessel.get("vessel_id"),
        "patrol_boat_id": patrol_boat_id,
        "risk_score_id": vessel.get("risk_score_id"),
        "risk_score_at_decision": None if pd.isna(score) else float(score),
        "rank_at_decision": None if pd.isna(rank) else int(rank),
        "rationale": (rationale or "").strip(),
        "data_mode": data_mode,
    }
    log.append(row)
    return row
