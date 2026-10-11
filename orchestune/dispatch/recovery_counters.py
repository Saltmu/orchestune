"""#1280: one rule for the Issue-body recovery counters (`recompute_count` /
`forced_serial`) of an execution.

The Issue body is the persistent budget (#1219); a launch, an attempt recovery
and a run-state recovery all start from it and may only keep or raise what the
execution already knows. The helpers are pure: the caller fetches the Issue.
"""

from __future__ import annotations

from dataclasses import replace

from orchestune.issue_parsing import recovery_counters_from_body
from orchestune.labels import StatusLabel
from orchestune.ledger.active_records import LaunchInfo
from orchestune.models import IssueRecord


def merged_recovery_counters(
    issue: IssueRecord,
    launch: LaunchInfo | None = None,
    *,
    task_forced_serial: bool = False,
) -> tuple[int, bool]:
    """count = max(known, body); forced_serial = known OR body OR label OR task.

    ``launch`` is what the execution already holds (active entry or claim
    reservation); ``task_forced_serial`` is the auxiliary task snapshot, which
    can only add a True. Missing / invalid body fields keep the parser's
    compatibility values (0 / False).
    """
    count, forced_serial = recovery_counters_from_body(issue.body)
    if launch is not None:
        count = max(count, launch.recompute_count)
        forced_serial = forced_serial or launch.forced_serial
    forced_serial = (
        forced_serial or task_forced_serial or StatusLabel.FORCE_SERIAL in issue.labels
    )
    return (count, forced_serial)


def launch_with_recovery_counters(
    launch: LaunchInfo,
    issue: IssueRecord,
    *,
    task_forced_serial: bool = False,
) -> LaunchInfo:
    """Return ``launch`` carrying the merged counters (other fields untouched)."""
    count, forced_serial = merged_recovery_counters(
        issue, launch, task_forced_serial=task_forced_serial
    )
    return replace(launch, recompute_count=count, forced_serial=forced_serial)
