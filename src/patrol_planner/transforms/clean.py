"""raw -> clean.

Inputs are records exactly as received from each source (see ``storage.RAW_DATASETS``).
Outputs are typed tables with stable ids, one function per output table.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable

import pandas as pd

from .. import schemas

GFW = "Global Fishing Watch"
EVENT_TYPES = {"fishing": "fishing", "encounter": "encounter", "gap": "gap"}


# --- helpers -----------------------------------------------------------------
def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _upper(value) -> str | None:
    text = _text(value)
    return text.upper() if text else None


def _lower(value) -> str | None:
    text = _text(value)
    return text.lower() if text else None


def _join(values) -> str:
    return "|".join(str(v) for v in (values or []))


def normalise_name(text: str) -> str:
    """Lower-case, strip accents and punctuation: used to match protected-area names."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join("".join(c if c.isalnum() else " " for c in plain.lower()).split())


def _frame(rows: list[dict], schema: schemas.TableSchema) -> pd.DataFrame:
    return schemas.conform(pd.DataFrame(rows, columns=list(schema.columns)), schema)


# --- vessels -----------------------------------------------------------------
def clean_vessels(
    raw_vessel_identities: Iterable[dict],
    raw_report_rows: Iterable[dict],
    raw_events: Iterable[dict],
) -> pd.DataFrame:
    """One row per vessel id.

    Identity comes, in order of preference, from the vessel identity records, the
    activity report rows, and finally the minimal vessel stub carried on each event.
    """
    rows: list[dict] = []

    for entry in raw_vessel_identities:
        combined = {c.get("vesselId"): c for c in entry.get("combinedSourcesInfo") or []}
        registry = entry.get("registryInfo") or []
        registry_imo = next((_text(r.get("imo")) for r in registry if _text(r.get("imo"))), None)
        for info in entry.get("selfReportedInfo") or []:
            vessel_id = _text(info.get("id"))
            if not vessel_id:
                continue
            sources = combined.get(vessel_id, {})
            rows.append(
                {
                    "vessel_id": vessel_id,
                    "name": _upper(info.get("shipname")),
                    "mmsi": _text(info.get("ssvid")),
                    "imo": _text(info.get("imo")) or registry_imo,
                    "callsign": _upper(info.get("callsign")),
                    "flag": _upper(info.get("flag")),
                    "gear_type": _latest_name(sources.get("geartypes"))
                    or _lower(_first(info.get("geartype"))),
                    "vessel_type": _latest_name(sources.get("shiptypes"))
                    or _lower(_first(info.get("shiptype"))),
                    "first_transmission": info.get("transmissionDateFrom"),
                    "last_transmission": info.get("transmissionDateTo"),
                    "_priority": 0,
                }
            )

    for row in raw_report_rows:
        vessel_id = _text(row.get("vesselId") or row.get("vessel_id"))
        if not vessel_id:
            continue
        rows.append(
            {
                "vessel_id": vessel_id,
                "name": _upper(row.get("shipName")),
                "mmsi": _text(row.get("mmsi")),
                "imo": _text(row.get("imo")),
                "callsign": _upper(row.get("callsign")),
                "flag": _upper(row.get("flag")),
                "gear_type": _lower(row.get("geartype")),
                "vessel_type": _lower(row.get("vesselType") or row.get("vessel_type")),
                "first_transmission": row.get("firstTransmissionDate"),
                "last_transmission": row.get("lastTransmissionDate"),
                "_priority": 1,
            }
        )

    for event in raw_events:
        vessel = event.get("vessel") or {}
        vessel_id = _text(vessel.get("id"))
        if not vessel_id:
            continue
        rows.append(
            {
                "vessel_id": vessel_id,
                "name": _upper(vessel.get("name")),
                "mmsi": _text(vessel.get("ssvid")),
                "flag": _upper(vessel.get("flag")),
                "vessel_type": _lower(vessel.get("type")),
                "_priority": 2,
            }
        )

    if not rows:
        return schemas.VESSELS.empty()

    frame = pd.DataFrame(rows).sort_values("_priority", kind="stable")
    merged = frame.groupby("vessel_id", sort=True).first().reset_index()
    merged["source"] = GFW
    return schemas.conform(merged, schemas.VESSELS)


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _latest_name(entries) -> str | None:
    """Pick the most recent entry from a list like [{"name": "TRAWLERS", "yearTo": 2024}]."""
    if not entries:
        return None
    latest = max(entries, key=lambda e: (e.get("yearTo") or 0, e.get("yearFrom") or 0))
    return _lower(latest.get("name"))


