# Decisions

Choices made while building, each with its reason.

| # | Decision | Reason |
|---|---|---|
| 1 | Git identity set for this repo only: GitHub login with GitHub's noreply address. | No git identity was configured on the machine; the noreply address attributes commits to the account without exposing an email. |
| 2 | Python 3.11+, with pandas, numpy, pyarrow, requests, python-dotenv, pypdf, streamlit, pytest. | All widely used and maintained PyPI packages. No skills, plugins or MCP servers were added, so no scan was needed. |
| 3 | Vessel data from the Global Fishing Watch API v3. | It provides AIS gaps, encounters, fishing effort, protected-area tags and identity. Terms checked: CC BY-NC 4.0, non-commercial, attribution required, 50,000 requests/day. The client caches every response and paces requests. |
| 4 | Ground truth is the Global Fishing Watch IUU-list indicator (official RFMO lists via the Combined IUU Vessel List). | It is the only public, official, per-vessel outcome that can be joined to AIS identities. Applied uniformly to every vessel. |
| 5 | The Combined IUU Vessel List spreadsheet is not downloaded directly. | The download asks for contact details and a reason, and its terms forbid repackaging. |
| 6 | Official RFMO list documents (IOTC, CCSBT, CCAMLR) are read only for IMO numbers, validated by check digit. | They are search seeds for finding listed vessels; they never set a label. |
| 7 | A vessel found via an IUU list is kept only if the indicator confirms a listing. | An IMO match may be a delisted vessel or a coincidence: neither a reliable positive nor a fair negative. |
| 8 | Protected-area data comes only through Global Fishing Watch; only areas the project uses are stored. | The World Database on Protected Areas restricts redistribution. |
| 9 | "Near a protected area" = apparent fishing hours within 12 nm of a watched area, from buffered activity reports. | Uses a documented API capability instead of copying boundary geometry. |
| 10 | Only AIS gaps of 12 h or more starting 50 nm or more offshore count as going dark. | Global Fishing Watch's published reliability thresholds; shorter or nearshore gaps are usually reception artefacts. |
| 11 | Gaps and encounters are fetched in full; fishing events only when flagged, plus the latest one per patrol-area vessel. | Full fishing histories are millions of events; counts come from the per-vessel indicators instead. |
| 12 | Loitering and port visits are not used. | Loitering is ambiguous for fishing vessels; port visits are not one of the requested signals. |
| 13 | Additive score with weights set a priori, not fitted to labels. | Fully explainable, and accuracy is not inflated by tuning on a small label set. |
| 14 | Evaluation metrics treat tied scores as a random draw; ranges come from a stratified bootstrap. | Many vessels tie at zero; ties must not flatter the model, and small samples need honest ranges. |
| 15 | Identities sharing an IMO number are counted once in the evaluation. | One hull can appear under several AIS identities. |
| 16 | Comparison vessels are drawn from random days of worldwide fishing events. | Simple and reproducible; it favours active vessels, which is stated as a limit. |
| 17 | Default patrol area: Galapagos and surrounding high seas; configurable. | Well-documented pressure from distant-water fleets next to major protected areas. |
| 18 | Patrol boats are an example fleet in the settings file, labelled as such. | No public source of real patrol assets exists. |
| 19 | Tables are Parquet files per stage; raw data is JSON lines; decisions are an append-only CSV. | One dataset per table with stable ids maps directly onto a hosted data platform; an append-only log is a full audit trail. |
| 20 | Raw API responses and the decision log are not committed. | Raw data is a cache; decisions are operational records. |
| 21 | Sample data is generated on demand, every id and name marked as sample, and never committed. | Lets the pipeline and tests run without a token while being impossible to mistake for real data. |
| 22 | Streamlit for the app; the package has no UI imports (enforced by a test). | Python-only, one command to run, and the logic stays portable. |
| 23 | "Last known position" is the position of the vessel's most recent recorded event. | The API does not provide live positions; the app says so. |
| 24 | Assignment suggestions are greedy: highest risk first, nearest free boat within half its range. | Easy for an officer to follow and override; a suggestion does nothing until accepted. |
| 25 | Analysis window: 12 months ending 2026-10-01. | A full year covers seasonal fleets and stays within the report API's range. |
| 26 | Explanations are template text, with no language model. | Deterministic and free to run; a language-model version belongs in the hosted platform later. |
