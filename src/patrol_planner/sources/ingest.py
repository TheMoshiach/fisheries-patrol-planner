"""Downloads real data into the raw landing zone.

Nothing here interprets the data: records are stored as received (plus a few
``_``-prefixed fields saying which request produced them). All interpretation
happens in ``transforms.clean``.

Which vessels are fetched
    patrol area     every fishing vessel with apparent fishing hours in the box,
                    over the main analysis window
    listed vessels  vessels whose IMO number appears on an official IUU list,
                    each over its own last year on AIS
    controls        for each listed vessel, vessels of the same broad class drawn
                    at random from worldwide events in the same months
"""

from __future__ import annotations

import os
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from ..config import PROJECT_ROOT, Settings
from ..geo import simplify_geometry
from ..storage import DataStore
from ..transforms.clean import match_watched_protected_areas
from . import iuu_lists
from .gfw import ATTRIBUTION, EVENT_DATASETS, GFWClient, GFWError

BATCH = 20  # vessels per events / identity request
MAX_FLAGGED_EVENTS_PER_VESSEL = 20
MAX_WATCHED_AREAS = 8
MIN_WINDOW_DAYS = 30
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
    shapes = []
    for area_id in watched_ids:
        try:
            geometry = simplify_geometry(client.protected_area_shape(area_id).get("geometry"))
        except GFWError:
            geometry = None
        if geometry:
            shapes.append({"id": area_id, "name": watched[area_id], "geometry": geometry})
    store.write_raw("protected_area_shapes", shapes)

    log("4/8 Vessels named on official IUU vessel lists")
    seeds = iuu_lists.fetch_seed_imos(log=log)
    store.write_raw("iuu_seed_imos", seeds)
    identities: dict[str, dict] = {}
    cases: list[dict] = []
    for imo in sorted({record["imo"] for record in seeds}):
        entries = client.search_vessels(f'imo="{imo}"')
        case = _best_identity(entries, imo, settings)
        if case:
            identities[case["vessel_id"]] = case.pop("_entry")
            cases.append(case)
    log(f"  {len(cases)} listed hulls have a usable AIS history since "
        f"{settings.evaluation.history_start:%Y-%m-%d}")

    log("5/8 Comparison vessels from the same months")
    cohort: list[dict] = []
    taken = {case["vessel_id"] for case in cases}
    for case in cases:
        cohort.append({**case, "selection_reason": "iuu_listed_vessel"})
        # Each group draws from its own random stream, so a change to one group
        # (or a refresh on another day) leaves every other group's draw untouched.
        rng = np.random.default_rng(
            [settings.evaluation.random_seed, zlib.crc32(case["vessel_id"].encode())]
        )
        controls = _random_vessels(
            client, case["vessel_class"], case["window_start"], case["window_end"],
            wanted=settings.evaluation.controls_per_case, exclude=taken, rng=rng,
        )
        taken.update(controls)
        log(f"  group {cases.index(case) + 1} of {len(cases)}: {len(controls)} comparison vessels")
        cohort.extend(
            {**case, "vessel_id": v, "imo": None, "selection_reason": "matched_control"}
            for v in controls
        )
    store.write_raw("cohort", cohort)
    log(f"  {len(cohort) - len(cases)} comparison vessels in {len(cases)} groups")

    # Every (vessel, window) pair that needs events and indicators.
    jobs = [(v, start, end) for v in patrol_ids]
    jobs += [(r["vessel_id"], r["window_start"], r["window_end"]) for r in cohort]
    all_ids = sorted({job[0] for job in jobs})

    log(f"6/8 Identity records for {len(all_ids)} vessels")
    missing = [v for v in all_ids if v not in identities]
    for batch in _batches(missing, BATCH):
        for entry in client.vessels_by_ids(batch):
            for info in entry.get("selfReportedInfo") or []:
                if info.get("id"):
                    identities[info["id"]] = entry
    store.write_raw("vessel_identities", _unique(identities.values()))

    log("7/8 AIS gaps and encounters")
    by_window: dict[tuple[str, str], list[str]] = {}
    for vessel_id, w_start, w_end in jobs:
        by_window.setdefault((w_start, w_end), []).append(vessel_id)
    events: list[dict] = []
    for event_type in ("gap", "encounter"):
        before = len(events)
        for (w_start, w_end), ids in by_window.items():
            for batch in _batches(sorted(set(ids)), BATCH):
                events.extend(client.iter_events(event_type, w_start, w_end, batch))
        log(f"  {len(events) - before} {event_type} events")

    log("8/8 Vessel indicators, flagged fishing events and last known positions")
    insights: list[dict] = []
    for chunk in _batches(jobs, 100):
        insights.extend(_parallel(lambda job: _try_insights(client, *job), chunk))
        log(f"  indicators for {len(insights)} of {len(jobs)} vessels")
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
    log(f"  {len(set(flagged))} flagged fishing events")
    events.extend(e for e in _parallel(lambda i: _try_event(client, i), sorted(set(flagged))) if e)
    log("  last known positions")
    events.extend(
        e for e in _parallel(lambda v: _latest_fishing_event(client, v, start, end), patrol_ids) if e
    )
    # Where each patrol-area vessel fished recently: drawn on the map as its trail.
    trail_start = (window.end - pd.Timedelta(days=area.trail_days)).strftime("%Y-%m-%d")
    before = len(events)
    for batch in _batches(patrol_ids, BATCH):
        events.extend(client.iter_events("fishing", trail_start, end, batch))
    log(f"  {len(events) - before} recent fishing events for vessel trails")
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
    log(f"Download finished. {client.requests_made} API requests this run.")
    return manifest


