"""Client for the Global Fishing Watch API (version 3).

Data from this API is licensed CC BY-NC 4.0 and must be attributed
("Powered by Global Fishing Watch", https://globalfishingwatch.org).

The client is deliberately polite: every response is cached on disk so a rerun
costs no requests, requests are spaced out, and it backs off on errors. The
documented limits are 50,000 requests per day and one report at a time.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import requests

BASE_URL = "https://gateway.api.globalfishingwatch.org/v3"
ATTRIBUTION = "Powered by Global Fishing Watch. https://globalfishingwatch.org"

IDENTITY_DATASET = "public-global-vessel-identity:latest"
FISHING_EFFORT_DATASET = "public-global-fishing-effort:latest"
EVENT_DATASETS = {
    "fishing": "public-global-fishing-events:latest",
    "encounter": "public-global-encounters-events:latest",
    "gap": "public-global-gaps-events:latest",
}
PROTECTED_AREA_DATASET = "public-mpa-all"
INSIGHTS = ("FISHING", "COVERAGE", "VESSEL-IDENTITY-FLAG-CHANGES", "VESSEL-IDENTITY-IUU-VESSEL-LIST")


class GFWError(RuntimeError):
    pass


class RateLimited(GFWError):
    pass


class GFWClient:
    def __init__(
        self,
        token: str,
        cache_dir: Path | str | None = None,
        refresh: bool = False,
        min_interval: float = 0.2,
        session: requests.Session | None = None,
        max_attempts: int = 5,
    ):
        if not token:
            raise GFWError(
                "No Global Fishing Watch API token. Put GFW_API_TOKEN=<your token> in the .env file."
            )
        self._token = token
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._refresh = refresh
        self._min_interval = min_interval
        self._session = session or requests.Session()
        self._max_attempts = max_attempts
        self._last_request = 0.0
        self._pace = threading.Lock()
        self.requests_made = 0
        self.dataset_versions: set[str] = set()

    # --- transport -----------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        params: list[tuple[str, str]] | None = None,
        body: dict | None = None,
        cache: bool = True,
        timeout: float = 150,
    ):
        params = params or []
        key = hashlib.sha1(
            json.dumps([method, path, params, body], sort_keys=True).encode()
        ).hexdigest()
        cached = self._cache_dir / f"{key}.json" if (self._cache_dir and cache) else None
        if cached and cached.exists() and not self._refresh:
            return json.loads(cached.read_text(encoding="utf-8"))

        payload = self._send(method, path, params, body, timeout)
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def _send(self, method, path, params, body, timeout):
        delay = 2.0
        last_error = "no attempt made"
        for attempt in range(1, self._max_attempts + 1):
            with self._pace:  # space requests out, also when called from several threads
                wait = self._min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
                self._last_request = time.monotonic()
                self.requests_made += 1
            try:
                response = self._session.request(
                    method,
                    BASE_URL + path,
                    params=params,
                    json=body,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=timeout,
                )
            except requests.RequestException as error:
                last_error = f"{type(error).__name__}: {error}"
            else:
                status = response.status_code
                if status == 200:
                    return response.json()
                if status in (401, 403):
                    raise GFWError(
                        f"Global Fishing Watch rejected the API token (HTTP {status}). "
                        "Check GFW_API_TOKEN in the .env file."
                    )
                if status == 429:
                    remaining = response.headers.get("x-ratelimit-daily-remaining-requests")
                    if remaining is not None and remaining.isdigit() and int(remaining) <= 0:
                        raise RateLimited("Daily Global Fishing Watch request limit reached.")
                    last_error = "HTTP 429 (another report is running or the rate limit was hit)"
                    delay = max(delay, 15.0)
                elif status == 524 and "/4wings/report" in path:
                    return self._wait_for_report()
                elif status >= 500:
                    last_error = f"HTTP {status}"
                else:
                    raise GFWError(f"HTTP {status} from {path}: {response.text[:400]}")
            if attempt < self._max_attempts:
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
        raise GFWError(f"{method} {path} failed after {self._max_attempts} attempts: {last_error}")

    def _wait_for_report(self, poll_seconds: float = 15.0, max_polls: int = 60):
        """A report that outlives the gateway timeout can be collected once it finishes."""
        for _ in range(max_polls):
            time.sleep(poll_seconds)
            self.requests_made += 1
            response = self._session.get(
                BASE_URL + "/4wings/last-report",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=150,
            )
            if response.status_code != 200:
                continue
            payload = response.json()
            if isinstance(payload, dict) and payload.get("status") == "running":
                continue
            if isinstance(payload, dict) and "entries" in payload:
                return payload
            raise GFWError(f"Report failed: {str(payload)[:400]}")
        raise GFWError("Report did not finish in time.")

    # --- reference data ---------------------------------------------------------
    def protected_area_list(self) -> list[dict]:
        """Every marine protected area known to the API: [{"id": ..., "label": ...}]."""
        return self.request("GET", f"/datasets/{PROTECTED_AREA_DATASET}/context-layers")

    # --- activity reports ---------------------------------------------------------
    def fishing_hours_by_vessel(
        self,
        start_date: str,
        end_date: str,
        *,
        geojson: dict | None = None,
        region_id: str | None = None,
        region_dataset: str = PROTECTED_AREA_DATASET,
        buffer_nm: float | None = None,
    ) -> list[dict]:
        """Apparent fishing hours per vessel inside a polygon or a named region."""
        if (geojson is None) == (region_id is None):
            raise ValueError("give exactly one of geojson or region_id")
        params = [
            ("spatial-resolution", "LOW"),
            ("temporal-resolution", "ENTIRE"),
            ("spatial-aggregation", "true"),
            ("group-by", "VESSEL_ID"),
            ("datasets[0]", FISHING_EFFORT_DATASET),
            ("date-range", f"{start_date},{end_date}"),
            ("format", "JSON"),
        ]
        if geojson is not None:
            body: dict = {"geojson": geojson}
        else:
            region: dict = {"dataset": region_dataset, "id": _maybe_int(region_id)}
            if buffer_nm:
                region.update({"bufferUnit": "NAUTICALMILES", "bufferValue": buffer_nm})
            body = {"region": region}
        payload = self.request("POST", "/4wings/report", params=params, body=body, timeout=200)
        rows: list[dict] = []
        for entry in payload.get("entries") or []:
            for dataset, dataset_rows in entry.items():
                self.dataset_versions.add(dataset)
                rows.extend(dataset_rows or [])
        return rows

    # --- events ---------------------------------------------------------------------
    def events_page(
        self,
        event_type: str,
        start_date: str,
        end_date: str,
        *,
        vessels: list[str] | None = None,
        offset: int = 0,
        limit: int = 500,
        sort: str | None = None,
    ) -> dict:
        body: dict = {
            "datasets": [EVENT_DATASETS[event_type]],
            "startDate": start_date,
            "endDate": end_date,
        }
        if vessels:
            body["vessels"] = list(vessels)
        params = [("offset", str(offset)), ("limit", str(limit))]
        if sort:
            params.append(("sort", sort))
        return self.request("POST", "/events", params=params, body=body)

    def iter_events(
        self,
        event_type: str,
        start_date: str,
        end_date: str,
        vessels: list[str],
        page_size: int = 500,
    ) -> Iterator[dict]:
        """All events of one type for the given vessels, following pagination."""
        offset = 0
        while True:
            page = self.events_page(
                event_type, start_date, end_date, vessels=vessels, offset=offset, limit=page_size
            )
            entries = page.get("entries") or []
            yield from entries
            next_offset = page.get("nextOffset")
            if not entries or next_offset is None or next_offset <= offset:
                return
            offset = next_offset

    def event(self, event_id: str, event_type: str) -> dict:
        return self.request(
            "GET", f"/events/{event_id}", params=[("dataset", EVENT_DATASETS[event_type])]
        )

    # --- vessels ----------------------------------------------------------------------
    def search_vessels(self, where: str, limit: int = 50) -> list[dict]:
        payload = self.request(
            "GET",
            "/vessels/search",
            params=[("where", where), ("datasets[0]", IDENTITY_DATASET), ("limit", str(limit))],
        )
        return payload.get("entries") or []

    def vessels_by_ids(self, vessel_ids: list[str]) -> list[dict]:
        params = [("datasets[0]", IDENTITY_DATASET)]
        params += [(f"ids[{i}]", vessel_id) for i, vessel_id in enumerate(vessel_ids)]
        payload = self.request("GET", "/vessels", params=params)
        return payload.get("entries") or []

    def insights(self, vessel_id: str, start_date: str, end_date: str) -> dict:
        """Indicators for one vessel: fishing counters, AIS coverage, flag changes, IUU listing."""
        body = {
            "includes": list(INSIGHTS),
            "startDate": start_date,
            "endDate": end_date,
            "vessels": [{"datasetId": IDENTITY_DATASET, "vesselId": vessel_id}],
        }
        return self.request("POST", "/insights/vessels", body=body)


def _maybe_int(value):
    text = str(value)
    return int(text) if text.isdigit() else text
