"""Coordinator must read the real Phase 5 events GSI, never controller index."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from scripts.aws import phase6_evening_coordinator as mod


def test_pending_event_check_uses_events_table_by_state():
    now = datetime(2026, 11, 20, 16, 30, tzinfo=timezone.utc)
    ddb = Mock()
    events = Mock()
    events.query.return_value = {"Items": [{"state": "pending"}]}
    ddb.Table.return_value = events
    ctrl = {
        "schema_ready": True,
        "webhooks_ready": True,
        "last_event_at": (now - timedelta(minutes=40)).isoformat(),
        "last_work_at": (now - timedelta(minutes=30)).isoformat(),
    }
    result = mod.quiet_now(now, ctrl, ddb, Mock(), Mock())
    assert result == "pending_projection_events"
    ddb.Table.assert_called_once_with("ecommerce-demo-phase5-events")
    assert events.query.call_args.kwargs["IndexName"] == "by_state"


def test_no_pending_events_passes_other_quiet_checks():
    now = datetime(2026, 11, 20, 16, 30, tzinfo=timezone.utc)
    ddb, sqs, ecs = Mock(), Mock(), Mock()
    ddb.Table.return_value.query.return_value = {"Items": []}
    sqs.get_queue_attributes.return_value = {"Attributes": {
        "ApproximateNumberOfMessages": "0",
        "ApproximateNumberOfMessagesNotVisible": "0",
    }}
    ecs.list_tasks.return_value = {"taskArns": []}
    ctrl = {
        "schema_ready": True, "webhooks_ready": True,
        "last_event_at": (now - timedelta(minutes=35)).isoformat(),
        "last_work_at": (now - timedelta(minutes=30)).isoformat(),
    }
    assert mod.quiet_now(now, ctrl, ddb, sqs, ecs) is None
    ddb.Table.assert_called_once_with(mod.EVENTS_TABLE)


def test_dynamodb_reserved_day_attribute_is_escaped_on_both_transitions():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    source = (root / "scripts/aws/phase6_evening_coordinator.py").read_text()
    assert source.count('ConditionExpression="#day=:day AND #owner=:owner"') == 2
    assert source.count('ExpressionAttributeNames={"#day": "day", "#owner": "owner"}') == 2


def test_windows_deployer_uses_standard_aws_cli_zip_upload():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    source = (root / "scripts/aws/deploy_phase6_evening_coordinator.ps1").read_text()
    assert "Compress-Archive" in source
    assert "lambda update-function-code" in source
    assert "fileb://" in source
    assert "lambda wait function-updated" in source
