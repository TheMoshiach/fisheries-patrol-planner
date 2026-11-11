"""Officer-facing app. Display only: every calculation and every write goes through
the ``patrol_planner`` package.

Run with:  streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import pandas as pd
import pydeck as pdk
import streamlit as st

from patrol_planner import actions, planning, reporting, views
from patrol_planner.config import load_settings
from patrol_planner.storage import open_store

TIER_COLOURS = {"High": [200, 40, 40], "Elevated": [230, 150, 30], "Low": [90, 140, 90]}

st.set_page_config(page_title="Fisheries Patrol Planner", layout="wide")

settings = load_settings()
store = open_store()
manifest = store.read_manifest()
is_sample = manifest.get("data_mode") != "real"
data_mode = "sample" if is_sample else "real"

if not store.exists("risk_scores"):
    st.error("No data has been built yet. Run `run.bat` (or `python -m patrol_planner sample`).")
    st.stop()


def graticule(west: float, south: float, east: float, north: float, step: int = 2):
    """Latitude/longitude grid lines and labels covering the patrol area."""
    lines, labels = [], []
    for lon in range(int(west // step * step), int(east) + 1, step):
        if west <= lon <= east:
            lines.append({"path": [[lon, south], [lon, north]]})
            labels.append({"position": [lon, south], "text": f"{abs(lon)}°{'W' if lon < 0 else 'E'}"})
    for lat in range(int(south // step * step), int(north) + 1, step):
        if south <= lat <= north:
            lines.append({"path": [[west, lat], [east, lat]]})
            labels.append({"position": [west, lat], "text": f"{abs(lat)}°{'S' if lat < 0 else 'N'}"})
    return lines, labels


def load_ranking() -> pd.DataFrame:
    return views.patrol_area_ranking(store)


def record(action, **parameters) -> None:
    """Submit one action; show the reason if it is rejected."""
    try:
        action(log, data_mode=data_mode, **parameters)
    except actions.ActionError as error:
        st.session_state["flash"] = ("error", str(error))
    else:
        st.session_state["flash"] = ("success", "Decision saved.")
    st.rerun()


log = views.decision_log(store)
ranking = load_ranking()
boats = store.read("patrol_boats")
decisions = log.read()
open_jobs = actions.open_assignments(decisions)
busy_boats = set(open_jobs["patrol_boat_id"])

# --- header ---------------------------------------------------------------------
st.title("Fisheries Patrol Planner")
if is_sample:
    st.warning(
        "**Synthetic sample data.** These vessels and events are generated, not real. "
        "Add a Global Fishing Watch token and run `python -m patrol_planner fetch` for real data."
    )
else:
    st.caption(
        f"{settings.patrol_area.name} · behaviour from {manifest.get('window_start')} to "
        f"{manifest.get('window_end')} · data accessed {manifest.get('accessed')} · "
        "[Powered by Global Fishing Watch](https://globalfishingwatch.org)"
    )

flash = st.session_state.pop("flash", None)
if flash:
    (st.error if flash[0] == "error" else st.success)(flash[1])

# --- sidebar ----------------------------------------------------------------------
with st.sidebar:
    st.header("Officer")
    officer = st.text_input("Your name", key="officer", placeholder="Required to save decisions")
    st.header("Filter the ranking")
    tiers = st.multiselect("Risk tier", ["High", "Elevated", "Low"], default=["High", "Elevated", "Low"])
    flags = st.multiselect("Flag", sorted(ranking["flag"].dropna().unique()))
    hide_dismissed = st.checkbox("Hide dismissed vessels", value=True)
    st.divider()
    free = len(boats[(boats["status"] == "available") & ~boats["patrol_boat_id"].isin(busy_boats)])
    st.metric("Patrol boats free", f"{free} of {len(boats)}")

shown = ranking[ranking["risk_tier"].isin(tiers)]
if flags:
    shown = shown[shown["flag"].isin(flags)]
if hide_dismissed:
    shown = shown[shown["status"] != "Dismissed"]
shown = shown.reset_index(drop=True)

tab_ranking, tab_plan, tab_log, tab_accuracy, tab_data = st.tabs(
    ["Ranking", "Patrol plan", "Decision log", "Accuracy", "About the data"]
)

# --- ranking ------------------------------------------------------------------------
with tab_ranking:
    counts = ranking["risk_tier"].value_counts()
    a, b, c, d = st.columns(4)
    a.metric("Vessels in patrol area", len(ranking))
    b.metric("High risk", int(counts.get("High", 0)))
    c.metric("Elevated risk", int(counts.get("Elevated", 0)))
    d.metric("Patrols assigned", len(open_jobs))

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Vessels ranked by risk")
        st.caption("Select a row to see why the vessel ranks where it does and to record a decision.")
        selection = st.dataframe(
            shown[
                ["rank", "name", "flag", "gear_type", "risk_score", "risk_tier", "data_confidence",
                 "status", "summary"]
            ],
            column_config={
                "rank": st.column_config.NumberColumn("Rank", width="small"),
                "name": "Vessel",
                "flag": st.column_config.TextColumn("Flag", width="small"),
                "gear_type": "Gear",
                "risk_score": st.column_config.ProgressColumn(
                    "Risk score", min_value=0, max_value=100, format="%.1f"
                ),
                "risk_tier": "Tier",
                "data_confidence": "Data confidence",
                "status": "Status",
                "summary": st.column_config.TextColumn("Main reasons", width="large"),
            },
            hide_index=True,
            width="stretch",
            height=430,
            on_select="rerun",
            selection_mode="single-row",
            key="ranking_table",
        )
    selected_rows = selection.selection.rows if selection else []
    selected = shown.iloc[selected_rows[0]] if selected_rows and len(shown) else None

    with right:
        st.subheader("Last known positions")
        located = shown.dropna(subset=["last_lat", "last_lon"]).copy()
        located["colour"] = located["risk_tier"].map(TIER_COLOURS)
        located["label"] = (
            located["name"] + " · score " + located["risk_score"].round(1).astype(str)
        )
        boat_points = boats.assign(
            label=boats["name"] + " · " + boats["home_port"],
            colour=[[30, 90, 200]] * len(boats),
        )
        west, south, east, north = settings.patrol_area.bbox
        grid_lines, grid_labels = graticule(west, south, east, north)
        layers = [
            pdk.Layer("PathLayer", data=grid_lines, get_path="path", get_color=[120, 120, 120, 90],
                      width_min_pixels=1),
            pdk.Layer("TextLayer", data=grid_labels, get_position="position", get_text="text",
                      get_size=11, get_color=[150, 150, 150], get_text_anchor="'start'",
                      get_alignment_baseline="'bottom'"),
            pdk.Layer(
                "PolygonLayer",
                data=[{"polygon": settings.patrol_area.geojson()["coordinates"][0]}],
                get_polygon="polygon",
                filled=False,
                stroked=True,
                get_line_color=[80, 80, 80],
                line_width_min_pixels=1,
            ),
            pdk.Layer(
                "ScatterplotLayer",
                data=located[["last_lat", "last_lon", "colour", "label"]],
                get_position="[last_lon, last_lat]",
                get_fill_color="colour",
                get_radius=9000,
                radius_min_pixels=3,
                pickable=True,
            ),
            pdk.Layer(
                "ScatterplotLayer",
                data=boat_points[["home_lat", "home_lon", "colour", "label"]],
                get_position="[home_lon, home_lat]",
                get_fill_color="colour",
                get_line_color=[255, 255, 255],
                stroked=True,
                get_radius=14000,
                radius_min_pixels=6,
                pickable=True,
            ),
        ]
        st.pydeck_chart(
            pdk.Deck(
                layers=layers,
                initial_view_state=pdk.ViewState(
                    latitude=(south + north) / 2, longitude=(west + east) / 2, zoom=4.2
                ),
                tooltip={"text": "{label}"},
                map_style="light",
            ),
            width="stretch",
            height=430,
        )
        st.caption(
            "Red, amber and green dots are vessels by tier; blue dots are patrol boat home ports. "
            "A position is where the vessel's most recent recorded event took place, not a live fix."
        )

    st.divider()
    if selected is None:
        st.info("Select a vessel in the table above.")
    else:
        vessel_id = selected["vessel_id"]
        st.subheader(f"{selected['name']} — rank {int(selected['rank'])} of {len(ranking)}")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Risk score", f"{selected['risk_score']:.1f} / 100")
        m2.metric("Tier", selected["risk_tier"])
        m3.metric("Data confidence", selected["data_confidence"])
        coverage = selected["ais_coverage_pct"]
        m4.metric("AIS coverage", "unknown" if pd.isna(coverage) else f"{coverage:.0f}%")
        m5.metric("Status", selected["status"])
        st.caption(
            f"Flag {selected['flag']} · gear {selected['gear_type']} · MMSI {selected['mmsi']} · "
            f"IMO {selected['imo'] if pd.notna(selected['imo']) else 'not reported'} · "
            f"{selected['patrol_area_fishing_hours']:.0f} apparent fishing hours in the patrol area"
        )

        why, act = st.columns([3, 2])
        with why:
            st.markdown("**Why this vessel ranks here**")
            reasons = views.vessel_reasons(store, vessel_id)
            for reason in reasons.itertuples(index=False):
                marker = "🔴" if reason.points >= 10 else ("🟠" if reason.points > 0 else "⚪")
                st.markdown(
                    f"{marker} **{reason.label}** — {reason.points:.1f} points  \n{reason.explanation}"
                )
            st.caption(
                "Points add up to the risk score. All events are *apparent* events inferred "
                "from AIS by Global Fishing Watch; they are leads to check, not proof."
            )
            events = views.vessel_events(store, vessel_id, settings)
            with st.expander(f"Events behind the score ({len(events)})"):
                st.dataframe(
                    events[["start_time", "event_type", "description", "counted", "lat", "lon"]],
                    column_config={
                        "start_time": st.column_config.DatetimeColumn("When (UTC)", format="YYYY-MM-DD HH:mm"),
                        "event_type": "Type",
                        "description": st.column_config.TextColumn("What happened", width="large"),
                        "counted": "Counts towards score",
                    },
                    hide_index=True,
                    width="stretch",
                )

        with act:
            st.markdown("**Record a decision**")
            free_boats = boats[
                (boats["status"] == "available") & ~boats["patrol_boat_id"].isin(busy_boats)
            ]
            with st.form("decision_form", clear_on_submit=True):
                choice = st.radio(
                    "Decision", ["Assign a patrol boat", "Add to watchlist", "Dismiss (no action)"]
                )
                boat_names = dict(zip(free_boats["patrol_boat_id"], free_boats["name"]))
                boat_id = st.selectbox(
                    "Patrol boat (for an assignment)",
                    list(boat_names),
                    format_func=lambda b: boat_names[b],
                    disabled=not boat_names,
                )
                rationale = st.text_area("Reason", placeholder="Why this decision?")
                submitted = st.form_submit_button("Save decision", type="primary")
            if submitted:
                common = dict(officer=officer, vessel_id=vessel_id, rationale=rationale,
                              ranked_vessels=ranking)
                if choice.startswith("Assign"):
                    if not boat_names:
                        st.error("No patrol boat is free. Release one in the Patrol plan tab.")
                    else:
                        record(actions.assign_patrol, patrol_boat_id=boat_id, patrol_boats=boats,
                               **common)
                elif choice.startswith("Add"):
                    record(actions.add_to_watchlist, **common)
                else:
                    record(actions.dismiss_vessel, **common)

# --- patrol plan --------------------------------------------------------------------
with tab_plan:
    st.subheader("Patrol boats")
    fleet = boats.merge(
        open_jobs[["patrol_boat_id", "vessel_id"]], on="patrol_boat_id", how="left"
    ).merge(ranking[["vessel_id", "name"]].rename(columns={"name": "target"}), on="vessel_id", how="left")
    fleet["state"] = fleet.apply(
        lambda r: f"On patrol: {r['target']}" if pd.notna(r["vessel_id"]) else r["status"].capitalize(),
        axis=1,
    )
    st.table(
        fleet[["name", "home_port", "max_speed_knots", "range_nm", "state"]].rename(columns={
            "name": "Boat", "home_port": "Home port", "max_speed_knots": "Speed (kn)",
            "range_nm": "Range (nm)", "state": "State"}).set_index("Boat")
    )
    if (boats["source"].str.contains("example")).any():
        st.caption("The fleet is an example defined in config/settings.toml. Replace it with your boats.")

    st.subheader("Suggested assignments")
    st.caption(
        "Works down the ranking and gives each vessel the nearest free boat that can reach its "
        "last known position and return. A suggestion does nothing until you accept it."
    )
    already_handled = set(ranking.loc[ranking["status"].isin(["Patrol assigned", "Dismissed"]), "vessel_id"])
    plan = planning.suggest_assignments(ranking, boats, busy_boats, already_handled)
    if plan.empty:
        st.info("No free boat can reach a vessel that still needs a decision.")
    for row in plan.itertuples(index=False):
        vessel = ranking[ranking["vessel_id"] == row.vessel_id].iloc[0]
        boat = boats[boats["patrol_boat_id"] == row.patrol_boat_id].iloc[0]
        text, button = st.columns([5, 1])
        text.markdown(
            f"**{boat['name']}** → **{vessel['name']}** (rank {row.rank}, score {row.risk_score:.1f}) · "
            f"{row.distance_nm:.0f} nm, about {row.transit_hours:.0f} h transit  \n{vessel['summary']}"
        )
        if button.button("Accept", key=f"accept_{row.patrol_boat_id}"):
            record(
                actions.assign_patrol, officer=officer, vessel_id=row.vessel_id,
                patrol_boat_id=row.patrol_boat_id, patrol_boats=boats, ranked_vessels=ranking,
                rationale=f"Accepted suggested plan: rank {row.rank}, {row.distance_nm:.0f} nm from {boat['home_port']}.",
            )

    st.subheader("Open assignments")
    if open_jobs.empty:
        st.info("No boat is currently assigned.")
    for job in open_jobs.itertuples(index=False):
        target = ranking[ranking["vessel_id"] == job.vessel_id]
        target_name = target["name"].iloc[0] if len(target) else job.vessel_id
        text, button = st.columns([5, 1])
        text.markdown(
            f"**{job.patrol_boat_id}** on **{target_name}** since "
            f"{pd.Timestamp(job.decided_at):%Y-%m-%d %H:%M} UTC, assigned by {job.officer}"
        )
        if button.button("Release", key=f"release_{job.patrol_boat_id}"):
            record(actions.release_boat, officer=officer, patrol_boat_id=job.patrol_boat_id,
                   rationale="Patrol completed.")

# --- decision log -------------------------------------------------------------------
with tab_log:
    st.subheader("Every decision, newest first")
    if decisions.empty:
        st.info("No decisions recorded yet.")
    else:
        names = ranking[["vessel_id", "name"]].rename(columns={"name": "vessel"})
        table = decisions.merge(names, on="vessel_id", how="left").sort_values("decided_at", ascending=False)
        st.dataframe(
            table[["decided_at", "officer", "decision_type", "vessel", "patrol_boat_id",
                   "risk_score_at_decision", "rank_at_decision", "rationale", "data_mode"]],
            column_config={
                "decided_at": st.column_config.DatetimeColumn("When (UTC)", format="YYYY-MM-DD HH:mm"),
                "decision_type": "Decision", "patrol_boat_id": "Boat",
                "risk_score_at_decision": "Score then", "rank_at_decision": "Rank then",
                "data_mode": "Data",
            },
            hide_index=True, width="stretch",
        )
        st.download_button(
            "Download as CSV", decisions.to_csv(index=False), "patrol_decisions.csv", "text/csv"
        )
    st.caption(f"Saved to {log.path}. Rows are only ever added, never changed.")

# --- accuracy -----------------------------------------------------------------------
with tab_accuracy:
    metrics = store.read("evaluation_metrics")
    st.subheader("How accurate is the ranking?")
    if is_sample:
        st.warning("These figures come from synthetic data and only show that the evaluation runs.")
    if metrics.empty:
        st.info("No evaluation results yet.")
    else:
        first = metrics.iloc[0]
        st.markdown(
            f"The score is checked against vessels that appear on **official IUU vessel lists** "
            f"published by regional fisheries bodies. The evaluation cohort has "
            f"**{int(first['n_vessels'])} vessels**, of which **{int(first['n_positive'])}** were "
            f"listed during the analysis window. The listing is never shown to the scoring step."
        )
        auc = metrics[(metrics["scorer"] == "risk_score") & (metrics["metric"] == "roc_auc")].iloc[0]
        st.metric(
            "Chance a listed vessel outranks an unlisted one (ROC AUC)",
            f"{auc['value']:.2f}",
            help="0.5 is no better than chance, 1.0 is perfect.",
        )
        st.markdown("**Against simple baselines and each signal on its own**")
        st.table(reporting.headline_table(metrics).set_index("Ranking"))
        st.markdown("**If you could only check the top of the list**")
        st.table(reporting.top_k_table(metrics).set_index("Top k"))
        listed = views.evaluation_listed_vessels(store)
        with st.expander(f"Where the {len(listed)} listed vessels landed"):
            st.dataframe(
                listed[["rank", "percentile", "name", "flag", "gear_type", "risk_score", "selection_reason"]],
                column_config={"percentile": "Top % of ranking", "selection_reason": "How it entered the cohort"},
                hide_index=True, width="stretch",
            )
        st.caption(
            "Limits: few listed vessels broadcast AIS, so the numbers are uncertain (see the ranges); "
            "an unlisted vessel is not proven innocent, so precision is a lower bound; and listing "
            "reflects past conduct, so this measures whether the score separates listed from "
            "unlisted vessels, not whether it predicts a future listing. See the README."
        )

# --- data ---------------------------------------------------------------------------
with tab_data:
    st.subheader("Where the data comes from")
    st.json(manifest)
    st.markdown(
        "- Vessel activity, identity and indicators: Global Fishing Watch API, licensed "
        "CC BY-NC 4.0 (non-commercial). [Powered by Global Fishing Watch](https://globalfishingwatch.org).\n"
        "- Ground truth: official IUU vessel lists of regional fisheries management organisations, "
        "as compiled in the Combined IUU Vessel List and exposed by Global Fishing Watch.\n"
        "- Protected-area boundaries: World Database on Protected Areas, via Global Fishing Watch.\n"
        "- Patrol boats: an example fleet from `config/settings.toml`."
    )
