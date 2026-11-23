"""Offline guards for event-driven dbt and on-demand NAT handoff."""
from datetime import datetime, timezone
from types import SimpleNamespace

from scripts.aws import phase7_dbt_orchestrator as bridge


class Table:
    def __init__(self, entries):
        self.entries = entries

    def get_item(self, Key, ConsistentRead=True):
        return {"Item": self.entries.get(Key["id"], {})}


def test_no_warehouse_ack_means_no_eligible_dbt_build(monkeypatch):
    class FakeDatetime:
        @staticmethod
        def now(zone):
            return datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(bridge, "datetime", FakeDatetime)
    assert bridge.ready_slots(Table({
        "phase6-morning": {
            "day": "2026-11-21", "phase": "dispatched",
            "batch_id": "20261121T080000Z"
        }
    })) == []


def test_verified_morning_ack_is_eligible(monkeypatch):
    class FakeDatetime:
        @staticmethod
        def now(zone):
            return datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(bridge, "datetime", FakeDatetime)
    assert bridge.ready_slots(Table({
        "phase6-morning": {
            "day": "2026-11-21", "phase": "completed",
            "warehouse_verified_at": 1795260000,
            "batch_id": "20261121T080000Z", "run_id": "jr_12345"
        }
    })) == [("morning", "2026-11-21", "20261121T080000Z")]


def test_false_warehouse_ack_fails_closed(monkeypatch):
    class FakeDatetime:
        @staticmethod
        def now(zone):
            return datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(bridge, "datetime", FakeDatetime)
    assert bridge.ready_slots(Table({
        "phase6-morning": {
            "day": "2026-11-21", "phase": "completed",
            "batch_id": "20261121T080000Z", "run_id": "jr_12345"
        }
    })) is None


def test_route_ready_requires_two_active_routes_and_available_nat():
    class EC2:
        def describe_route_tables(self, **kwargs):
            return {"RouteTables": [
                {"RouteTableId": f"rtb-{i}", "Routes": [{
                    "DestinationCidrBlock": "0.0.0.0/0",
                    "State": "active", "NatGatewayId": "nat-123"
                }]} for i in range(2)
            ]}
        def describe_nat_gateways(self, **kwargs):
            return {"NatGateways": [{"State": "available"}]}
    assert bridge.route_ready(EC2()) is True


def test_event_orchestrator_stays_disabled_on_old_image(monkeypatch):
    monkeypatch.setenv("DBT_LAUNCH_ENABLED", "false")
    result = bridge.reconcile(
        Table({}), SimpleNamespace(), "morning",
        "2026-11-21", "20261121T080000Z")
    assert result["phase7_dbt"] == "disabled_until_image_auth_grants_validated"


def test_future_nat_controller_tracks_warehouse_dbt_marker():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    nat = (root / "scripts/aws/phase6_nat_lifecycle_lambda.py").read_text()
    assert "awaiting_verified_dbt_build" in nat
    assert 'f"phase7-dbt:{dbt_slot}:{batch}"' in nat


def test_deployment_verifies_enabled_guarded_dbt_launch():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    deploy = (root / "scripts/aws/deploy_phase7_dbt_orchestrator.ps1").read_text()
    assert "DBT_LAUNCH_ENABLED -ne 'true'" in deploy
    assert 'ConvertTo-Json -InputObject @(' in deploy
    assert "put-targets" in deploy
    assert "enable-rule" in deploy
    assert "pinned image, networking and DynamoDB lock" in deploy
