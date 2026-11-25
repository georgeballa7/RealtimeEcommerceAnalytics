"""No AWS writes: test opt-in NAT lifecycle decisions and route ownership."""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from scripts.aws import phase6_nat_lifecycle_lambda as mod


BERLIN = ZoneInfo("Europe/Berlin")
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("month", "day", "hour", "minute", "expected"),
    [
        (11, 21, 7, 25, None),
        (11, 21, 7, 30, "morning"),
        (11, 21, 8, 0, "morning"),
        (11, 21, 13, 55, "morning"),
        (11, 21, 14, 0, None),
        (11, 21, 15, 25, None),
        (11, 21, 15, 30, "afternoon"),
        (11, 21, 16, 0, "afternoon"),
        (11, 21, 18, 0, "afternoon"),
        (11, 21, 22, 55, "afternoon"),
        (11, 21, 23, 0, None),
        (7, 21, 7, 30, "morning"),  # daylight saving time
        (7, 21, 15, 30, "afternoon"),
    ],
)
def test_nat_prewarm_windows(month, day, hour, minute, expected):
    local = datetime(2026, month, day, hour, minute, tzinfo=BERLIN)
    assert mod.slot_window(local) == expected


def test_nat_earliest_release_tracks_1630_lifecycle_not_old_2200():
    at = lambda h, m: datetime(2026, 11, 21, h, m, tzinfo=BERLIN)
    assert not mod.can_consider_release(at(7, 55), "morning")
    assert mod.can_consider_release(at(8, 30), "morning")
    assert not mod.can_consider_release(at(16, 29), "afternoon")
    assert mod.can_consider_release(at(16, 30), "afternoon")
    assert mod.can_consider_release(at(18, 20), "afternoon")
    assert mod.can_consider_release(at(22, 25), "afternoon")
    # This only sets the earliest release time: live queue, ECS, Glue, dbt,
    # lifecycle completion and quiet-period guards must independently pass.
    assert mod.is_stale(at(14, 0), "morning")
    assert not mod.is_stale(at(23, 0), "afternoon")
    assert mod.is_stale(at(3, 0), "afternoon")


def test_nat_routes_can_be_created_only_when_absent():
    ec2 = Mock()
    ec2.describe_route_tables.return_value = {
        "RouteTables": [
            {"RouteTableId": "rtb-a", "Routes": []},
            {"RouteTableId": "rtb-b", "Routes": []},
        ]
    }
    mod.ensure_default_routes(ec2, ["rtb-a", "rtb-b"], "nat-owned")
    assert ec2.create_route.call_count == 2
    assert all(
        call.kwargs["NatGatewayId"] == "nat-owned"
        for call in ec2.create_route.call_args_list
    )


def test_nat_route_never_overwrites_foreign_gateway():
    ec2 = Mock()
    ec2.describe_route_tables.return_value = {
        "RouteTables": [
            {"RouteTableId": "rtb-a", "Routes": [{
                "DestinationCidrBlock": "0.0.0.0/0",
                "NatGatewayId": "nat-terraform-static", "State": "active",
            }]},
            {"RouteTableId": "rtb-b", "Routes": []},
        ]
    }
    with pytest.raises(RuntimeError, match="refusing replacement"):
        mod.ensure_default_routes(ec2, ["rtb-a", "rtb-b"], "nat-new")
    ec2.create_route.assert_not_called()


def test_nat_delete_only_owned_default_routes():
    ec2 = Mock()
    ec2.describe_route_tables.return_value = {
        "RouteTables": [
            {"RouteTableId": "rtb-a", "Routes": [
                {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-owned"},
                {"DestinationCidrBlock": "pl-s3", "GatewayId": "vpce-s3"},
            ]},
            {"RouteTableId": "rtb-b", "Routes": []},
        ]
    }
    mod.remove_default_routes(ec2, ["rtb-a", "rtb-b"], "nat-owned")
    ec2.delete_route.assert_called_once_with(
        RouteTableId="rtb-a", DestinationCidrBlock="0.0.0.0/0"
    )


def test_nat_will_not_delete_when_projector_or_error_hold_exists():
    ec2 = Mock()
    clients = {"ecs": Mock(), "sqs": Mock(), "ddb": Mock(), "glue": Mock()}
    settings = {"ECS_CLUSTER": "ecommerce-demo", "EVENT_QUEUE_URL": "queue",
                "EVENTS_TABLE": "events", "GLUE_JOB_NAME": "job"}
    local = datetime(2026, 11, 21, 22, 30, tzinfo=BERLIN)
    assert mod.inactive_workloads(
        clients, settings, {"worker_task_arn": "ecs/active"}, local, "afternoon"
    ) == "projector_not_idle"
    assert mod.inactive_workloads(
        clients, settings, {"error_hold": True}, local, "afternoon"
    ) == "projector_not_idle"
    ec2.delete_nat_gateway.assert_not_called()


def test_terraform_keeps_static_nat_by_default_and_dynamic_opt_in_isolated():
    network = (ROOT / "infrastructure/terraform/networking.tf").read_text()
    dynamic = (ROOT / "infrastructure/terraform/phase6_nat_on_demand.tf").read_text()
    assert 'default     = "on_demand"' in network
    assert 'var.nat_gateway_mode == "static" ? var.nat_gateway_count : 0' in network
    assert 'local.nat_lifecycle_count' in dynamic
    assert 'var.nat_gateway_mode == "on_demand" ? "ENABLED" : "DISABLED"' in dynamic
    assert 'ec2:ResourceTag/Purpose' in dynamic
    assert 'ecommerce-phase6-nat-lifecycle' in dynamic
    assert 's3' in network.lower() and 'Gateway' in network


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (7, 49, False), (7, 50, True), (8, 0, True),
        (8, 9, True), (8, 10, False),
        (15, 49, False), (15, 50, True), (16, 5, True),
        (16, 10, False),
        (16, 19, False), (16, 20, True), (16, 39, True),
        (16, 40, False), (21, 50, False), (22, 9, False),
    ],
)
def test_outbound_readiness_windows(hour, minute, expected):
    local = datetime(2026, 11, 21, hour, minute, tzinfo=BERLIN)
    assert mod.generation_readiness_window(local) is expected


