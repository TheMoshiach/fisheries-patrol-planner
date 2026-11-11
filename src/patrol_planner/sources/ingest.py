"""Downloads real data into the raw landing zone.

Nothing here interprets the data: records are stored as received (plus a few
``_``-prefixed fields saying which request produced them). All interpretation
happens in ``transforms.clean``.

Which vessels are fetched
    patrol area   every fishing vessel with apparent fishing hours in the box
    IUU seeds     vessels whose IMO number appears on an official IUU list
    controls      fishing vessels drawn at random from worldwide fishing events
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from ..config import PROJECT_ROOT, Settings
from ..storage import DataStore
from ..transforms.clean import match_watched_protected_areas
from . import iuu_lists
from .gfw import ATTRIBUTION, EVENT_DATASETS, GFWClient, GFWError

BATCH = 20  # vessels per events / identity request
MAX_FLAGGED_EVENTS_PER_VESSEL = 20
MAX_WATCHED_AREAS = 8
WORKERS = 4


def load_token() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    return os.environ.get("GFW_API_TOKEN", "").strip()


def fetch_real(store: DataStore, settings: Settings, refresh: bool = False, log=print) -> dict:
    client = GFWClient(load_token(), cache_dir=store.root / "raw" / "http_cache", refresh=refresh)
    window, area = settings.window, settings.patrol_area
    start, end = window.start_date, window.end_date

    log("1/8 Protected-area list")
    area_list = client.protected_area_list()
    store.write_raw("protected_area_list", area_list)
    watched = match_watched_protected_areas(area_list, area.watched_protected_areas)
    for area_id, name in list(watched.items())[:MAX_WATCHED_AREAS]:
        log(f"  watching {name} (id {area_id})")
    if not watched:
        log("  no protected area matched the names in settings; check watched_protected_areas")
    watched_ids = list(watched)[:MAX_WATCHED_AREAS]

    log("2/8 Fishing vessels active in the patrol area")
    area_report = client.fishing_hours_by_vessel(start, end, geojson=area.geojson())
    store.write_raw("patrol_area_report", area_report)
    patrol_ids = _patrol_area_vessels(area_report, area.min_fishing_hours)
    log(f"  {len(area_report)} vessels reported, {len(patrol_ids)} fishing vessels kept")

    log("3/8 Fishing inside and near the watched protected areas")
    area_reports: list[dict] = []
    for area_id in watched_ids:
        for buffer_nm in (0.0, area.buffer_nm):
            rows = client.fishing_hours_by_vessel(
                start, end, region_id=area_id, buffer_nm=buffer_nm or None
            )
            area_reports.extend(
                {
                    **row,
                    "_protected_area_id": area_id,
                    "_buffer_nm": buffer_nm,
                    "_window_start": window.start.isoformat(),
                    "_window_end": window.end.isoformat(),
                }
                for row in rows
            )
    store.write_raw("protected_area_reports", area_reports)

    log("4/8 Vessels named on official IUU vessel lists")
    seeds = iuu_lists.fetch_seed_imos(log=log)
    store.write_raw("iuu_seed_imos", seeds)
    identities: dict[str, dict] = {}
    seed_ids: list[str] = []
    for imo in sorted({record["imo"] for record in seeds}):
        for entry in client.search_vessels(f'imo="{imo}"'):
            for vessel_id in _identities_in_window(entry, window.start, window.end):
                identities[vessel_id] = entry
                seed_ids.append(vessel_id)
    seed_ids = sorted(set(seed_ids))
    log(f"  {len(seed_ids)} vessel identities transmitted AIS during the window")

    log("5/8 Random comparison vessels")
    control_ids = _random_fishing_vessels(
        client,
        start,
        end,
        wanted=settings.evaluation.control_vessels,
        exclude=set(patrol_ids) | set(seed_ids),
        seed=settings.evaluation.random_seed,
    )
    log(f"  {len(control_ids)} vessels")

    cohort = [{"vessel_id": v, "selection_reason": "iuu_list_seed"} for v in seed_ids]
    cohort += [{"vessel_id": v, "selection_reason": "random_control"} for v in control_ids]
    store.write_raw("cohort", cohort)
    all_ids = sorted(set(patrol_ids) | set(seed_ids) | set(control_ids))

    log(f"6/8 Identity records for {len(all_ids)} vessels")
    missing = [v for v in all_ids if v not in identities]
    for batch in _batches(missing, BATCH):
        for entry in client.vessels_by_ids(batch):
            for info in entry.get("selfReportedInfo") or []:
                if info.get("id"):
                    identities[info["id"]] = entry
    store.write_raw("vessel_identities", _unique(identities.values()))

    log("7/8 AIS gaps and encounters")
    events: list[dict] = []
    for event_type in ("gap", "encounter"):
        before = len(events)
        for batch in _batches(all_ids, BATCH):
            events.extend(client.iter_events(event_type, start, end, batch))
        log(f"  {len(events) - before} {event_type} events")

    log("8/8 Vessel indicators, flagged fishing events and last known positions")
    insights = _parallel(lambda v: _try_insights(client, v, start, end), all_ids)
    failed = sum(1 for record in insights if "_error" in record)
    if failed:
        log(f"  indicators unavailable for {failed} vessels")
    store.write_raw("insights", insights)

    flagged: list[str] = []
    for record in insights:
        fishing = record.get("apparentFishing") or {}
        ids = list(fishing.get("eventsInNoTakeMpas") or [])
        ids += list(fishing.get("eventsInRfmoWithoutKnownAuthorization") or [])
        flagged.extend(ids[:MAX_FLAGGED_EVENTS_PER_VESSEL])
    events.extend(e for e in _parallel(lambda i: _try_event(client, i), sorted(set(flagged))) if e)
    events.extend(
        e for e in _parallel(lambda v: _latest_fishing_event(client, v, start, end), patrol_ids) if e
    )
    store.write_raw("events", events)

    manifest = {
        "data_mode": "real",
        "label": "Real data from Global Fishing Watch and official RFMO IUU vessel lists",
        "attribution": ATTRIBUTION,
        "licence": "CC BY-NC 4.0 (non-commercial use only)",
        "accessed": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "window_start": start,
        "window_end": end,
        "patrol_area": area.name,
        "watched_protected_areas": {i: watched[i] for i in watched_ids},
        "datasets": sorted(client.dataset_versions | set(EVENT_DATASETS.values())),
        "iuu_list_sources": sorted({r["url"] for r in seeds}),
        "vessels_fetched": len(all_ids),
        "api_requests_this_run": client.requests_made,
    }
    store.write_manifest(manifest)
    log(f"Done. {client.requests_made} API requests this run.")
    return manifest


# --- selection helpers -----------------------------------------------------------
def _patrol_area_vessels(report_rows: list[dict], min_hours: float) -> list[str]:
    hours: dict[str, float] = {}
    for row in report_rows:
        if str(row.get("vesselType") or "").lower() != "fishing" or not row.get("vesselId"):
            continue
        hours[row["vesselId"]] = hours.get(row["vesselId"], 0.0) + float(row.get("hours") or 0.0)
    return sorted(v for v, h in hours.items() if h >= min_hours)


def _identities_in_window(entry: dict, start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    """Vessel ids on an identity record whose AIS transmissions overlap the window."""
    ids = []
    for info in entry.get("selfReportedInfo") or []:
        first = pd.to_datetime(info.get("transmissionDateFrom"), utc=True, errors="coerce")
        last = pd.to_datetime(info.get("transmissionDateTo"), utc=True, errors="coerce")
        if info.get("id") and pd.notna(first) and pd.notna(last) and first < end and last >= start:
            ids.append(info["id"])
    return ids


def _random_fishing_vessels(
    client: GFWClient, start: str, end: str, wanted: int, exclude: set[str], seed: int
) -> list[str]:
    """Draw vessels by sampling random days of worldwide apparent fishing events.

    A vessel's chance of being drawn grows with how often it fishes, so the
    comparison group is weighted towards active vessels.
    """
    rng = np.random.default_rng(seed)
    days = pd.date_range(start, end, freq="D", inclusive="left")
    chosen: list[str] = []
    seen = set(exclude)
    attempts = 0
    while len(chosen) < wanted and attempts < wanted:
        attempts += 1
        day = days[int(rng.integers(0, len(days)))]
        day_start, day_end = day.strftime("%Y-%m-%d"), (day + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        total = int(client.events_page("fishing", day_start, day_end, limit=1).get("total") or 0)
        if total == 0:
            continue
        offset = int(rng.integers(0, max(total - 50, 1)))
        page = client.events_page("fishing", day_start, day_end, offset=offset, limit=50)
        for event in page.get("entries") or []:
            vessel_id = (event.get("vessel") or {}).get("id")
            if vessel_id and vessel_id not in seen and len(chosen) < wanted:
                seen.add(vessel_id)
                chosen.append(vessel_id)
    return chosen


def _latest_fishing_event(client: GFWClient, vessel_id: str, start: str, end: str) -> dict | None:
    """The vessel's most recent apparent fishing event in the window, if any."""
    try:
        first = client.events_page("fishing", start, end, vessels=[vessel_id], limit=1)
        total = int(first.get("total") or 0)
        if total <= 1:
            return (first.get("entries") or [None])[0]
        last = client.events_page("fishing", start, end, vessels=[vessel_id], offset=total - 1, limit=1)
        return (last.get("entries") or [None])[0]
    except GFWError:
        return None


def _try_insights(client: GFWClient, vessel_id: str, start: str, end: str) -> dict:
    try:
        return {**client.insights(vessel_id, start, end), "_vessel_id": vessel_id}
    except GFWError as error:
        return {"_vessel_id": vessel_id, "_error": str(error)}


def _try_event(client: GFWClient, event_id: str) -> dict | None:
    try:
        return client.event(event_id, "fishing")
    except GFWError:
        return None


# --- small utilities ----------------------------------------------------------------
def _batches(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _unique(entries) -> list[dict]:
    seen: set[int] = set()
    out = []
    for entry in entries:
        if id(entry) not in seen:
            seen.add(id(entry))
            out.append(entry)
    return out


def _parallel(function, items: list) -> list:
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(function, items))
