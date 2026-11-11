# Moving to Palantir Foundry / AIP

This guide maps each piece of the local project onto Foundry and AIP. The project
was laid out so that the move is mostly mapping: tables become datasets and object
types, pure functions become transforms, action functions become action types, and
the app becomes a Workshop module (or keeps running against the Ontology SDK).

Product names and APIs change; each step links the Palantir documentation page it
relies on. Check those pages when you do the step, and expect small differences.

## 1. What goes where

| Local piece | Where it lives now | Foundry / AIP counterpart |
|---|---|---|
| Raw API responses | `data/real/raw/*.jsonl` | Raw datasets filled by Data Connection or an external transform |
| Table contracts | `src/patrol_planner/schemas.py` | Dataset schemas, plus object type and link type definitions |
| raw → clean | `transforms/clean.py` | One Python transform per output dataset |
| clean → features | `transforms/features.py` | One Python transform |
| features → scores | `transforms/scoring.py` | Two Python transforms (`risk_scores`, `risk_signals`) |
| Accuracy | `evaluation.py` | Two Python transforms, scheduled after scores |
| Pipeline wiring | `pipeline.py` | The build graph itself (inputs and outputs of each transform) |
| Settings | `config/settings.toml` | A constants module in the repository, or a small config dataset |
| Officer decisions | `actions.py`, CSV log | Action types that create `PatrolDecision` objects |
| Read-only views | `views.py` | Object sets, links and Workshop variables |
| Assignment suggestions | `planning.py` | A Python function published to the Ontology |
| App | `app/streamlit_app.py` | Workshop module, or the same app on the Ontology SDK |
| Plain-words reasons | templates in `scoring.py` | Kept as-is; optionally enriched by an AIP Logic function |
| Tests | `tests/` | Unit tests in the code repository, plus data health checks |

Not moved: `storage.py` (Foundry stores datasets), `cli.py`, `run.bat`, and the
synthetic sample generator (keep it locally for tests).

## 2. Before you start

