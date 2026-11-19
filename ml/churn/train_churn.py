"""Time-based customer churn baseline on dbt/Redshift monthly snapshots.

A 30-day no-purchase label is a *synthetic behavioral proxy*, not observed
real-world attrition. Default preview never writes artifacts or models.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score, balanced_accuracy_score, brier_score_loss,
    f1_score, roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

FEATURES = ("order_count_90d", "spend_90d_gbp", "lifetime_order_count", "recency_days")
SAFE_TABLE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
TRAINING_TABLE = "analytics_marts.mart_customer_churn_training"
SCORING_TABLE = "analytics_marts.mart_customer_churn_scoring"


def query_data(client, workgroup: str, database: str, table: str, *, timeout=1200) -> pd.DataFrame:
    if not SAFE_TABLE.fullmatch(table):
        raise ValueError("Untrusted query table")
    query = (
        f"SELECT customer_key, as_of_date, {', '.join(FEATURES)}, "
        + ("churn_next_30d" if table == TRAINING_TABLE else "cast(NULL as int) as churn_next_30d")
        + f" FROM {table}"
    )
    result = client.execute_statement(WorkgroupName=workgroup, Database=database, Sql=query)
    statement_id = result["Id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        details = client.describe_statement(Id=statement_id)
        if details["Status"] in ("FAILED", "ABORTED"):
            raise RuntimeError("Redshift churn query failed: " + str(details.get("Error"))[:500])
        if details["Status"] == "FINISHED":
            break
        time.sleep(5)
    else:
        raise TimeoutError("Redshift churn query outcome unknown")
    records = []
    token = None
    while True:
        args = {"Id": statement_id}
        if token:
            args["NextToken"] = token
        page = client.get_statement_result(**args)
        columns = [m["name"] for m in page["ColumnMetadata"]]
        for record in page.get("Records", []):
            values = []
            for field in record:
                if field.get("isNull"):
                    values.append(None)
                else:
                    values.append(next(iter(field.values())))
            records.append(dict(zip(columns, values)))
        token = page.get("NextToken")
        if not token:
            break
    return pd.DataFrame.from_records(records, columns=[
        "customer_key", "as_of_date", *FEATURES, "churn_next_30d"
    ])


def compare_models(train: pd.DataFrame, holdout: pd.DataFrame):
    if train.empty or holdout.empty:
        raise ValueError("Temporal train/test split is empty")
    x_train = train[list(FEATURES)].astype(float)
    x_test = holdout[list(FEATURES)].astype(float)
    y_train = train["churn_next_30d"].astype(int)
    y_test = holdout["churn_next_30d"].astype(int)
    if y_train.nunique() < 2 or y_test.nunique() < 2:
        raise ValueError("Churn labels lack both classes in training/test split")
    candidates = {
        "prevalence_baseline": Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("model", DummyClassifier(strategy="prior")),
        ]),
        "logistic": Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("model", LogisticRegression(max_iter=1000, class_weight="balanced")),
        ]),
        "hist_gradient_boost": Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("model", HistGradientBoostingClassifier(
                max_iter=100, max_leaf_nodes=15, learning_rate=0.05, random_state=42
            )),
        ]),
    }
    results = []
    for name, model in candidates.items():
        model.fit(x_train, y_train)
        p = model.predict_proba(x_test)[:, 1]
        pred = (p >= 0.5).astype(int)
        metrics = {
            "average_precision": float(average_precision_score(y_test, p)),
            "roc_auc": float(roc_auc_score(y_test, p)),
            "balanced_accuracy": float(balanced_accuracy_score(y_test, pred)),
            "f1": float(f1_score(y_test, pred)),
            "brier": float(brier_score_loss(y_test, p)),
        }
        results.append((name, model, metrics))
    # Optimize on held-out AP, not training fit; record baseline for comparison.
    winner = max(results, key=lambda row: (row[2]["average_precision"], -row[2]["brier"]))
    return results, winner


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", default="ecommerce-admin")
    p.add_argument("--region", default="eu-central-1")
    p.add_argument("--workgroup", default="ecommerce-analytics-demo")
    p.add_argument("--database", default="analytics")
    p.add_argument("--tracking-uri", default=os.environ.get("MLFLOW_TRACKING_URI", "file:./mlruns"))
    p.add_argument("--output-dir", type=Path, default=Path("ml/churn/output"))
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    redshift = boto3.Session(profile_name=a.profile, region_name=a.region).client("redshift-data")
    frame = query_data(redshift, a.workgroup, a.database, TRAINING_TABLE)
    if frame.empty:
        raise RuntimeError("dbt churn training mart has no labeled monthly examples")
    frame["as_of_date"] = pd.to_datetime(frame["as_of_date"], utc=True).dt.date
    frame["churn_next_30d"] = frame["churn_next_30d"].astype(int)
    for col in FEATURES:
        frame[col] = pd.to_numeric(frame[col], errors="raise")
    months = sorted(frame["as_of_date"].unique())
    if len(months) < 3:
        raise RuntimeError("Need at least three monthly cohorts for temporal validation")
    last_month = months[-1]
    train = frame.loc[frame["as_of_date"] < last_month]
    test = frame.loc[frame["as_of_date"] == last_month]
    results, (name, winner, best_metrics) = compare_models(train, test)
    report = {"mode": "APPLY" if a.apply else "PREVIEW", "train_months": len(months)-1,
              "holdout_month": str(last_month), "train_rows": len(train),
              "holdout_rows": len(test), "winner": name, "winner_metrics": best_metrics,
              "models": {n: m for n, _, m in results},
              "warning": "All labeled outcomes derive from synthetic behavior and must not be marketed as real customer churn."}
    print(json.dumps(report, indent=2))
    if not a.apply:
        return
    import mlflow
    import mlflow.sklearn
    mlflow.set_tracking_uri(a.tracking_uri)
    mlflow.set_experiment("ecommerce-churn-synthetic-2026")
    with mlflow.start_run(run_name=f"churn-holdout-{last_month}"):
        mlflow.log_params({"holdout_month": str(last_month),
                           "train_rows": len(train), "holdout_rows": len(test),
                           "selected_model": name, "label": "no_order_next_30d"})
        for model_name, _, metrics in results:
            for key, value in metrics.items():
                mlflow.log_metric(f"{model_name}_{key}", value)
        mlflow.sklearn.log_model(winner, artifact_path="model")
        a.output_dir.mkdir(parents=True, exist_ok=True)
        with (a.output_dir / "evaluation.json").open("w", encoding="utf-8") as out:
            json.dump(report, out, indent=2)
        live = query_data(redshift, a.workgroup, a.database, SCORING_TABLE)
        if not live.empty:
            score_x = live[list(FEATURES)].astype(float)
            live["churn_probability_next_30d"] = winner.predict_proba(score_x)[:, 1]
            live["model_name"] = name
            live[["customer_key", "as_of_date", "churn_probability_next_30d",
                  "model_name"]].to_csv(a.output_dir / "churn_scores.csv", index=False)


if __name__ == "__main__":
    main()
