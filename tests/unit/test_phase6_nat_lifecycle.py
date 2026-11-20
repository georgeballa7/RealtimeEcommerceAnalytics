"""No AWS writes: test opt-in NAT lifecycle decisions and route ownership."""
from datetime import datetime
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


def test_nat_is_retained_for_18_00_lifecycle_ecs_task():
    at = lambda h, m: datetime(2026, 11, 21, h, m, tzinfo=BERLIN)
    assert not mod.can_consider_release(at(7, 55), "morning")
    assert mod.can_consider_release(at(8, 30), "morning")
    assert not mod.can_consider_release(at(17, 59), "afternoon")
    assert not mod.can_consider_release(at(18, 20), "afternoon")
    assert mod.can_consider_release(at(18, 25), "afternoon")
    assert mod.is_stale(at(14, 0), "morning")
    assert mod.is_stale(at(23, 0), "afternoon")


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
    local = datetime(2026, 11, 21, 19, 0, tzinfo=BERLIN)
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
    assert 'default     = "static"' in network
    assert 'var.nat_gateway_mode == "static" ? var.nat_gateway_count : 0' in network
    assert 'var.nat_gateway_mode == "static" ? 2 : 0' in network
    assert 'var.nat_gateway_mode == "on_demand" ? 1 : 0' in dynamic
    assert 'ec2:ResourceTag/Purpose' in dynamic
    assert 'ecommerce-phase6-nat-lifecycle' in dynamic
    assert 's3' in network.lower() and 'Gateway' in network


def test_morning_and_afternoon_use_distinct_daily_nat_records():
    day = "2026-11-21"
    assert mod.slot_key(day, "morning") != mod.slot_key(day, "afternoon")
    assert mod.slot_key(day, "morning") == "phase6-nat:2026-11-21:morning"


def test_nat_release_window_advances_only_after_legacy_cutover(monkeypatch):
    at = datetime(2026, 11, 21, 16, 35, tzinfo=BERLIN)
    monkeypatch.delenv("PHASE5_LEGACY_LIFECYCLE_ENABLED", raising=False)
    assert not mod.can_consider_release(at, "afternoon")
    monkeypatch.setenv("PHASE5_LEGACY_LIFECYCLE_ENABLED", "false")
    assert mod.can_consider_release(at, "afternoon")
    monkeypatch.setenv("PHASE5_LEGACY_LIFECYCLE_ENABLED", "true")
    assert not mod.can_consider_release(at, "afternoon")


def test_terraform_legacy_schedule_defaults_to_preserved_until_verified():
    script = (ROOT / "infrastructure/terraform/phase5_scheduled_lifecycle.tf").read_text()
    assert 'enable_phase5_legacy_lifecycle_schedule' in script
    assert 'default     = true' in script
    assert 'var.enable_phase5_legacy_lifecycle_schedule ? 1 : 0' in script
