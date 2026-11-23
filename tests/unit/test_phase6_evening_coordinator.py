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


def test_slot_windows_cover_both_berlin_runs_and_summer_time():
    from zoneinfo import ZoneInfo

    berlin = ZoneInfo("Europe/Berlin")
    morning = datetime(2026, 11, 20, 8, 0, tzinfo=berlin)
    afternoon = datetime(2026, 11, 20, 16, 0, tzinfo=berlin)
    summer = datetime(2026, 7, 20, 8, 0, tzinfo=berlin)
    assert mod.slot_for_time(morning) == "morning"
    assert mod.slot_for_time(afternoon) is None
    assert mod.slot_for_time(datetime(2026, 11, 20, 22, 0, tzinfo=berlin)) == "afternoon"
    assert mod.slot_for_time(datetime(2026, 11, 21, 1, 0, tzinfo=berlin)) == "afternoon"
    assert mod.processing_day(datetime(2026, 11, 21, 1, 0, tzinfo=berlin), "afternoon") == "2026-11-20"
    assert mod.slot_for_time(summer) == "morning"
    assert mod.slot_for_time(datetime(2026, 11, 20, 7, 55, tzinfo=berlin)) is None
    assert mod.slot_for_time(datetime(2026, 11, 20, 21, 55, tzinfo=berlin)) == "afternoon"
    assert mod.SLOT_IDS["afternoon"] == "phase6-evening"  # existing ledger retained
    assert mod.SLOT_IDS["morning"] != mod.SLOT_IDS["afternoon"]


def test_morning_quiet_check_rejects_previous_day_events():
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 11, 20, 8, 30, tzinfo=berlin).astimezone(timezone.utc)
    old_event = datetime(2026, 11, 20, 7, 50, tzinfo=berlin).astimezone(timezone.utc)
    ctrl = {
        "schema_ready": True, "webhooks_ready": True,
        "last_event_at": old_event.isoformat(),
    }
    assert mod.quiet_now(now, ctrl, Mock(), Mock(), Mock(), slot="morning") == (
        "no_events_after_slot_start"
    )


def test_morning_quiet_check_accepts_settled_current_slot():
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 11, 20, 8, 35, tzinfo=berlin).astimezone(timezone.utc)
    last_event = datetime(2026, 11, 20, 8, 20, tzinfo=berlin).astimezone(timezone.utc)
    ctrl = {
        "schema_ready": True, "webhooks_ready": True,
        "last_event_at": last_event.isoformat(), "last_work_at": last_event.isoformat(),
    }
    ddb, sqs, ecs = Mock(), Mock(), Mock()
    ddb.Table.return_value.query.return_value = {"Items": []}
    sqs.get_queue_attributes.return_value = {"Attributes": {
        "ApproximateNumberOfMessages": "0",
        "ApproximateNumberOfMessagesNotVisible": "0",
    }}
    ecs.list_tasks.return_value = {"taskArns": []}
    assert mod.quiet_now(now, ctrl, ddb, sqs, ecs, slot="morning") is None


def test_coordinator_deployer_preserves_existing_readiness_schedule():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts/aws/deploy_phase6_evening_coordinator.ps1").read_text()
    assert "cron(0/5 0-2,8-15,22-23 * * ? *)" in script
    assert "ecommerce-phase6-evening-glue-demo" in script
    assert "scheduler update-schedule" in script
    assert "scheduler get-schedule" in script
    assert "FlexibleTimeWindow" in script
    assert "Europe/Berlin" in script