def test_outbound_readiness_reports_missing_nat_and_incomplete_routes():
    local = datetime(2026, 11, 21, 15, 55, tzinfo=BERLIN)
    ec2 = Mock()
    with pytest.raises(RuntimeError, match="not available"):
        mod.require_outbound_ready(local, ec2, ["rtb-a", "rtb-b"], None)
    ec2.describe_route_tables.return_value = {
        "RouteTables": [
            {"RouteTableId": "rtb-a", "Routes": [
                {"DestinationCidrBlock": "0.0.0.0/0",
                 "NatGatewayId": "nat-ready", "State": "active"}]},
            {"RouteTableId": "rtb-b", "Routes": []},
        ]
    }
    with pytest.raises(RuntimeError, match="not active"):
        mod.require_outbound_ready(
            local, ec2, ["rtb-a", "rtb-b"],
            {"State": "available", "NatGatewayId": "nat-ready"}
        )
    ec2.describe_route_tables.return_value["RouteTables"][1]["Routes"].append(
        {"DestinationCidrBlock": "0.0.0.0/0",
         "NatGatewayId": "nat-ready", "State": "active"}
    )
    assert mod.require_outbound_ready(
        local, ec2, ["rtb-a", "rtb-b"],
        {"State": "available", "NatGatewayId": "nat-ready"}
    ) is None


def test_morning_and_afternoon_use_distinct_daily_nat_records():
    day = "2026-11-21"
    assert mod.slot_key(day, "morning") != mod.slot_key(day, "afternoon")
    assert mod.slot_key(day, "morning") == "phase6-nat:2026-11-21:morning"


def test_nat_release_window_allows_early_safe_release_and_overnight_recovery():
    before = datetime(2026, 11, 21, 16, 25, tzinfo=BERLIN)
    after = datetime(2026, 11, 21, 18, 35, tzinfo=BERLIN)
    overnight = datetime(2026, 11, 22, 1, 10, tzinfo=BERLIN)
    assert not mod.can_consider_release(before, "afternoon")
    assert mod.can_consider_release(after, "afternoon")
    assert mod.can_consider_release(overnight, "afternoon")

def test_terraform_legacy_schedule_defaults_to_preserved_until_verified():
    script = (ROOT / "infrastructure/terraform/phase5_scheduled_lifecycle.tf").read_text()
    assert 'enable_phase5_legacy_lifecycle_schedule' in script
    assert 'default     = true' in script
    assert 'var.enable_phase5_legacy_lifecycle_schedule ? 1 : 0' in script


def test_natural_afternoon_handoff_records_slot_before_nat_release(monkeypatch):
    """If morning NAT survives to 15:30, it must be kept for 16:00 orders."""
    now = datetime(2026, 11, 21, 14, 30, tzinfo=timezone.utc)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    nat = {
        "NatGatewayId": "nat-morning",
        "State": "available",
        "Tags": [{"Key": "Day", "Value": "2026-11-21"},
                 {"Key": "Slot", "Value": "morning"}],
    }
    settings = {
        "NAT_VPC_ID": "vpc-test",
        "CONTROLLER_TABLE": "controller",
        "route_tables": ["rtb-a", "rtb-b"],
    }
    table = Mock()
    session = Mock()
    session.resource.return_value.Table.return_value = table
    recorded = []
    monkeypatch.setattr(mod, "datetime", FixedDatetime)
    monkeypatch.setattr(mod, "config", lambda: settings)
    monkeypatch.setattr(mod.boto3, "Session", lambda **kwargs: session)
    monkeypatch.setattr(mod, "acquire_lock", lambda *args: True)
    monkeypatch.setattr(mod, "managed_nat", lambda *args: nat)
    monkeypatch.setattr(mod, "ensure_default_routes", lambda *args: recorded.append("routes"))
    monkeypatch.setattr(mod, "record_warm", lambda _table, day, slot:
                        recorded.append(("warm", day, slot)))
    def fake_release(*args):
        recorded.append("release_check")
        return {"nat_lifecycle": "retained"}
    monkeypatch.setattr(mod, "release_if_idle", fake_release)

    monkeypatch.setenv("NAT_LIFECYCLE_ACTIVE", "true")
    result = mod.handler({}, None)
    assert result == {"nat_lifecycle": "retained"}
    assert recorded == [
        ("warm", "2026-11-21", "afternoon"), "routes", "release_check"
    ]


