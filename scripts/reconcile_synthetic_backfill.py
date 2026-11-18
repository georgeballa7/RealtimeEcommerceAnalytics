"""Read-only S3 synthetic backfill reconciliation entrypoint.

Run from the repository root:
    python scripts/reconcile_synthetic_backfill.py
"""
from ecommerce.application.use_cases.synthetic_reconciliation import main


if __name__ == "__main__":
    main()