# --- events ------------------------------------------------------------------
def clean_events(raw_events: Iterable[dict]) -> pd.DataFrame:
    """Fishing, encounter and AIS-gap events, de-duplicated by event id."""
    rows: dict[str, dict] = {}
    for event in raw_events:
        event_type = EVENT_TYPES.get(str(event.get("type", "")).lower())
        event_id = _text(event.get("id"))
        vessel_id = _text((event.get("vessel") or {}).get("id"))
        if not (event_type and event_id and vessel_id):
            continue
        regions = event.get("regions") or {}
        distances = event.get("distances") or {}
        position = event.get("position") or {}
        encounter = event.get("encounter") or {}
        other = encounter.get("vessel") or {}
        detail = event.get(event_type) or {}
        start = pd.to_datetime(event.get("start"), utc=True, errors="coerce")
        end = pd.to_datetime(event.get("end"), utc=True, errors="coerce")
        duration = (end - start).total_seconds() / 3600 if pd.notna(start) and pd.notna(end) else None
        rows[event_id] = {
            "event_id": event_id,
            "event_type": event_type,
            "vessel_id": vessel_id,
            "start_time": start,
            "end_time": end,
            "duration_hours": duration,
            "lat": position.get("lat"),
            "lon": position.get("lon"),
            "start_distance_from_shore_km": distances.get("startDistanceFromShoreKm"),
            "end_distance_from_shore_km": distances.get("endDistanceFromShoreKm"),
            "eez_ids": _join(regions.get("eez")),
            "rfmo_ids": _join(regions.get("rfmo")),
            "on_high_seas": bool(regions.get("highSeas")) or not regions.get("eez"),
            "in_protected_area": bool(_protected_area_ids(regions)),
            "in_no_take_area": bool(regions.get("mpaNoTake")),
            "potential_risk": bool(detail.get("potentialRisk")),
            "other_vessel_id": _text(other.get("id")),
            "other_vessel_name": _upper(other.get("name")),
            "other_vessel_flag": _upper(other.get("flag")),
            "other_vessel_type": _lower(other.get("type")),
            "encounter_type": _lower(encounter.get("type")),
            "source": GFW,
        }
    return _frame(list(rows.values()), schemas.EVENTS)


def _protected_area_ids(regions: dict) -> list[str]:
    ids: list[str] = []
    for key in ("mpa", "mpaNoTake", "mpaNoTakePartial"):
        for value in regions.get(key) or []:
            if str(value) not in ids:
                ids.append(str(value))
    return ids


def clean_event_protected_areas(raw_events: Iterable[dict]) -> pd.DataFrame:
    """Join table: which protected areas each event falls in."""
    rows: dict[str, dict] = {}
    for event in raw_events:
        if str(event.get("type", "")).lower() not in EVENT_TYPES:
            continue
        event_id = _text(event.get("id"))
        if not event_id or not _text((event.get("vessel") or {}).get("id")):
            continue
        regions = event.get("regions") or {}
        no_take = {str(v) for v in regions.get("mpaNoTake") or []}
        for area_id in _protected_area_ids(regions):
            key = f"{event_id}|{area_id}"
            rows[key] = {
                "event_protected_area_id": key,
                "event_id": event_id,
                "protected_area_id": area_id,
                "no_take": area_id in no_take,
            }
    return _frame(list(rows.values()), schemas.EVENT_PROTECTED_AREAS)


