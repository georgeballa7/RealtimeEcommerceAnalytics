"""Read-only, evidence-based review of historical product catalog eligibility.

'merchandise_candidate' is a classification, NOT business approval.
No automatically approved Shopify products are returned.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from ecommerce.application.use_cases.catalog_selection import select_catalog_candidates


def catalog_review(tables: dict[str, pd.DataFrame], bronze: pd.DataFrame) -> dict:
    """Return safe stock-code review evidence and no write authorization.

    Flags any observed description ambiguity, zero-price records, or absence
    of a strictly positive sale, including issues outside the chosen Silver
    description. All comparisons use source stock codes, not guessed GIDs.
    """
    selection = select_catalog_candidates(
        tables["products"], tables["product_variants"]
    )
    items = tables["order_items"]
    required = {"source_stock_code", "unit_price", "is_positive_sale"}
    if required - set(items.columns):
        raise ValueError("Silver order_items missing catalog review columns")
    if {"StockCode", "Description"} - set(bronze.columns):
        raise ValueError("Bronze missing product-description evidence")

    product_by_code = {
        c.source_stock_code: c for c in selection.candidates
    }
    source = bronze[["StockCode", "Description"]].copy()
    source = source.loc[source["StockCode"].notna()]
    source["stock_code"] = source["StockCode"].astype(str)
    source["normalized_description"] = (
        source["Description"].astype("string").str.strip().str.upper()
    )
    counts = source.groupby("stock_code")["normalized_description"].nunique(
        dropna=True
    )
    ambiguous = set(counts[counts > 1].index.astype(str))

    item_codes = items["source_stock_code"].astype("string")
    zero_price = set(
        item_codes.loc[
            items["unit_price"].notna()
            & items["unit_price"].map(lambda v: str(v) in {"0", "0.0", "0.00"})
        ].dropna().astype(str)
    )
    positive_sale = set(
        item_codes.loc[items["is_positive_sale"].fillna(False)]
        .dropna().astype(str)
    )

    review_rows = []
    reason_counts: Counter[str] = Counter()
    for code, candidate in sorted(product_by_code.items()):
        reasons = []
        if code in ambiguous:
            reasons.append("multiple_source_descriptions")
        if code in zero_price:
            reasons.append("observed_zero_price")
        if code not in positive_sale:
            reasons.append("no_positive_merchandise_sale")
        for reason in reasons:
            reason_counts[reason] += 1
        review_rows.append(
            {
                "stock_code": code,
                "product_id": candidate.product_id,
                "variant_id": candidate.variant_id,
                "silver_description": candidate.description,
                "distinct_bronze_descriptions": int(counts.get(code, 0)),
                "review_reasons": reasons,
                "needs_manual_review": bool(reasons),
            }
        )

    flagged = sum(row["needs_manual_review"] for row in review_rows)
    return {
        "source_products": selection.product_count,
        "source_variants": selection.variant_count,
        "candidate_products": len(review_rows),
        "excluded_by_class": selection.excluded_by_class,
        "manual_review_required": flagged,
        "without_detected_flags": len(review_rows) - flagged,
        "business_approved": 0,
        "review_reason_counts_nonexclusive": dict(sorted(reason_counts.items())),
        "review_queue": review_rows,
    }
