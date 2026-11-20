"""One-off refund catch-up remains explicit, bounded and independent from daily policy."""
import json
import sys
from pathlib import Path

import pytest

from scripts import run_phase5_lifecycle

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("args", [
    ["--refund-backfill-percent", "5", "--apply", "--confirm-apply"],
    ["--refund-backfill-percent", "41", "--apply", "--confirm-apply"],
    ["--refund-backfill-percent", "30", "--apply"],
    ["--refund-backfill-percent", "30", "--confirm-apply"],
    ["--refund-backfill-percent", "30", "--apply", "--confirm-apply",
     "--commit-nightly", "--require-settled"],
    ["--refund-backfill-percent", "30", "--apply", "--confirm-apply",
     "--refund-order", "gid://shopify/Order/123"],
])
def test_backfill_cli_refuses_unconfirmed_or_invalid_modes(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["lifecycle"] + args)
    with pytest.raises(SystemExit) as exc:
        run_phase5_lifecycle.main()
    assert exc.value.code == 2


def test_daily_policy_is_not_raised_by_one_off_backfill():
    policy = json.loads((ROOT / "config/phase5-synthetic-refund-policy.json").read_text())
    assert policy["probability_percent"] == 5
    assert policy["max_delay_days"] == 2
    assert policy["allow_verified_synthetic_manual_transactions"] is True
    source = (ROOT / "scripts/run_phase5_lifecycle.py").read_text()
    assert 'ap.add_argument("--refund-backfill-percent"' in source
    assert '"probability_percent": args.refund_backfill_percent' in source
    assert '"max_delay_days": 0' in source
    assert "if args.refund_backfill_percent is None:" in source
    assert "operations = []" in source
    assert "if args.refund_order or args.commit_nightly:" in source


def test_backfill_preview_is_read_only_and_counts_eligible(monkeypatch, capsys):
    """A read-only preview should not require --apply or contact live AWS."""
    import boto3
    from ecommerce.application.use_cases.shopify_order_lifecycle import Operation

    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", "realtime-ecommerce-analytics.myshopify.com")
    monkeypatch.setenv("SHOPIFY_CLIENT_ID", "local-preview-only")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "local-preview-only")
    monkeypatch.setenv("S3_LANDING_BUCKET", "preview-only")
    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: object())

    class Client:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def verify_shop(self):
            pass

    monkeypatch.setattr(run_phase5_lifecycle, "ShopifyClientCredentials",
                        lambda *args: object())
    monkeypatch.setattr(run_phase5_lifecycle, "ShopifyGraphQLClient",
                        lambda *args, **kwargs: Client())
    monkeypatch.setattr(run_phase5_lifecycle, "S3LifecycleJournal",
                        lambda *args: type("Journal", (), {"get": lambda self, key: None})())
    monkeypatch.setattr(run_phase5_lifecycle, "completed_generation_records",
                        lambda *args, **kwargs: iter([(
                            "live-20261120-afternoon", "gid://shopify/Order/123"
                        )]))
    monkeypatch.setattr(run_phase5_lifecycle, "eligible_cancellations",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("Backfill must not select cancellations")))
    monkeypatch.setattr(run_phase5_lifecycle, "eligible_synthetic_refunds",
                        lambda *args, **kwargs: iter([
                            Operation("live-20261120-afternoon",
                                      "gid://shopify/Order/123", "refund",
                                      "2026-11-20T20:00:00+00:00",
                                      "gid://shopify/LineItem/456", 1,
                                      synthetic_nonfinancial=True)
                        ]))
    monkeypatch.setattr(run_phase5_lifecycle, "advance",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("Preview must not mutate Shopify")))
    monkeypatch.setattr(sys, "argv", [
        "lifecycle", "--run-id", "live-20261120-afternoon",
        "--refund-backfill-percent", "30", "--limit", "1",
    ])
    run_phase5_lifecycle.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary["mode"] == "PREVIEW"
    assert summary["eligible_operations"] == 1
    assert summary["refund_backfill_percent"] == 30
    assert summary["seen"] == 0
