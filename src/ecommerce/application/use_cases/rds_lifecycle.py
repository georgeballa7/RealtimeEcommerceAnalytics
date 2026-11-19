"""Fail-closed RDS start/stop decision policy for the Phase 5 controller.

The actual orchestration must serialize transitions via a durable distributed
lock. This module deliberately never invokes StartDBInstance/StopDBInstance
by itself; a workflow should call the guarded decisions and then RDS APIs.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class RdsObservation:
    status: str
    pending_s3_events: int
    workers_running: int
    latest_event_at: datetime | None
    last_completed_at: datetime | None
    controller_lock_held: bool


def may_start(obs: RdsObservation) -> bool:
    """A new unprocessed event can wake an idle RDS instance once."""
    if obs.pending_s3_events < 0 or obs.workers_running < 0:
        raise ValueError("Negative backlog or worker count")
    return obs.status == "stopped" and obs.pending_s3_events > 0 and obs.controller_lock_held


def may_stop(obs: RdsObservation, now: datetime, idle_minutes: int = 30) -> bool:
    """Never stop on missing telemetry or potentially unprocessed events.

    Source event timestamps and worker-completion time must both be known;
    the distributed lock must still be held at the moment of the stop call.
    Recheck them after waiting and immediately before stopping RDS.
    """
    if idle_minutes < 10:
        raise ValueError("Idle threshold cannot be shorter than 10 minutes")
    if not obs.controller_lock_held or obs.status != "available":
        return False
    if obs.pending_s3_events != 0 or obs.workers_running != 0:
        return False
    if obs.latest_event_at is None or obs.last_completed_at is None:
        return False
    for stamp in (now, obs.latest_event_at, obs.last_completed_at):
        if stamp.tzinfo is None:
            raise ValueError("Controller timestamps must be timezone-aware")
    quiet_since = max(obs.latest_event_at, obs.last_completed_at)
    return now >= quiet_since + timedelta(minutes=idle_minutes)
