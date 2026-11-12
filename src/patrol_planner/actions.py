"""Officer decisions.

Each function here is one *action*: it takes named parameters, checks them against
the current tables, and appends rows to the ``patrol_decisions`` log. Nothing is
ever updated or deleted, so the log is a complete audit trail and the current
state (which boat is on which vessels) is always derived from it.

A patrol is one boat sent to one or more vessels in a planned order. It is stored
as one row per vessel, sharing a ``patrol_id``.
"""

from __future__ import annotations

import csv
import uuid
from collections.abc import Sequence
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

    def append(self, rows: Sequence[dict]) -> None:
        columns = list(schemas.PATROL_DECISIONS.columns)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A log written by an earlier version may lack newer columns: bring it up to date first.
        if self.path.exists():
            with open(self.path, newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle), [])
            if header != columns:
                self.read().to_csv(self.path, index=False)
        is_new = not self.path.exists()
        with open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            if is_new:
                writer.writeheader()
            for row in rows:
                writer.writerow({c: "" if row.get(c) is None else row[c] for c in columns})


# --- state derived from the log ---------------------------------------------------
def open_assignments(decisions: pd.DataFrame) -> pd.DataFrame:
    """Vessels a boat is currently assigned to: assignments made since its last release."""
    assigned = decisions[decisions["decision_type"] == ASSIGN_PATROL]
    released = decisions[decisions["decision_type"] == RELEASE_BOAT]
    last_release = released.groupby("patrol_boat_id")["decided_at"].max()
    since = pd.to_datetime(assigned["patrol_boat_id"].map(last_release.to_dict()), utc=True)
    still_open = assigned[since.isna() | (assigned["decided_at"] > since)]
    return still_open.sort_values(["decided_at", "route_stop"], kind="stable").reset_index(drop=True)


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
def assign_route(
    log: DecisionLog,
    *,
    officer: str,
    patrol_boat_id: str,
    vessel_ids: Sequence[str],
    rationale: str,
    ranked_vessels: pd.DataFrame,
    patrol_boats: pd.DataFrame,
    data_mode: str,
    now: pd.Timestamp | None = None,
) -> list[dict]:
    """Send a patrol boat to one or more vessels, in the order given."""
    officer = _require_officer(officer)
    vessel_ids = list(dict.fromkeys(vessel_ids))
    if not vessel_ids:
        raise ActionError("Choose at least one vessel for the patrol.")
    vessels = [_require_vessel(vessel_id, ranked_vessels) for vessel_id in vessel_ids]
    boats = patrol_boats[patrol_boats["patrol_boat_id"] == patrol_boat_id]
    if boats.empty:
        raise ActionError(f"Unknown patrol boat: {patrol_boat_id}")
    boat = boats.iloc[0]
    if boat["status"] != "available":
        raise ActionError(f"{boat['name']} is not available (status: {boat['status']}).")

    active = open_assignments(log.read())
    if (active["patrol_boat_id"] == patrol_boat_id).any():
        raise ActionError(f"{boat['name']} is already on a patrol. Release it first.")
    covered = active[active["vessel_id"].isin(vessel_ids)]
    if not covered.empty:
        first = covered.iloc[0]
        raise ActionError(
            f"Vessel {first['vessel_id']} already has {first['patrol_boat_id']} assigned to it."
        )

    decided_at = now if now is not None else pd.Timestamp.now(tz="UTC")
    patrol_id = str(uuid.uuid4())
    rows = [
        _row(ASSIGN_PATROL, officer, rationale, data_mode, decided_at, vessel, patrol_boat_id,
             patrol_id, stop)
        for stop, vessel in enumerate(vessels, start=1)
    ]
    log.append(rows)
    return rows


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
    """Send a patrol boat after a single vessel."""
    return assign_route(
        log, officer=officer, patrol_boat_id=patrol_boat_id, vessel_ids=[vessel_id],
        rationale=rationale, ranked_vessels=ranked_vessels, patrol_boats=patrol_boats,
        data_mode=data_mode, now=now,
    )[0]


def release_boat(
    log: DecisionLog,
    *,
    officer: str,
    patrol_boat_id: str,
    rationale: str,
    data_mode: str,
    now: pd.Timestamp | None = None,
) -> list[dict]:
    """End a boat's current patrol so it can be used again."""
    officer = _require_officer(officer)
    active = open_assignments(log.read())
    current = active[active["patrol_boat_id"] == patrol_boat_id]
    if current.empty:
        raise ActionError(f"{patrol_boat_id} has no open patrol to release.")
    decided_at = now if now is not None else pd.Timestamp.now(tz="UTC")
    rows = [
        _row(
            RELEASE_BOAT, officer, rationale, data_mode, decided_at,
            {
                "vessel_id": previous["vessel_id"],
                "risk_score_id": previous["risk_score_id"],
                "risk_score": previous["risk_score_at_decision"],
                "rank": previous["rank_at_decision"],
            },
            patrol_boat_id, previous["patrol_id"], previous["route_stop"],
        )
        for _, previous in current.iterrows()
    ]
    log.append(rows)
    return rows


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
    return _record(log, WATCHLIST, officer, rationale, data_mode, now, vessel)


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
    return _record(log, DISMISS, officer, rationale, data_mode, now, vessel)


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


def _record(log, decision_type, officer, rationale, data_mode, now, vessel) -> dict:
    decided_at = now if now is not None else pd.Timestamp.now(tz="UTC")
    row = _row(decision_type, officer, rationale, data_mode, decided_at, vessel, None, None, None)
    log.append([row])
    return row


def _row(decision_type, officer, rationale, data_mode, decided_at, vessel, patrol_boat_id,
         patrol_id, route_stop) -> dict:
    score = vessel.get("risk_score")
    rank = vessel.get("rank")
    return {
        "decision_id": str(uuid.uuid4()),
        "decided_at": decided_at.isoformat(),
        "officer": officer,
        "decision_type": decision_type,
        "vessel_id": vessel.get("vessel_id"),
        "patrol_boat_id": patrol_boat_id,
        "patrol_id": patrol_id,
        "route_stop": None if route_stop is None or pd.isna(route_stop) else int(route_stop),
        "risk_score_id": vessel.get("risk_score_id"),
        "risk_score_at_decision": None if pd.isna(score) else float(score),
        "rank_at_decision": None if pd.isna(rank) else int(rank),
        "rationale": (rationale or "").strip(),
        "data_mode": data_mode,
    }
