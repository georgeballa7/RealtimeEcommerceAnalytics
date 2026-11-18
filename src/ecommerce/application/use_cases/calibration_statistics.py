"""Deterministic descriptive statistics for historical synthetic-order calibration.

Rates are represented as integer counts and integer exposure days to avoid
rounding assumptions and preserve reproducible machine-readable evidence.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from math import ceil

import pandas as pd


def empirical_percentiles(values: list[int], percentiles: tuple[int, ...] = (50, 90, 95, 99)) -> dict:
    """Nearest-rank empirical percentiles, including zeros and outliers."""
    if not values:
        raise ValueError("Cannot calculate percentiles from an empty population")
    ordered = sorted(int(value) for value in values)
    return {
        f"p{p:02d}": ordered[max(0, ceil(p * len(ordered) / 100) - 1)]
        for p in percentiles
    }


def calendar_exposure(naive_timestamps: pd.Series) -> dict:
    """Count observed orders and calendar days by month and weekday.

    The observation window spans the first through last source invoice dates,
    *inclusive*. Boundary days and months can be partial; this is explicitly
    an exposure approximation, not verified merchant operating hours.
    """
    if naive_timestamps.empty:
        raise ValueError("Cannot estimate exposure without observed invoice timestamps")
    if naive_timestamps.isna().any() or naive_timestamps.dt.tz is not None:
        raise ValueError("Exposure requires valid timezone-naive timestamps")
    start = naive_timestamps.min().date()
    end = naive_timestamps.max().date()
    monthly_days: Counter[str] = Counter()
    weekday_days: Counter[str] = Counter()
    current = start
    while current <= end:
        monthly_days[current.strftime("%Y-%m")] += 1
        weekday_days[str(current.weekday())] += 1
        current += timedelta(days=1)
    return {
        "observation_start_source_date": start.isoformat(),
        "observation_end_source_date_inclusive": end.isoformat(),
        "observation_calendar_days": (end - start).days + 1,
        "month_exposure_days": dict(sorted(monthly_days.items())),
        "weekday_exposure_days": dict(sorted(weekday_days.items())),
        "exposure_assumption": (
            "First through last invoice calendar dates inclusive; boundary dates "
            "may be partial; no timezone or operating-hours assumption"
        ),
    }
