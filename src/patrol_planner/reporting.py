"""Turns the evaluation tables into text and small summary tables for people to read."""

from __future__ import annotations

import pandas as pd

from .storage import DataStore

SCORER_NAMES = {
    "risk_score": "Risk score",
    "baseline:activity": "Baseline: busiest vessels first",
    "baseline:random": "Baseline: random order",
    "signal:going_dark": "Signal alone: going dark",
    "signal:encounters": "Signal alone: encounters",
    "signal:protected_areas": "Signal alone: protected areas",
    "signal:unauthorized_fishing": "Signal alone: unauthorised fishing",
    "signal:identity_changes": "Signal alone: flag changes",
}
# Fishing vessels come first: they are what the score is built to rank, and the
# only subset whose comparison vessels are drawn without reference to any signal.
SUBSET_NAMES = {
    "fishing": "Fishing vessels",
    "support": "Support vessels (carriers, tankers, cargo)",
    "all": "All vessels together",
}
SUPPORT_CAVEAT = (
    "Support vessels are not a fair test. The only way to draw comparison support "
    "vessels from the public data is from encounter events, so every comparison "
    "vessel has encounters by construction, which is one of the score's own signals."
)
HEADLINE_METRICS = (
    ("matched_auc", "Matched AUC"),
    ("roc_auc", "Pooled AUC"),
    ("average_precision", "Average precision"),
)


def cohort_summary(metrics: pd.DataFrame) -> pd.DataFrame:
    """How many vessels and how many listed vessels each subset contains."""
    first = metrics.drop_duplicates("subset").set_index("subset")
    rows = [
        {
            "Subset": SUBSET_NAMES[subset],
            "Vessels": int(first.loc[subset, "n_vessels"]),
            "IUU-listed": int(first.loc[subset, "n_positive"]),
        }
        for subset in SUBSET_NAMES
        if subset in first.index
    ]
    return pd.DataFrame(rows)


def headline_table(metrics: pd.DataFrame, subset: str = "fishing") -> pd.DataFrame:
    """Headline metrics for every scorer in one subset, one row each."""
    part = metrics[(metrics["subset"] == subset) & metrics["k"].isna()]
    rows = []
    for scorer, group in part.groupby("scorer", sort=False):
        by_metric = group.set_index("metric")
        row = {"Ranking": SCORER_NAMES.get(scorer, scorer)}
        for metric, title in HEADLINE_METRICS:
            if metric in by_metric.index:
                row[title] = _with_interval(by_metric.loc[metric])
        rows.append(row)
    return pd.DataFrame(rows)


def top_k_table(metrics: pd.DataFrame, subset: str = "fishing") -> pd.DataFrame:
    """Listed vessels found in the top k, for the risk score and both baselines."""
    at_k = metrics[(metrics["subset"] == subset) & metrics["k"].notna()]
    rows = []
    for k in sorted(at_k["k"].unique()):
        row = {"Top k": int(k)}
        for scorer, name in (
            ("risk_score", "risk score"),
            ("baseline:activity", "busiest first"),
            ("baseline:random", "random"),
        ):
            found = at_k[(at_k["k"] == k) & (at_k["scorer"] == scorer)].set_index("metric")
            if found.empty:
                continue
            hits = found.loc["precision_at_k", "value"] * k
            recall = found.loc["recall_at_k", "value"]
            row[f"Listed found ({name})"] = f"{hits:.1f} ({recall:.0%} of all)"
        rows.append(row)
    return pd.DataFrame(rows)


def accuracy_report(store: DataStore) -> str:
    metrics = store.read("evaluation_metrics")
    manifest = store.read_manifest()
    if metrics.empty:
        return "No evaluation results yet. Run the pipeline first."
    lines = []
    if manifest.get("data_mode") == "sample":
        lines.append(f"*** {manifest.get('label')} ***")
        lines.append("These numbers only show that the evaluation runs; they say nothing about reality.")
        lines.append("")
    lines.append(f"Model version: {metrics['model_version'].iloc[0]}")
    lines.append("")
    lines.append(cohort_summary(metrics).to_string(index=False))
    for subset, title in SUBSET_NAMES.items():
        table = headline_table(metrics, subset)
        if table.empty:
            continue
        lines += ["", f"== {title} ==", table.to_string(index=False)]
        if subset == "support":
            lines.append(SUPPORT_CAVEAT)
    lines += ["", "== Fishing vessels: listed vessels found near the top ==",
              top_k_table(metrics, "fishing").to_string(index=False)]
    return "\n".join(lines)


def _with_interval(row: pd.Series) -> str:
    if pd.isna(row["value"]):
        return "n/a"
    text = f"{row['value']:.3f}"
    if pd.notna(row["ci_low"]) and pd.notna(row["ci_high"]):
        text += f" ({row['ci_low']:.3f}-{row['ci_high']:.3f})"
    return text
