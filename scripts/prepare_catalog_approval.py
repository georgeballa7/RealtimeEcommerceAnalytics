"""Produce a deterministic Shopify SKU allowlist from the completed catalog audit.

This is local-only, no AWS, Shopify, or PostgreSQL calls. The approved population
is exactly the audited unflagged subset. No existing file is overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ecommerce.application.use_cases.catalog_approval import manifest_from_audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit", type=Path, default=Path("catalog-review-2026-11-18.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("catalog-approval-unflagged-v1.json")
    )
    args = parser.parse_args()
    report = json.loads(args.audit.read_text(encoding="utf-8"))
    content = manifest_from_audit(report)
    # Exclusive creation prevents accidentally overwriting previous approvals.
    with args.output.open("xb") as stream:
        stream.write(content)
    print(f"Approved unflagged stock codes: {report['without_detected_flags']}")
    print(f"Manifest: {args.output}")
    print(f"SHA-256: {hashlib.sha256(content).hexdigest()}")
    print("Local manifest only; no AWS upload or Shopify writes performed.")


if __name__ == "__main__":
    main()
