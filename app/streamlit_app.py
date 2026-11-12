"""Officer-facing app. Display only: every calculation and every write goes through
the ``patrol_planner`` package.

Run with:  streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import folium
import pandas as pd
import streamlit as st
from folium.plugins import Draw
from streamlit_folium import st_folium

from patrol_planner import actions, config, jobs, pipeline, planning, views
from patrol_planner.sources.ingest import load_token
from patrol_planner.storage import open_store, settings_for

TIER_COLOURS = {"High": "#c62828", "Elevated": "#ef8f00", "Low": "#4f8f5b"}
EVENT_STYLE = {
    "gap": ("#37474f", "AIS switched off"),
    "encounter": ("#6a1b9a", "Met another vessel"),
    "fishing": ("#c62828", "Flagged fishing"),
}
SIGNAL_LABELS = {
    "going_dark": "Going dark (AIS gaps)",
    "encounters": "Meeting other vessels at sea",
    "protected_areas": "Fishing in or near protected areas",
    "unauthorized_fishing": "Fishing without a known authorisation",
    "identity_changes": "Changing flag",
}

st.set_page_config(page_title="Fisheries Patrol Planner", layout="wide")
store = open_store("real")


# --- data refresh -----------------------------------------------------------------
def start_refresh() -> None:
    if not load_token():
        st.session_state["flash"] = (
            "error",
            "No Global Fishing Watch token found. Open the file named .env in the project "
            "folder with Notepad and paste your token after GFW_API_TOKEN=",
        )
        return
    jobs.start_refresh()


@st.fragment(run_every=4)
def refresh_status(location: str) -> None:
    """Shows refresh progress and reloads the page once new data is ready."""
    status = jobs.read_status()
    state = status.get("state")
    if state == "running":
        lines = jobs.progress_lines(1)
        st.info(f"Refreshing data… {lines[-1].strip() if lines else 'starting'}")
        st.caption("This takes 10 to 20 minutes. You can keep working; the page reloads when it is done.")
        st.session_state["refresh_watching"] = True
    elif state in ("failed", "stalled"):
        st.error(f"The last refresh did not finish: {status.get('message')}")
    elif state == "done" and st.session_state.pop("refresh_watching", False):
        st.session_state["flash"] = ("success", f"Data refreshed. {status.get('message', '')}")
        st.rerun(scope="app")
    st.button(
        "Refresh data from Global Fishing Watch",
        key=f"refresh_{location}",
        on_click=start_refresh,
        disabled=state == "running",
        help="Downloads the newest available data and rebuilds the ranking. Takes 10 to 20 minutes.",
    )


if not store.exists("risk_scores"):
    st.title("Fisheries Patrol Planner")
    st.info("There is no data yet. Click the button below to download it.")
    flash = st.session_state.pop("flash", None)
    if flash:
        st.error(flash[1])
    refresh_status("first_run")
    st.stop()

settings = settings_for(store)
manifest = store.read_manifest()
data_mode = manifest.get("data_mode", "real")
log = views.decision_log(store)


# --- shared state -------------------------------------------------------------------
def select_vessel(vessel_id: str | None) -> None:
    st.session_state["selected"] = vessel_id
    st.session_state["picker"] = vessel_id


def record(action, **parameters) -> None:
    """Submit one action; show the reason if it is rejected."""
    try:
        action(log, data_mode=data_mode, **parameters)
    except actions.ActionError as error:
        st.session_state["flash"] = ("error", str(error))
    else:
        st.session_state["flash"] = ("success", "Decision saved.")
    st.rerun()


# A selection made by clicking the map is applied here, before any widget is drawn,
# so the search box, the list and the map always agree.
if "pending_select" in st.session_state:
    select_vessel(st.session_state.pop("pending_select"))
st.session_state.setdefault("selected", None)
st.session_state.setdefault("route_stops", [])
if st.session_state.pop("clear_route", False):
    st.session_state["route_stops"] = []

ranking = views.patrol_area_ranking(store)
boats = store.read("patrol_boats")
decisions = log.read()
open_jobs = actions.open_assignments(decisions)
busy_boats = set(open_jobs["patrol_boat_id"])
free_boats = boats[(boats["status"] == "available") & ~boats["patrol_boat_id"].isin(busy_boats)]

# --- header -------------------------------------------------------------------------
st.title("Fisheries Patrol Planner")
if data_mode == "sample":
    st.warning("**Synthetic sample data.** These vessels and events are generated, not real.")
st.caption(
    f"{settings.patrol_area.name} · vessel behaviour from {manifest.get('window_start')} to "
    f"{manifest.get('window_end')} · data downloaded {manifest.get('accessed', 'n/a')} · "
    "[Powered by Global Fishing Watch](https://globalfishingwatch.org)"
)
flash = st.session_state.pop("flash", None)
if flash:
    (st.error if flash[0] == "error" else st.success)(flash[1])

# --- sidebar ------------------------------------------------------------------------
with st.sidebar:
    st.header("Officer")
    officer = st.text_input("Your name", key="officer", placeholder="Required to save decisions")
    st.header("Show on the map")
    tiers = st.multiselect("Risk level", ["High", "Elevated", "Low"], default=["High", "Elevated", "Low"])
    flags = st.multiselect("Flag", sorted(ranking["flag"].dropna().unique()))
    hide_dismissed = st.checkbox("Hide dismissed vessels", value=True)
    show_reserves = st.checkbox("Protected areas", value=True)
    show_boats = st.checkbox("Patrol boats", value=True)
    show_trail = st.checkbox("Selected vessel's trail and events", value=True)
    st.divider()
    st.metric("Patrol boats free", f"{len(free_boats)} of {len(boats)}")
    st.header("Data")
    refresh_status("sidebar")

shown = ranking[ranking["risk_tier"].isin(tiers)]
if flags:
    shown = shown[shown["flag"].isin(flags)]
if hide_dismissed:
    shown = shown[shown["status"] != "Dismissed"]
shown = shown.reset_index(drop=True)
by_id = ranking.set_index("vessel_id")
if st.session_state["selected"] not in set(shown["vessel_id"]):
    select_vessel(None)
selected_id = st.session_state["selected"]
selected = by_id.loc[selected_id] if selected_id else None
# Only vessels with a known position can be on a route, and only a free boat can sail it.
routable = set(ranking.dropna(subset=["last_lat", "last_lon"])["vessel_id"])
st.session_state["route_stops"] = [v for v in st.session_state["route_stops"] if v in routable]
if st.session_state.get("route_boat") not in set(free_boats["patrol_boat_id"]):
    st.session_state["route_boat"] = free_boats["patrol_boat_id"].iloc[0] if len(free_boats) else None


def vessel_label(vessel_id: str) -> str:
    row = by_id.loc[vessel_id]
    return f"#{int(row['rank'])}  {row['name']}  ·  {row['flag']}  ·  score {row['risk_score']:.0f}  ·  {row['risk_tier']}"


tab_map, tab_log, tab_reliability, tab_settings = st.tabs(
    ["Map", "Decision log", "How reliable is this?", "Settings"]
)

# =====================================================================================
# Map
# =====================================================================================
with tab_map:
    counts = ranking["risk_tier"].value_counts()
    a, b, c, d = st.columns(4)
    a.metric("Vessels in patrol area", len(ranking))
    b.metric("High risk", int(counts.get("High", 0)))
    c.metric("Elevated risk", int(counts.get("Elevated", 0)))
    d.metric("Vessels with a patrol assigned", len(open_jobs))

    side, main = st.columns([4, 7], gap="medium")

    # --- find a vessel ---------------------------------------------------------------
    with side:
        st.selectbox(
            "Find a vessel",
            list(shown["vessel_id"]),
            index=None,
            key="picker",
            format_func=vessel_label,
            placeholder="Type a name, or pick from the list (highest risk first)",
            on_change=lambda: st.session_state.update(selected=st.session_state["picker"]),
        )
        # The selected vessel's details sit directly under the search box, above the list.
        detail_slot = st.container()

        def pick_from_list() -> None:
            rows = st.session_state["rank_list"]["selection"]["rows"]
            ids = st.session_state.get("list_ids", [])
            if rows and rows[0] < len(ids):
                select_vessel(ids[rows[0]])

        st.session_state["list_ids"] = list(shown["vessel_id"])
        with st.expander(f"All {len(shown)} vessels, highest risk first", expanded=selected is None):
            st.dataframe(
                shown[["rank", "name", "flag", "risk_score", "risk_tier", "status"]],
                column_config={
                    "rank": st.column_config.NumberColumn("#", width="small"),
                    "name": "Vessel",
                    "flag": st.column_config.TextColumn("Flag", width="small"),
                    "risk_score": st.column_config.ProgressColumn(
                        "Score", min_value=0, max_value=100, format="%.0f"
                    ),
                    "risk_tier": "Risk",
                    "status": "Status",
                },
                hide_index=True,
                width="stretch",
                height=330,
                on_select=pick_from_list,
                selection_mode="single-row",
                key="rank_list",
            )
            st.caption("Click the box at the start of a row to open that vessel.")

    # --- the map ---------------------------------------------------------------------
    with main:
        west, south, east, north = settings.patrol_area.bbox
        base = folium.Map(
            location=[(south + north) / 2, (west + east) / 2],
            zoom_start=6,
            tiles="OpenStreetMap",
            control_scale=True,
        )
        folium.Rectangle(
            [[south, west], [north, east]], color="#555555", weight=1.5, fill=False, dash_array="6",
            tooltip="Patrol area",
        ).add_to(base)

        layers = []
        if show_reserves:
            reserves = folium.FeatureGroup(name="Protected areas")
            for shape in views.protected_area_shapes(store):
                folium.GeoJson(
                    shape["geometry"],
                    style_function=lambda _: {"color": "#1b7f5a", "weight": 1.5, "fillOpacity": 0.08},
                    tooltip=f"Protected area: {shape['name']}",
                ).add_to(reserves)
            layers.append(reserves)

        vessel_layer = folium.FeatureGroup(name="Vessels")
        located = shown.dropna(subset=["last_lat", "last_lon"])
        # Drawn lowest risk first so the highest-risk dots end up on top.
        for row in located.iloc[::-1].itertuples(index=False):
            is_selected = row.vessel_id == selected_id
            folium.CircleMarker(
                [row.last_lat, row.last_lon],
                radius=10 if is_selected else 6,
                color="#111111" if is_selected else "#ffffff",
                weight=3 if is_selected else 1,
                fill=True,
                fill_color=TIER_COLOURS[row.risk_tier],
                fill_opacity=0.95,
                tooltip=f"#{row.rank} {row.name} · score {row.risk_score:.0f} · {row.risk_tier}",
            ).add_to(vessel_layer)
        layers.append(vessel_layer)

        if show_boats:
            boat_layer = folium.FeatureGroup(name="Patrol boats")
            for boat in boats.itertuples(index=False):
                state = "on patrol" if boat.patrol_boat_id in busy_boats else boat.status
                folium.Marker(
                    [boat.home_lat, boat.home_lon],
                    icon=folium.Icon(color="blue", icon="ship", prefix="fa"),
                    tooltip=f"Patrol boat: {boat.name} · {boat.home_port} · {state}",
                ).add_to(boat_layer)
            layers.append(boat_layer)

        if selected is not None and show_trail:
            detail = folium.FeatureGroup(name="Selected vessel")
            trail = views.vessel_trail(store, selected_id, settings)
            if len(trail) > 1:
                folium.PolyLine(
                    trail[["lat", "lon"]].values.tolist(), color="#1565c0", weight=2, opacity=0.6,
                    dash_array="4", tooltip="Order of recent fishing locations (not the actual track)",
                ).add_to(detail)
            for point in trail.itertuples(index=False):
                folium.CircleMarker(
                    [point.lat, point.lon], radius=2.5, color="#1565c0", fill=True, fill_opacity=0.8,
                    weight=0, tooltip=f"Fished here {point.start_time:%d %b %Y}",
                ).add_to(detail)
            for event in views.vessel_events(store, selected_id, settings).dropna(
                subset=["lat", "lon"]
            ).itertuples(index=False):
                colour, title = EVENT_STYLE[event.event_type]
                folium.CircleMarker(
                    [event.lat, event.lon], radius=7, color=colour, weight=2, fill=True,
                    fill_color=colour, fill_opacity=0.35 if not event.counted else 0.85,
                    tooltip=f"{title} · {event.start_time:%d %b %Y}: {event.description}",
                ).add_to(detail)
            layers.append(detail)

        route_boat_id = st.session_state.get("route_boat")
        route_boat = boats[boats["patrol_boat_id"] == route_boat_id]
        route_legs = None
        if st.session_state["route_stops"] and not route_boat.empty:
            stops = ranking[ranking["vessel_id"].isin(st.session_state["route_stops"])]
            route_legs = planning.plan_route(route_boat.iloc[0], stops)
            if not route_legs.empty:
                route_layer = folium.FeatureGroup(name="Planned route")
                home = [float(route_boat.iloc[0]["home_lat"]), float(route_boat.iloc[0]["home_lon"])]
                path = [home] + route_legs[["to_lat", "to_lon"]].values.tolist()
                folium.PolyLine(path, color="#111111", weight=3, tooltip="Planned patrol route").add_to(route_layer)
                for leg in route_legs[route_legs["vessel_id"].notna()].itertuples(index=False):
                    folium.Marker(
                        [leg.to_lat, leg.to_lon],
                        icon=folium.DivIcon(
                            html=f'<div style="background:#fff;border:2px solid #1a237e;border-radius:50%;'
                                 f'width:22px;height:22px;line-height:18px;text-align:center;font-weight:700;'
                                 f'color:#1a237e;margin:-5px 0 0 -5px">{leg.leg}</div>'
                        ),
                        tooltip=f"Stop {leg.leg}: {leg.to}",
                    ).add_to(route_layer)
                layers.append(route_layer)

        clicked = st_folium(
            base,
            feature_group_to_add=layers,
            returned_objects=["last_object_clicked_tooltip", "last_object_clicked"],
            height=540,
            width="stretch",
            key="patrol_map",
        )
        st.caption(
            "Dots are vessels: red is high risk, amber elevated, green low. Click a dot to open the "
            "vessel. A dot marks where the vessel's most recent recorded event took place; it is not "
            "a live position. Blue markers are patrol boat home ports."
        )
        tooltip = (clicked or {}).get("last_object_clicked_tooltip") or ""
        click_key = f"{tooltip}|{(clicked or {}).get('last_object_clicked')}"
        if tooltip.startswith("#") and click_key != st.session_state.get("handled_click"):
            st.session_state["handled_click"] = click_key
            rank = int(tooltip[1:].split(" ", 1)[0])
            match = ranking[ranking["rank"] == rank]
            if not match.empty and match.iloc[0]["vessel_id"] in set(shown["vessel_id"]):
                st.session_state["pending_select"] = match.iloc[0]["vessel_id"]
                st.rerun()

    # --- selected vessel -------------------------------------------------------------
    with detail_slot:
        if selected is None:
            st.info("Pick a vessel from the search box, the list, or by clicking a dot on the map.")
        else:
            st.subheader(f"{selected['name']}")
            st.caption(
                f"Rank {int(selected['rank'])} of {len(ranking)} · flag {selected['flag']} · "
                f"{str(selected['gear_type']).replace('_', ' ')} · MMSI {selected['mmsi']} · "
                f"IMO {selected['imo'] if pd.notna(selected['imo']) else 'not reported'}"
            )
            m1, m2 = st.columns(2)
            m1.metric("Risk score", f"{selected['risk_score']:.0f} / 100")
            m2.metric("Risk", selected["risk_tier"])
            st.markdown(f"**Status:** {selected['status']}")
            coverage = selected["ais_coverage_pct"]
            tracker = "an unknown share" if pd.isna(coverage) else f"{coverage:.0f}%"
            st.caption(
                f"Data confidence: {selected['data_confidence']} (tracker on {tracker} of its time "
                f"at sea) · {selected['patrol_area_fishing_hours']:.0f} fishing hours in the patrol area"
            )

            st.markdown("**Why it ranks here**")
            for reason in views.vessel_reasons(store, selected["risk_score_id"]).itertuples(index=False):
                if reason.points <= 0:
                    continue
                st.markdown(f"**+{reason.points:.0f}** · {reason.label}  \n{reason.explanation}")
            if selected["risk_score"] <= 0:
                st.markdown("No risk signals in the analysis period.")
            st.caption(
                "The points add up to the score. Events are inferred from tracker data by Global "
                "Fishing Watch: they are leads to check, not proof."
            )

            on_route = selected_id in st.session_state["route_stops"]

            def toggle_route() -> None:
                stops = st.session_state["route_stops"]
                st.session_state["route_stops"] = (
                    [v for v in stops if v != selected_id] if on_route else stops + [selected_id]
                )

            st.button(
                "Remove from patrol route" if on_route else "Add to patrol route",
                on_click=toggle_route,
                type="secondary" if on_route else "primary",
                disabled=bool(pd.isna(selected["last_lat"])),
                help="Routes are planned below the map.",
            )
            with st.expander("Watchlist or dismiss"):
                with st.form("decision_form", clear_on_submit=True):
                    choice = st.radio("Decision", ["Add to watchlist", "Dismiss (no action needed)"])
                    rationale = st.text_area("Reason", placeholder="Why this decision?")
                    if st.form_submit_button("Save decision"):
                        common = dict(officer=officer, vessel_id=selected_id, rationale=rationale,
                                      ranked_vessels=ranking)
                        record(actions.add_to_watchlist if choice.startswith("Add") else actions.dismiss_vessel,
                               **common)
            events = views.vessel_events(store, selected_id, settings)
            with st.expander(f"Events behind the score ({len(events)})"):
                st.dataframe(
                    events[["start_time", "description", "counted"]],
                    column_config={
                        "start_time": st.column_config.DatetimeColumn("When (UTC)", format="YYYY-MM-DD"),
                        "description": st.column_config.TextColumn("What happened", width="large"),
                        "counted": "Counts",
                    },
                    hide_index=True, width="stretch",
                )

    # --- patrol route planner --------------------------------------------------------
    with main:
        st.subheader("Plan a patrol")
        if free_boats.empty:
            st.info("No patrol boat is free. Release one below to plan a new patrol.")
        else:
            boat_names = dict(zip(free_boats["patrol_boat_id"], free_boats["name"] + " · " + free_boats["home_port"]))
            p1, p2 = st.columns([2, 1])
            boat_id = p1.selectbox("Patrol boat", list(boat_names), format_func=boat_names.get, key="route_boat")
            boat = boats[boats["patrol_boat_id"] == boat_id].iloc[0]
            handled = set(open_jobs["vessel_id"]) | set(ranking.loc[ranking["status"] == "Dismissed", "vessel_id"])

            def suggest() -> None:
                st.session_state["route_stops"] = planning.suggest_route(boat, ranking, handled)

            p2.write("")
            p2.button("Suggest a route", on_click=suggest,
                      help="Picks the highest-risk vessels this boat can visit and still get home.")
            choices = list(ranking.dropna(subset=["last_lat"])["vessel_id"])
            st.multiselect(
                "Vessels to visit", choices, key="route_stops", format_func=vessel_label,
                placeholder="Add vessels here, or use 'Add to patrol route' on a vessel",
            )
            if route_legs is not None and not route_legs.empty and route_boat_id == boat_id:
                summary = planning.route_summary(route_legs, boat)
                table = route_legs[["leg", "from", "to", "distance_nm", "bearing_deg", "hours"]].rename(columns={
                    "leg": "Leg", "from": "From", "to": "To", "distance_nm": "Distance (nm)",
                    "bearing_deg": "Bearing (°)", "hours": "Hours"})
                table["Distance (nm)"] = table["Distance (nm)"].map("{:.0f}".format)
                table["Bearing (°)"] = table["Bearing (°)"].map("{:03.0f}".format)
                table["Hours"] = table["Hours"].map("{:.1f}".format)
                st.table(table.set_index("Leg"))
                plural = "s" if summary["stops"] != 1 else ""
                text = (f"**{summary['total_nm']:.0f} nm** in about **{summary['total_hours']:.0f} hours** at "
                        f"{boat['max_speed_knots']:.0f} knots, visiting {summary['stops']} vessel{plural} "
                        f"and returning to {boat['home_port']}.")
                if summary["within_range"]:
                    st.markdown(text + f" Within the boat's {summary['range_nm']:.0f} nm range.")
                else:
                    st.warning(text + f" This is beyond the boat's {summary['range_nm']:.0f} nm range.")
                st.caption("Straight-line legs to each vessel's last recorded position. "
                           "Weather, currents and land are not taken into account.")
                note = st.text_input("Note for the log (optional)", key="route_note")
                if st.button("Save patrol", type="primary"):
                    ordered = list(route_legs["vessel_id"].dropna())
                    st.session_state["clear_route"] = True
                    record(
                        actions.assign_route, officer=officer, patrol_boat_id=boat_id, vessel_ids=ordered,
                        ranked_vessels=ranking, patrol_boats=boats,
                        rationale=note or f"Patrol of {summary['stops']} vessel{plural}, {summary['total_nm']:.0f} nm.",
                    )
            elif st.session_state["route_stops"]:
                st.caption("The route appears once the page updates.")

        if not open_jobs.empty:
            st.markdown("**Patrols under way**")
            for boat_id, job in open_jobs.groupby("patrol_boat_id", sort=False):
                names = [by_id.loc[v, "name"] if v in by_id.index else v for v in job["vessel_id"]]
                text, button = st.columns([5, 1])
                text.markdown(
                    f"**{boat_id}** → {', '.join(names)} · since "
                    f"{pd.Timestamp(job['decided_at'].iloc[0]):%Y-%m-%d %H:%M} UTC, by {job['officer'].iloc[0]}"
                )
                if button.button("Release", key=f"release_{boat_id}"):
                    record(actions.release_boat, officer=officer, patrol_boat_id=boat_id,
                           rationale="Patrol completed.")

# =====================================================================================
# Decision log
# =====================================================================================
with tab_log:
    st.subheader("Every decision, newest first")
    if decisions.empty:
        st.info("No decisions recorded yet.")
    else:
        names = ranking[["vessel_id", "name"]].rename(columns={"name": "vessel"})
        table = decisions.merge(names, on="vessel_id", how="left").sort_values(
            ["decided_at", "route_stop"], ascending=[False, True])
        table["vessel"] = table["vessel"].fillna(table["vessel_id"])
        st.dataframe(
            table[["decided_at", "officer", "decision_type", "vessel", "patrol_boat_id", "route_stop",
                   "risk_score_at_decision", "rank_at_decision", "rationale"]],
            column_config={
                "decided_at": st.column_config.DatetimeColumn("When (UTC)", format="YYYY-MM-DD HH:mm"),
                "officer": "Officer", "decision_type": "Decision", "vessel": "Vessel",
                "patrol_boat_id": "Boat", "route_stop": "Stop",
                "risk_score_at_decision": "Score then", "rank_at_decision": "Rank then",
                "rationale": "Reason",
            },
            hide_index=True, width="stretch",
        )
        st.download_button("Download as CSV", decisions.to_csv(index=False), "patrol_decisions.csv", "text/csv")
    st.caption(f"Saved to {log.path}. Rows are only ever added, never changed.")

# =====================================================================================
# Reliability
# =====================================================================================
with tab_reliability:
    st.subheader("How reliable is the ranking?")
    metrics = store.read("evaluation_metrics")
    listed = views.evaluation_listed_vessels(store)
    fishing = listed[listed["vessel_class"] == "fishing"]
    if metrics.empty or fishing.empty:
        st.info("There are not enough known cases in this data to test the ranking.")
    else:
        auc = metrics[(metrics["subset"] == "fishing") & (metrics["scorer"] == "risk_score")
                      & (metrics["metric"] == "matched_auc")].iloc[0]
        st.markdown(
            "The ranking was tested against fishing vessels that appear on **official lists of "
            "vessels caught fishing illegally**, published by regional fisheries bodies. Each listed "
            "vessel was scored over its last year of tracker data and compared with ordinary "
            "fishing vessels from the same months. The scoring never sees who is listed."
        )
        left, right = st.columns([1, 2])
        left.metric("Listed fishing vessels tested", len(fishing))
        left.metric("Chance a listed vessel outranks an ordinary one", f"{auc['value']:.0%}",
                    help="50% would be no better than guessing.")
        table = fishing[["name", "flag", "window_start", "window_end", "risk_score"]].copy()
        table["place"] = [f"{r:.0f} of {int(n)}" for r, n in zip(fishing["rank_in_group"], fishing["group_size"])]
        right.dataframe(
            table,
            column_config={
                "name": "Listed vessel", "flag": "Flag",
                "window_start": st.column_config.DatetimeColumn("Scored from", format="YYYY-MM-DD"),
                "window_end": st.column_config.DatetimeColumn("to", format="YYYY-MM-DD"),
                "risk_score": st.column_config.NumberColumn("Score", format="%.0f"),
                "place": "Place in its group",
            },
            hide_index=True, width="stretch",
        )
        st.markdown(
            f"""
