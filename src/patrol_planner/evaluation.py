"""How well does the ranking find vessels that really were IUU-listed?

Ground truth is membership of an official RFMO IUU vessel list during the analysis
window. The label never enters the features, so no part of the score has seen it.

All ranking metrics treat tied scores as interchangeable (the expected value over
random tie-breaking), so a pile of vessels tied at zero cannot flatter the model.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import schemas
from .config import SIGNALS, EvaluationSettings, ScoringSettings, Window
from .transforms.scoring import signal_points


# --- metrics -------------------------------------------------------------------
def roc_auc(labels: np.ndarray, scores: np.ndarray) -> float:
    """Probability that a random listed vessel outranks a random unlisted one."""
    labels = np.asarray(labels, dtype=bool)
    n_pos, n_neg = int(labels.sum()), int((~labels).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = pd.Series(np.asarray(scores, dtype=float)).rank(method="average").to_numpy()
    return float((ranks[labels].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def average_precision(labels: np.ndarray, scores: np.ndarray) -> float:
    """Area under the precision-recall curve, stepping once per distinct score."""
    labels = np.asarray(labels, dtype=bool)
    n_pos = int(labels.sum())
    if n_pos == 0:
        return float("nan")
    frame = pd.DataFrame({"score": np.asarray(scores, dtype=float), "label": labels})
    groups = frame.groupby("score")["label"].agg(["sum", "size"]).sort_index(ascending=False)
    hits = groups["sum"].cumsum()
    seen = groups["size"].cumsum()
    precision = hits / seen
    recall_step = groups["sum"] / n_pos
    return float((precision * recall_step).sum())


def expected_hits_at_k(labels: np.ndarray, scores: np.ndarray, k: int) -> float:
    """Expected number of listed vessels in the top k when ties are broken at random."""
    labels = np.asarray(labels, dtype=bool)
    frame = pd.DataFrame({"score": np.asarray(scores, dtype=float), "label": labels})
    groups = frame.groupby("score")["label"].agg(["sum", "size"]).sort_index(ascending=False)
    remaining = float(min(k, len(frame)))
    hits = 0.0
    for positives, size in zip(groups["sum"], groups["size"]):
        if remaining <= 0:
            break
        take = min(remaining, size)
        hits += positives * take / size
        remaining -= take
    return hits


def precision_at_k(labels: np.ndarray, scores: np.ndarray, k: int) -> float:
    k = min(k, len(labels))
    return expected_hits_at_k(labels, scores, k) / k if k else float("nan")


def recall_at_k(labels: np.ndarray, scores: np.ndarray, k: int) -> float:
    n_pos = int(np.asarray(labels, dtype=bool).sum())
    return expected_hits_at_k(labels, scores, k) / n_pos if n_pos else float("nan")


def bootstrap_interval(
    metric,
    labels: np.ndarray,
    scores: np.ndarray,
    samples: int,
    seed: int,
) -> tuple[float, float]:
    """95% interval from resampling vessels, keeping the number of listed vessels fixed."""
    labels = np.asarray(labels, dtype=bool)
    scores = np.asarray(scores, dtype=float)
    positives, negatives = np.flatnonzero(labels), np.flatnonzero(~labels)
    if len(positives) < 2 or len(negatives) < 2 or samples <= 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    values = np.empty(samples)
    for i in range(samples):
        index = np.concatenate(
            [
                rng.choice(positives, size=len(positives), replace=True),
                rng.choice(negatives, size=len(negatives), replace=True),
            ]
        )
        values[i] = metric(labels[index], scores[index])
    low, high = np.nanpercentile(values, [2.5, 97.5])
    return float(low), float(high)


# --- tables --------------------------------------------------------------------
def build_evaluation_vessels(
    risk_scores: pd.DataFrame,
    iuu_listings: pd.DataFrame,
    cohort_members: pd.DataFrame,
    vessels: pd.DataFrame,
    window: Window,
) -> pd.DataFrame:
    """Label and score for every vessel in the evaluation cohort.

    A vessel counts as listed if any listing period overlaps the analysis window.
    Where several vessel identities share one IMO number (the same hull under
    different names or MMSIs) only the most recently transmitting one is kept, so
    a single ship is never counted twice.
    """
    cohort = cohort_members[cohort_members["cohort"] == "evaluation"]
    table = cohort[["vessel_id", "selection_reason"]].merge(
        risk_scores[["vessel_id", "risk_score"]], on="vessel_id", how="inner"
    )

    starts = iuu_listings["listed_from"].fillna(window.start)
    ends = iuu_listings["listed_to"].fillna(window.end)
    overlapping = iuu_listings[(starts < window.end) & (ends >= window.start)]
    table["iuu_listed"] = table["vessel_id"].isin(set(overlapping["vessel_id"]))

    identity = vessels[["vessel_id", "imo", "last_transmission"]]
    table = table.merge(identity, on="vessel_id", how="left")
    hull = table["imo"].fillna(table["vessel_id"])
    table = (
        table.assign(_hull=hull)
        .sort_values(["_hull", "last_transmission", "vessel_id"], na_position="first")
        .drop_duplicates("_hull", keep="last")
    )

    table = table.sort_values(["risk_score", "vessel_id"], ascending=[False, True])
    table["rank"] = np.arange(1, len(table) + 1)
    return schemas.conform(table, schemas.EVALUATION_VESSELS)


def evaluate(
    evaluation_vessels: pd.DataFrame,
    vessel_features: pd.DataFrame,
    scoring: ScoringSettings,
    settings: EvaluationSettings,
    window: Window,
) -> pd.DataFrame:
    """Metrics for the risk score, for each signal on its own, and for two baselines."""
    table = evaluation_vessels.merge(vessel_features, on="vessel_id", how="left")
    labels = table["iuu_listed"].fillna(False).to_numpy(dtype=bool)
    n, n_pos = len(table), int(labels.sum())
    run_id = f"{scoring.version_id}|{window.end_date}"

    scorers: dict[str, np.ndarray] = {
        "risk_score": table["risk_score"].to_numpy(dtype=float),
        # Would simply patrolling the busiest vessels do as well?
        "baseline:activity": table["fishing_events"].fillna(0).to_numpy(dtype=float),
    }
    points = signal_points(table, scoring)
    for signal in SIGNALS:
        scorers[f"signal:{signal}"] = points[signal].fillna(0).to_numpy(dtype=float)

    rows: list[dict] = []

    def add(scorer: str, metric: str, value: float, k=None, interval=(np.nan, np.nan)) -> None:
        suffix = f"@{k}" if k is not None else ""
        rows.append(
            {
                "metric_id": f"{run_id}|{scorer}|{metric}{suffix}",
                "evaluation_run_id": run_id,
                "model_version": scoring.version_id,
                "scorer": scorer,
                "metric": metric,
                "k": k,
                "value": value,
                "ci_low": interval[0],
                "ci_high": interval[1],
                "n_vessels": n,
                "n_positive": n_pos,
            }
        )

    prevalence = n_pos / n if n else float("nan")
    for scorer, scores in scorers.items():
        with_interval = scorer in ("risk_score", "baseline:activity")
        for metric_name, metric in (("roc_auc", roc_auc), ("average_precision", average_precision)):
            interval = (
                bootstrap_interval(
                    metric, labels, scores, settings.bootstrap_samples, settings.random_seed
                )
                if with_interval
                else (np.nan, np.nan)
            )
            add(scorer, metric_name, metric(labels, scores), interval=interval)
        if not with_interval:
            continue
        for k in settings.top_k:
            if k > n:
                continue
            precision = precision_at_k(labels, scores, k)
            add(scorer, "precision_at_k", precision, k)
            add(scorer, "recall_at_k", recall_at_k(labels, scores, k), k)
            add(scorer, "lift_at_k", precision / prevalence if prevalence else float("nan"), k)

    # A ranking with no information: AUC 0.5, precision equal to the base rate.
    add("baseline:random", "roc_auc", 0.5 if n_pos and n_pos < n else float("nan"))
    add("baseline:random", "average_precision", prevalence)
    for k in settings.top_k:
        if k <= n:
            add("baseline:random", "precision_at_k", prevalence, k)
            add("baseline:random", "recall_at_k", k / n, k)
            add("baseline:random", "lift_at_k", 1.0, k)

    return schemas.conform(pd.DataFrame(rows), schemas.EVALUATION_METRICS)