def test_cross_midnight_nat_release_preserves_original_slot_date(monkeypatch):
    """00:30 belongs to yesterday's 22:00 lifecycle, not today's morning."""
    nat = {
        "NatGatewayId": "nat-morning",
        "State": "available",
        "Tags": [{"Key": "Day", "Value": "2026-11-21"},
                 {"Key": "Slot", "Value": "morning"}],
    }
    local = datetime(2026, 11, 22, 0, 30, tzinfo=BERLIN)
    table = Mock()
    data = {
        "phase6-nat:2026-11-21:morning": {
            "phase": "warming",
        },
        "phase6-nat:2026-11-21:afternoon": {
            "phase": "warming",
        },
        "phase6-morning": {"day": "2026-11-21", "phase": "completed"},
        "phase6-evening": {"day": "2026-11-21", "phase": "completed"},
        "phase5-controller": {},
    }
    table.get_item.side_effect = lambda Key, **kwargs: {"Item": data[Key["id"]]} \
        if Key["id"] in data else {}
    ec2 = Mock()
    monkeypatch.setattr(mod, "inactive_workloads", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "remove_default_routes", lambda *a: None)
    response = mod.release_if_idle(
        ec2, {}, table, {"route_tables": ["rtb-a", "rtb-b"]},
        nat, local,
    )
    assert response["nat_lifecycle"] == "delete_requested"
    ec2.delete_nat_gateway.assert_called_once_with(NatGatewayId="nat-morning")
    updated = [call.kwargs["Key"]["id"]
               for call in table.update_item.call_args_list]
    assert updated == [
        "phase6-nat:2026-11-21:morning",
        "phase6-nat:2026-11-21:afternoon",
    ]


def test_new_day_open_slot_blocks_old_gateway_release(monkeypatch):
    """A gateway carried over from yesterday must protect today's 08:00 run."""
    nat = {
        "NatGatewayId": "nat-yesterday",
        "State": "available",
        "Tags": [{"Key": "Day", "Value": "2026-11-20"},
                 {"Key": "Slot", "Value": "afternoon"}],
    }
    local = datetime(2026, 11, 21, 7, 30, tzinfo=BERLIN)
    table = Mock()
    data = {
        "phase6-nat:2026-11-20:afternoon": {"phase": "warming"},
        "phase6-nat:2026-11-21:morning": {"phase": "warming"},
        "phase6-evening": {"day": "2026-11-20", "phase": "completed"},
        "phase5-controller": {},
    }
    table.get_item.side_effect = lambda Key, **kwargs: {"Item": data[Key["id"]]} \
        if Key["id"] in data else {}
    ec2 = Mock()
    monkeypatch.setattr(mod, "inactive_workloads", lambda *a, **kw: None)
    response = mod.release_if_idle(
        ec2, {}, table, {"route_tables": ["rtb-a", "rtb-b"]},
        nat, local,
    )
    assert response["nat_lifecycle"] == "awaiting_slot_completion"
    assert response["day"] == "2026-11-21"
    assert response["slot"] == "morning"
    ec2.delete_nat_gateway.assert_not_called()


def test_previous_day_morning_can_release_after_midnight_if_idle():
    """Yesterday's morning cannot become stuck until 08:30 the next day."""
    local = datetime(2026, 11, 22, 0, 30, tzinfo=BERLIN)
    clients = {
        "ecs": Mock(), "sqs": Mock(), "ddb": Mock(), "glue": Mock()
    }
    clients["ecs"].list_tasks.return_value = {"taskArns": []}
    clients["sqs"].get_queue_attributes.return_value = {
        "Attributes": {"ApproximateNumberOfMessages": "0",
                       "ApproximateNumberOfMessagesNotVisible": "0"}
    }
    clients["ddb"].Table.return_value.query.return_value = {"Count": 0}
    clients["glue"].get_job_runs.return_value = {"JobRuns": []}
    settings = {
        "ECS_CLUSTER": "ecommerce-demo", "EVENT_QUEUE_URL": "queue",
        "EVENTS_TABLE": "events", "GLUE_JOB_NAME": "job"
    }
    assert mod.inactive_workloads(clients, settings, {}, local, "morning",
                                  day="2026-11-21") is None
