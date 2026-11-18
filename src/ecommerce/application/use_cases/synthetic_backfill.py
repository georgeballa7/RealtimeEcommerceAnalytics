"""Deterministic, source-grounded 2026 synthetic order construction.

Read-only and pure: no S3, Shopify, RDS, filesystem or clock access.
All random draws are seeded by simulation config + calendar day, independent
of invocation cutoff or the size of the requested backfill interval.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pandas as pd

from ecommerce.domain.identifiers import canonical_id

SYNTHETIC_SOURCE = "synthetic_2026"
START_UTC = datetime(2026, 1, 1, tzinfo=UTC)
PRICE_TIERS_EUR = ("4.99", "9.99", "19.99", "39.99")


@dataclass(frozen=True)
class SimulationPolicy:
    """Explicit hypothetical 2026 business assumptions, not observed facts."""

    seed: int = 20261118
    target_orders_per_day: int = 45
    retail_probability_percent: int = 90
    retail_line_cap: int = 40
    wholesale_line_cap: int = 70
    retail_units_per_line_cap: int = 12
    wholesale_units_per_line_cap: int = 100
    weekday_uniform_smoothing_percent: int = 25
    cancellation_probability_percent: int = 10
    cancellation_delay_max_days: int = 14
    currency: str = "EUR"
    version: str = "synthetic-2026-policy-v1"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.seed, int)
            or not isinstance(self.target_orders_per_day, int)
            or self.target_orders_per_day <= 0
            or not isinstance(self.retail_probability_percent, int)
            or not 0 <= self.retail_probability_percent <= 100
            or not isinstance(self.weekday_uniform_smoothing_percent, int)
            or not 0 <= self.weekday_uniform_smoothing_percent <= 100
            or min(
                self.retail_line_cap, self.wholesale_line_cap,
                self.retail_units_per_line_cap, self.wholesale_units_per_line_cap,
            ) <= 0
            or not isinstance(self.cancellation_probability_percent, int)
            or not 0 <= self.cancellation_probability_percent <= 100
            or not isinstance(self.cancellation_delay_max_days, int)
            or not 1 <= self.cancellation_delay_max_days <= 90
            or self.currency != "EUR"
            or self.version != "synthetic-2026-policy-v1"
        ):
            raise ValueError("Invalid explicit synthetic simulation policy")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def _seed(*components: object) -> int:
    encoded = json.dumps([str(component) for component in components], separators=(",", ":"))
    return int.from_bytes(hashlib.sha256(encoded.encode()).digest()[:16], "big")


def _poisson(rng: random.Random, rate: float) -> int:
    """Exact Knuth sampler; demo-config daily rates are modest."""
    if not 0 < rate < 250:
        raise ValueError("Daily order rate must be between 0 and 250")
    threshold = math.exp(-rate)
    product = 1.0
    count = 0
    while product > threshold:
        product *= rng.random()
        count += 1
    return count - 1


def _choose(rng: random.Random, values: list, weights: list[float]):
    if not values or len(values) != len(weights) or sum(weights) <= 0:
        raise ValueError("Empty or invalid weighted sampling population")
    return rng.choices(values, weights=weights, k=1)[0]


def _eligible_source(
    tables: dict[str, pd.DataFrame], approved_stock_codes: frozenset[str]
) -> tuple[list[str], list[float], list[list[tuple[str, int]]], dict[str, tuple[str, str]]]:
    required = {
        "customers": {"customer_id"},
        "orders": {"order_id", "customer_id"},
        "order_items": {
            "order_id", "source_stock_code", "quantity", "is_positive_sale"
        },
        "products": {"product_id", "source_stock_code"},
        "product_variants": {"variant_id", "source_stock_code"},
    }
    for key, columns in required.items():
        if key not in tables or columns - set(tables[key]):
            raise ValueError(f"Missing required Silver fields: {key}")
    if not approved_stock_codes:
        raise ValueError("Approved catalog cannot be empty")
    customers = sorted(str(value) for value in tables["customers"]["customer_id"])
    if len(customers) != len(set(customers)) or not customers:
        raise ValueError("Historical customer population must be nonempty and unique")
    items = tables["order_items"]
    sales = items.loc[
        items["is_positive_sale"].fillna(False).eq(True)
        & items["source_stock_code"].isin(approved_stock_codes)
    ].copy()
    if sales.empty:
        raise ValueError("No approved positive historical basket templates")
    sales["quantity"] = pd.to_numeric(sales["quantity"], errors="raise")
    if sales["quantity"].isna().any() or not sales["quantity"].gt(0).all():
        raise ValueError("Invalid approved historical quantities")
    if not sales["quantity"].map(lambda q: float(q).is_integer()).all():
        raise ValueError("Approved quantities must be positive integers")

    products = tables["products"].set_index("source_stock_code")
    variants = tables["product_variants"].set_index("source_stock_code")
    if not products.index.is_unique or not variants.index.is_unique:
        raise ValueError("Ambiguous source SKU identity mapping")
    if not approved_stock_codes.issubset(set(products.index) & set(variants.index)):
        raise ValueError("Approved catalog missing canonical product/variant identities")
    identities = {
        sku: (str(products.at[sku, "product_id"]), str(variants.at[sku, "variant_id"]))
        for sku in sorted(approved_stock_codes)
    }
    if any(not p or not v or p == "nan" or v == "nan" for p, v in identities.values()):
        raise ValueError("Missing canonical identities")

    # Stable sort ensures row-order changes do not alter template contents.
    templates = [
        sorted(
            (str(row.source_stock_code), int(row.quantity))
            for row in group.itertuples(index=False)
        )
        for _, group in sales.groupby("order_id", sort=True)
    ]
    orders = tables["orders"]
    if not orders["order_id"].is_unique:
        raise ValueError("Source order IDs must be unique")
    order_customer = orders.set_index("order_id")["customer_id"]
    if not set(sales["order_id"]).issubset(set(order_customer.index)):
        raise ValueError("Approved basket references unknown source order")
    counts = sales["order_id"].drop_duplicates().map(order_customer).value_counts()
    if not set(counts.index).issubset(set(tables["customers"]["customer_id"])):
        raise ValueError("Approved basket references unknown customer")
    weights = [float(int(counts.get(customer, 0)) + 1) for customer in customers]
    return customers, weights, templates, identities


def _seasonal_factors(calibration: dict, policy: SimulationPolicy):
    weekday = calibration["weekday_orders_per_exposure_day"]
    raw_week = [
        float(weekday[str(day)]["orders"]) / int(weekday[str(day)]["exposure_days"])
        for day in range(7)
    ]
    if not all(math.isfinite(x) and x >= 0 for x in raw_week) or sum(raw_week) <= 0:
        raise ValueError("Invalid weekday exposure statistics")
    mean_week = sum(raw_week) / 7
    alpha = policy.weekday_uniform_smoothing_percent / 100
    weekday_factors = [(1-alpha) * rate / mean_week + alpha for rate in raw_week]

    monthly = calibration["monthly_orders_per_exposure_day"]
    totals = {month: [0, 0] for month in range(1, 13)}
    for key, value in monthly.items():
        month = int(key[-2:])
        totals[month][0] += int(value["orders"])
        totals[month][1] += int(value["exposure_days"])
    rates = [
        total / days if days else 0
        for total, days in (totals[month] for month in range(1, 13))
    ]
    if not all(math.isfinite(x) and x >= 0 for x in rates) or sum(rates) <= 0:
        raise ValueError("Invalid monthly exposure statistics")
    mean_month = sum(rates) / 12
    month_factors = [rate / mean_month for rate in rates]

    hours = calibration["hour_order_counts"]
    hour_values = sorted(int(key) for key in hours)
    hour_weights = [float(hours[str(hour)]) for hour in hour_values]
    if not hour_values or min(hour_values) < 0 or max(hour_values) > 23:
        raise ValueError("Invalid source-clock hour histogram")
    return weekday_factors, month_factors, hour_values, hour_weights


def generate_interval(
    tables: dict[str, pd.DataFrame],
    calibration: dict,
    approved_stock_codes: frozenset[str],
    *,
    start_utc: datetime,
    cutoff_utc: datetime,
    policy: SimulationPolicy,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Emit canonical orders, items, and linked cancellation events for UTC interval.

    This is a pure in-memory implementation. Actual publish/commit and
    continuation ledger safety are separate responsibilities.
    """
    if (
        start_utc.tzinfo is None or start_utc.utcoffset() != timedelta(0)
        or cutoff_utc.tzinfo is None or cutoff_utc.utcoffset() != timedelta(0)
        or not START_UTC <= start_utc < cutoff_utc
    ):
        raise ValueError("Expected ordered UTC-aware cutoff after 2026-01-01")
    if calibration.get("schema_version") != "historical-behavior-calibration-v2":
        raise ValueError("Pinned historical calibration v2 is required")
    if len(approved_stock_codes) != calibration.get("eligible_stock_codes"):
        raise ValueError("Calibration/catalog population mismatch")
    customers, customer_weights, templates, identities = _eligible_source(
        tables, approved_stock_codes
    )
    weekdays, months, hours, hour_weights = _seasonal_factors(calibration, policy)
    orders_out: list[dict] = []
    items_out: list[dict] = []
    cancellations_out: list[dict] = []
    # Inspect preceding days for cancellations due within this interval.
    # Reconstructing order candidates is pure and does not republish old orders.
    lookback_start = max(START_UTC.date(), start_utc.date() - timedelta(
        days=policy.cancellation_delay_max_days
    ))
    day = lookback_start
    # Per-day seeds and event IDs are independent of the requested interval.
    while day <= cutoff_utc.date():
        rng = random.Random(_seed(policy.fingerprint, policy.seed, day.isoformat()))
        intensity = (
            policy.target_orders_per_day * weekdays[day.weekday()] * months[day.month - 1]
        )
        daily_n = _poisson(rng, intensity)
        daily = []
        for sequence in range(daily_n):
            hour = _choose(rng, hours, hour_weights)
            event = datetime.combine(
                day, time(hour=hour, minute=rng.randrange(60), second=rng.randrange(60)),
                tzinfo=UTC,
            )
            daily.append((event, sequence))
        # Stable chronological ordering, including across midnight cutoffs.
        for event, sequence in sorted(daily):
            within_order_interval = start_utc <= event < cutoff_utc
            # Per-order streams ensure interval slicing cannot change content.
            item_rng = random.Random(_seed(policy.fingerprint, day.isoformat(), sequence))
            customer = _choose(item_rng, customers, customer_weights)
            template = templates[item_rng.randrange(len(templates))]
            is_retail = item_rng.randrange(100) < policy.retail_probability_percent
            mode = "retail_bounded" if is_retail else "wholesale_tail"
            max_lines = policy.retail_line_cap if is_retail else policy.wholesale_line_cap
            max_units = (
                policy.retail_units_per_line_cap
                if is_retail else policy.wholesale_units_per_line_cap
            )
            # Derive order identity solely from date, per-day candidate ordinal
            # and immutable policy fingerprint; cutoffs and batches never enter it.
            source_order_key = f"{policy.fingerprint}:{day.isoformat()}:{sequence}"
            order_id = canonical_id("order", SYNTHETIC_SOURCE, source_order_key)
            order_total = Decimal("0.00")
            chosen = template[:max_lines]
            lines = []
            for line_index, (sku, observed_units) in enumerate(chosen):
                units = min(observed_units, max_units)
                price = PRICE_TIERS_EUR[_seed(policy.seed, sku) % len(PRICE_TIERS_EUR)]
                value = Decimal(price) * units
                order_total += value
                product_id, variant_id = identities[sku]
                lines.append({
                    "order_item_id": canonical_id(
                        "order_item", SYNTHETIC_SOURCE, f"{source_order_key}:{line_index}"
                    ),
                    "order_id": order_id,
                    "product_id": product_id,
                    "variant_id": variant_id,
                    "source_stock_code": sku,
                    "quantity": units,
                    "unit_price": price,
                    "line_amount": str(value.quantize(Decimal("0.01"))),
                    "currency": policy.currency,
                    "synthetic": True,
                    "source_system": SYNTHETIC_SOURCE,
                    "event_time_utc": event.isoformat(),
                })
            batch_id = f"synthetic-{day.isoformat()}-{policy.fingerprint[:12]}"
            if within_order_interval:
                orders_out.append({
                "order_id": order_id,
                "customer_id": customer,
                "source_system": SYNTHETIC_SOURCE,
                "synthetic": True,
                "event_type": "order_created",
                "event_id": canonical_id("event", SYNTHETIC_SOURCE, source_order_key),
                "event_time_utc": event.isoformat(),
                "currency": policy.currency,
                "order_total": str(order_total.quantize(Decimal("0.01"))),
                "basket_mode": mode,
                "batch_id": batch_id,
                "policy_sha256": policy.fingerprint,
            })
                items_out.extend(lines)

            # A cancellation is a separate, future-dated lifecycle event.
            # It is a simulation assumption, NOT a verified historical refund.
            cancel_rng = random.Random(_seed(policy.fingerprint, "cancel", source_order_key))
            if cancel_rng.randrange(100) < policy.cancellation_probability_percent:
                delay_days = 1 + cancel_rng.randrange(policy.cancellation_delay_max_days)
                cancellation_time = event + timedelta(days=delay_days)
                if start_utc <= cancellation_time < cutoff_utc:
                    cancelled_line = lines[cancel_rng.randrange(len(lines))]
                    cancelled_units = 1 + cancel_rng.randrange(cancelled_line["quantity"])
                    cancellation_key = f"{source_order_key}:cancellation"
                    cancellations_out.append({
                        "event_id": canonical_id("event", SYNTHETIC_SOURCE, cancellation_key),
                        "event_type": "order_item_cancelled",
                        "order_id": order_id,
                        "order_item_id": cancelled_line["order_item_id"],
                        "customer_id": customer,
                        "product_id": cancelled_line["product_id"],
                        "variant_id": cancelled_line["variant_id"],
                        "source_stock_code": cancelled_line["source_stock_code"],
                        "cancelled_quantity": cancelled_units,
                        "event_time_utc": cancellation_time.isoformat(),
                        "original_order_time_utc": event.isoformat(),
                        "currency": policy.currency,
                        "synthetic": True,
                        "source_system": SYNTHETIC_SOURCE,
                        "provenance": "simulated_cancellation_not_verified_refund",
                        "batch_id": f"synthetic-{cancellation_time.date()}-{policy.fingerprint[:12]}",
                        "policy_sha256": policy.fingerprint,
                    })
        day += timedelta(days=1)
    cancellations_out.sort(key=lambda c: (c["event_time_utc"], c["event_id"]))
    return orders_out, items_out, cancellations_out
