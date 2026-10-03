"""Both worktree bookkeeping and attempt reconciliation honor actual releases."""

from dataclasses import replace

from orchestune.consistency.models import ConsistencyScope, RepairCommand
from orchestune.dispatch.attempt_record import LaunchAttempt
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.launch_attempts import reconcile_attempt
from orchestune.dispatch.recovery import (
    RecoveryBookkeepingSnapshot,
    _apply_missing_entry_bookkeeping,
)
from orchestune.ledger.run_state import load_run_state_readonly
from orchestune.recovery.service import recover_claim
from tests.test_recovery_external_stop import external_claim as _external_claim

external_claim = _external_claim
pytest_plugins = ["tests.test_local_claim_identity"]


def test_release_prevents_both_restoration_routes(
    external_claim, fake_forge, monkeypatch
):
    workspace, active, _, request = external_claim
    assert recover_claim(
        replace(request, apply=True), cwd=workspace.repository_root
    ).success
    state = load_run_state_readonly(workspace.run_state_path)
    config = DispatcherConfig(
        parent_issue_number=0,
        apply=True,
        forge=fake_forge,
        run_state_path=workspace.run_state_path,
        events_log_path=workspace.repository_root / "events.jsonl",
    )
    snapshot = RecoveryBookkeepingSnapshot({}, (), (("7", "task", active),), (), ())
    monkeypatch.setattr("orchestune.dispatch.recovery._restorable", lambda _: True)
    command = RepairCommand(
        code="execution.update-bookkeeping",
        scope=ConsistencyScope.TASK,
        subject_id="7",
        idempotency_key="bookkeeping:7",
    )
    result = _apply_missing_entry_bookkeeping(command, state, snapshot, config)
    assert result.diagnostics == ("claim generation was explicitly released",)
    from tests.dispatch_gc_test_support import _task

    task = replace(_task(), issue_number=7)
    attempt = LaunchAttempt(
        "attempt", "launched", "codex-cloud", active.core.branch, "main", 10.0, "run"
    )
    assert reconcile_attempt(attempt, task, state, config)
    assert not state.active_worktrees
    fake_forge.get_issue_labels.assert_not_called()
    fake_forge.add_comment.assert_not_called()
