# How the ranking was tested

Results below use real data accessed on 2026-11-12, with model version
`rules-v1-516a782c`. Run `python -m patrol_planner report` to print the same tables from
the current data.

## Ground truth

The outcome is whether a vessel has appeared on an official IUU vessel list published by
a regional fisheries management organisation (CCAMLR, CCSBT, ICCAT, IOTC, WCPFC and
others). It is read from Global Fishing Watch's IUU-list indicator, which draws on the
Combined IUU Vessel List. No other public, official, per-vessel outcome was found that can be matched to AIS
identities.

The label lives in its own table, `iuu_listings`, which the feature and scoring steps
never receive. A test relabels every vessel as unlisted, rebuilds, and checks that no
score changes.

## Why the test looks back in time

Listed vessels mostly stop broadcasting AIS. In the current 12-month window none of the
patrol-area vessels is listed, and across everything fetched for that window only one
listed vessel was transmitting at all. Measuring accuracy on the current window would
mean measuring one vessel.

So the evaluation goes back to when each listed vessel was still on AIS:

1. Take the IMO numbers on the official IOTC, CCSBT and CCAMLR lists and look each one
   up. IOTC and CCSBT also carry the vessels listed by the other bodies.
2. Keep hulls that have at least 500 AIS positions since 1 January 2020 and that the
   indicator confirms as listed. The indicators do not go back before 2020.
3. Score each one over its own last 12 months on AIS.
4. For each, draw 30 comparison vessels of the same broad class at random from
   worldwide events in the same months, and score them over the same window.
5. Ask how often the listed vessel outranks its comparison vessels.

This is a year-matched case-control study. The main number is the matched AUC, the
chance that a listed vessel outranks a comparison vessel from its own group. A value of
0.5 is chance and 1.0 is perfect. Ties count as half, so the many vessels tied at zero
cannot flatter the score. The 95% ranges come from resampling whole groups.

Two baselines sit alongside the score: random order, and ranking the busiest vessels
first.

## How many vessels there are

66 IMO numbers on the official lists led to 15 hulls with usable AIS history, and the
indicator confirmed 14 of them. One comparison vessel turned out to be listed as well.
That gives 15 listed vessels among 434: 5 fishing vessels and 10 support vessels
(carriers, tankers and cargo ships).

## Fishing vessels: 5 listed among 155

Fishing vessels are what the score is built to rank, so this is the result to look at.

| Ranking | Matched AUC (95% range) |
|---|---|
| Risk score | 0.97 (0.95 to 0.99) |
| Busiest vessels first | 0.42 (0.20 to 0.64) |
| Random order | 0.50 |
| Going dark alone | 0.95 |
| Encounters alone | 0.70 |
| Unauthorised fishing alone | 0.58 |
| Protected areas alone | 0.49 |
| Flag changes alone | 0.48 |

Within their own groups of 31, the five listed fishing vessels ranked 1st, 2nd, 2nd,
2nd and 3rd. Pooled across groups, 3 of the 5 are in the top 10 of 155 and 4 are in the
top 25. Random order would put 0.3 and 0.8 there.

The figures depend on which comparison vessels are drawn. Across three separate draws
made while building this, the matched AUC came out between 0.95 and 0.97, and the
worst-placed listed vessel landed between 3rd and 7th of 31. Each group now draws from
its own fixed random stream, so the numbers stay the same from one refresh to the next.

### What this shows and what it leaves open

Five vessels is very little evidence. Three of the five are sister ships from one fleet
(ISRAR 1, 2 and 3), so there are closer to three independent cases. The 95% range is
computed from those same five and is too narrow to trust.

The test mostly confirms one signal. Going dark on its own does almost as well as the
full score, and encounters help a little. The protected-area, unauthorised-fishing and
flag-change signals show no measurable link to listing in this data. A listing is rarely
for fishing near the Galapagos, so this ground truth cannot confirm those signals or
rule them out.

The test does not measure prediction. Three of the five listed fishing vessels were on a
list during the months they were scored, and the source gives no listing dates for the
other two. None is known to have been scored before it was listed. The result says the
score separates listed vessels from unlisted ones and says nothing about forecasting.

## Support vessels: 10 listed among 279

The score ranks listed support vessels below their comparison vessels, with a matched
AUC of 0.17 (0.00 to 0.36). This comparison is biased by the way it has to be built, so
it is not evidence for or against the score.

The public API offers no way to draw random carriers or tankers except from encounter
events. Every comparison vessel therefore has encounters, and encounters are one of the
score's own signals. Most listed support vessels, meanwhile, were laid up with little or
no activity. The result is reported here because leaving it out would hide a weakness of
the method. The app does not rank support vessels.

Pooling both classes gives 0.44 (0.21 to 0.67). That number mixes a valid test with an
invalid one and should not be quoted.

## The current patrol list

For the 12 months to 7 November 2026, 127 fishing vessels had at least 24 apparent
fishing hours in the patrol area. 9 score High, 66 Elevated and 52 Low, and 93 of them
fly the Ecuadorian flag. Going dark contributes the most points on average, followed by
fishing inside or just outside the two reserves.

## What would make this better

Inspection outcomes would be a stronger ground truth than IUU lists, because they record
what a patrol actually found. The decision log stores the vessel, the time and the score
for each patrol, so it can be joined to inspection results once those exist.

IUU-listed vessels that appear on the lists without an IMO number, which includes many
on the NPFC list, are not found by the IMO lookup. Matching them by name would add
cases but needs care, because vessel names are often reused.
