"""One-step AWS RDS controller, invoked every minute by EventBridge Scheduler.

Webhook intake never calls RDS. One Lambda invocation takes a short DynamoDB
lease, reads a durable pending-event index, and advances the DB/worker lifecycle.
The next invocation continues after RDS or ECS asynchronous state changes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4


class ControllerBusy(RuntimeError):
    pass


class ProjectionFailed(RuntimeError):
    pass


class RdsController:
    def __init__(self, *, db, ecs, index, control, db_identifier, cluster,
                 task_definition, subnets, security_groups, idle_minutes=30,
                 automatic_stop_enabled=False):
        if idle_minutes < 10:
            raise ValueError("RDS idle window must be at least 10 minutes")
        self.db, self.ecs, self.index, self.control = db, ecs, index, control
        self.db_identifier, self.cluster = db_identifier, cluster
        self.task_definition, self.subnets, self.security_groups = task_definition, subnets, security_groups
        self.idle = timedelta(minutes=idle_minutes)
        self.automatic_stop_enabled = automatic_stop_enabled

    def _lock(self, now):
        from botocore.exceptions import ClientError
        token = str(uuid4())
        expires = int(now.timestamp()) + 50
        try:
            self.control.update_item(
                Key={"id": "phase5-controller"},
                UpdateExpression="SET lease_token=:token, lease_until=:expiry",
                ConditionExpression="attribute_not_exists(lease_until) OR lease_until < :now",
                ExpressionAttributeValues={":token": token, ":expiry": expires,
                                           ":now": int(now.timestamp())},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ControllerBusy("Another controller invocation owns the lease") from exc
            raise
        return token

    def _unlock(self, token):
        self.control.update_item(
            Key={"id": "phase5-controller"},
            UpdateExpression="REMOVE lease_token, lease_until",
            ConditionExpression="lease_token = :token",
            ExpressionAttributeValues={":token": token},
        )

    def _state(self):
        return self.control.get_item(Key={"id": "phase5-controller"}, ConsistentRead=True).get("Item", {})

    def _set(self, **fields):
        names = {f"#f{i}": key for i, key in enumerate(fields)}
        values = {f":v{i}": value for i, value in enumerate(fields.values())}
        assignments = [f"{k} = :v{i}" for i, k in enumerate(names)]
        self.control.update_item(
            Key={"id": "phase5-controller"},
            UpdateExpression="SET " + ", ".join(assignments),
            ExpressionAttributeNames=names, ExpressionAttributeValues=values,
        )

    def _remove(self, *keys):
        names = {f"#f{i}": key for i, key in enumerate(keys)}
        self.control.update_item(Key={"id": "phase5-controller"},
                                 UpdateExpression="REMOVE " + ", ".join(names),
                                 ExpressionAttributeNames=names)

    def _pending(self):
        return bool(self.index.query(
            IndexName="by_state", KeyConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":pending": "pending"}, Limit=1,
        ).get("Items"))

    def _rds_status(self):
        rows = self.db.describe_db_instances(DBInstanceIdentifier=self.db_identifier)["DBInstances"]
        if len(rows) != 1:
            raise RuntimeError("Expected one configured RDS instance")
        return rows[0]["DBInstanceStatus"]

    def _worker(self, task_arn):
        tasks = self.ecs.describe_tasks(cluster=self.cluster, tasks=[task_arn])["tasks"]
        if len(tasks) != 1:
            raise RuntimeError("Tracked ECS worker disappeared")
        task = tasks[0]
        if task["lastStatus"] != "STOPPED":
            return "running"
        containers = task.get("containers", [])
        if (not containers or any(c.get("exitCode") != 0 for c in containers)
                or task.get("stopCode") == "TaskFailedToStart"):
            raise ProjectionFailed("ECS projection task failed; RDS stays available")
        return "complete"

    def advance(self, now=None):
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("UTC-aware controller clock required")
        token = self._lock(now)
        try:
            state = self._state()
            if state.get("error_hold"):
                return "error_hold"
            active = state.get("worker_task_arn")
            if active:
                if self._worker(active) == "running":
                    return "worker_running"
                self._remove("worker_task_arn", "launch_token")
                self._set(last_work_at=now.isoformat())
                state = self._state()
            db_status = self._rds_status()
            pending = self._pending()
            if db_status == "stopped":
                if pending:
                    self.db.start_db_instance(DBInstanceIdentifier=self.db_identifier)
                    self._remove("idle_since") if state.get("idle_since") else None
                    return "rds_start_requested"
                return "rds_stopped_idle"
            if db_status in {"starting", "stopping", "backing-up", "modifying"}:
                return "rds_transitioning"
            if db_status != "available":
                raise RuntimeError(f"Unexpected RDS status {db_status}; no unsafe transition")
            if pending:
                # No pre-existing worker may run. A single controller invocation
                # has the DynamoDB lease, preventing concurrent launch attempts.
                launch_token = state.get("launch_token")
                if not launch_token:
                    launch_token = str(uuid4())
                    self._set(launch_token=launch_token)
                r = self.ecs.run_task(
                    cluster=self.cluster, taskDefinition=self.task_definition, clientToken=launch_token,
                    launchType="FARGATE", count=1,
                    networkConfiguration={"awsvpcConfiguration": {
                        "subnets": self.subnets, "securityGroups": self.security_groups,
                        "assignPublicIp": "DISABLED"}},
                )
                if r.get("failures") or len(r.get("tasks", [])) != 1:
                    raise RuntimeError("ECS projection launch failed")
                self._set(worker_task_arn=r["tasks"][0]["taskArn"])
                if state.get("idle_since"):
                    self._remove("idle_since")
                return "worker_launched"
            # A fresh landing may have updated last_event_at while a previous
            # GSI result is still stale. Never stop until 30 min after that.
            activity = state.get("last_event_at")
            idle_since = state.get("idle_since")
            if not idle_since:
                self._set(idle_since=now.isoformat())
                return "idle_window_started"
            quiet_from = datetime.fromisoformat(idle_since)
            if activity:
                quiet_from = max(quiet_from, datetime.fromisoformat(activity))
            if now - quiet_from < self.idle:
                return "idle_grace"
            # Automatic stopping is opt-in only after reviewing every RDS client.
            if not self.automatic_stop_enabled:
                return "automatic_stop_disabled"
            # Fail closed for other running ECS workloads; RDS may be shared.
            # This cannot detect manual clients outside ECS and must be combined
            # with an operator-approved RDS exclusivity policy before enablement.
            for desired in ("RUNNING", "PENDING"):
                other = self.ecs.list_tasks(cluster=self.cluster, desiredStatus=desired)
                if other.get("taskArns"):
                    return "other_ecs_workloads_running"
            # Refresh the strongly consistent state immediately before stopping;
            # an event arriving after this can still be replayed after shutdown.
            refreshed = self._state()
            if (refreshed.get("worker_task_arn")
                    or refreshed.get("last_event_at") != activity
                    or refreshed.get("error_hold")):
                return "activity_changed"
            if self._pending():
                return "pending_detected"
            self.db.stop_db_instance(DBInstanceIdentifier=self.db_identifier)
            self._remove("idle_since")
            return "rds_stop_requested"
        except ProjectionFailed:
            self._set(error_hold="projection_failure")
            raise
        finally:
            self._unlock(token)
