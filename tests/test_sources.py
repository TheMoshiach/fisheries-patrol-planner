import pytest

from patrol_planner.sources import gfw, iuu_lists


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    """Replays queued responses and records the requests made."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        return self.responses.pop(0)


def client(responses, **options):
    session = FakeSession(responses)
    return gfw.GFWClient("test-token", session=session, min_interval=0, **options), session


def test_a_token_is_required():
    with pytest.raises(gfw.GFWError, match="GFW_API_TOKEN"):
        gfw.GFWClient("")


def test_events_follow_pagination_and_send_the_token(monkeypatch):
    pages = [
        FakeResponse(payload={"entries": [{"id": "a"}, {"id": "b"}], "nextOffset": 2, "total": 3}),
        FakeResponse(payload={"entries": [{"id": "c"}], "nextOffset": None, "total": 3}),
    ]
    api, session = client(pages)
    events = list(api.iter_events("gap", "2025-10-01", "2026-10-01", ["v1", "v2"], page_size=2))
    assert [e["id"] for e in events] == ["a", "b", "c"]
    first, second = session.calls
    assert first["url"].endswith("/v3/events")
    assert first["headers"]["Authorization"] == "Bearer test-token"
    assert first["json"]["datasets"] == ["public-global-gaps-events:latest"]
    assert first["json"]["vessels"] == ["v1", "v2"]
    assert ("offset", "0") in first["params"] and ("offset", "2") in second["params"]


def test_responses_are_cached_on_disk(tmp_path):
    api, session = client([FakeResponse(payload=[{"id": "1", "label": "Reserve"}])], cache_dir=tmp_path)
    assert api.protected_area_list() == api.protected_area_list()
    assert len(session.calls) == 1
    fresh, fresh_session = client([FakeResponse(payload=[])], cache_dir=tmp_path)
    assert fresh.protected_area_list() == [{"id": "1", "label": "Reserve"}]
    assert fresh_session.calls == []


def test_server_errors_are_retried_then_reported(monkeypatch):
    monkeypatch.setattr(gfw.time, "sleep", lambda seconds: None)
    api, session = client([FakeResponse(status=503), FakeResponse(payload={"entries": []})])
    assert api.search_vessels('imo="9139610"') == []
    assert len(session.calls) == 2

    failing, _ = client([FakeResponse(status=500)] * 3, max_attempts=3)
    with pytest.raises(gfw.GFWError, match="failed after 3 attempts"):
        failing.search_vessels('imo="9139610"')


def test_a_rejected_token_is_not_retried():
    api, session = client([FakeResponse(status=401)])
    with pytest.raises(gfw.GFWError, match="rejected the API token"):
        api.protected_area_list()
    assert len(session.calls) == 1


def test_report_rows_are_flattened_and_the_buffer_is_sent():
    payload = {"entries": [{"public-global-fishing-effort:v5.0": [
        {"vesselId": "v1", "hours": 12.5}, {"vesselId": "v2", "hours": 3.0}]}]}
    api, session = client([FakeResponse(payload=payload)])
    rows = api.fishing_hours_by_vessel("2025-10-01", "2026-10-01", region_id="11753", buffer_nm=12)
    assert [r["vesselId"] for r in rows] == ["v1", "v2"]
    assert api.dataset_versions == {"public-global-fishing-effort:v5.0"}
    call = session.calls[0]
    assert call["json"] == {"region": {"dataset": "public-mpa-all", "id": 11753,
                                       "bufferUnit": "NAUTICALMILES", "bufferValue": 12}}
    assert ("group-by", "VESSEL_ID") in call["params"]
    with pytest.raises(ValueError):
        api.fishing_hours_by_vessel("2025-10-01", "2026-10-01")


@pytest.mark.parametrize("number, valid", [
    ("9074729", True),   # the standard worked example of the IMO check digit
    ("9139610", True),
    ("9074728", False),
    ("1234567", True),
    ("7654321", False),
    ("907472", False),
])
def test_imo_check_digit(number, valid):
    assert iuu_lists.is_valid_imo(number) is valid


def test_imo_numbers_are_extracted_from_list_text():
    text = "AMORINN  IMO 7036345  call sign 5VAN9; owner reg. 12345678; tel 9074728; IMO: 9074729."
    assert iuu_lists.extract_imo_numbers(text) == ["7036345", "9074729"]
    assert iuu_lists.extract_imo_numbers(iuu_lists.html_text(
        b"<table><tr><td>7036345</td><script>var x = 9074729;</script></tr></table>")) == ["7036345"]
