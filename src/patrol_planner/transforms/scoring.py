"""features -> scores.

The score is a transparent weighted sum. Each of five signals is reduced to an
*intensity* (a count-like number), squashed to a *strength* between 0 and 1 with
``intensity / (intensity + half_point)``, and multiplied by its weight. The score
is the sum of those points, on a 0-100 scale. Because it is additive, every score
decomposes exactly into the reasons shown to the officer.
"""

from __future__ import annotations

import pandas as pd

from .. import schemas
from ..config import SIGNALS, ScoringSettings

LABELS = {
    "going_dark": "Going dark (AIS gaps)",
    "encounters": "Meeting other vessels at sea",
    "protected_areas": "Fishing in or near protected areas",
    "unauthorized_fishing": "Fishing without a known authorisation",
    "identity_changes": "Changing flag",
}


def signal_intensities(features: pd.DataFrame) -> pd.DataFrame:
    """One column per signal: how much of that behaviour each vessel showed."""
    f = features
    return pd.DataFrame(
        {
            # Each qualifying gap counts once, plus one more for every two days dark.
            "going_dark": f["gap_events"] + f["gap_hours"] / 48.0,
            # Encounters with a carrier or bunker vessel, and encounters where the
            # pair has no matching public authorisation, each count double.
            "encounters": f["encounter_events"]
            + f["encounters_with_carrier"]
            + f["encounters_potential_risk"],
            # Fishing in a no-take area is the strongest evidence; hours inside a
            # watched area count fully, hours just outside it count a quarter.
            "protected_areas": 3.0 * f["no_take_fishing_events"]
            + f["gaps_in_protected_areas"]
            + f["watched_area_hours_inside"] / 12.0
            + f["watched_area_hours_near"] / 48.0,
            "unauthorized_fishing": f["unauthorized_fishing_events"],
            "identity_changes": f["flag_changes"],
        },
        index=features.index,
    ).astype("float64")


def signal_points(features: pd.DataFrame, scoring: ScoringSettings) -> pd.DataFrame:
    """Points (0-100 scale) contributed by each signal."""
    intensity = signal_intensities(features)
    points = pd.DataFrame(index=features.index)
    for signal in SIGNALS:
        strength = intensity[signal] / (intensity[signal] + scoring.half_points[signal])
        points[signal] = 100.0 * scoring.weights[signal] * strength
    return points


def score_vessels(features: pd.DataFrame, scoring: ScoringSettings) -> pd.DataFrame:
    """One risk score per vessel."""
    if features.empty:
        return schemas.RISK_SCORES.empty()
    points = signal_points(features, scoring)
    score = points.sum(axis=1).round(2)
    reasons = _reason_table(features, scoring)

    out = pd.DataFrame(
        {
            "vessel_id": features["vessel_id"],
            "as_of_date": features["window_end"],
            "window_start": features["window_start"],
            "model_version": scoring.version_id,
            "risk_score": score,
            "risk_tier": score.map(lambda s: _tier(s, scoring)),
            "data_confidence": [
                _confidence(c, n)
                for c, n in zip(features["ais_coverage_pct"], features["fishing_events"])
            ],
            "summary": _summaries(reasons, risk_score_ids(features, scoring)),
            "last_lat": features["last_lat"],
            "last_lon": features["last_lon"],
            "last_seen": features["last_seen"],
        }
    )
    out["risk_score_id"] = risk_score_ids(features, scoring)
    return schemas.conform(out, schemas.RISK_SCORES)


def explain_signals(features: pd.DataFrame, scoring: ScoringSettings) -> pd.DataFrame:
    """One row per vessel and signal: the points it added and why, in plain words."""
    if features.empty:
        return schemas.RISK_SIGNALS.empty()
    return schemas.conform(_reason_table(features, scoring), schemas.RISK_SIGNALS)


def risk_score_ids(features: pd.DataFrame, scoring: ScoringSettings) -> pd.Series:
    as_of = pd.to_datetime(features["window_end"], utc=True).dt.strftime("%Y-%m-%d")
    return features["vessel_id"] + "|" + as_of + "|" + scoring.version_id


