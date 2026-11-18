"""Evidence-only analysis of published historical Silver business-rule exceptions.

All functions operate on already loaded DataFrames. No S3 write capability.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

import pandas as pd


def _price_is_zero(value: Any) -> bool:
    return pd.notna(value) and Decimal(str(value)) == 0


def analyze_exceptions(tables: dict[str, pd.DataFrame], *, bronze: pd.DataFrame | None = None,
                       sample_limit: int = 100) -> dict:
    """Return JSON-serializable evidence; never infer a confirmed cancellation match.

    Candidate matching uses the same customer and stock code, an earlier
    non-cancellation positive-quantity sale, and the same absolute quantity.
    Counts distinguish no candidate, one candidate and multiple candidates;
    matches are *not* allocated or represented as actual refunds.
    """
    if sample_limit < 0:
        raise ValueError("sample_limit cannot be negative")
    items = tables["order_items"].copy()
    orders = tables["orders"][["order_id", "customer_id"]].copy()
    needed = {"order_item_id", "order_id", "source_stock_code", "quantity", "unit_price",
              "item_class", "is_cancellation", "order_timestamp", "source_record_key"}
    if needed - set(items):
        raise ValueError(f"Missing order item fields: {sorted(needed - set(items))}")
    items = items.merge(orders, on="order_id", how="left", validate="many_to_one")
    if items["customer_id"].isna().any():
        raise ValueError("Cannot inspect matching with missing customer IDs")
    items["stock_key"] = items["source_stock_code"].astype("string").str.strip().str.upper()
    items["order_timestamp"] = pd.to_datetime(items["order_timestamp"], errors="coerce")
    items["is_cancellation"] = items["is_cancellation"].fillna(False).astype(bool)
    # Product descriptions in Silver are canonicalized to the first observed text.
    # Do not claim that these prove full Bronze description ambiguity.
    ambiguous = []
    if bronze is not None:
        required = {"StockCode", "Description"}
        if required - set(bronze):
            raise ValueError("Bronze lacks StockCode or Description")
        observed = bronze[["StockCode", "Description"]].dropna(subset=["StockCode"]).copy()
        observed["stock_key"] = observed["StockCode"].astype("string").str.strip().str.upper()
        observed["description_key"] = observed["Description"].astype("string").str.strip().str.upper()
        grouped = observed.groupby("stock_key", dropna=False)["description_key"].nunique(dropna=True)
        for code, count in grouped.items():
            if count > 1:
                descriptions = observed.loc[observed["stock_key"].eq(code), "description_key"]
                ambiguous.append({"stock_code": str(code), "distinct_descriptions": int(count),
                                  "source_lines": len(descriptions),
                                  "description_samples": descriptions.dropna().drop_duplicates().head(5).tolist()})
        ambiguous.sort(key=lambda x: (-x["distinct_descriptions"], -x["source_lines"], x["stock_code"]))
    review_codes = (items.loc[items["item_class"].eq("requires_review"),
                               ["stock_key", "item_class"]]
                    .groupby("stock_key", dropna=False).size().sort_values(ascending=False))
    code_review = [{"stock_code": str(code), "lines": int(n)}
                   for code, n in review_codes.items()]

    zero = items.loc[items["unit_price"].map(_price_is_zero)].copy()
    zero_classes = {str(k): int(v) for k, v in zero["item_class"].fillna("<missing>").value_counts().items()}
    zero_samples = [
        {"order_item_id": str(r.order_item_id), "source_record_key": str(r.source_record_key),
         "stock_code": None if pd.isna(r.stock_key) else str(r.stock_key),
         "quantity": int(r.quantity) if pd.notna(r.quantity) else None,
         "item_class": str(r.item_class), "is_cancellation": bool(r.is_cancellation)}
        for r in zero.head(sample_limit).itertuples(index=False)
    ]

    # This explicitly analyzes negative-quantity lines, including C-prefixed
    # invoices. Positive-quantity C invoices remain separately counted.
    cancelled = items.loc[items["quantity"].lt(0)].copy()
    positive = items.loc[items["quantity"].gt(0) & ~items["is_cancellation"]].copy()
    positive = positive.loc[positive["stock_key"].notna() & positive["order_timestamp"].notna()]
    positive_groups = {key: group.sort_values("order_timestamp")
                       for key, group in positive.groupby(["customer_id", "stock_key"], dropna=False)}
    patterns: Counter[str] = Counter()
    match_samples = []
    for row in cancelled.itertuples(index=False):
        if pd.isna(row.stock_key) or pd.isna(row.order_timestamp):
            candidates = positive.iloc[0:0]
        else:
            candidate_group = positive_groups.get((row.customer_id, row.stock_key))
            candidates = (positive.iloc[0:0] if candidate_group is None else
                          candidate_group.loc[(candidate_group["order_timestamp"] < row.order_timestamp)
                                              & candidate_group["quantity"].eq(abs(row.quantity))])
        count = len(candidates)
        label = "no_exact_candidate" if count == 0 else ("one_exact_candidate" if count == 1 else "multiple_exact_candidates")
        patterns[label] += 1
        if len(match_samples) < sample_limit:
            match_samples.append({
                "cancellation_item_id": str(row.order_item_id),
                "source_record_key": str(row.source_record_key),
                "stock_code": str(row.stock_key) if pd.notna(row.stock_key) else None,
                "quantity": int(row.quantity),
                "candidate_count": count,
                "pattern": label,
                "candidate_item_ids": candidates["order_item_id"].astype(str).head(5).tolist(),
            })
    return {
        "analysis_version": "historical-silver-exceptions-v1",
        "limitations": [
            "Different descriptions for one StockCode are evidence for review, not proof of different physical products.",
            "Same customer, StockCode, earlier timestamp and absolute quantity identifies candidates, not confirmed refunds.",
            "Candidate purchases are not consumed; several cancellations may reference the same sale.",
            "Only customer-linked records represented in published Silver are examined.",
        ],
        "ambiguous_product_codes": {"bronze_analyzed": bronze is not None,
                                    "codes_with_multiple_descriptions": len(ambiguous),
                                    "description_ambiguities": ambiguous[:sample_limit],
                                    "requires_review_code_count": len(code_review),
                                    "requires_review_codes": code_review[:sample_limit]},
        "zero_price": {"lines": len(zero), "by_item_class": zero_classes,
                       "samples": zero_samples},
        "cancellations": {"negative_quantity_lines": len(cancelled),
                          "c_prefixed_positive_quantity_lines": int((items["is_cancellation"] & items["quantity"].gt(0)).sum()),
                          "matching_patterns": dict(patterns), "samples": match_samples},
    }


def analyze_published_silver(s3: Any, bucket: str, prefix: str, *, sample_limit: int = 100) -> dict:
    """Read checksum-verified S3 objects via existing audit code; return findings only."""
    from ecommerce.pipelines.silver.audit import read_published_silver
    from ecommerce.pipelines.silver.historical import load_verified_bronze

    tables, silver, _ = read_published_silver(s3, bucket, prefix)
    bronze_prefix = silver["source_bronze_manifest_key"].removesuffix("/manifest.json")
    bronze, _ = load_verified_bronze(s3, bucket, bronze_prefix)
    return analyze_exceptions(tables, bronze=bronze, sample_limit=sample_limit)
