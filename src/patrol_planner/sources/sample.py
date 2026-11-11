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


def generate_sample(store: DataStore, settings: Settings, seed: int = 7) -> dict:
    """Write a full set of synthetic raw datasets and a manifest that labels them."""
    rng = np.random.default_rng(seed)
    window, area = settings.window, settings.patrol_area
    west, south, east, north = area.bbox
    start, end = window.start, window.end
    span_hours = (end - start).total_seconds() / 3600

    areas = [
        {"id": f"sample-mpa-{i + 1}", "label": f"{name} (synthetic sample)"}
        for i, name in enumerate(area.watched_protected_areas)
    ]
    areas.append({"id": "sample-mpa-notake", "label": "No-take reserve (synthetic sample)"})

    vessels = []

    def add_vessels(count: int, group: str, listed_share: float) -> None:
        for _ in range(count):
            index = len(vessels) + 1
            listed = bool(rng.random() < listed_share)
            # Listed vessels behave worse on average, with plenty of overlap.
            risk = float(np.clip(rng.beta(4, 3) if listed else rng.beta(1.2, 6), 0, 1))
            vessels.append(
                {
                    "id": f"sample-vessel-{index:04d}",
                    "name": f"SAMPLE VESSEL {index:04d}",
                    "mmsi": f"9990{index:05d}",
                    "flag": str(rng.choice(FLAGS)),
                    "gear": str(rng.choice(GEARS)),
                    "group": group,
                    "listed": listed,
                    "risk": risk,
                }
            )

    add_vessels(120, "patrol_area", 0.03)
    add_vessels(260, "random_control", 0.0)
    add_vessels(24, "iuu_list_seed", 0.75)

    def timestamp(offset_hours: float) -> str:
        return (start + pd.Timedelta(hours=float(offset_hours))).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def position(in_area: bool) -> tuple[float, float]:
        if in_area:
            return float(rng.uniform(south, north)), float(rng.uniform(west, east))
        return float(rng.uniform(-40, 40)), float(rng.uniform(-170, 170))

    events, insights, identities, cohort = [], [], [], []
    area_report, area_reports = [], []
    counter = 0

    def event(vessel: dict, kind: str, begin: float, hours: float, lat: float, lon: float,
              shore_km: float, regions: dict, detail: dict) -> None:
        nonlocal counter
        counter += 1
        events.append(
            {
                "id": f"sample-event-{counter:06d}",
                "type": kind,
                "start": timestamp(begin),
                "end": timestamp(begin + hours),
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

        # AIS gaps: some are too short or too close to shore to count.
        for _ in range(rng.poisson(0.4 + 7 * risk)):
            lat, lon = position(in_area)
            hours = float(rng.choice([6, 9, 14, 20, 36, 72, 120]))
            shore = float(rng.choice([20, 60, 110, 250, 400]))
            regions = {"highSeas": ["sample-hs"]} if shore > 370 else {"eez": ["sample-eez"]}
            if rng.random() < 0.08 * (1 + 2 * risk):
                regions["mpa"] = [areas[0]["id"]]
            event(vessel, "gap", rng.uniform(0, span_hours - hours), hours, lat, lon, shore,
                  regions, {})

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

        fishing_events = int(rng.integers(40, 900))
        insights.append(
            {
                "_vessel_id": vessel["id"],
                "period": {"startDate": window.start_date, "endDate": window.end_date},
                "apparentFishing": {
                    "periodSelectedCounters": {
                        "events": fishing_events,
                        "eventsInNoTakeMPAs": no_take,
                        "eventsInRFMOWithoutKnownAuthorization": int(rng.poisson(2.5 * risk**2)),
                    }
                },
                "coverage": {"percentage": round(float(rng.uniform(15, 98)), 1)},
                "vesselIdentity": {
                    "flagsChanges": {
                        "totalTimesListedInThePeriod": int(rng.random() < 0.05 + 0.25 * risk)
                    },
                    "iuuVesselList": {
                        "valuesInThePeriod": (
                            [{"from": f"{window.start_date}T00:00:00Z",
                              "to": f"{window.end_date}T00:00:00Z"}]
                            if vessel["listed"] else []
                        ),
                        "totalTimesListed": int(vessel["listed"]),
                        "totalTimesListedInThePeriod": int(vessel["listed"]),
                    },
                },
            }
        )

        identity_row = {
            "vesselId": vessel["id"], "shipName": vessel["name"], "mmsi": vessel["mmsi"],
            "flag": vessel["flag"], "geartype": vessel["gear"].upper(), "vesselType": "FISHING",
            "imo": None, "callsign": None,
            "firstTransmissionDate": "2018-01-01T00:00:00Z",
            "lastTransmissionDate": timestamp(span_hours),
        }
        identities.append(
            {
                "selfReportedInfo": [
                    {
                        "id": vessel["id"], "shipname": vessel["name"], "ssvid": vessel["mmsi"],
                        "flag": vessel["flag"], "imo": None, "callsign": None,
                        "transmissionDateFrom": "2018-01-01T00:00:00Z",
                        "transmissionDateTo": timestamp(span_hours),
                    }
                ],
                "combinedSourcesInfo": [
                    {
                        "vesselId": vessel["id"],
                        "geartypes": [{"name": vessel["gear"].upper(), "yearTo": 2026}],
                        "shiptypes": [{"name": "FISHING", "yearTo": 2026}],
                    }
                ],
            }
        )

        if in_area:
            hours_in_area = float(rng.uniform(30, 2500))
            area_report.append({**identity_row, "hours": round(hours_in_area, 1)})
            for watched in areas[: len(area.watched_protected_areas)]:
                if rng.random() < 0.10 + 0.5 * risk:
                    inside = float(rng.uniform(0, 60) * risk)
                    near = inside + float(rng.uniform(2, 150))
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
            cohort.append({"vessel_id": vessel["id"], "selection_reason": vessel["group"]})

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
