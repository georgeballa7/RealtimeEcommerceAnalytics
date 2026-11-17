"""Read-only decision evidence for historical Silver business-rule review v2.

This is not a Silver transform. FIFO allocations are simulations, not verified refunds.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from decimal import Decimal
from typing import Any

import pandas as pd

# Operational remarks are not trustworthy product names. This is intentionally
# conservative: candidates are *suggestions* and never automatically published.
OPERATIONAL_WORDS = (
    "ADJUST", "DAMAG", "BROKEN", "SMASH", "MISSING", "WRONGLY",
    "INCORRECT", "CHECK", "FOUND", "AMAZON", "DOTCOM", "MAILOUT",
    "STOCK", "CREDIT", "REVERSE", "TEST", "LOST", "SOLD AS",
)


def _as_decimal(value: Any) -> Decimal | None:
    if pd.isna(value):
        return None
    result = Decimal(str(value))
    return result if result.is_finite() else None


def _normalized_code(value: Any) -> str | None:
    if pd.isna(value):
        return None
    code = str(value).strip().upper()
    return code or None


def _description_evidence(bronze: pd.DataFrame, sample_limit: int) -> dict:
    required = {"StockCode", "Description", "Quantity", "Price", "Invoice"}
    if required - set(bronze):
        raise ValueError(f"Missing Bronze columns: {sorted(required - set(bronze))}")
    b = bronze[list(required)].copy()
    b["code"] = b["StockCode"].map(_normalized_code)
    b["description"] = b["Description"].astype("string").str.strip().str.upper()
    b = b.loc[b["code"].notna() & b["description"].notna() & b["description"].ne("")]
    b["operational_note"] = b["description"].map(
        lambda s: any(word in s for word in OPERATIONAL_WORDS) or s in {"?", "??", "???"}
    )
    b["paid_sale"] = (
        pd.to_numeric(b["Quantity"], errors="coerce").gt(0)
        & pd.to_numeric(b["Price"], errors="coerce").gt(0)
        & ~b["Invoice"].astype("string").str.strip().str.upper().str.startswith("C", na=False)
    )
    b["eligible_description"] = b["paid_sale"] & ~b["operational_note"]
    counts = b.groupby("code", sort=False)["description"].nunique()
    ambiguous_codes = counts[counts.gt(1)].index
    ranked = (b.loc[b["eligible_description"]]
              .groupby(["code", "description"], sort=False).size()
              .rename("count").reset_index())
    ranked = ranked.sort_values(["code", "count", "description"], ascending=[True, False, True])
    best = ranked.drop_duplicates("code").set_index("code")
    examples = []
    for code in ambiguous_codes:
        options = ranked.loc[ranked["code"].eq(code)].head(3)
        examples.append({
            "stock_code": code,
            "distinct_descriptions": int(counts[code]),
            "suggested_description": (str(best.loc[code, "description"]) if code in best.index else None),
            "eligible_paid_sale_lines": int(options["count"].sum()),
            "top_eligible_descriptions": [
                {"description": str(x.description), "lines": int(x.count)}
                for x in options.itertuples(index=False)
            ],
        })
    examples.sort(key=lambda x: (-x["distinct_descriptions"], x["stock_code"]))
    return {
        "codes_with_multiple_descriptions": len(ambiguous_codes),
        "ambiguous_codes_without_suggested_description": sum(
            x["suggested_description"] is None for x in examples
        ),
        "operational_note_lines_heuristic": int(b["operational_note"].sum()),
        "method": "Most frequent non-operational description on positive-quantity, positive-price, non-C invoice rows; suggestion only.",
        "examples": examples[:sample_limit],
    }


def _cancellation_allocation(items: pd.DataFrame, sample_limit: int) -> dict:
    """Greedy chronological FIFO simulation, grouped by customer+StockCode.

    A cancellation can consume multiple earlier sales; prior simulated refunds
    reduce capacity. Same-timestamp sales are never eligible for a cancellation.
    No S3 data or input DataFrame is modified.
    """
    pools: dict[tuple[str, str], deque[tuple[pd.Timestamp, int]]] = defaultdict(deque)
    patterns: Counter[str] = Counter()
    unresolved_units = 0
    cancelled_units = 0
    examples: list[dict] = []
    events = items.loc[(items["quantity"].gt(0) & ~items["is_cancellation"] & items["unit_price"].map(
        lambda x: (d := _as_decimal(x)) is not None and d > 0
    )) | items["quantity"].lt(0)].copy()
    events["is_return"] = events["quantity"].lt(0)
    # Returns precede sales at the same timestamp, preventing same-time matches.
    events = events.sort_values(
        ["order_timestamp", "is_return", "source_record_key"],
        ascending=[True, False, True], na_position="first", kind="mergesort"
    )
    for r in events.itertuples(index=False):
        key = (r.customer_id, r.stock_code)
        if not r.is_return:
            if r.stock_code is not None and pd.notna(r.order_timestamp):
                pools[key].append((r.order_timestamp, int(r.quantity)))
            continue
        required = abs(int(r.quantity))
        cancelled_units += required
        unallocated = required
        purchase_lines_consumed = 0
        pool = pools[key]
        # Sells at the same timestamp always follow returns above.
        if r.stock_code is not None and pd.notna(r.order_timestamp):
            while pool and unallocated > 0:
                purchased_at, available = pool[0]
                if not purchased_at < r.order_timestamp:
                    break
                taken = min(available, unallocated)
                unallocated -= taken
                available -= taken
                purchase_lines_consumed += 1
                if available == 0:
                    pool.popleft()
                else:
                    pool[0] = (purchased_at, available)
        allocated = required - unallocated
        unresolved_units += unallocated
        if allocated == 0:
            outcome = "no_prior_paid_capacity"
        elif unallocated:
            outcome = "partial_capacity"
        elif purchase_lines_consumed == 1:
            outcome = "covered_by_one_purchase"
        else:
            outcome = "covered_by_multiple_purchases"
        patterns[outcome] += 1
        if len(examples) < sample_limit:
            examples.append({
                "source_record_key": str(r.source_record_key),
                "stock_code": r.stock_code,
                "required_units": required,
                "simulated_allocated_units": allocated,
                "unallocated_units": unallocated,
                "purchase_lines_used": purchase_lines_consumed,
                "pattern": outcome,
            })
    return {
        "cancellation_lines": sum(patterns.values()),
        "cancelled_units": cancelled_units,
        "simulated_allocated_units": cancelled_units - unresolved_units,
        "unallocated_units": unresolved_units,
        "patterns": dict(patterns),
        "samples": examples,
        "method": "Chronological FIFO capacity simulation within the same customer and StockCode, using earlier paid positive sales only; not proof of actual refunds.",
    }


def review_business_rules(tables: dict[str, pd.DataFrame], bronze: pd.DataFrame,
                          *, sample_limit: int = 100) -> dict:
    """Produce structured decision evidence while keeping Silver v1 untouched."""
    if sample_limit < 0:
        raise ValueError("sample_limit must not be negative")
    order_items = tables["order_items"].copy()
    orders = tables["orders"][["order_id", "customer_id"]]
    items = order_items.merge(orders, on="order_id", how="left", validate="many_to_one")
    if items["customer_id"].isna().any():
        raise ValueError("Some order_items have no customer")
    items["stock_code"] = items["source_stock_code"].map(_normalized_code)
    items["is_cancellation"] = items["is_cancellation"].fillna(False).astype(bool)
    items["order_timestamp"] = pd.to_datetime(items["order_timestamp"], errors="coerce")
    zero = items.loc[items["unit_price"].map(lambda x: _as_decimal(x) == Decimal(0))]
    zero_counts = {str(k): int(v) for k, v in zero["item_class"].value_counts(dropna=False).items()}
    return {
        "review_version": "historical-silver-business-review-v2",
        "status": "decision_support_only",
        "bronze_source_rows": len(bronze),
        "silver_customer_linked_lines": len(items),
        "product_descriptions": _description_evidence(bronze, sample_limit),
        "zero_price": {
            "lines": len(zero),
            "by_item_class": zero_counts,
            "positive_quantity_lines": int(zero["quantity"].gt(0).sum()),
            "samples": [
                {"source_record_key": str(r.source_record_key),
                 "stock_code": r.stock_code, "quantity": int(r.quantity),
                 "item_class": str(r.item_class)}
                for r in zero.head(sample_limit).itertuples(index=False)
            ],
        },
        "cancellation_allocation_simulation": _cancellation_allocation(items, sample_limit),
        "approval_notes": [
            "Description suggestions are heuristic; no product records are modified.",
            "FIFO capacity does not establish the original invoice or financial refund.",
            "Only customer-linked Silver lines contribute to cancellation capacity.",
            "Anonymous revenue and currency provenance remain separate business decisions.",
        ],
    }


def review_published_silver(s3: Any, bucket: str, silver_prefix: str,
                            *, sample_limit: int = 100) -> dict:
    from ecommerce.pipelines.silver.audit import read_published_silver
    from ecommerce.pipelines.silver.historical import load_verified_bronze

    tables, silver_manifest, _ = read_published_silver(s3, bucket, silver_prefix)
    bronze_prefix = silver_manifest["source_bronze_manifest_key"].removesuffix("/manifest.json")
    bronze, _ = load_verified_bronze(s3, bucket, bronze_prefix)
    report = review_business_rules(tables, bronze, sample_limit=sample_limit)
    report["bronze_source_sha256"] = silver_manifest["source_bronze_sha256"]
    return report
