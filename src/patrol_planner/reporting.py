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


def headline_table(metrics: pd.DataFrame) -> pd.DataFrame:
    """AUC and average precision for every scorer, one row each."""
    wide = metrics[metrics["metric"].isin(["roc_auc", "average_precision"])]
    rows = []
    for scorer, group in wide.groupby("scorer", sort=False):
        by_metric = group.set_index("metric")
        row = {"Ranking": SCORER_NAMES.get(scorer, scorer)}
        for metric, title in (("roc_auc", "ROC AUC"), ("average_precision", "Average precision")):
            if metric in by_metric.index:
                row[title] = _with_interval(by_metric.loc[metric])
        rows.append(row)
    return pd.DataFrame(rows)


def top_k_table(metrics: pd.DataFrame) -> pd.DataFrame:
    """Listed vessels found in the top k, for the risk score and both baselines."""
    at_k = metrics[metrics["metric"].isin(["precision_at_k", "recall_at_k", "lift_at_k"])]
    rows = []
    for k in sorted(at_k["k"].dropna().unique()):
        row = {"Top k": int(k)}
        for scorer, name in (
            ("risk_score", "risk score"),
            ("baseline:activity", "busiest first"),
            ("baseline:random", "random"),
        ):
            subset = at_k[(at_k["k"] == k) & (at_k["scorer"] == scorer)].set_index("metric")
            if subset.empty:
                continue
            found = subset.loc["precision_at_k", "value"] * k
            recall = subset.loc["recall_at_k", "value"]
            row[f"Listed found ({name})"] = f"{found:.1f} ({recall:.0%} of all)"
        lift = at_k[(at_k["k"] == k) & (at_k["scorer"] == "risk_score") & (at_k["metric"] == "lift_at_k")]
        if not lift.empty:
            row["Lift over random"] = f"{lift['value'].iloc[0]:.1f}x"
        rows.append(row)
    return pd.DataFrame(rows)


def accuracy_report(store: DataStore) -> str:
    metrics = store.read("evaluation_metrics")
    manifest = store.read_manifest()
    if metrics.empty:
        return "No evaluation results yet. Run the pipeline first."
    first = metrics.iloc[0]
    lines = []
    if manifest.get("data_mode") == "sample":
        lines.append(f"*** {manifest.get('label')} ***")
        lines.append("These numbers only show that the evaluation runs; they say nothing about reality.")
        lines.append("")
    lines.append(
        f"Evaluation cohort: {int(first['n_vessels'])} vessels, "
        f"{int(first['n_positive'])} on an official IUU vessel list during the window."
    )
    lines.append(f"Model version: {first['model_version']}")
    lines.append("")
    lines.append(headline_table(metrics).to_string(index=False))
    lines.append("")
    lines.append(top_k_table(metrics).to_string(index=False))
    return "\n".join(lines)


def _with_interval(row: pd.Series) -> str:
    if pd.isna(row["value"]):
        return "n/a"
    text = f"{row['value']:.3f}"
    if pd.notna(row["ci_low"]) and pd.notna(row["ci_high"]):
        text += f" ({row['ci_low']:.3f}-{row['ci_high']:.3f})"
    return text
