import pandas as pd
import pytest
from conftest import raw_event

from patrol_planner import schemas
from patrol_planner.transforms import clean


def test_events_are_typed_deduplicated_and_unknown_types_dropped():
    raw = [
        raw_event("e1", "gap"),
        raw_event("e1", "gap"),  # same event returned twice
        raw_event("e2", "port_visit"),
        raw_event("e3", "encounter", detail={
            "vessel": {"id": "other", "name": "reefer one", "flag": "pan", "type": "carrier"},
            "type": "fishing-carrier", "potentialRisk": True}),
    ]
    events = clean.clean_events(raw)
    schemas.validate(events, schemas.EVENTS)
    assert sorted(events["event_id"]) == ["e1", "e3"]
    gap = events.set_index("event_id").loc["e1"]
    assert gap["duration_hours"] == pytest.approx(24.0)
    encounter = events.set_index("event_id").loc["e3"]
    assert encounter["other_vessel_name"] == "REEFER ONE"
    assert encounter["other_vessel_flag"] == "PAN"
    assert bool(encounter["potential_risk"]) is True


def test_event_region_flags():
    raw = [
        raw_event("hs", regions={"highSeas": ["1"]}),
        raw_event("eez", regions={"eez": ["8403"]}),
        raw_event("mpa", regions={"eez": ["8403"], "mpa": ["11753"]}),
        raw_event("notake", regions={"eez": ["8403"], "mpaNoTake": ["99"]}),
        raw_event("partial", regions={"eez": ["8403"], "mpaNoTakePartial": ["77"]}),
    ]
    events = clean.clean_events(raw).set_index("event_id")
    assert bool(events.loc["hs", "on_high_seas"]) and not bool(events.loc["eez", "on_high_seas"])
    assert bool(events.loc["mpa", "in_protected_area"]) and not bool(events.loc["mpa", "in_no_take_area"])
    assert bool(events.loc["notake", "in_protected_area"]) and bool(events.loc["notake", "in_no_take_area"])
    # A partly no-take area is a protected area but not treated as a no-take violation.
    assert bool(events.loc["partial", "in_protected_area"]) and not bool(events.loc["partial", "in_no_take_area"])


def test_event_protected_area_join_table():
    raw = [raw_event("e1", regions={"mpa": ["11753", "5"], "mpaNoTake": ["5"]}), raw_event("e2")]
    links = clean.clean_event_protected_areas(raw)
    schemas.validate(links, schemas.EVENT_PROTECTED_AREAS)
    assert set(links["event_protected_area_id"]) == {"e1|11753", "e1|5"}
    assert links.set_index("protected_area_id")["no_take"].to_dict() == {"11753": False, "5": True}


def test_vessel_identity_prefers_registry_then_report_then_event_stub():
    identities = [{
        "selfReportedInfo": [{"id": "v1", "shipname": "good name", "ssvid": "111", "flag": "ecu",
                              "transmissionDateFrom": "2020-01-01T00:00:00Z",
                              "transmissionDateTo": "2026-09-01T00:00:00Z"}],
        "registryInfo": [{"imo": "9139610"}],
        "combinedSourcesInfo": [{"vesselId": "v1", "geartypes": [
            {"name": "TRAWLERS", "yearTo": 2019}, {"name": "SQUID_JIGGER", "yearTo": 2025}]}],
    }]
    report = [
        {"vesselId": "v1", "shipName": "report name", "geartype": "OTHER", "callsign": "abc1"},
        {"vesselId": "v2", "shipName": "only in report", "flag": "CHN", "vesselType": "FISHING"},
    ]
    events = [raw_event("e1", vessel="v3")]
    vessels = clean.clean_vessels(identities, report, events).set_index("vessel_id")
    schemas.validate(vessels.reset_index(), schemas.VESSELS)
    assert vessels.loc["v1", "name"] == "GOOD NAME"
    assert vessels.loc["v1", "imo"] == "9139610"
    assert vessels.loc["v1", "gear_type"] == "squid_jigger"  # most recent classification
    assert vessels.loc["v1", "callsign"] == "ABC1"  # filled from the report where missing
    assert vessels.loc["v2", "name"] == "ONLY IN REPORT"
    assert vessels.loc["v3", "name"] == "VESSEL V3"


def test_protected_area_activity_splits_inside_and_buffer():
    base = {"_window_start": "2025-10-01T00:00:00+00:00", "_window_end": "2026-10-01T00:00:00+00:00"}
    rows = [
        {"vesselId": "v1", "hours": 10, "_protected_area_id": "11753", "_buffer_nm": 0.0, **base},
        {"vesselId": "v1", "hours": 25, "_protected_area_id": "11753", "_buffer_nm": 12.0, **base},
        {"vesselId": "v2", "hours": 8, "_protected_area_id": "11753", "_buffer_nm": 12.0, **base},
    ]
    activity = clean.clean_protected_area_activity(rows).set_index("vessel_id")
    schemas.validate(activity.reset_index(), schemas.PROTECTED_AREA_ACTIVITY)
    assert activity.loc["v1", "fishing_hours_inside"] == 10
    # Buffered rows cover only the ring outside the area, so the two are added.
    assert activity.loc["v1", "fishing_hours_within_buffer"] == 35
    assert activity.loc["v2", "fishing_hours_inside"] == 0  # only ever near, never inside
    assert activity.loc["v2", "fishing_hours_within_buffer"] == 8
    assert clean.clean_protected_area_activity([]).empty