def _reason_table(features: pd.DataFrame, scoring: ScoringSettings) -> pd.DataFrame:
    intensity = signal_intensities(features)
    points = signal_points(features, scoring)
    score_ids = risk_score_ids(features, scoring)
    frames = []
    for signal in SIGNALS:
        frames.append(
            pd.DataFrame(
                {
                    "risk_signal_id": score_ids + "|" + signal,
                    "risk_score_id": score_ids,
                    "vessel_id": features["vessel_id"],
                    "signal": signal,
                    "label": LABELS[signal],
                    "intensity": intensity[signal].round(3),
                    "strength": (
                        intensity[signal] / (intensity[signal] + scoring.half_points[signal])
                    ).round(4),
                    "weight": scoring.weights[signal],
                    "points": points[signal].round(2),
                    "explanation": [
                        _explain(signal, row) for row in features.itertuples(index=False)
                    ],
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def _tier(score: float, scoring: ScoringSettings) -> str:
    if score >= scoring.tier_high:
        return "High"
    if score >= scoring.tier_elevated:
        return "Elevated"
    return "Low"


def _confidence(coverage_pct, fishing_events) -> str:
    """How much of the vessel's behaviour the data can actually see."""
    if pd.notna(coverage_pct):
        if coverage_pct >= 60:
            return "high"
        return "medium" if coverage_pct >= 30 else "low"
    return "medium" if (fishing_events or 0) >= 20 else "low"


def _summaries(reasons: pd.DataFrame, score_ids: pd.Series) -> list[str]:
    active = reasons[reasons["points"] > 0].sort_values("points", ascending=False)
    top = active.groupby("risk_score_id")["explanation"].apply(lambda s: " ".join(s.head(2)))
    return [top.get(i, "No risk signals in the analysis window.") for i in score_ids]


def _n(count, singular: str, plural: str | None = None) -> str:
    count = int(count)
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _explain(signal: str, row) -> str:
    if signal == "going_dark":
        if row.gap_events <= 0:
            return "No long AIS gaps far from shore."
        text = (
            f"Stopped transmitting AIS {_n(row.gap_events, 'time')} far from shore, "
            f"{row.gap_hours:.0f} hours dark in total (longest gap {row.longest_gap_hours:.0f} h)."
        )
        return text

    if signal == "encounters":
        if row.encounter_events <= 0:
            return "No encounters with other vessels at sea."
        details = []
        if row.encounters_with_carrier > 0:
            details.append(f"{int(row.encounters_with_carrier)} with a carrier or bunker vessel")
        if row.encounters_high_seas > 0:
            details.append(f"{int(row.encounters_high_seas)} on the high seas")
        if row.encounters_potential_risk > 0:
            details.append(
                f"{int(row.encounters_potential_risk)} without a matching public authorisation"
            )
        suffix = f" ({', '.join(details)})" if details else ""
        return f"Met another vessel at sea {_n(row.encounter_events, 'time')}{suffix}."

    if signal == "protected_areas":
        parts = []
        if row.no_take_fishing_events > 0:
            parts.append(
                f"{_n(row.no_take_fishing_events, 'apparent fishing event')} inside a no-take area"
            )
        if row.watched_area_hours_inside > 0:
            parts.append(
                f"{row.watched_area_hours_inside:.0f} apparent fishing hours inside a watched protected area"
            )
        if row.watched_area_hours_near > 0:
            parts.append(
                f"{row.watched_area_hours_near:.0f} apparent fishing hours just outside a watched protected area"
            )
        if row.gaps_in_protected_areas > 0:
            parts.append(f"{_n(row.gaps_in_protected_areas, 'AIS gap')} inside a protected area")
        if not parts:
            return "No apparent fishing in or near protected areas."
        return _sentence(parts)

    if signal == "unauthorized_fishing":
        if row.unauthorized_fishing_events <= 0:
            return "No apparent fishing without a known authorisation."
        return (
            f"{_n(row.unauthorized_fishing_events, 'apparent fishing event')} in a regional "
            "fisheries body's waters with no known public authorisation."
        )

    if signal == "identity_changes":
        if row.flag_changes <= 0:
            return "No change of flag."
        return f"Changed flag {_n(row.flag_changes, 'time')} during the window."

    raise KeyError(signal)


def _sentence(parts: list[str]) -> str:
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."
