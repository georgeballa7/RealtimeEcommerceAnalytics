import importlib.util
from datetime import datetime, timezone
from pathlib import Path

file = Path(__file__).parents[2] / 'scripts/phase5_slot_runner.py'
spec = importlib.util.spec_from_file_location('slot_runner', file)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def test_morning_deterministic_in_berlin():
    timestamp = datetime(2026, 11, 19, 7, 0, tzinfo=timezone.utc)
    command = m.build_command('morning', timestamp)
    assert command[-4:] == ['--run-id', 'live-20261119-morning','--count','100']
    assert '--apply' not in command

def test_afternoon_produces_150_and_needs_explicit_apply():
    timestamp = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    command = m.build_command('afternoon', timestamp, apply=True)
    assert command[-4:] == ['150','--apply','--confirm-run-id','live-20261119-afternoon']

def test_dst_time_uses_berlin_date_not_utc():
    timestamp = datetime(2027, 6, 1, 22, 30, tzinfo=timezone.utc)
    command = m.build_command('morning', timestamp)
    assert 'live-20270602-morning' in command


def test_isolated_smoke_creates_only_one_order():
    from datetime import datetime, timezone
    from scripts.phase5_slot_runner import build_command
    cmd=build_command("morning",datetime(2026,11,19,tzinfo=timezone.utc),apply=True,smoke_run_id="smoke-abc123-20261119")
    assert cmd[cmd.index("--count")+1] == "1"
    assert "smoke-abc123-20261119" in cmd


def test_production_slot_requires_schema_and_webhook_readiness(monkeypatch):
    from scripts.phase5_slot_runner import assert_deployment_ready
    from unittest.mock import MagicMock
    import sys, types, pytest
    fake=types.SimpleNamespace(resource=MagicMock())
    monkeypatch.setitem(sys.modules,"boto3",fake)
    monkeypatch.setenv("PHASE5_CONTROLLER_TABLE","test-controller")
    table=fake.resource.return_value.Table.return_value
    table.get_item.return_value={"Item":{"schema_ready":True,"webhooks_ready":False}}
    with pytest.raises(RuntimeError): assert_deployment_ready()
    table.get_item.return_value={"Item":{"schema_ready":True,"webhooks_ready":True}}
    assert_deployment_ready()


def test_production_slot_counts_are_bounded_and_distinct():
    from scripts.phase5_slot_runner import build_command
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    morning = build_command('morning', now, apply=True)
    afternoon = build_command('afternoon', now, apply=True)
    assert morning[morning.index('--count') + 1] == '100'
    assert afternoon[afternoon.index('--count') + 1] == '150'
    assert morning[morning.index('--run-id') + 1] != afternoon[afternoon.index('--run-id') + 1]
    assert morning[-2:] == ['--confirm-run-id', 'live-20261120-morning']
    assert afternoon[-2:] == ['--confirm-run-id', 'live-20261120-afternoon']
