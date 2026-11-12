# Fisheries Patrol Planner

A fisheries officer has a few patrol boats and far more vessels than they can check.
This app ranks fishing vessels by illegal-fishing risk using Global Fishing Watch
data, shows why each vessel ranks where it does, suggests which boat to send, and
saves every decision the officer makes. It also measures how well its ranking
agrees with a real public outcome: official lists of vessels caught fishing illegally.

## Run it

Double-click the **Fisheries Patrol Planner** shortcut on the Desktop, or `run.bat`
in this folder. From a terminal the same thing is:

```
.\run.bat
```

The first run creates the Python environment (Python 3.11 or newer is required) and
installs dependencies; after that it starts straight away and opens
http://localhost:8501. Keep the small console window open while you use the app;
closing it stops the app. If the Desktop shortcut is missing, right-click
`tools\create_shortcut.ps1` and choose "Run with PowerShell".

The repository includes a snapshot of real data, so the app shows real vessels as
soon as it starts. Run the tests with:

```
.\test.bat
```

Everything else is done inside the app. Nothing below needs a terminal.

## Using the app

**Map (main screen).** Each dot is a fishing vessel active in the patrol area: red is
high risk, amber elevated, green low. The outlined shapes are the watched reserves,
the dashed box is the patrol area and the blue markers are patrol boats.

- **Find a vessel** three ways: type its name in the search box (it lists every
  vessel with rank, flag and score), tick it in the ranked list, or click its dot.
- **Selected vessel.** Its score, the reasons in plain words with the points each
  added, and the events behind them. On the map its recent fishing locations appear
  as a dashed trail, with markers where it switched its tracker off, met another
  ship, or had fishing flagged.
- **Plan a patrol.** Pick a boat, add vessels (or click "Suggest a route"), and the
  route is drawn with the distance, bearing and time of each leg, and a warning if
  it is beyond the boat's range. "Save patrol" records it. "Release" frees the boat.
- **Watchlist or dismiss** a vessel with a reason; dismissed vessels drop off the map.

**Decision log.** Every decision with who, when, and the score and rank at the time.
Rows are only ever added. Download as CSV.

**How reliable is this?** A plain-language summary of how the ranking was tested.

**Settings.**

- **Refresh data** downloads the newest data from Global Fishing Watch and rebuilds
  everything (10 to 20 minutes, in the background; the page reloads when done). The
  first refresh needs an API token: copy `.env.example` to `.env`, and paste a token
  from https://globalfishingwatch.org/our-apis/tokens after `GFW_API_TOKEN=`.
- **Patrol boats**, **how much each behaviour counts**, and the **patrol area** (drag
  a rectangle on a map) with its watched reserves. Boat and weight changes take
  effect at once; a new area takes effect at the next refresh.

For developers, the same operations exist as commands (`python -m patrol_planner`
followed by `fetch`, `build`, `report`, `sample` or `status`).

## The problem

Illegal, unreported and unregulated (IUU) fishing happens far from shore, and a patrol
boat can only be in one place. The officer needs a short list: which of the vessels in
my waters deserve a visit, and why? "Why" matters as much as the ranking, because the
officer has to justify sending a boat and has to be able to disagree with the machine.

## The approach

Data flows through four separate steps. Each step is a pure function from tables to
tables, so it can be tested alone and moved to another platform unchanged.

```
raw (as received)  ->  clean (typed tables, stable ids)  ->  features  ->  scores
```

**Patrol area.** By default the Galapagos and the surrounding high seas (a box from
97°W to 84°W and 6°S to 5°N), with the Galapagos and Hermandad marine reserves as the
watched protected areas. All of this is set in `config/settings.toml`.

**Signals.** The score is a weighted sum of five behaviours over the last 12 months:

| Signal | What it looks at | Weight |
|---|---|---|
| Going dark | AIS gaps of 12 hours to 30 days that start 50 nm or more from shore | 0.30 |
| Meeting other vessels | Encounters at sea; ones with a carrier or bunker vessel, or with no matching public authorisation, count double | 0.25 |
| Protected areas | Apparent fishing in no-take areas; hours fishing inside a watched area, and within 12 nm of it; AIS gaps inside a protected area | 0.25 |
| Unauthorised fishing | Apparent fishing in a regional fisheries body's waters with no known public authorisation | 0.15 |
| Flag changes | Changes of flag during the window | 0.05 |