# --- protected areas ---------------------------------------------------------
def match_watched_protected_areas(
    raw_protected_area_list: Iterable[dict], watched_names: Iterable[str]
) -> dict[str, str]:
    """Return {protected_area_id: name} for list entries whose name contains a watched name."""
    wanted = [normalise_name(name) for name in watched_names]
    matches: dict[str, str] = {}
    for entry in raw_protected_area_list:
        label = _text(entry.get("label"))
        area_id = _text(entry.get("id"))
        if not (label and area_id):
            continue
        normalised = normalise_name(label)
        if any(name and name in normalised for name in wanted):
            matches[area_id] = label
    return matches


def clean_protected_areas(
    raw_protected_area_list: Iterable[dict],
    event_protected_areas: pd.DataFrame,
    protected_area_activity: pd.DataFrame,
    watched_names: Iterable[str],
) -> pd.DataFrame:
    """Protected areas that are watched or referenced by an event or activity row.

    The full global list is deliberately not copied: only the areas this project uses.
    """
    raw_list = list(raw_protected_area_list)
    names = {
        _text(e.get("id")): _text(e.get("label")) for e in raw_list if _text(e.get("id"))
    }
    watched = set(match_watched_protected_areas(raw_list, watched_names))
    watched |= set(protected_area_activity["protected_area_id"].dropna())
    referenced = set(event_protected_areas["protected_area_id"].dropna())
    rows = [
        {
            "protected_area_id": area_id,
            "name": names.get(area_id) or f"Protected area {area_id}",
            "watched": area_id in watched,
            "source": f"{GFW} (World Database on Protected Areas)",
        }
        for area_id in sorted(watched | referenced)
    ]
    return _frame(rows, schemas.PROTECTED_AREAS)


def clean_protected_area_activity(raw_protected_area_reports: Iterable[dict]) -> pd.DataFrame:
    """Fishing hours per vessel inside each watched area and inside its buffer."""
    rows = []
    for row in raw_protected_area_reports:
        vessel_id = _text(row.get("vesselId") or row.get("vessel_id"))
        area_id = _text(row.get("_protected_area_id"))
        if not (vessel_id and area_id):
            continue
        rows.append(
            {
                "vessel_id": vessel_id,
                "protected_area_id": area_id,
                "window_start": row.get("_window_start"),
                "window_end": row.get("_window_end"),
                "buffer": float(row.get("_buffer_nm") or 0.0),
                "hours": float(row.get("hours") or 0.0),
            }
        )
    if not rows:
        return schemas.PROTECTED_AREA_ACTIVITY.empty()

    frame = pd.DataFrame(rows)
    keys = ["vessel_id", "protected_area_id", "window_start", "window_end"]
    inside = frame[frame["buffer"] == 0].groupby(keys)["hours"].sum().rename("fishing_hours_inside")
    buffered = frame[frame["buffer"] > 0].groupby(keys).agg(
        fishing_hours_within_buffer=("hours", "sum"), buffer_nm=("buffer", "max")
    )
    out = pd.concat([inside, buffered], axis=1).reset_index()
    out["fishing_hours_inside"] = out["fishing_hours_inside"].fillna(0.0)
    # The buffered region contains the area itself, so it can never hold fewer hours.
    out["fishing_hours_within_buffer"] = (
        out["fishing_hours_within_buffer"].fillna(0.0).clip(lower=out["fishing_hours_inside"])
    )
    out["buffer_nm"] = out["buffer_nm"].fillna(0.0)
    window_end = pd.to_datetime(out["window_end"], utc=True).dt.strftime("%Y-%m-%d")
    out["activity_id"] = out["vessel_id"] + "|" + out["protected_area_id"] + "|" + window_end
    out["source"] = GFW
    return schemas.conform(out, schemas.PROTECTED_AREA_ACTIVITY)