# --- selection helpers -----------------------------------------------------------
def _patrol_area_vessels(report_rows: list[dict], min_hours: float) -> list[str]:
    hours: dict[str, float] = {}
    for row in report_rows:
        if str(row.get("vesselType") or "").lower() != "fishing" or not row.get("vesselId"):
            continue
        hours[row["vesselId"]] = hours.get(row["vesselId"], 0.0) + float(row.get("hours") or 0.0)
    return sorted(v for v, h in hours.items() if h >= min_hours)


def _best_identity(entries: list[dict], imo: str, settings: Settings) -> dict | None:
    """Pick the AIS identity of a hull to evaluate, and the window to evaluate it over.

    A hull can appear under several names and MMSIs. The identity with the longest
    stretch of AIS inside the usable history is chosen (the most recent one on a
    tie), and it is analysed over its last twelve months of transmission.
    """
    history_start = settings.evaluation.history_start
    latest = settings.window.end
    best = None
    for entry in entries:
        classes = {
            source.get("vesselId"): {str(t.get("name", "")).upper() for t in source.get("shiptypes") or []}
            for source in entry.get("combinedSourcesInfo") or []
        }
        for info in entry.get("selfReportedInfo") or []:
            first = pd.to_datetime(info.get("transmissionDateFrom"), utc=True, errors="coerce")
            last = pd.to_datetime(info.get("transmissionDateTo"), utc=True, errors="coerce")
            if not info.get("id") or pd.isna(first) or pd.isna(last):
                continue
            if int(info.get("positionsCounter") or 0) < settings.evaluation.min_positions:
                continue
            window_end = min(last, latest).normalize()
            window_start = max(window_end - pd.DateOffset(months=12), first.normalize(), history_start)
            days = (window_end - window_start).days
            if days < MIN_WINDOW_DAYS:
                continue
            candidate = (days, window_end)
            if best is None or candidate > best[0]:
                is_fishing = "FISHING" in classes.get(info["id"], set())
                best = (
                    candidate,
                    {
                        "vessel_id": info["id"],
                        "imo": imo,
                        "match_group": info["id"],
                        "vessel_class": "fishing" if is_fishing else "support",
                        "window_start": window_start.strftime("%Y-%m-%d"),
                        "window_end": window_end.strftime("%Y-%m-%d"),
                        "_entry": entry,
                    },
                )
    return best[1] if best else None


def _random_vessels(
    client: GFWClient,
    vessel_class: str,
    start: str,
    end: str,
    wanted: int,
    exclude: set[str],
    rng: np.random.Generator,
) -> list[str]:
    """Draw vessels of one class from random days of worldwide events in a period.

    Fishing vessels are drawn from apparent fishing events; support vessels
    (carriers, bunkers and the like) from the non-fishing side of encounters. A
    vessel's chance of being drawn grows with how active it is.
    """
    event_type = "fishing" if vessel_class == "fishing" else "encounter"
    days = pd.date_range(start, end, freq="D", inclusive="left")
    chosen: list[str] = []
    seen = set(exclude)
    for _ in range(wanted * 4):
        if len(chosen) >= wanted or not len(days):
            break
        day = days[int(rng.integers(0, len(days)))]
        day_start = day.strftime("%Y-%m-%d")
        day_end = (day + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            total = int(client.events_page(event_type, day_start, day_end, limit=1).get("total") or 0)
            if total == 0:
                continue
            offset = int(rng.integers(0, max(total - 50, 1)))
            page = client.events_page(event_type, day_start, day_end, offset=offset, limit=50)
        except GFWError:
            continue  # one unavailable day is not worth stopping for; draw another
        entries = page.get("entries") or []
        for index in rng.permutation(len(entries)):
            vessel = entries[int(index)].get("vessel") or {}
            is_fishing = str(vessel.get("type") or "").lower() == "fishing"
            if is_fishing != (vessel_class == "fishing"):
                continue
            vessel_id = vessel.get("id")
            if vessel_id and vessel_id not in seen:
                seen.add(vessel_id)
                chosen.append(vessel_id)
                # At most a few vessels per day, so one fleet cannot fill a group.
                if len(chosen) >= wanted or len(chosen) % 5 == 0:
                    break
    return chosen


def _latest_fishing_event(client: GFWClient, vessel_id: str, start: str, end: str) -> dict | None:
    """The vessel's most recent apparent fishing event in the window, if any."""
    try:
        page = client.events_page(
            "fishing", start, end, vessels=[vessel_id], limit=1, sort="-start"
        )
        return (page.get("entries") or [None])[0]
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
def _batches(items: list, size: int):
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
