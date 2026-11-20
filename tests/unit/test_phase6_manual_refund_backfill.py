"""One-off refund catch-up remains explicit, bounded and independent from daily policy."""
import json
import sys
from pathlib import Path

import pytest

from scripts import run_phase5_lifecycle

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("args", [
    ["--refund-backfill-percent", "40"],
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
