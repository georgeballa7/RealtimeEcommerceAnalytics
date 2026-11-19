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
    assert command[-4:] == ['--run-id', 'live-20261119-morning','--count','20']
    assert '--apply' not in command

def test_afternoon_produces_25_and_needs_explicit_apply():
    timestamp = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    command = m.build_command('afternoon', timestamp, apply=True)
    assert command[-4:] == ['25','--apply','--confirm-run-id','live-20261119-afternoon']

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