INSIGHT = {
    "_vessel_id": "v1",
    "period": {"startDate": "2025-10-01", "endDate": "2026-10-01"},
    "apparentFishing": {"periodSelectedCounters": {
        "events": 120, "eventsInNoTakeMPAs": 2, "eventsInRFMOWithoutKnownAuthorization": 1}},
    "coverage": {"percentage": 71.5},
    "vesselIdentity": {
        "iuuVesselList": {"valuesInThePeriod": [{"from": "2025-10-01T00:00:00Z", "to": "2026-03-01T00:00:00Z"}],
                          "totalTimesListed": 1, "totalTimesListedInThePeriod": 1},
    },
}


def test_insights_and_ground_truth_are_separate_tables():
    identities = [{
        "selfReportedInfo": [
            {"id": "v1", "flag": "PAN", "transmissionDateFrom": "2026-02-01T00:00:00Z",
             "transmissionDateTo": "2026-09-30T00:00:00Z"},
            {"id": "v1-old", "flag": "BLZ", "transmissionDateFrom": "2019-01-01T00:00:00Z",
             "transmissionDateTo": "2026-01-31T00:00:00Z"},
            {"id": "v1-older", "flag": "KHM", "transmissionDateFrom": "2012-01-01T00:00:00Z",
             "transmissionDateTo": "2018-12-31T00:00:00Z"},  # before the window: not counted
        ],
        "registryInfo": [{"flag": "pan", "transmissionDateFrom": "2026-02-01T00:00:00Z",
                          "transmissionDateTo": "2026-09-30T00:00:00Z"}],
    }]
    insights = clean.clean_vessel_insights([INSIGHT], identities)
    listings = clean.clean_iuu_listings([INSIGHT, {"_vessel_id": "v2", "vesselIdentity": {
        "iuuVesselList": {"valuesInThePeriod": [], "totalTimesListed": 0, "totalTimesListedInThePeriod": 0}}}])
    row = insights.iloc[0]
    assert (row["fishing_events"], row["no_take_fishing_events"], row["unauthorized_fishing_events"]) == (120, 2, 1)
    assert row["ais_coverage_pct"] == 71.5 and row["flag_changes"] == 1
    assert not any("iuu" in column for column in insights.columns)
    assert listings["vessel_id"].tolist() == ["v1"]
    assert listings["listed_to"].iloc[0] == pd.Timestamp("2026-03-01", tz="UTC")


def test_one_vessel_can_have_indicators_for_two_windows():
    earlier = {**INSIGHT, "period": {"startDate": "2021-01-01", "endDate": "2022-01-01"}}
    insights = clean.clean_vessel_insights([INSIGHT, earlier], [])
    schemas.validate(insights, schemas.VESSEL_INSIGHTS)
    assert sorted(insights["insight_id"]) == ["v1|2022-01-01", "v1|2026-10-01"]


def test_cohorts_keep_only_groups_whose_vessel_is_confirmed_listed():
    start, end = pd.Timestamp("2025-10-01", tz="UTC"), pd.Timestamp("2026-10-01", tz="UTC")
    report = [
        {"vesselId": "p1", "vesselType": "FISHING", "hours": 30},
        {"vesselId": "p2", "vesselType": "FISHING", "hours": 5},  # below the hours threshold
        {"vesselId": "c9", "vesselType": "CARRIER", "hours": 500},  # not a fishing vessel
    ]
    group = {"window_start": "2021-03-01", "window_end": "2022-03-01", "vessel_class": "fishing"}
    raw_cohort = [
        {"vessel_id": "listed", "selection_reason": "iuu_listed_vessel", "match_group": "listed", **group},
        {"vessel_id": "control_a", "selection_reason": "matched_control", "match_group": "listed", **group},
        {"vessel_id": "unconfirmed", "selection_reason": "iuu_listed_vessel", "match_group": "unconfirmed", **group},
        {"vessel_id": "control_b", "selection_reason": "matched_control", "match_group": "unconfirmed", **group},
    ]
    listings = clean.clean_iuu_listings([{**INSIGHT, "_vessel_id": "listed"}])
    cohort = clean.clean_cohort_members(raw_cohort, report, 24.0, listings, start, end)
    schemas.validate(cohort, schemas.COHORT_MEMBERS)
    patrol = cohort[cohort["cohort"] == "patrol_area"]
    evaluation = cohort[cohort["cohort"] == "evaluation"]
    assert patrol["vessel_id"].tolist() == ["p1"]
    assert patrol["window_end"].iloc[0] == end
    # The unconfirmed vessel and the comparison vessels drawn for it are both dropped.
    assert set(evaluation["vessel_id"]) == {"listed", "control_a"}
    assert set(evaluation["window_end"]) == {pd.Timestamp("2022-03-01", tz="UTC")}
    assert set(evaluation["match_group"]) == {"listed"}


def test_watched_protected_areas_match_ignoring_accents_and_case():
    areas = [{"id": "11753", "label": "Galápagos Marine Reserve"},
             {"id": "2", "label": "Hermandad - Reserva Marina"}, {"id": "3", "label": "Elsewhere"}]
    matches = clean.match_watched_protected_areas(areas, ["galapagos marine reserve", "HERMANDAD"])
    assert set(matches) == {"11753", "2"}
