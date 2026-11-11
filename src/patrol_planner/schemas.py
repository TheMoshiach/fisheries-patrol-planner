"""Table contracts.

Every table the system reads or writes is declared here once: its columns, types,
primary key and the tables it points to. The same declarations drive validation,
empty-table creation and the object-type mapping documented in AIP_MIGRATION.md.

Type names are pandas extension dtypes so that missing values survive round trips.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

STRING = "string"
FLOAT = "float64"
INT = "Int64"
BOOL = "boolean"
TIMESTAMP = "datetime64[ns, UTC]"


@dataclass(frozen=True)
class Link:
    """A foreign key from this table to the primary key of another table."""

    column: str
    target_table: str
    name: str


@dataclass(frozen=True)
class TableSchema:
    name: str
    primary_key: str
    columns: dict[str, str]
    description: str
    object_type: str
    title_column: str | None = None
    links: tuple[Link, ...] = field(default_factory=tuple)

    def empty(self) -> pd.DataFrame:
        return conform(pd.DataFrame(columns=list(self.columns)), self)


VESSELS = TableSchema(
    name="vessels",
    primary_key="vessel_id",
    object_type="Vessel",
    title_column="name",
    description="One row per vessel identity as tracked by Global Fishing Watch.",
    columns={
        "vessel_id": STRING,
        "name": STRING,
        "mmsi": STRING,
        "imo": STRING,
        "callsign": STRING,
        "flag": STRING,
        "gear_type": STRING,
        "vessel_type": STRING,
        "first_transmission": TIMESTAMP,
        "last_transmission": TIMESTAMP,
        "source": STRING,
    },
)

EVENTS = TableSchema(
    name="events",
    primary_key="event_id",
    object_type="VesselEvent",
    title_column="event_type",
    description="Apparent fishing, encounter and AIS-gap events for a vessel.",
    links=(Link("vessel_id", "vessels", "vessel"),),
    columns={
        "event_id": STRING,
        "event_type": STRING,  # fishing | encounter | gap
        "vessel_id": STRING,
        "start_time": TIMESTAMP,
        "end_time": TIMESTAMP,
        "duration_hours": FLOAT,
        "lat": FLOAT,
        "lon": FLOAT,
        "start_distance_from_shore_km": FLOAT,
        "end_distance_from_shore_km": FLOAT,
        "eez_ids": STRING,  # "|"-joined region ids, empty on the high seas
        "rfmo_ids": STRING,
        "on_high_seas": BOOL,
        "in_protected_area": BOOL,
        "in_no_take_area": BOOL,
        "potential_risk": BOOL,  # no matching public RFMO authorisation
        "other_vessel_id": STRING,
        "other_vessel_name": STRING,
        "other_vessel_flag": STRING,
        "other_vessel_type": STRING,
        "encounter_type": STRING,
        "source": STRING,
    },
)

EVENT_PROTECTED_AREAS = TableSchema(
    name="event_protected_areas",
    primary_key="event_protected_area_id",
    object_type="(link) VesselEvent <-> ProtectedArea",
    description="Many-to-many join between events and the protected areas they fall in.",
    links=(
        Link("event_id", "events", "event"),
        Link("protected_area_id", "protected_areas", "protectedArea"),
    ),
    columns={
        "event_protected_area_id": STRING,
        "event_id": STRING,
        "protected_area_id": STRING,
        "no_take": BOOL,
    },
)

PROTECTED_AREAS = TableSchema(
    name="protected_areas",
    primary_key="protected_area_id",
    object_type="ProtectedArea",
    title_column="name",
    description="Marine protected areas referenced by events or watched by the officer.",
    columns={
        "protected_area_id": STRING,
        "name": STRING,
        "watched": BOOL,
        "source": STRING,
    },
)

PROTECTED_AREA_ACTIVITY = TableSchema(
    name="protected_area_activity",
    primary_key="activity_id",
    object_type="ProtectedAreaActivity",
    description=(
        "Apparent fishing hours per vessel inside a watched protected area and "
        "inside a buffer around it, for one analysis window."
    ),
    links=(
        Link("vessel_id", "vessels", "vessel"),
        Link("protected_area_id", "protected_areas", "protectedArea"),
    ),
    columns={
        "activity_id": STRING,
        "vessel_id": STRING,
        "protected_area_id": STRING,
        "window_start": TIMESTAMP,
        "window_end": TIMESTAMP,
        "buffer_nm": FLOAT,
        "fishing_hours_inside": FLOAT,
        "fishing_hours_within_buffer": FLOAT,  # includes the hours inside
        "source": STRING,
    },
)

VESSEL_INSIGHTS = TableSchema(
    name="vessel_insights",
    primary_key="vessel_id",
    object_type="(properties on Vessel)",
    description=(
        "Per-vessel indicators for the window: fishing-event counts, fishing in "
        "no-take areas, fishing without a known RFMO authorisation, AIS coverage "
        "and flag changes."
    ),
    links=(Link("vessel_id", "vessels", "vessel"),),
    columns={
        "vessel_id": STRING,
        "window_start": TIMESTAMP,
        "window_end": TIMESTAMP,
        "fishing_events": INT,
        "no_take_fishing_events": INT,
        "unauthorized_fishing_events": INT,
        "ais_coverage_pct": FLOAT,
        "flag_changes": INT,
        "source": STRING,
    },
)

IUU_LISTINGS = TableSchema(
    name="iuu_listings",
    primary_key="listing_id",
    object_type="IuuListing",
    description=(
        "Ground truth: periods during which a vessel appeared on an official RFMO "
        "IUU vessel list. Never an input to features or scores."
    ),
    links=(Link("vessel_id", "vessels", "vessel"),),
    columns={
        "listing_id": STRING,
        "vessel_id": STRING,
        "listed_from": TIMESTAMP,
        "listed_to": TIMESTAMP,
        "times_listed_total": INT,
        "source": STRING,
    },
)

COHORT_MEMBERS = TableSchema(
    name="cohort_members",
    primary_key="cohort_member_id",
    object_type="(property/set on Vessel)",
    description="Which vessels belong to the patrol-area cohort and the evaluation cohort.",
    links=(Link("vessel_id", "vessels", "vessel"),),
    columns={
        "cohort_member_id": STRING,
        "cohort": STRING,  # patrol_area | evaluation
        "vessel_id": STRING,
        "selection_reason": STRING,  # active_in_patrol_area | iuu_list_seed | random_control
        "patrol_area_fishing_hours": FLOAT,
    },
)

VESSEL_FEATURES = TableSchema(
    name="vessel_features",
    primary_key="feature_id",
    object_type="(intermediate dataset)",
    description="One row of behavioural features per vessel and analysis window.",
    links=(Link("vessel_id", "vessels", "vessel"),),
    columns={
        "feature_id": STRING,
        "vessel_id": STRING,
        "window_start": TIMESTAMP,
        "window_end": TIMESTAMP,
        "fishing_events": INT,
        "patrol_area_fishing_hours": FLOAT,
        "gap_events": INT,
        "gap_hours": FLOAT,
        "longest_gap_hours": FLOAT,
        "gaps_in_protected_areas": INT,
        "encounter_events": INT,
        "encounters_with_carrier": INT,
        "encounters_potential_risk": INT,
        "encounters_high_seas": INT,
        "no_take_fishing_events": INT,
        "watched_area_hours_inside": FLOAT,
        "watched_area_hours_near": FLOAT,
        "unauthorized_fishing_events": INT,
        "flag_changes": INT,
        "ais_coverage_pct": FLOAT,
        "last_lat": FLOAT,
        "last_lon": FLOAT,
        "last_seen": TIMESTAMP,
    },
)

RISK_SCORES = TableSchema(
    name="risk_scores",
    primary_key="risk_score_id",
    object_type="RiskScore",
    title_column="summary",
    description="One risk score per vessel, analysis window and model version.",
    links=(Link("vessel_id", "vessels", "vessel"),),
    columns={
        "risk_score_id": STRING,
        "vessel_id": STRING,
        "as_of_date": TIMESTAMP,
        "window_start": TIMESTAMP,
        "model_version": STRING,
        "risk_score": FLOAT,  # 0-100
        "risk_tier": STRING,  # High | Elevated | Low
        "data_confidence": STRING,  # high | medium | low
        "summary": STRING,
        "last_lat": FLOAT,
        "last_lon": FLOAT,
        "last_seen": TIMESTAMP,
    },
)

RISK_SIGNALS = TableSchema(
    name="risk_signals",
    primary_key="risk_signal_id",
    object_type="RiskSignal",
    title_column="label",
    description="The contribution of each signal to a risk score, with a plain-words reason.",
    links=(
        Link("risk_score_id", "risk_scores", "riskScore"),
        Link("vessel_id", "vessels", "vessel"),
    ),
    columns={
        "risk_signal_id": STRING,
        "risk_score_id": STRING,
        "vessel_id": STRING,
        "signal": STRING,
        "label": STRING,
        "intensity": FLOAT,
        "strength": FLOAT,  # 0-1 after saturation
        "weight": FLOAT,
        "points": FLOAT,  # weight * strength * 100
        "explanation": STRING,
    },
)

PATROL_BOATS = TableSchema(
    name="patrol_boats",
    primary_key="patrol_boat_id",
    object_type="PatrolBoat",
    title_column="name",
    description="The officer's patrol assets.",
    columns={
        "patrol_boat_id": STRING,
        "name": STRING,
        "home_port": STRING,
        "home_lat": FLOAT,
        "home_lon": FLOAT,
        "max_speed_knots": FLOAT,
        "range_nm": FLOAT,
        "status": STRING,  # available | maintenance
        "source": STRING,
    },
)

PATROL_DECISIONS = TableSchema(
    name="patrol_decisions",
    primary_key="decision_id",
    object_type="PatrolDecision",
    title_column="decision_type",
    description="Append-only log of officer decisions; each row is one submitted action.",
    links=(
        Link("vessel_id", "vessels", "vessel"),
        Link("patrol_boat_id", "patrol_boats", "patrolBoat"),
        Link("risk_score_id", "risk_scores", "riskScore"),
    ),
    columns={
        "decision_id": STRING,
        "decided_at": TIMESTAMP,
        "officer": STRING,
        "decision_type": STRING,  # ASSIGN_PATROL | RELEASE_BOAT | WATCHLIST | DISMISS
        "vessel_id": STRING,
        "patrol_boat_id": STRING,
        "risk_score_id": STRING,
        "risk_score_at_decision": FLOAT,
        "rank_at_decision": INT,
        "rationale": STRING,
        "data_mode": STRING,  # real | sample
    },
)

EVALUATION_METRICS = TableSchema(
    name="evaluation_metrics",
    primary_key="metric_id",
    object_type="EvaluationMetric",
    description="Accuracy of a ranking against the IUU-list ground truth.",
    columns={
        "metric_id": STRING,
        "evaluation_run_id": STRING,
        "model_version": STRING,
        "scorer": STRING,  # risk_score | baseline:* | signal:*
        "metric": STRING,
        "k": INT,
        "value": FLOAT,
        "ci_low": FLOAT,
        "ci_high": FLOAT,
        "n_vessels": INT,
        "n_positive": INT,
    },
)

EVALUATION_VESSELS = TableSchema(
    name="evaluation_vessels",
    primary_key="vessel_id",
    object_type="(evaluation detail)",
    description="Label and score for each vessel in the evaluation cohort.",
    links=(Link("vessel_id", "vessels", "vessel"),),
    columns={
        "vessel_id": STRING,
        "iuu_listed": BOOL,
        "risk_score": FLOAT,
        "rank": INT,
        "selection_reason": STRING,
    },
)

ALL_TABLES: tuple[TableSchema, ...] = (
    VESSELS,
    EVENTS,
    EVENT_PROTECTED_AREAS,
    PROTECTED_AREAS,
    PROTECTED_AREA_ACTIVITY,
    VESSEL_INSIGHTS,
    IUU_LISTINGS,
    COHORT_MEMBERS,
    VESSEL_FEATURES,
    RISK_SCORES,
    RISK_SIGNALS,
    PATROL_BOATS,
    PATROL_DECISIONS,
    EVALUATION_METRICS,
    EVALUATION_VESSELS,
)

BY_NAME: dict[str, TableSchema] = {t.name: t for t in ALL_TABLES}


class SchemaError(ValueError):
    """Raised when a table does not satisfy its contract."""


def conform(df: pd.DataFrame, schema: TableSchema) -> pd.DataFrame:
    """Return ``df`` with exactly the schema's columns, in order, with the schema's types.

    Missing columns are added as nulls; extra columns are dropped.
    """
    out = pd.DataFrame(index=df.index)
    for column, dtype in schema.columns.items():
        if column in df.columns:
            series = df[column]
        else:
            series = pd.Series([pd.NA] * len(df), index=df.index, dtype="object")
        out[column] = _cast(series, dtype)
    return out.reset_index(drop=True)


def _cast(series: pd.Series, dtype: str) -> pd.Series:
    if dtype == TIMESTAMP:
        return pd.to_datetime(series, utc=True, errors="coerce", format="ISO8601").astype(TIMESTAMP)
    if dtype == FLOAT:
        return pd.to_numeric(series, errors="coerce").astype(FLOAT)
    if dtype == INT:
        return pd.to_numeric(series, errors="coerce").round().astype(INT)
    if dtype == BOOL:
        return series.astype(BOOL)
    if dtype == STRING:
        return series.astype(STRING)
    raise SchemaError(f"unsupported dtype {dtype!r}")


def validate(df: pd.DataFrame, schema: TableSchema) -> pd.DataFrame:
    """Check columns, types and primary-key integrity. Returns ``df`` unchanged."""
    missing = [c for c in schema.columns if c not in df.columns]
    if missing:
        raise SchemaError(f"{schema.name}: missing columns {missing}")
    for column, dtype in schema.columns.items():
        actual = str(df[column].dtype)
        if actual != dtype:
            raise SchemaError(f"{schema.name}.{column}: expected {dtype}, found {actual}")
    key = df[schema.primary_key]
    if key.isna().any():
        raise SchemaError(f"{schema.name}: null values in primary key {schema.primary_key}")
    if key.duplicated().any():
        dupes = key[key.duplicated()].head(3).tolist()
        raise SchemaError(f"{schema.name}: duplicate primary keys, e.g. {dupes}")
    return df


def check_links(tables: dict[str, pd.DataFrame]) -> list[str]:
    """Return a description of every foreign key value with no matching target row."""
    problems: list[str] = []
    for schema in ALL_TABLES:
        if schema.name not in tables:
            continue
        df = tables[schema.name]
        for link in schema.links:
            if link.target_table not in tables:
                continue
            target = BY_NAME[link.target_table]
            known = set(tables[link.target_table][target.primary_key].dropna())
            values = df[link.column].dropna()
            orphans = values[~values.isin(known)]
            if len(orphans):
                problems.append(
                    f"{schema.name}.{link.column} -> {link.target_table}: "
                    f"{orphans.nunique()} unknown ids, e.g. {orphans.iloc[0]}"
                )
    return problems
