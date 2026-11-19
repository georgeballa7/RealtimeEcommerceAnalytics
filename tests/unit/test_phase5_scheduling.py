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
