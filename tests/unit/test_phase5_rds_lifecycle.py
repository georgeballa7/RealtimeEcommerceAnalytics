from datetime import datetime,timedelta,timezone
from dataclasses import replace
from ecommerce.application.use_cases.rds_lifecycle import RdsObservation,may_start,may_stop

NOW=datetime(2026,11,19,20,0,tzinfo=timezone.utc)
BASE=RdsObservation(status="available",pending_s3_events=0,workers_running=0,
                    latest_event_at=NOW-timedelta(minutes=40),
                    last_completed_at=NOW-timedelta(minutes=35),controller_lock_held=True)


def test_database_starts_only_with_pending_events_and_serialization():
    assert may_start(replace(BASE,status="stopped",pending_s3_events=1))
    assert not may_start(replace(BASE,status="stopped",pending_s3_events=1,controller_lock_held=False))
    assert not may_start(replace(BASE,status="stopped",pending_s3_events=0))


def test_no_stop_while_events_arrive_or_workers_run():
    assert may_stop(BASE,NOW)
    assert not may_stop(replace(BASE,pending_s3_events=1),NOW)
    assert not may_stop(replace(BASE,workers_running=1),NOW)
    assert not may_stop(replace(BASE,latest_event_at=NOW-timedelta(minutes=5)),NOW)
    assert not may_stop(replace(BASE,controller_lock_held=False),NOW)
    assert not may_stop(replace(BASE,last_completed_at=None),NOW)
