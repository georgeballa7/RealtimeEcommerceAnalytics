"""The 22:00 development-store lifecycle is the Silver ingestion barrier."""
import io
import json
from unittest.mock import Mock

import pytest

from scripts.run_phase5_lifecycle import publish_nightly_lifecycle


BUCKET = "test-landing"
DAY = "2026-11-21"


def generation(slot):
    run_id = f"live-20261121-{slot}"
    return {"schema_version": 1, "run_id": run_id,
            "slot": slot, "generation": "complete"}


def test_nightly_refuses_missing_or_incomplete_generation():
    s3 = Mock()
    from botocore.exceptions import ClientError
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "GetObject")
    with pytest.raises(ClientError):
        publish_nightly_lifecycle(s3, BUCKET, DAY, {"mode": "APPLY"})
    s3.put_object.assert_not_called()


def test_nightly_writes_completion_only_after_both_generators():
    s3 = Mock()
    records = {
        "simulation/phase5/slots/v1/live-20261121-morning.json":
            generation("morning"),
        "simulation/phase5/slots/v1/live-20261121-afternoon.json":
            generation("afternoon"),
    }
    def get_object(*, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(records[Key]).encode())}
    s3.get_object.side_effect = get_object
    with pytest.raises(RuntimeError, match="requires applied"):
        publish_nightly_lifecycle(s3, BUCKET, DAY, {"mode": "PREVIEW"})
    s3.put_object.assert_not_called()
    publish_nightly_lifecycle(s3, BUCKET, DAY,
                              {"mode": "APPLY", "outcomes": {"refunded": 2}})
    assert s3.put_object.call_args.kwargs["Key"] == (
        "simulation/phase5/slots/v1/lifecycle-2026-11-21.json")
    body = json.loads(s3.put_object.call_args.kwargs["Body"])
    assert body["lifecycle"] == "settled"
    assert body["generation_slots"] == ["morning", "afternoon"]


def test_terraform_scheduled_lifecycle_is_2200_berlin():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] /
            "infrastructure/terraform/phase5_scheduled_lifecycle.tf").read_text()
    assert 'cron(0 22 * * ? *)' in text
    assert 'cron(0 18 * * ? *)' not in text
    assert '"--commit-nightly"' in text
    assert 'schedule_expression_timezone = "Europe/Berlin"' in text
