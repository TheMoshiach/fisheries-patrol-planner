# Fisheries Patrol Planner

A fisheries officer has a few patrol boats and far more vessels than they can check.
This app ranks fishing vessels by illegal-fishing risk, shows why each vessel ranks
where it does, suggests which boat to send, and saves every decision the officer makes.

> **Status.** The pipeline, app and tests are complete and run on a labelled synthetic
> sample. The real-data run (Global Fishing Watch) is written but has not been executed
> yet because it needs an API token. Until it has run, this README has no real accuracy
> results, and `AIP_MIGRATION.md` is still to be written. See "What is left" below.

## Run it

Windows, one command (creates the environment and a dataset on first run):

```
run.bat
```

Tests:

```
test.bat
```

To use real data, copy `.env.example` to `.env`, paste a Global Fishing Watch API token
(https://globalfishingwatch.org/our-apis/tokens), then:

```
.venv\Scripts\python.exe -m patrol_planner fetch
```

Other commands: `python -m patrol_planner sample | build | report | status`.

## Approach

Data flows through four separate steps, each a pure function from tables to tables:

```
raw (as received)  ->  clean (typed tables, stable ids)  ->  features  ->  scores
```

The score is a transparent weighted sum of five signals, each squashed to 0-1:

| Signal | What it looks at | Weight |
|---|---|---|
| Going dark | AIS gaps of 12 h or more that start 50 nm or more from shore | 0.30 |
| Encounters | Meetings at sea, weighted up for carriers/bunkers and missing authorisation | 0.25 |
| Protected areas | Fishing in no-take areas; fishing inside or within 12 nm of watched areas | 0.25 |
| Unauthorised fishing | Fishing in RFMO waters with no known public authorisation | 0.15 |
| Flag changes | Changes of flag during the window | 0.05 |

Weights are set from domain reasoning in `config/settings.toml`, not fitted to the ground
truth. Because the score is additive, every score splits exactly into the reasons shown.

## How accuracy is measured

Ground truth is membership of an **official RFMO IUU vessel list** during the analysis
window, taken from the Global Fishing Watch IUU-list indicator (which draws on the
Combined IUU Vessel List). The label is kept in its own table and never reaches the
feature or scoring steps; a test relabels every vessel and checks scores do not move.

The evaluation cohort is patrol-area vessels, vessels found through official IUU lists,
and randomly drawn fishing vessels. Reported: ROC AUC and average precision with
bootstrap ranges, listed vessels found in the top k, each signal on its own, and two
baselines (random order; busiest vessels first).

Limits, stated plainly: few listed vessels broadcast AIS, so numbers will be uncertain;
an unlisted vessel is not proven innocent; and listing reflects past conduct, so this
measures separation of listed from unlisted vessels, not prediction of future listings.

## Results

Not available yet: the real-data run has not been executed. The sample dataset is
synthetic and its accuracy figures are meaningless by construction.

## Layout

- `src/patrol_planner/` scoring, accuracy, actions and data access; no UI code
  - `schemas.py` table contracts (ids, types, links)
  - `transforms/` `clean.py`, `features.py`, `scoring.py`
  - `evaluation.py`, `planning.py`, `actions.py` (decision log), `views.py`
  - `sources/` Global Fishing Watch client, ingestion, IUU list reader, sample generator
- `app/streamlit_app.py` the officer app
- `config/settings.toml` patrol area, weights, example patrol fleet
- `tests/` 73 tests

## Data and licences

- Global Fishing Watch API: CC BY-NC 4.0, non-commercial use only.
  [Powered by Global Fishing Watch](https://globalfishingwatch.org).
- Official IUU vessel lists (IOTC, CCSBT, CCAMLR) are read only to find IMO numbers to look up.
- Protected areas: World Database on Protected Areas, via Global Fishing Watch.
- The patrol fleet in `config/settings.toml` is an example, not real assets.
- All events are *apparent* events inferred from AIS: leads to check, not proof.

## What is left

1. Add the API token and run `fetch`; fix anything the live API shapes reveal.
2. Put the real accuracy results in this README.
3. Write `AIP_MIGRATION.md` (Foundry/AIP mapping guide).
4. Expand `DECISIONS.md` with choices made during the real-data run.
