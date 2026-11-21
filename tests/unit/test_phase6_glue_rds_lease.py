"""Unit tests for safe Glue lease arbitration; no AWS or Terraform calls."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from scripts.aws.phase6_glue_rds_lease import (
    LeaseBusy, attach_run, is_safe_to_release, release, renew, reserve,
    TTL_SECONDS,
)


def conditional_failure():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}},
        "UpdateItem",
    )


def test_reservation_uses_controller_mutex_and_forbids_stealing_expired_run():
    table = MagicMock()
    owner = reserve(table, now=1000, owner="batch-1")
    assert owner == "batch-1"
    kwargs = table.update_item.call_args.kwargs
    condition = kwargs["ConditionExpression"]
    assert "lease_until" in condition
    assert "attribute_not_exists(glue_lease_owner)" in condition
    assert kwargs["ExpressionAttributeValues"][":expiry"] == 1000 + TTL_SECONDS
    table.update_item.side_effect = conditional_failure()
    with pytest.raises(LeaseBusy):
        reserve(table, now=1000, owner="batch-2")


def test_attach_requires_original_owner_and_unexpired_reservation():
    table = MagicMock()
    attach_run(table, owner="batch-1", run_id="jr_123", now=1000)
    condition = table.update_item.call_args.kwargs["ConditionExpression"]
    assert "glue_lease_owner=:owner" in condition
    assert "attribute_not_exists(glue_lease_job_run_id)" in condition
    with pytest.raises(ValueError):
        attach_run(table, owner="batch-1", run_id="not-a-glue-id", now=1000)


def test_renew_and_release_are_owner_conditional():
    table = MagicMock()
    renew(table, owner="batch-1", now=1000)
    assert "glue_lease_expires_at > :now" in (
        table.update_item.call_args.kwargs["ConditionExpression"]
    )
    release(table, owner="batch-1")
    assert table.update_item.call_args.kwargs["ConditionExpression"] == (
        "glue_lease_owner=:owner"
    )


@pytest.mark.parametrize("status,expected", [
    ("STARTING", False), ("RUNNING", False), ("STOPPING", False),
    ("WAITING", False), ("SUCCEEDED", True), ("FAILED", True),
    ("TIMEOUT", True), ("STOPPED", True),
])
def test_release_only_after_verified_terminal_glue_run(status, expected):
    table, glue = MagicMock(), MagicMock()
    table.get_item.return_value = {
        "Item": {"glue_lease_owner": "batch-1", "glue_lease_job_run_id": "jr_123"}
    }
    glue.get_job_run.return_value = {"JobRun": {"JobRunState": status}}
    assert is_safe_to_release(table, glue, owner="batch-1") is expected


def test_missing_run_id_refuses_automatic_release():
    table, glue = MagicMock(), MagicMock()
    table.get_item.return_value = {"Item": {"glue_lease_owner": "batch-1"}}
    assert not is_safe_to_release(table, glue, owner="batch-1")
    glue.get_job_run.assert_not_called()


def test_another_owner_cannot_release():
    table, glue = MagicMock(), MagicMock()
    table.get_item.return_value = {"Item": {"glue_lease_owner": "another-batch"}}
    with pytest.raises(LeaseBusy):
        is_safe_to_release(table, glue, owner="batch-1")