1. **Licence.** Global Fishing Watch data is CC BY-NC 4.0: non-commercial use, with
   attribution. Confirm your Foundry use is non-commercial, keep the attribution
   ("Powered by Global Fishing Watch", linked to https://globalfishingwatch.org) in
   any app that shows the data, and do not share the datasets outside that scope.
2. **Developer Tier limits.** The free tier caps some resources (for example the
   number of action types and language-model request rates). This project needs
   about 9 object types, 10 link types and 4 action types, which is small, but
   check the current limits before building.
3. **Create a project** with folders `raw`, `clean`, `features`, `scores`,
   `evaluation`, mirroring the local stage folders, and one code repository.
   Docs: [Code Repositories](https://www.palantir.com/docs/foundry/code-repositories/overview/).

## 3. Step 1: bring the raw data in

The local raw landing zone is one JSON-lines file per source request type:
`protected_area_list`, `patrol_area_report`, `protected_area_reports`,
`vessel_identities`, `events`, `insights`, `cohort`, `iuu_seed_imos`.

Two ways to fill the equivalent raw datasets, simplest first:

- **Upload.** Run `python -m patrol_planner fetch` locally and upload the eight
  `.jsonl` files as raw datasets. Good for a first pass; nothing else to configure.
- **External transform.** Port `sources/gfw.py` and `sources/ingest.py` into a
  Python transform that calls the API from inside Foundry. This needs a REST
  source for `gateway.api.globalfishingwatch.org`, an egress policy allowing that
  host, and the API token stored as a secret on the source rather than in code.
  Schedule it (weekly is plenty; the API allows 50,000 requests a day and this
  pipeline uses a few thousand).
  Docs: [Data Connection](https://www.palantir.com/docs/foundry/data-connection/overview/),
  [External transforms](https://www.palantir.com/docs/foundry/data-connection/external-transforms/),
  [Schedules](https://www.palantir.com/docs/foundry/data-integration/schedules/).

Keep raw datasets exactly as received. `ingest.py` adds only `_`-prefixed fields
that record which request produced a row; all interpretation stays in `clean`.

## 4. Step 2: turn each function into a transform

Every function in `transforms/` takes tables and returns one table, with no file
access and no global state. Each becomes one transform whose inputs and output are
the datasets named below. The function bodies are pandas and can be copied as they
are. The data volumes are small (thousands of rows), so single-node pandas
transforms are appropriate; Spark is not needed.
Docs: [Python transforms](https://www.palantir.com/docs/foundry/transforms-python/overview/),
[Compute engine selection](https://www.palantir.com/docs/foundry/transforms-python/compute-engines/).

| Transform (function) | Inputs | Output dataset |
|---|---|---|
| `clean.clean_events` | raw `events` | `clean/events` |
| `clean.clean_event_protected_areas` | raw `events` | `clean/event_protected_areas` |
| `clean.clean_vessels` | raw `vessel_identities`, `patrol_area_report`, `protected_area_reports`, `events` | `clean/vessels` |
| `clean.clean_protected_area_activity` | raw `protected_area_reports` | `clean/protected_area_activity` |
| `clean.clean_protected_areas` | raw `protected_area_list`, `clean/event_protected_areas`, `clean/protected_area_activity` | `clean/protected_areas` |
| `clean.clean_vessel_insights` | raw `insights`, `vessel_identities` | `clean/vessel_insights` |
| `clean.clean_iuu_listings` | raw `insights` | `clean/iuu_listings` |
| `clean.clean_cohort_members` | raw `cohort`, `patrol_area_report`, `clean/iuu_listings` (membership check only) | `clean/cohort_members` |
| `clean.clean_patrol_boats` | settings (or a manually entered dataset) | `clean/patrol_boats` |
| `features.build_cohort_features` | `clean/vessels`, `events`, `protected_area_activity`, `vessel_insights`, `cohort_members` | `features/vessel_features` |
| `scoring.score_vessels` | `features/vessel_features` | `scores/risk_scores` |
| `scoring.explain_signals` | `features/vessel_features` | `scores/risk_signals` |
| `evaluation.build_evaluation_vessels` | `scores/risk_scores`, `clean/iuu_listings`, `cohort_members` | `evaluation/evaluation_vessels` |
| `evaluation.evaluate` | `evaluation/evaluation_vessels`, `features/vessel_features` | `evaluation/evaluation_metrics` |

A transform is a thin wrapper around the existing function. The shape is:

```python
from transforms.api import transform_pandas, Input, Output

from patrol_planner.config import SETTINGS          # see "Settings" below
from patrol_planner.transforms import scoring


@transform_pandas(
    Output("/Fisheries/scores/risk_scores"),
    vessel_features=Input("/Fisheries/features/vessel_features"),
)
def risk_scores(vessel_features):
    return scoring.score_vessels(vessel_features, SETTINGS.scoring)
```

Points to handle while porting:

- **Raw JSON.** The clean functions take a list of records. In Foundry, read the
  raw dataset's files (or a single JSON string column) and parse each line with
  `json.loads` before calling the function.
- **Settings.** `config.py` reads a TOML file. In the repository, either ship
  `settings.toml` as a package resource and load it the same way, or replace
  `load_settings()` with constants. Keep `ScoringSettings.version_id`: it stamps
  each score with the model version and a fingerprint of the weights.
- **The pipeline lines in `pipeline.run_clean`** that drop vessels outside every
  cohort are plain filters; move them into the corresponding transforms.
- **Types.** `schemas.conform` already forces every column to a declared type.
  Keep calling it as the last line of each transform so dataset schemas are stable.
  Join-id columns are strings; timestamps are UTC.
- **One row per vessel and window.** A vessel is scored over the patrol window
  and, if it is part of the evaluation, over that group's own earlier window.
  `vessel_insights`, `vessel_features` and `risk_scores` therefore have one row
  per vessel *and* window, and `build_cohort_features` simply applies the
  single-window function `build_vessel_features` to each window in turn.
- **Ground truth isolation.** `iuu_listings` must never be an input to the
  features or scores transforms. (`clean_cohort_members` reads it only to decide
  which evaluation groups to keep; it passes no listing information on.) Locally a test enforces this; in Foundry the
  build graph shows it, and you can enforce it by putting `iuu_listings` and the
  evaluation datasets in a separate folder with their own permissions.

**Checks.** Port `tests/` into the repository as unit tests, and turn the rules in
`schemas.validate` (non-null unique primary key, declared columns) and
`schemas.check_links` (no orphan foreign keys) into dataset checks so a bad build
fails instead of publishing.
Docs: [Unit tests](https://www.palantir.com/docs/foundry/transforms-python/unit-tests/),
[Health checks](https://www.palantir.com/docs/foundry/data-health/overview/).

## 5. Step 3: build the Ontology

`schemas.py` already declares, for every table, the primary key, a title column,
the object type name and the foreign keys. Create one object type per row below,
backed by the dataset of the same name, with the listed primary key.
Docs: [Ontology overview](https://www.palantir.com/docs/foundry/ontology/overview/),
[Object types](https://www.palantir.com/docs/foundry/object-link-types/object-types-overview/),
[Create an object type](https://www.palantir.com/docs/foundry/object-link-types/create-object-type/),
[Design best practices](https://www.palantir.com/docs/foundry/ontology/ontology-best-practices/).

| Object type | Backing dataset | Primary key | Title | Notes |
|---|---|---|---|---|
| `Vessel` | `vessels` | `vessel_id` | `name` | Join the patrol-window row of `vessel_insights` in as extra properties |
| `VesselEvent` | `events` | `event_id` | `event_type` | `lat`/`lon` as a geopoint for maps |
| `ProtectedArea` | `protected_areas` | `protected_area_id` | `name` | |
| `ProtectedAreaActivity` | `protected_area_activity` | `activity_id` | n/a | Hours inside and near a watched area |
| `RiskScore` | `risk_scores` | `risk_score_id` | `summary` | One per vessel, window and model version; the officer app shows only the patrol window |
| `RiskSignal` | `risk_signals` | `risk_signal_id` | `label` | The "why" behind a score |
| `PatrolBoat` | `patrol_boats` | `patrol_boat_id` | `name` | |
| `PatrolDecision` | written by actions | `decision_id` | `decision_type` | See step 4 |
| `IuuListing` | `iuu_listings` | `listing_id` | n/a | Restrict to analysts; keep out of the officer app |
| `EvaluationMetric` | `evaluation_metrics` | `metric_id` | `metric` | Optional; a dashboard on the dataset also works |

Link types, taken from the `links` declared on each schema.
Docs: [Link types](https://www.palantir.com/docs/foundry/object-link-types/link-types-overview/),
[Create a link type](https://www.palantir.com/docs/foundry/object-link-types/create-link-type/).

| Link | Kind | Key |
|---|---|---|
| Vessel → VesselEvent | one-to-many | `events.vessel_id` |
| Vessel → RiskScore | one-to-many | `risk_scores.vessel_id` |
| RiskScore → RiskSignal | one-to-many | `risk_signals.risk_score_id` |
| Vessel → ProtectedAreaActivity | one-to-many | `protected_area_activity.vessel_id` |
| ProtectedArea → ProtectedAreaActivity | one-to-many | `protected_area_activity.protected_area_id` |
| VesselEvent ↔ ProtectedArea | many-to-many | join dataset `event_protected_areas` |
| Vessel → PatrolDecision | one-to-many | `patrol_decisions.vessel_id` |
| PatrolBoat → PatrolDecision | one-to-many | `patrol_decisions.patrol_boat_id` |
| RiskScore → PatrolDecision | one-to-many | `patrol_decisions.risk_score_id` |
| Vessel → IuuListing | one-to-many | `iuu_listings.vessel_id` |

Three tables are deliberately not object types: `vessel_features` (an intermediate
dataset), `cohort_members` (expose it as an `in_patrol_area` boolean and
`patrol_area_fishing_hours` number on `Vessel`), and `evaluation_vessels`.

**Stable ids.** All primary keys are deterministic strings built from source ids
(`vessel_id|as_of_date|model_version` for scores, and so on), so a rebuild updates
objects in place instead of creating new ones, and decisions keep pointing at the
score the officer actually saw.

## 6. Step 4: decisions become action types

Each function in `actions.py` is one action: named parameters, validation, and one
appended row. Create one action type per function. The validation each function
does maps onto submission criteria; the row it appends becomes "create a
`PatrolDecision` object".
Docs: [Action types](https://www.palantir.com/docs/foundry/action-types/overview/),
[Rules](https://www.palantir.com/docs/foundry/action-types/rules/),
[Submission criteria](https://www.palantir.com/docs/foundry/action-types/submission-criteria/),
[Function-backed actions](https://www.palantir.com/docs/foundry/action-types/function-actions-overview/),
[Object edits](https://www.palantir.com/docs/foundry/object-edits/overview/).

| Action type | Parameters | Submission criteria (from the code) | Edit |
|---|---|---|---|
| Assign patrol | vessel, patrol boat, rationale | boat status is `available`; boat has no open assignment; vessel has no open assignment | create `PatrolDecision` (`ASSIGN_PATROL`) |
| Release boat | patrol boat, rationale | boat has an open assignment | create `PatrolDecision` (`RELEASE_BOAT`) |
| Add to watchlist | vessel, rationale | none beyond a valid vessel | create `PatrolDecision` (`WATCHLIST`) |
| Dismiss vessel | vessel, rationale | rationale is not empty | create `PatrolDecision` (`DISMISS`) |

Details:

- `officer` and `decided_at` should come from the logged-in user and the current
  time, not from parameters. This is stronger than the local app, where the
  officer types their name.
- Each decision stores `risk_score_id`, `risk_score_at_decision` and
  `rank_at_decision`: fill them from the vessel's current `RiskScore` so the log
  records what the officer saw even after scores are rebuilt.
- "Open assignment" is derived from the log (`actions.open_assignments`): the
  latest assign/release decision per boat. Two ways to express that for
  submission criteria: make the assign and release actions function-backed and
  port `open_assignments` into the function; or also have those actions set a
  `current_vessel_id` property on `PatrolBoat` and test that property. The second
  is simpler to configure; the first keeps the log as the only source of truth.
- Keep the log append-only: give officers no action that modifies or deletes a
  `PatrolDecision`.
- For analysis, materialise the edited `PatrolDecision` objects back to a dataset
  so decisions can be joined against later outcomes (did an inspection find a
  violation?). That is the route to a second, stronger ground truth than IUU lists.

## 7. Step 5: the officer app

Either route works; they are not exclusive.

- **Workshop.** Rebuild the five tabs as a module: an object table of `Vessel`
  filtered to `in_patrol_area` and sorted by the linked score; a map of last known
  positions and boat home ports; a detail panel listing linked `RiskSignal` and
  `VesselEvent` objects; buttons bound to the four action types; a table of
  `PatrolDecision`; and a metrics view on `evaluation_metrics`. `views.py` shows
  exactly which joins each panel needs.
  Docs: [Workshop](https://www.palantir.com/docs/foundry/workshop/overview/).
- **Keep the Streamlit app.** Replace the body of each function in `views.py` and
  the `DecisionLog` class with Ontology SDK calls (load objects, apply actions).
  The app file itself does not change, because it only talks to `views`,
  `actions`, `planning` and `reporting`.
  Docs: [Ontology SDK](https://www.palantir.com/docs/foundry/ontology-sdk/overview/).

`planning.suggest_assignments` is a pure function of ranked vessels and boats.
Publish it as a Python function so Workshop can show the suggested plan and offer
each suggestion as a pre-filled "Assign patrol" action.
Docs: [Functions](https://www.palantir.com/docs/foundry/functions/overview/),
[Python functions](https://www.palantir.com/docs/foundry/functions/python-getting-started/).

## 8. Step 6: accuracy as part of the pipeline

`evaluation.py` already runs as the last stage. In Foundry, schedule the two
evaluation transforms after the scores build, and:

- chart `evaluation_metrics` over time, keyed by `model_version` and `subset`, so
  a weight change shows up as a new series rather than silently replacing the
  old numbers;
- add a check that fails the build if the risk score's matched AUC for fishing
  vessels drops below the "busiest vessels first" baseline;
- keep the caveat that travels with the numbers: only a handful of listed fishing
  vessels exist, and the support-vessel comparison is biased (see the README);
- when weights change, bump `model_version` in settings; the fingerprint in
  `version_id` changes on its own.

If you later fit weights to labels instead of setting them by hand, that is the
point to move scoring into Foundry's model tooling and to evaluate on held-out
vessels only.
Docs: [Model integration](https://www.palantir.com/docs/foundry/model-integration/overview/).

## 9. Step 7: where AIP's language-model features add value

The score itself should stay a deterministic transform: it is auditable, cheap,
and its accuracy is measured. Language models are useful around it, not inside it.
Docs: [AIP overview](https://www.palantir.com/docs/foundry/aip/overview/),
[AIP Logic](https://www.palantir.com/docs/foundry/logic/overview/),
[AIP Evals](https://www.palantir.com/docs/foundry/aip-evals/overview/),
[AIP Chatbot Studio](https://www.palantir.com/docs/foundry/agent-studio/overview/),
[Automate](https://www.palantir.com/docs/foundry/automate/overview/).

| Use | How | Why it helps |
|---|---|---|
| **Explain a vessel's risk in plain words** | AIP Logic function: input a `Vessel`; tools read its linked `RiskScore`, `RiskSignal` and recent `VesselEvent` objects; output a short briefing | Turns five signal rows and a list of events into a paragraph an officer can read at a glance, in their language |
| **Patrol briefing** | AIP Logic function over an accepted assignment: vessel, boat, distance, events, protected areas involved | A one-page brief for the boat crew, generated when "Assign patrol" is submitted (Automate can trigger it) |
| **Ask questions of the data** | A chatbot with Ontology tools scoped to these object types | "Which high-risk vessels met a carrier near the reserve this month?" without building a filter |
| **Draft the decision reason** | Suggest a rationale in the action form from the top signals; the officer edits and submits | Faster, more consistent log entries; the human still decides |
| **Read IUU list documents** | Extract vessel name, IMO, flag and listing date from the RFMO PDFs into a dataset | Replaces the IMO-only reader in `sources/iuu_lists.py` and recovers vessels listed without an IMO number |
| **Link field reports to vessels** | Extract vessel identifiers from free-text inspection or sighting reports and propose links | Builds the stronger ground truth (inspection outcomes) mentioned in step 4 |

Rules to hold these to:

- **Ground every statement.** The explanation function should receive the
  structured rows (`RiskSignal.points`, `explanation`, event times and places) and
  be told to use only those. The template sentences already in `risk_signals` are
  a faithful baseline to compare against.
- **Test it.** Build an AIP Evals suite from real vessels: every number in the
  generated text must appear in the vessel's `RiskSignal` or `VesselEvent` rows;
  the text must say "apparent" for model-inferred events; and it must not state or
  imply that a vessel broke the law.
- **No decisions.** A language-model function may draft and summarise. Creating a
  `PatrolDecision` stays a human action.
- **Keep the ground truth away from it.** Do not give the explanation function
  access to `IuuListing`; otherwise "why is this vessel risky" turns into "because
  it is listed", which the score never used.
- **Mind the rate limits** on the Developer Tier: generate explanations on demand
  for the vessel being viewed, not for the whole fleet on every build.

## 10. Suggested order

1. Upload raw files; port the clean, features and scores transforms; compare the
   output datasets row-for-row with the local Parquet files.
2. Port evaluation; confirm the metrics match the local report.
3. Create object types and links; check counts against the datasets.
4. Create the four action types; replay a few local decisions by hand.
5. Build the Workshop module (or point the Streamlit app at the Ontology SDK).
6. Add the explanation function and its evaluation suite.
7. Replace the upload with an external transform and schedule the pipeline.

## 11. Things that will need a decision in Foundry

- **Vessel identity.** `vessel_id` is Global Fishing Watch's id for one AIS
  identity; one hull can have several. The evaluation picks one identity per
  listed hull. In the Ontology, consider a `Hull` object type that groups identities.
- **Evaluation scores sit beside operational ones.** Scores for earlier windows
  exist only to measure accuracy. Either filter the officer-facing object set to
  the current `as_of_date`, or back `RiskScore` with a view that holds only the
  patrol window and keep the rest as a plain dataset.
- **Events are partial by design.** Gaps and encounters are complete; fishing
  events are only those flagged (no-take area, no known authorisation) plus the
  most recent one per patrol-area vessel. Counts of all fishing events come from
  `vessel_insights`.
- **Positions are not live.** `last_lat`/`last_lon` is the place of the most
  recent recorded event. A live AIS feed would be a separate source.
- **Patrol boats** are an example fleet; replace the dataset with real assets, or
  make boats editable through their own action type.