Each signal is squashed to a 0-1 strength with `intensity / (intensity + half_point)`
so that a vessel cannot run up an unlimited score on one behaviour, then multiplied by
its weight. The result is a 0-100 score. Because it is a plain sum, every score splits
exactly into the points each signal contributed, and each contribution comes with a
sentence in plain words ("Stopped transmitting AIS 5 times far from shore, 565 hours
dark in total").

The weights were set from domain reasoning before looking at any outcome, and have not
been tuned since. That is deliberate: with so few known outcomes (see below), fitting
weights to them would produce a flattering number and a worse tool.

**Decisions.** The officer can send a patrol boat to one or more vessels, release it,
put a vessel on a watchlist, or dismiss it with a reason. Each decision is appended as
rows recording who decided, when, about which vessel and boat, and the score and rank
the officer saw at the time. Rows are never changed or deleted. A boat cannot be on two
patrols at once. The app can suggest a route (the highest-risk vessels a boat can reach
and still get home), which does nothing until saved.

## How accuracy is measured

**Ground truth.** The outcome is whether a vessel has appeared on an official IUU
vessel list published by a regional fisheries management organisation (CCAMLR, CCSBT,
ICCAT, IOTC, WCPFC and others). It is read from Global Fishing Watch's IUU-list
indicator, which draws on the Combined IUU Vessel List. This is the only public,
official, per-vessel outcome that can be matched to AIS identities.

**The label is kept away from the score.** It lives in its own table that the feature
and scoring steps never receive. A test relabels every vessel and checks that no score
changes.

**Why a look-back design.** Listed vessels mostly stop broadcasting AIS. In the current
12-month window not one of the patrol-area vessels is listed, and across everything
fetched for that window only one listed vessel was transmitting. Measuring accuracy on
the current window alone would be measuring nothing. So the evaluation looks back:

1. Take the IMO numbers on the official IOTC, CCSBT and CCAMLR lists (IOTC and CCSBT
   cross-list the other bodies' vessels) and look each one up.
2. Keep hulls with at least 500 AIS positions since 1 January 2020, the earliest date
   the per-vessel indicators cover, and that the indicator confirms as listed.
3. Score each one over **its own last 12 months on AIS**.
4. For each, draw 30 comparison vessels of the same broad class at random from
   worldwide events **in the same months**, and score them over the same window.
5. Ask how often the listed vessel outranks its comparison vessels.

This is a year-matched case-control study. The main number is the *matched AUC*: the
chance that a listed vessel outranks a comparison vessel from its own group (0.5 is
chance, 1.0 is perfect). Ties count as half, so the many vessels tied at zero cannot
flatter the score. Ranges come from resampling whole groups. Two baselines are shown
alongside: random order, and "busiest vessels first".

## Results

Real data, accessed 2026-11-12. Model version `rules-v1-516a782c`.

**The evaluation set is small.** 66 IMO numbers on the official lists led to 15 hulls
with usable AIS history, 14 of which the indicator confirmed. One comparison vessel
turned out to be listed as well. That gives 15 listed vessels among 434: 5 fishing
vessels and 10 support vessels (carriers, tankers, cargo).

### Fishing vessels: 5 listed among 155

This is the test that matters, because fishing vessels are what the score is built to
rank.

| Ranking | Matched AUC (95% range) |
|---|---|
| **Risk score** | **0.97** (0.95-0.99) |
| Busiest vessels first | 0.42 (0.20-0.64) |
| Random order | 0.50 |
| Going dark alone | 0.95 |
| Encounters alone | 0.70 |
| Unauthorised fishing alone | 0.58 |
| Protected areas alone | 0.49 |
| Flag changes alone | 0.48 |

Within their own groups of 31, the five listed fishing vessels ranked 1st, 2nd, 2nd,
2nd and 3rd. Pooled across groups, 3 of the 5 are in the top 10 of 155 and 4 are in the
top 25; random order would put 0.3 and 0.8 there.

These figures depend on which comparison vessels are drawn. Across three separate
draws made while building this, the matched AUC came out between 0.95 and 0.97 and
the worst-placed listed vessel between 3rd and 7th of 31. The draw is now fixed per
group, so the numbers stay the same from one refresh to the next.

What this does and does not show:

- **Five vessels is very little evidence.** Three of the five are sister ships from one
  fleet (ISRAR 1, 2 and 3), so there are closer to three independent cases. The 95%
  range is computed from those same five and is too narrow to trust.
- **It validates one signal, not five.** Going dark on its own does almost as well as
  the full score. Encounters help a little. The protected-area, unauthorised-fishing and
  flag-change signals show no measurable link to listing in this data. That does not
  mean they are useless (a listing is rarely *for* fishing near Galapagos), only that
  this ground truth cannot confirm them.
- **It is not a prediction test.** Eight of the 15 listed vessels were already listed
  while being scored and the source gives no dates for the other seven. None was scored
  before its listing. The result says the score separates listed from unlisted vessels,
  not that it forecasts who will be listed.

### Support vessels: 10 listed among 279 (not a fair test)

The score ranks listed support vessels *below* their comparison vessels (matched AUC
0.17, range 0.00-0.36). This comparison is biased by construction and should not be
read as evidence either way. The public API offers no way to draw random carriers or
tankers except from encounter events, so every comparison vessel has encounters, which
is one of the score's own signals, while most listed support vessels were laid up with
little or no activity. It is reported because hiding it would be less honest than
explaining it. The tool is not meant to rank support vessels.

Pooling both classes gives 0.44 (0.21-0.67), which mixes a valid test with an invalid
one and is not a useful number.

### The current patrol list

For the 12 months to 7 November 2026, 127 fishing vessels had at least 24 apparent
fishing hours in the patrol area: 9 score High, 66 Elevated and 52 Low. 93 are
Ecuadorian-flagged. Going dark contributes the most points on average, followed by
fishing inside or just outside the two reserves.

Two cautions for anyone using that list. Many tuna vessels switch AIS off for
commercial reasons, so a high going-dark score is a reason to look, not an accusation.
And some vessels with many fishing hours inside the Galapagos reserve may be local
vessels licensed to fish there; the public data cannot tell them apart, which is what
the "Dismiss" decision is for.

Run `python -m patrol_planner report` for the full tables.

## Limits

- All events are *apparent* events that Global Fishing Watch infers from AIS. They are
  leads to check, not proof.
- A vessel with no AIS is invisible. Small boats and vessels that never transmit are not
  ranked at all.
- "Last known position" is where a vessel's most recent recorded event happened, not a
  live position. The source publishes with a delay of a few days.
- A vessel's trail on the map is where it apparently fished over the last 90 days, not
  its track; tracks are not available to free API tokens.
- Patrol routes are straight lines to last known positions. Weather, currents and land
  are not considered.
- Being unlisted is not being innocent. Lists are short, slow and incomplete.
- IUU-listed vessels without an IMO number on the lists (many on the NPFC list) are not
  found by the IMO lookup.
- The patrol fleet in `config/settings.toml` is an example, not real assets.

A stronger ground truth would be inspection outcomes: what the patrol actually found.
The decision log is built to be joined to those once they exist.

## Layout

```
src/patrol_planner/       scoring, accuracy, actions and data access; no UI code
  schemas.py              table contracts: columns, types, primary keys, links
  transforms/clean.py     raw -> clean
  transforms/features.py  clean -> features
  transforms/scoring.py   features -> scores and plain-words reasons
  evaluation.py           accuracy against the IUU-list ground truth
  planning.py             ranking, patrol routes and suggested routes
  geo.py                  distances, bearings, outline thinning
  actions.py              officer decisions and the append-only log
  jobs.py                 background data refresh
  views.py                read-only table joins for any front end
  sources/                Global Fishing Watch client, ingestion, IUU list reader, sample generator
app/streamlit_app.py      the officer app (display only)
config/settings.toml      default patrol area, weights, thresholds, example patrol fleet
config/overrides.json     settings changed in the app (created on first save)
tools/create_shortcut.ps1 makes the Desktop shortcut
data/real/                snapshot of real cleaned tables, features, scores and evaluation
tests/                    95 tests
DECISIONS.md              every choice made while building, with its reason
AIP_MIGRATION.md          step-by-step guide for moving to Palantir Foundry / AIP
```

## Data and licences

- **Global Fishing Watch API** (vessel activity, identity, indicators): CC BY-NC 4.0,
  non-commercial use only. [Powered by Global Fishing Watch](https://globalfishingwatch.org).
  Global Fishing Watch. 2026, updated daily. Fishing effort v5.0, fishing, encounter and
  AIS-gap events, vessel identity, 2020-01-01 to 2026-11-07. Data set accessed 2026-11-12
  at https://globalfishingwatch.org/our-apis/. The snapshot in `data/real/` is derived
  from this data and carries the same licence; keep the attribution if you share it.
- **Official IUU vessel lists** of IOTC, CCSBT and CCAMLR: public documents, read only
  to find IMO numbers to look up. The Combined IUU Vessel List is not downloaded
  directly; it reaches this project only through the Global Fishing Watch indicator.
- **Protected areas**: World Database on Protected Areas, via Global Fishing Watch. Only
  the areas this project refers to are stored. Their outlines are downloaded on refresh
  and kept on this machine only, so a fresh copy of the repository shows them after its
  first refresh.
- **Map background**: © OpenStreetMap contributors.
- **Synthetic sample**: generated by `sources/sample.py` for the tests. Every id starts
  with `sample-` and every name contains `SAMPLE`. It is never committed, the app does
  not use it, and its accuracy figures mean nothing.
