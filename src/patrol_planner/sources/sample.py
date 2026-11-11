"""SYNTHETIC SAMPLE DATA. Nothing produced here describes a real vessel.

The generator writes raw records in the same shape the Global Fishing Watch API
returns, so the whole pipeline (clean -> features -> scores -> evaluation) can run
and be tested without an API token. Every name carries the word SAMPLE and every
id starts with ``sample-`` so the data cannot be mistaken for real observations.

Accuracy figures computed on this data are meaningless: the generator decides
which vessels are "listed" and gives them more suspicious behaviour on purpose.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Settings
from ..storage import DataStore

LABEL = "SYNTHETIC SAMPLE DATA - not real vessels, not real events"
FLAGS = ("ECU", "CHN", "PAN", "PER", "COL", "ESP", "KOR", "TWN")
GEARS = ("tuna_purse_seines", "drifting_longlines", "squid_jigger", "trawlers")
PATROL_VESSELS = 120
EVALUATION_GROUPS = 20
CONTROLS_PER_GROUP = 12
UNCONFIRMED_GROUPS = 3  # found through a list but not confirmed as listed: must be dropped


def generate_sample(store: DataStore, settings: Settings, seed: int = 7) -> dict:
    """Write a full set of synthetic raw datasets and a manifest that labels them."""
    rng = np.random.default_rng(seed)
    window, area = settings.window, settings.patrol_area
    west, south, east, north = area.bbox

    areas = [
        {"id": f"sample-mpa-{i + 1}", "label": f"{name} (synthetic sample)"}
        for i, name in enumerate(area.watched_protected_areas)
    ]
    areas.append({"id": "sample-mpa-notake", "label": "No-take reserve (synthetic sample)"})

    vessels: list[dict] = []

    def add_vessel(group: str, listed: bool, start: pd.Timestamp, end: pd.Timestamp, **extra) -> dict:
        index = len(vessels) + 1
        # Listed vessels behave worse on average, with plenty of overlap.
        risk = float(np.clip(rng.beta(4, 3) if listed else rng.beta(1.2, 6), 0, 1))
        vessel = {
            "id": f"sample-vessel-{index:04d}",
            "name": f"SAMPLE VESSEL {index:04d}",
            "mmsi": f"9990{index:05d}",
            "flag": str(rng.choice(FLAGS)),
            "gear": str(rng.choice(GEARS)),
            "group": group,
            "listed": listed,
            "risk": risk,
            "start": start,
            "end": end,
            **extra,
        }
        vessels.append(vessel)
        return vessel

    for _ in range(PATROL_VESSELS):
        add_vessel("patrol_area", False, window.start, window.end)

    history_start = settings.evaluation.history_start
    latest_start = window.end - pd.DateOffset(months=12)
    span_days = max((latest_start - history_start).days, 1)
    for number in range(EVALUATION_GROUPS + UNCONFIRMED_GROUPS):
        group_start = history_start + pd.Timedelta(days=int(rng.integers(0, span_days)))
        group_end = group_start + pd.DateOffset(months=12)
        vessel_class = "fishing" if rng.random() < 0.7 else "support"
        case = add_vessel("iuu_listed_vessel", number < EVALUATION_GROUPS, group_start, group_end,
                          vessel_class=vessel_class)
        case["match_group"] = case["id"]
        for _ in range(CONTROLS_PER_GROUP):
            add_vessel("matched_control", False, group_start, group_end,
                       vessel_class=vessel_class, match_group=case["id"])

    events, insights, identities, cohort = [], [], [], []
    area_report, area_reports = [], []
    counter = 0

    def stamp(moment: pd.Timestamp) -> str:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def position(in_area: bool) -> tuple[float, float]:
        if in_area:
            return float(rng.uniform(south, north)), float(rng.uniform(west, east))
        return float(rng.uniform(-40, 40)), float(rng.uniform(-170, 170))

    def event(vessel: dict, kind: str, begin: float, hours: float, lat: float, lon: float,
              shore_km: float, regions: dict, detail: dict) -> None:
        nonlocal counter
        counter += 1
        begins = vessel["start"] + pd.Timedelta(hours=float(begin))
        events.append(
            {
                "id": f"sample-event-{counter:06d}",
                "type": kind,
                "start": stamp(begins),
                "end": stamp(begins + pd.Timedelta(hours=float(hours))),
                "position": {"lat": round(lat, 4), "lon": round(lon, 4)},
                "regions": {
                    "mpa": [], "eez": [], "rfmo": [], "highSeas": [],
                    "mpaNoTake": [], "mpaNoTakePartial": [], **regions,
                },
                "distances": {
                    "startDistanceFromShoreKm": round(shore_km, 1),
                    "endDistanceFromShoreKm": round(shore_km, 1),
                },
                "vessel": {"id": vessel["id"], "name": vessel["name"], "ssvid": vessel["mmsi"]},
                kind: detail,
            }
        )

    for vessel in vessels:
        in_area = vessel["group"] == "patrol_area"
        risk = vessel["risk"]
        span_hours = (vessel["end"] - vessel["start"]).total_seconds() / 3600

        # AIS gaps: some are too short, too close to shore or unintentional to count.
        for _ in range(rng.poisson(0.4 + 7 * risk)):
            lat, lon = position(in_area)
            hours = float(rng.choice([6, 9, 14, 20, 36, 72, 120]))
            shore = float(rng.choice([20, 60, 110, 250, 400]))
            regions = {"highSeas": ["sample-hs"]} if shore > 370 else {"eez": ["sample-eez"]}
            if rng.random() < 0.08 * (1 + 2 * risk):
                regions["mpa"] = [areas[0]["id"]]
            event(vessel, "gap", rng.uniform(0, span_hours - hours), hours, lat, lon, shore,
                  regions, {"intentionalDisabling": bool(rng.random() < 0.85)})

        for _ in range(rng.poisson(0.2 + 5 * risk)):
            lat, lon = position(in_area)
            with_carrier = rng.random() < 0.25 + 0.5 * risk
            other = int(rng.integers(1, 400))
            event(
                vessel, "encounter", rng.uniform(0, span_hours - 6), float(rng.uniform(2, 6)),
                lat, lon, 300.0, {"highSeas": ["sample-hs"], "rfmo": ["SAMPLE-RFMO"]},
                {
                    "vessel": {
                        "id": f"sample-other-{other:04d}",
                        "name": f"SAMPLE {'CARRIER' if with_carrier else 'VESSEL'} {other:04d}",
                        "flag": str(rng.choice(FLAGS)),
                        "type": "carrier" if with_carrier else "fishing",
                    },
                    "type": "fishing-carrier" if with_carrier else "fishing-fishing",
                    "potentialRisk": bool(rng.random() < 0.15 + 0.4 * risk),
                },
            )

        no_take = int(rng.poisson(1.5 * risk**2))
        for _ in range(no_take):
            lat, lon = position(in_area)
            event(vessel, "fishing", rng.uniform(0, span_hours - 4), float(rng.uniform(1, 4)),
                  lat, lon, 80.0,
                  {"eez": ["sample-eez"], "mpa": ["sample-mpa-notake"], "mpaNoTake": ["sample-mpa-notake"]},
                  {"potentialRisk": False})

        # The most recent apparent fishing event gives the last known position.
        lat, lon = position(in_area)
        event(vessel, "fishing", span_hours - float(rng.uniform(6, 400)), 3.0, lat, lon, 150.0,
              {"eez": ["sample-eez"]}, {"potentialRisk": False})

        listing_from = stamp(vessel["start"] - pd.Timedelta(days=200))
        insights.append(
            {
                "_vessel_id": vessel["id"],
                "period": {"startDate": f"{vessel['start']:%Y-%m-%d}", "endDate": f"{vessel['end']:%Y-%m-%d}"},
                "apparentFishing": {
                    "periodSelectedCounters": {
                        "events": int(rng.integers(40, 900)),
                        "eventsInNoTakeMPAs": no_take,
                        "eventsInRFMOWithoutKnownAuthorization": int(rng.poisson(2.5 * risk**2)),
                    }
                },
                "coverage": {"percentage": round(float(rng.uniform(15, 98)), 1)},
                "vesselIdentity": {
                    "iuuVesselList": {
                        "valuesInThePeriod": (
                            [{"from": listing_from, "to": stamp(vessel["end"])}]
                            if vessel["listed"] else []
                        ),
                        "totalTimesListed": int(vessel["listed"]),
                        "totalTimesListedInThePeriod": int(vessel["listed"]),
                    },
                },
            }
        )

        first_seen, last_seen = "2018-01-01T00:00:00Z", stamp(vessel["end"])
        ship_type = "FISHING" if vessel.get("vessel_class", "fishing") == "fishing" else "CARRIER"
        identity_row = {
            "vesselId": vessel["id"], "shipName": vessel["name"], "mmsi": vessel["mmsi"],
            "flag": vessel["flag"], "geartype": vessel["gear"].upper(), "vesselType": ship_type,
            "imo": None, "callsign": None,
            "firstTransmissionDate": first_seen, "lastTransmissionDate": last_seen,
        }
        identities.append(
            {
                "selfReportedInfo": [
                    {
                        "id": vessel["id"], "shipname": vessel["name"], "ssvid": vessel["mmsi"],
                        "flag": vessel["flag"], "imo": None, "callsign": None,
                        "transmissionDateFrom": first_seen, "transmissionDateTo": last_seen,
                    }
                ],
                # A registry entry under another flag during the window is a flag change.
                "registryInfo": (
                    [{"flag": "XXX", "transmissionDateFrom": stamp(vessel["start"]),
                      "transmissionDateTo": stamp(vessel["start"] + pd.Timedelta(days=90))}]
                    if rng.random() < 0.05 + 0.25 * risk else []
                ),
                "combinedSourcesInfo": [
                    {
                        "vesselId": vessel["id"],
                        "geartypes": [{"name": vessel["gear"].upper(), "yearTo": 2026}],
                        "shiptypes": [{"name": ship_type, "yearTo": 2026}],
                    }
                ],
            }
        )

        if in_area:
            area_report.append({**identity_row, "hours": round(float(rng.uniform(30, 2500)), 1)})
            for watched in areas[: len(area.watched_protected_areas)]:
                if rng.random() < 0.10 + 0.5 * risk:
                    inside = float(rng.uniform(0, 60) * risk)
                    near = float(rng.uniform(2, 150))  # hours in the ring just outside
                    for buffer_nm, hours in ((0.0, inside), (area.buffer_nm, near)):
                        if hours > 0:
                            area_reports.append(
                                {
                                    **identity_row, "hours": round(hours, 1),
                                    "_protected_area_id": watched["id"], "_buffer_nm": buffer_nm,
                                    "_window_start": window.start.isoformat(),
                                    "_window_end": window.end.isoformat(),
                                }
                            )
        else:
            cohort.append(
                {
                    "vessel_id": vessel["id"],
                    "selection_reason": vessel["group"],
                    "match_group": vessel["match_group"],
                    "vessel_class": vessel["vessel_class"],
                    "window_start": f"{vessel['start']:%Y-%m-%d}",
                    "window_end": f"{vessel['end']:%Y-%m-%d}",
                }
            )

    store.write_raw("protected_area_list", areas)
    store.write_raw("patrol_area_report", area_report)
    store.write_raw("protected_area_reports", area_reports)
    store.write_raw("vessel_identities", identities)
    store.write_raw("events", events)
    store.write_raw("insights", insights)
    store.write_raw("cohort", cohort)

    manifest = {
        "data_mode": "sample",
        "label": LABEL,
        "generated_with_seed": seed,
        "window_start": window.start_date,
        "window_end": window.end_date,
        "patrol_area": area.name,
        "sources": ["patrol_planner.sources.sample (synthetic generator)"],
    }
    store.write_manifest(manifest)
    return manifest