# --- insights and ground truth -----------------------------------------------
def clean_vessel_insights(raw_insights: Iterable[dict]) -> pd.DataFrame:
    """Per-vessel indicators. The IUU-list indicator is deliberately left out here."""
    rows: dict[str, dict] = {}
    for record in raw_insights:
        vessel_id = _text(record.get("_vessel_id"))
        if not vessel_id:
            continue
        period = record.get("period") or {}
        fishing = (record.get("apparentFishing") or {}).get("periodSelectedCounters") or {}
        coverage = record.get("coverage") or {}
        flags = (record.get("vesselIdentity") or {}).get("flagsChanges") or {}
        rows[vessel_id] = {
            "vessel_id": vessel_id,
            "window_start": period.get("startDate"),
            "window_end": period.get("endDate"),
            "fishing_events": fishing.get("events"),
            "no_take_fishing_events": fishing.get("eventsInNoTakeMPAs"),
            "unauthorized_fishing_events": fishing.get("eventsInRFMOWithoutKnownAuthorization"),
            "ais_coverage_pct": coverage.get("percentage"),
            "flag_changes": flags.get("totalTimesListedInThePeriod"),
            "source": GFW,
        }
    return _frame(list(rows.values()), schemas.VESSEL_INSIGHTS)


def clean_iuu_listings(raw_insights: Iterable[dict]) -> pd.DataFrame:
    """Ground truth: one row per period a vessel spent on an RFMO IUU vessel list."""
    rows: dict[str, dict] = {}
    for record in raw_insights:
        vessel_id = _text(record.get("_vessel_id"))
        listing = (record.get("vesselIdentity") or {}).get("iuuVesselList") or {}
        if not vessel_id or not listing:
            continue
        periods = listing.get("valuesInThePeriod") or []
        in_period = int(listing.get("totalTimesListedInThePeriod") or 0)
        if not periods and in_period > 0:
            periods = [{}]
        for index, period in enumerate(periods):
            start = _text(period.get("from"))
            key = f"{vessel_id}|{start[:10] if start else index}"
            rows[key] = {
                "listing_id": key,
                "vessel_id": vessel_id,
                "listed_from": start,
                "listed_to": period.get("to"),
                "times_listed_total": listing.get("totalTimesListed"),
                "source": f"{GFW} Insights (TMT Combined IUU Vessel List of official RFMO lists)",
            }
    return _frame(list(rows.values()), schemas.IUU_LISTINGS)


# --- cohorts and patrol fleet --------------------------------------------------
def clean_cohort_members(
    raw_cohort: Iterable[dict],
    raw_patrol_area_report: Iterable[dict],
    min_fishing_hours: float,
) -> pd.DataFrame:
    """Patrol-area cohort (fishing vessels active in the box) and evaluation cohort.

    Every patrol-area vessel is also in the evaluation cohort, next to the vessels
    seeded from official IUU lists and the random comparison vessels.
    """
    hours: dict[str, float] = {}
    for row in raw_patrol_area_report:
        vessel_id = _text(row.get("vesselId") or row.get("vessel_id"))
        vessel_type = _lower(row.get("vesselType") or row.get("vessel_type"))
        if vessel_id and vessel_type == "fishing":
            hours[vessel_id] = hours.get(vessel_id, 0.0) + float(row.get("hours") or 0.0)
    patrol = {v: h for v, h in hours.items() if h >= min_fishing_hours}

    rows: dict[str, dict] = {}

    def add(cohort: str, vessel_id: str, reason: str) -> None:
        key = f"{cohort}|{vessel_id}"
        if key not in rows:
            rows[key] = {
                "cohort_member_id": key,
                "cohort": cohort,
                "vessel_id": vessel_id,
                "selection_reason": reason,
                "patrol_area_fishing_hours": patrol.get(vessel_id),
            }

    # Seeds first so that a vessel found both ways keeps the more specific reason.
    ordered = sorted(raw_cohort, key=lambda r: r.get("selection_reason") != "iuu_list_seed")
    for record in ordered:
        vessel_id = _text(record.get("vessel_id"))
        if vessel_id:
            add("evaluation", vessel_id, _text(record.get("selection_reason")) or "unknown")
    for vessel_id in sorted(patrol):
        add("patrol_area", vessel_id, "active_in_patrol_area")
        add("evaluation", vessel_id, "active_in_patrol_area")
    return _frame(list(rows.values()), schemas.COHORT_MEMBERS)


def clean_patrol_boats(configured_boats: Iterable[dict]) -> pd.DataFrame:
    rows = [{**boat, "source": "example fleet from config/settings.toml"} for boat in configured_boats]
    return _frame(rows, schemas.PATROL_BOATS)