def test_coordinator_requires_generator_and_lifecycle_marker_before_glue():
    import io
    from zoneinfo import ZoneInfo
    now = datetime(2026, 11, 20, 8, 35, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(timezone.utc)
    source_time = (now - timedelta(minutes=15)).isoformat()
    ctrl = {"schema_ready": True, "webhooks_ready": True,
            "last_event_at": source_time, "last_work_at": source_time}
    ddb, sqs, ecs, s3 = Mock(), Mock(), Mock(), Mock()
    ddb.Table.return_value.query.return_value = {"Items": []}
    sqs.get_queue_attributes.return_value = {"Attributes": {
        "ApproximateNumberOfMessages": "0",
        "ApproximateNumberOfMessagesNotVisible": "0"}}
    ecs.list_tasks.return_value = {"taskArns": []}
    from botocore.exceptions import ClientError
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
    assert mod.quiet_now(now, ctrl, ddb, sqs, ecs, slot="morning", s3=s3) == (
        "generation_morning_not_complete"
    )
    s3.get_object.side_effect = None
    import json
    correct = {"schema_version": 1, "run_id": "live-20261120-morning",
               "slot": "morning", "generation": "complete"}
    s3.get_object.return_value = {"Body": io.BytesIO(json.dumps(correct).encode())}
    assert mod.quiet_now(now, ctrl, ddb, sqs, ecs, slot="morning", s3=s3) is None
    assert s3.get_object.call_args.kwargs["Key"].endswith(
        "live-20261120-morning.json"
    )


def test_coordinator_rejects_inconsistent_success_marker():
    import io, json, pytest
    from zoneinfo import ZoneInfo
    now = datetime(2026, 11, 20, 8, 35, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(timezone.utc)
    ctrl = {"schema_ready": True, "webhooks_ready": True}
    s3 = Mock()
    wrong = {"schema_version": 1, "run_id": "live-20261120-morning",
             "slot": "morning", "generation": "pending"}
    s3.get_object.return_value = {"Body": io.BytesIO(json.dumps(wrong).encode())}
    with pytest.raises(RuntimeError, match="Invalid or conflicting"):
        mod.quiet_now(now, ctrl, Mock(), Mock(), Mock(), slot="morning", s3=s3)


def test_nightly_requires_both_generator_markers_and_lifecycle_settled():
    import io, json
    from zoneinfo import ZoneInfo
    local = datetime(2026, 11, 20, 22, 40, tzinfo=ZoneInfo("Europe/Berlin"))
    now = local.astimezone(timezone.utc)
    activity = (now - timedelta(minutes=15)).isoformat()
    ctrl = {"schema_ready": True, "webhooks_ready": True,
            "last_event_at": activity, "last_work_at": activity}
    ddb, sqs, ecs, s3 = Mock(), Mock(), Mock(), Mock()
    ddb.Table.return_value.query.return_value = {"Items": []}
    sqs.get_queue_attributes.return_value = {"Attributes": {
        "ApproximateNumberOfMessages": "0",
        "ApproximateNumberOfMessagesNotVisible": "0"}}
    ecs.list_tasks.return_value = {"taskArns": []}

    objects = {
        "simulation/phase5/slots/v1/live-20261120-morning.json":
            {"schema_version": 1, "run_id": "live-20261120-morning",
             "slot": "morning", "generation": "complete"},
        "simulation/phase5/slots/v1/live-20261120-afternoon.json":
            {"schema_version": 1, "run_id": "live-20261120-afternoon",
             "slot": "afternoon", "generation": "complete"},
        "simulation/phase5/slots/v1/lifecycle-2026-11-20.json":
            {"schema_version": 1, "day": "2026-11-20",
             "lifecycle": "settled", "generation_slots": ["morning", "afternoon"]},
    }
    from botocore.exceptions import ClientError
    def get_object(*, Bucket, Key):
        if Key not in objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(json.dumps(objects[Key]).encode())}
    s3.get_object.side_effect = get_object
    assert mod.quiet_now(now, ctrl, ddb, sqs, ecs, slot="afternoon", s3=s3) is None
    del objects["simulation/phase5/slots/v1/lifecycle-2026-11-20.json"]
    assert mod.quiet_now(now, ctrl, ddb, sqs, ecs, slot="afternoon", s3=s3) == (
        "lifecycle_night_not_complete")