**What to take from this**

- The listed vessels came out at or near the top of their groups. That is encouraging.
- It is **thin evidence**: only {len(fishing)} listed fishing vessels have usable tracker history, and some belong to the same fleet.
- It mainly confirms one signal: **switching the tracker off**. The other signals could not be confirmed or ruled out with this few cases.
- It shows the score can tell listed vessels from ordinary ones. It does **not** show it can predict who will be caught next.
- A vessel that is not on a list is not proven innocent, and a high score is a reason to look, not an accusation.

The full figures and method are in the project's README.
"""
        )

# =====================================================================================
# Settings
# =====================================================================================
with tab_settings:
    overrides = config.read_overrides()

    def save(changes: dict, rebuild: bool, message: str) -> None:
        try:
            config.write_overrides(config.merge_settings(overrides, changes))
            if rebuild:
                with st.spinner("Rebuilding the ranking…"):
                    pipeline.build(store, settings_for(store))
        except Exception as error:  # show the reason instead of a stack trace
            st.session_state["flash"] = ("error", f"Could not save: {error}")
        else:
            st.session_state["flash"] = ("success", message)
        st.rerun()

    st.subheader("Data")
    st.markdown(
        f"- **Source:** Global Fishing Watch (non-commercial licence) and official IUU vessel lists\n"
        f"- **Covers:** {manifest.get('window_start')} to {manifest.get('window_end')}\n"
        f"- **Downloaded:** {manifest.get('accessed', 'n/a')}\n"
        "- The source publishes with a delay of a few days, so this is never a live picture."
    )
    refresh_status("settings")

    st.divider()
    st.subheader("Patrol boats")
    edited = st.data_editor(
        boats[["patrol_boat_id", "name", "home_port", "home_lat", "home_lon", "max_speed_knots", "range_nm", "status"]],
        column_config={
            "patrol_boat_id": "ID", "name": "Name", "home_port": "Home port",
            "home_lat": st.column_config.NumberColumn("Latitude", min_value=-90, max_value=90, format="%.4f"),
            "home_lon": st.column_config.NumberColumn("Longitude", min_value=-180, max_value=180, format="%.4f"),
            "max_speed_knots": st.column_config.NumberColumn("Speed (knots)", min_value=1),
            "range_nm": st.column_config.NumberColumn("Range (nm)", min_value=1),
            "status": st.column_config.SelectboxColumn("Status", options=["available", "maintenance"]),
        },
        num_rows="dynamic", hide_index=True, width="stretch", key="boat_editor",
    )
    if st.button("Save boats"):
        rows = edited.dropna(subset=["patrol_boat_id", "name", "home_lat", "home_lon"])
        if rows["patrol_boat_id"].duplicated().any() or rows.empty:
            st.error("Each boat needs its own ID, a name and a position.")
        else:
            rows = rows.astype(object).where(rows.notna(), None)
            cleaned = [
                {
                    "patrol_boat_id": str(r["patrol_boat_id"]), "name": str(r["name"]),
                    "home_port": str(r["home_port"] or ""),
                    "home_lat": float(r["home_lat"]), "home_lon": float(r["home_lon"]),
                    "max_speed_knots": float(r["max_speed_knots"] or 20.0),
                    "range_nm": float(r["range_nm"] or 500.0),
                    "status": str(r["status"] or "available"),
                }
                for r in rows.to_dict("records")
            ]
            save({"patrol_boats": cleaned}, True, "Boats saved.")

    st.divider()
    st.subheader("How much each behaviour counts")
    st.caption("Set the relative importance of each signal. They are rescaled to add up to 100%.")
    columns = st.columns(len(SIGNAL_LABELS))
    raw_weights = {
        signal: column.number_input(label, 0, 100, int(round(settings.scoring.weights[signal] * 100)),
                                    key=f"weight_{signal}")
        for column, (signal, label) in zip(columns, SIGNAL_LABELS.items())
    }
    if st.button("Save weights"):
        total = sum(raw_weights.values())
        if total <= 0:
            st.error("At least one behaviour must count for something.")
        else:
            save({"scoring": {"weights": {k: v / total for k, v in raw_weights.items()}}}, True,
                 "Weights saved and the ranking rebuilt.")

    st.divider()
    st.subheader("Patrol area")
    st.caption("Drag a rectangle on the map (use the square tool on the left), or type the edges below.")
    west, south, east, north = settings.patrol_area.bbox
    area_map = folium.Map(location=[(south + north) / 2, (west + east) / 2], zoom_start=5, tiles="OpenStreetMap")
    folium.Rectangle([[south, west], [north, east]], color="#555555", weight=1.5, fill=False,
                     tooltip="Current patrol area").add_to(area_map)
    area_map.fit_bounds([[south - 4, west - 4], [north + 4, east + 4]])
    Draw(draw_options={"polyline": False, "polygon": False, "circle": False, "marker": False,
                       "circlemarker": False, "rectangle": True},
         edit_options={"edit": False, "remove": False}).add_to(area_map)
    drawn = st_folium(area_map, returned_objects=["last_active_drawing"], height=380, width="stretch", key="area_map")
    drawing = (drawn or {}).get("last_active_drawing")
    if drawing:
        ring = drawing["geometry"]["coordinates"][0]
        west, east = min(p[0] for p in ring), max(p[0] for p in ring)
        south, north = min(p[1] for p in ring), max(p[1] for p in ring)
        st.info("Rectangle drawn. Check the edges below, then save.")
    area_name = st.text_input("Name", settings.patrol_area.name)
    e1, e2, e3, e4 = st.columns(4)
    new_box = [
        e1.number_input("West (longitude)", -180.0, 180.0, float(round(west, 2))),
        e2.number_input("South (latitude)", -90.0, 90.0, float(round(south, 2))),
        e3.number_input("East (longitude)", -180.0, 180.0, float(round(east, 2))),
        e4.number_input("North (latitude)", -90.0, 90.0, float(round(north, 2))),
    ]
    st.markdown("**Protected areas to watch**")
    st.caption("One per line, written as in the Global Fishing Watch list. Use the search to find the exact name.")
    area_text = st.text_area("Watched protected areas", "\n".join(settings.patrol_area.watched_protected_areas),
                             label_visibility="collapsed")
    query = st.text_input("Search protected-area names", placeholder="for example: Cocos")
    if query:
        needle = query.lower()
        found = [a["label"] for a in store.iter_raw("protected_area_list") if needle in str(a.get("label", "")).lower()]
        st.write(found[:25] if found else "No protected area with that in its name.")
    if st.button("Save patrol area"):
        watched = [line.strip() for line in area_text.splitlines() if line.strip()]
        save({"patrol_area": {"name": area_name, "bbox": new_box, "watched_protected_areas": watched}}, False,
             "Patrol area saved. Click 'Refresh data' to download data for the new area.")
