"""Fresh executor transition-table tripwire, independent of rejection guards."""

import logging

import pytest

from orchestune.consistency.models import ConsistencyScope, RepairCommand, RepairStatus
from orchestune.consistency.repairs.status import (
    COMMAND_ADD_LABEL,
    COMMAND_REMOVE_LABEL,
    COMMAND_TRANSITION_LABEL,
)
from orchestune.dispatch.status_repair import execute_status_repair_command
from tests.conftest import make_issue, make_task
from tests.test_consistency_status_repair import _config, _evidence


@pytest.mark.parametrize(
    "labels,target,code,warning",
    [
        (("status:done",), "status:queued", COMMAND_TRANSITION_LABEL, True),
        (
            ("status:queued",),
            "status:manual-merge-required",
            COMMAND_TRANSITION_LABEL,
            True,
        ),
        (("status:queued",), "status:blocked", COMMAND_TRANSITION_LABEL, False),
        (("status:queued",), "status:queued", COMMAND_TRANSITION_LABEL, False),
        ((), "status:queued", COMMAND_ADD_LABEL, False),
        (
            ("status:done", "status:queued"),
            "status:queued",
            COMMAND_REMOVE_LABEL,
            False,
        ),
        (
            ("status:done", "status:queued"),
            "status:blocked",
            COMMAND_TRANSITION_LABEL,
            False,
        ),
        (("status:queued",), "status:force-serial", COMMAND_TRANSITION_LABEL, False),
    ],
)
def test_warning_uses_single_fresh_lifecycle(
    tmp_path, in_memory_forge, caplog, labels, target, code, warning
):
    in_memory_forge.seed_issue(make_issue(708, labels=labels))
    parameters = (
        (("new_label", target), ("old_labels", labels))
        if code == COMMAND_TRANSITION_LABEL
        else (("label", "status:done" if code == COMMAND_REMOVE_LABEL else target),)
    )
    preconditions = (
        (f"retains-primary-status:{target}",) if code == COMMAND_REMOVE_LABEL else ()
    )
    command = RepairCommand(
        code=code,
        scope=ConsistencyScope.TASK,
        subject_id="708",
        idempotency_key="warning-test",
        parameters=parameters,
        preconditions=preconditions,
    )
    with caplog.at_level(logging.WARNING, logger="orchestune.dispatch.status_repair"):
        result = execute_status_repair_command(
            command,
            {708: make_task(708, status_labels=labels)},
            completion_evidence=_evidence(),
            config=_config(tmp_path, in_memory_forge),
        )
    warnings = [
        r for r in caplog.records if "status.repair-illegal-transition" in r.message
    ]
    assert bool(warnings) is warning
    if warning:
        assert warnings[0].levelno == logging.WARNING
        assert all(
            s in warnings[0].message
            for s in (
                "issue=708",
                f"command={code}",
                f"source={labels[0]}",
                f"target={target}",
            )
        )
    if labels == ("status:done",):
        assert result.status is RepairStatus.SKIPPED
        assert in_memory_forge.get_issue_labels(708) == labels
    if target == "status:manual-merge-required":
        assert (
            result.status is RepairStatus.APPLIED
        )  # logging does not enforce the table
