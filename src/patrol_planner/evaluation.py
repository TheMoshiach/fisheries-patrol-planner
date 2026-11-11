"""How well does the ranking separate vessels that were IUU-listed from vessels that were not?

Ground truth is appearance on an official RFMO IUU vessel list. Listed vessels
mostly stop broadcasting AIS once listed, so very few are visible in any one
year. The evaluation therefore looks back: each listed vessel is scored over its
own last year on AIS and compared with vessels of the same broad class drawn at
random from the same months (a year-matched case-control design).

The label never enters the features, so no part of the score has seen it.

All ranking metrics treat tied scores as interchangeable (the expected value over
random tie-breaking), so a pile of vessels tied at zero cannot flatter the model.
Intervals come from resampling whole groups (a listed vessel with its comparison
vessels), which respects the matched design.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import schemas
from .config import SIGNALS, EvaluationSettings, ScoringSettings
from .transforms.scoring import signal_points

SUBSETS = ("all", "fishing", "support")


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


def matched_auc(labels: np.ndarray, scores: np.ndarray, groups: np.ndarray) -> float:
    """Chance that a listed vessel outranks a comparison vessel from its own group.

    Each listed vessel is compared only with the vessels drawn from the same
    months, so differences between years cannot help or hurt. Ties count half.
    """
    wins, pairs = _wins_by_group(labels, scores, groups)
    return float(wins.sum() / pairs.sum()) if pairs.sum() else float("nan")


def _wins_by_group(labels, scores, groups) -> tuple[np.ndarray, np.ndarray]:
    """Per group: how many listed-versus-comparison pairs the listed vessel wins, and how many pairs."""
    labels = np.asarray(labels, dtype=bool)
    scores = np.asarray(scores, dtype=float)
    groups = np.asarray(groups)
    wins, pairs = [], []
    for name in pd.unique(groups):
        member = groups == name
        cases = scores[member & labels]
        controls = scores[member & ~labels]
        diff = cases[:, None] - controls[None, :]
        wins.append(float((diff > 0).sum() + 0.5 * (diff == 0).sum()))
        pairs.append(diff.size)
    return np.asarray(wins), np.asarray(pairs, dtype=float)


def matched_auc_interval(
    labels: np.ndarray, scores: np.ndarray, groups: np.ndarray, samples: int, seed: int
) -> tuple[float, float]:
    """95% interval for the matched AUC from resampling whole groups with replacement."""
    wins, pairs = _wins_by_group(labels, scores, groups)
    usable = pairs > 0
    wins, pairs = wins[usable], pairs[usable]
    if len(pairs) < 3 or samples <= 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(pairs), size=(samples, len(pairs)))
    values = wins[picks].sum(axis=1) / pairs[picks].sum(axis=1)
    low, high = np.percentile(values, [2.5, 97.5])
    return float(low), float(high)


def group_bootstrap_interval(
    metric,
    labels: np.ndarray,
    scores: np.ndarray,
    groups: np.ndarray,
    samples: int,
    seed: int,
) -> tuple[float, float]:
    """95% interval from resampling whole match groups with replacement."""
    labels = np.asarray(labels, dtype=bool)
    scores = np.asarray(scores, dtype=float)
    groups = np.asarray(groups)
    members = [np.flatnonzero(groups == g) for g in pd.unique(groups)]
    if len(members) < 3 or samples <= 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    values = np.full(samples, np.nan)
    for i in range(samples):
        picks = rng.integers(0, len(members), size=len(members))
        index = np.concatenate([members[p] for p in picks])
        values[i] = metric(labels[index], scores[index])
    if np.isnan(values).all():
        return float("nan"), float("nan")
    low, high = np.nanpercentile(values, [2.5, 97.5])
    return float(low), float(high)


# --- tables --------------------------------------------------------------------
def build_evaluation_vessels(
    risk_scores: pd.DataFrame,
    iuu_listings: pd.DataFrame,
    cohort_members: pd.DataFrame,
) -> pd.DataFrame:
    """Label and score for every vessel in the evaluation cohort.

    A vessel counts as listed if the ground-truth table has any listing for it.
    ``listing_timing`` says whether the analysed window fell before, during or
    after the listing, where the source gives dates.
    """
    cohort = cohort_members[cohort_members["cohort"] == "evaluation"]
    scores = risk_scores[["vessel_id", "as_of_date", "risk_score"]].rename(
        columns={"as_of_date": "window_end"}
    )
    table = cohort.merge(scores, on=["vessel_id", "window_end"], how="inner")

    listed_ids = set(iuu_listings["vessel_id"].dropna())
    table["iuu_listed"] = table["vessel_id"].isin(listed_ids)
    first = iuu_listings.groupby("vessel_id")["listed_from"].min()
    last = iuu_listings.groupby("vessel_id")["listed_to"].max()
    table["listing_timing"] = [
        _timing(listed, start, end, first.get(vessel), last.get(vessel))
        for listed, vessel, start, end in zip(
            table["iuu_listed"], table["vessel_id"], table["window_start"], table["window_end"]
        )
    ]

    table = table.sort_values(["risk_score", "vessel_id"], ascending=[False, True])
    table["rank"] = np.arange(1, len(table) + 1)
    table["evaluation_vessel_id"] = (
        table["vessel_id"] + "|" + table["window_end"].dt.strftime("%Y-%m-%d")
    )
    return schemas.conform(table, schemas.EVALUATION_VESSELS)


def _timing(listed: bool, start, end, first_listed, last_listed) -> str | None:
    if not listed:
        return None
    if pd.isna(first_listed):
        return "listing dates unknown"
    if first_listed >= end:
        return "listed later"
    if pd.notna(last_listed) and last_listed < start:
        return "listed earlier"
    return "listed during window"


def evaluate(
    evaluation_vessels: pd.DataFrame,
    vessel_features: pd.DataFrame,
    scoring: ScoringSettings,
    settings: EvaluationSettings,
) -> pd.DataFrame:
    """Metrics for the risk score, each signal on its own, and two baselines.

    Computed for all vessels and separately for fishing vessels and support
    vessels (carriers, tankers, cargo), because the score is built for the former.
    """
    features = vessel_features.drop(columns=["window_start"])
    table = evaluation_vessels.merge(features, on=["vessel_id", "window_end"], how="left")
    run_id = f"{scoring.version_id}|year-matched"
    rows: list[dict] = []
    for subset in SUBSETS:
        part = table if subset == "all" else table[table["vessel_class"] == subset]
        rows.extend(_metrics_for(part.reset_index(drop=True), subset, run_id, scoring, settings))
    return schemas.conform(pd.DataFrame(rows), schemas.EVALUATION_METRICS)


def _metrics_for(table, subset, run_id, scoring, settings) -> list[dict]:
    labels = table["iuu_listed"].fillna(False).to_numpy(dtype=bool)
    groups = table["match_group"].fillna("none").to_numpy()
    n, n_pos = len(table), int(labels.sum())
    prevalence = n_pos / n if n else float("nan")

    scorers: dict[str, np.ndarray] = {
        "risk_score": table["risk_score"].to_numpy(dtype=float),
        # Would simply patrolling the busiest vessels do as well?
        "baseline:activity": table["fishing_events"].fillna(0).to_numpy(dtype=float),
    }
    points = signal_points(table, scoring) if n else pd.DataFrame(columns=list(SIGNALS))
    for signal in SIGNALS:
        scorers[f"signal:{signal}"] = points[signal].fillna(0).to_numpy(dtype=float)

    rows: list[dict] = []

    def add(scorer: str, metric: str, value: float, k=None, interval=(np.nan, np.nan)) -> None:
        suffix = f"@{k}" if k is not None else ""
        rows.append(
            {
                "metric_id": f"{run_id}|{subset}|{scorer}|{metric}{suffix}",
                "evaluation_run_id": run_id,
                "model_version": scoring.version_id,
                "subset": subset,
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

    boot = (settings.bootstrap_samples, settings.random_seed)
    for scorer, scores in scorers.items():
        headline = scorer in ("risk_score", "baseline:activity")
        add(scorer, "roc_auc", roc_auc(labels, scores),
            interval=group_bootstrap_interval(roc_auc, labels, scores, groups, *boot)
            if headline else (np.nan, np.nan))
        add(scorer, "matched_auc", matched_auc(labels, scores, groups),
            interval=matched_auc_interval(labels, scores, groups, *boot)
            if headline else (np.nan, np.nan))
        add(scorer, "average_precision", average_precision(labels, scores))
        if not headline:
            continue
        for k in settings.top_k:
            if k > n:
                continue
            precision = precision_at_k(labels, scores, k)
            add(scorer, "precision_at_k", precision, k)
            add(scorer, "recall_at_k", recall_at_k(labels, scores, k), k)
            add(scorer, "lift_at_k", precision / prevalence if prevalence else float("nan"), k)

    # A ranking with no information: AUC 0.5, precision equal to the share listed.
    informative = 0 < n_pos < n
    add("baseline:random", "roc_auc", 0.5 if informative else float("nan"))
    add("baseline:random", "matched_auc", 0.5 if informative else float("nan"))
    add("baseline:random", "average_precision", prevalence)
    for k in settings.top_k:
        if k <= n:
            add("baseline:random", "precision_at_k", prevalence, k)
            add("baseline:random", "recall_at_k", k / n, k)
            add("baseline:random", "lift_at_k", 1.0, k)
    return rows
