"""Shared names for facts exchanged inside the consistency kernel.

This is the sole owner of observation and desired-state fact names.  Producers
and consumers import from here so the observer/invariant seam remains explicit
and mechanically testable.
"""

from __future__ import annotations

from orchestune.consistency.models import ConsistencyScope

# Observed repository facts.
FACT_EXECUTION_COUNT = "execution_count"
FACT_FORGE_REACHABLE = "forge_reachable"
FACT_ISSUE_COUNT = "issue_count"
FACT_PULL_REQUEST_COUNT = "pull_request_count"

# Observed parent facts.
FACT_CHILD_ISSUE_NUMBERS = "child_issue_numbers"
FACT_PARENT_STATE = "parent_state"

# Observed task facts.
FACT_BRANCH_EXISTS = "branch_exists"
FACT_BRANCH_NAME = "branch_name"
FACT_EXECUTION_EXTERNAL_ID = "execution_external_id"
FACT_EXECUTION_EXTERNAL_STATUS = "execution_external_status"
FACT_EXECUTION_KIND = "execution_kind"
FACT_EXECUTION_PID = "execution_pid"
FACT_EXECUTION_PROCESS_ALIVE = "execution_process_alive"
FACT_ISSUE_LABELS = "issue_labels"
FACT_ISSUE_NUMBER = "issue_number"
FACT_ISSUE_STATE = "issue_state"
FACT_ISSUE_STATUS_LABELS = "issue_status_labels"
FACT_PARENT_ISSUE_NUMBER = "parent_issue_number"
FACT_PULL_REQUEST_BASE_REF = "pull_request_base_ref"
FACT_PULL_REQUEST_HEAD_REF = "pull_request_head_ref"
FACT_PULL_REQUEST_NUMBER = "pull_request_number"
FACT_PULL_REQUEST_STATE = "pull_request_state"
FACT_WORKTREE_EXISTS = "worktree_exists"
FACT_WORKTREE_PATH = "worktree_path"

# Desired repository facts.
DESIRED_ACTIVE_COUNT = "dispatch.active_count"
DESIRED_AVAILABLE_SLOTS = "dispatch.available_slots"
DESIRED_FORCED_SERIAL_ACTIVE = "dispatch.forced_serial_active"
DESIRED_MAX_CONCURRENT = "dispatch.max_concurrent"

# Desired task facts.
DESIRED_DEPENDENCIES_RESOLVED = "task.dependencies_resolved"
DESIRED_DISPATCH_ELIGIBLE = "task.dispatch_eligible"
DESIRED_RUN_STATE_ACTIVE = "task.run_state_active"
DESIRED_STATUS_LABEL = "task.status_label"
DESIRED_UNRESOLVED_DEPENDENCIES = "task.unresolved_dependencies"

OBSERVED_FACT_NAMES_BY_SCOPE = {
    ConsistencyScope.REPOSITORY: frozenset(
        {
            FACT_EXECUTION_COUNT,
            FACT_FORGE_REACHABLE,
            FACT_ISSUE_COUNT,
            FACT_PULL_REQUEST_COUNT,
        }
    ),
    ConsistencyScope.PARENT: frozenset(
        {
            FACT_CHILD_ISSUE_NUMBERS,
            FACT_PARENT_ISSUE_NUMBER,
            FACT_PARENT_STATE,
        }
    ),
    ConsistencyScope.TASK: frozenset(
        {
            FACT_BRANCH_EXISTS,
            FACT_BRANCH_NAME,
            FACT_EXECUTION_EXTERNAL_ID,
            FACT_EXECUTION_EXTERNAL_STATUS,
            FACT_EXECUTION_KIND,
            FACT_EXECUTION_PID,
            FACT_EXECUTION_PROCESS_ALIVE,
            FACT_ISSUE_LABELS,
            FACT_ISSUE_NUMBER,
            FACT_ISSUE_STATE,
            FACT_ISSUE_STATUS_LABELS,
            FACT_PARENT_ISSUE_NUMBER,
            FACT_PULL_REQUEST_BASE_REF,
            FACT_PULL_REQUEST_HEAD_REF,
            FACT_PULL_REQUEST_NUMBER,
            FACT_PULL_REQUEST_STATE,
            FACT_WORKTREE_EXISTS,
            FACT_WORKTREE_PATH,
        }
    ),
}

DESIRED_FACT_NAMES_BY_SCOPE = {
    ConsistencyScope.REPOSITORY: frozenset(
        {
            DESIRED_ACTIVE_COUNT,
            DESIRED_AVAILABLE_SLOTS,
            DESIRED_FORCED_SERIAL_ACTIVE,
            DESIRED_MAX_CONCURRENT,
        }
    ),
    ConsistencyScope.TASK: frozenset(
        {
            DESIRED_DEPENDENCIES_RESOLVED,
            DESIRED_DISPATCH_ELIGIBLE,
            DESIRED_RUN_STATE_ACTIVE,
            DESIRED_STATUS_LABEL,
            DESIRED_UNRESOLVED_DEPENDENCIES,
        }
    ),
}

__all__ = [name for name in globals() if name.startswith(("FACT_", "DESIRED_"))] + [
    "DESIRED_FACT_NAMES_BY_SCOPE",
    "OBSERVED_FACT_NAMES_BY_SCOPE",
]
