"""Completion reservations protect all mutation readers before publication."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestune.claim.service import _execute_claim_in_lock
from orchestune.dispatch.execution_repair import revalidate_reclaim_preconditions
from orchestune.dispatch.gc import _resolve_completion
from orchestune.dispatch.gc_service import _is_standalone_gc_candidate
from orchestune.ledger.run_state import RunState
from tests.dispatch_test_support import flat_active_worktree


def _active(**fields):
    return flat_active_worktree(
        issue_number=10,
        branch="task-10",
        worktree_path="worktrees/task-10",
        pid=None,
        started_at=None,
        declared_footprint=(),
        **fields,
    )


def _pending(active=None):
    return RunState(
        active_worktrees={} if active is None else {"10": active},
        completion_reservations={"repo::10": {"issue_number": 10, "stage": "reserved"}},
    )


def test_dispatch_completion_cannot_bypass_issue_reservation():
    active = _active(owner_kind="interactive", completion_handoff_ready=True)
    ctx = SimpleNamespace(
        completion_reserved=lambda _: True,
        handoff_matches=lambda _: False,
        config=Mock(),
    )
    resolution = _resolve_completion(ctx, "10", active, None)
    assert resolution.state != "ready"


def test_standalone_gc_rejects_legacy_unverified_handoff():
    active = _active(owner_kind="interactive", completion_handoff_ready=True)
    assert not _is_standalone_gc_candidate(active)


def test_claim_checks_worktree_free_reservation_before_forge(monkeypatch):
    state = _pending()
    monkeypatch.setattr("orchestune.claim.service.load_run_state", lambda _: state)
    request = SimpleNamespace(issue_number=10, resume_claim_id=None)
    forge = Mock()
    outcome = _execute_claim_in_lock(
        request,
        "token",
        SimpleNamespace(run_state_path="state"),
        forge,
        True,
        None,
        "main",
    )
    assert not outcome.success
    assert "completion" in outcome.failure.message.lower()
    forge.get_issue.assert_not_called()


def test_zombie_reclaim_does_not_consume_pending_reservation():
    active = _active()
    command = SimpleNamespace(
        subject_id="10",
        parameters=(("finding_codes", ("execution.handleless-orphan",)),),
    )
    assert (
        revalidate_reclaim_preconditions(
            command,
            _pending(active),
            key="10",
            expected_active=active,
            expected_finding_codes=("execution.handleless-orphan",),
            config=Mock(),
            held_worktree_paths=frozenset(),
        )
        is None
    )


def test_status_repair_holds_before_any_forge_operation(monkeypatch):
    from orchestune.dispatch.status_repair import execute_status_repair_command

    monkeypatch.setattr(
        "orchestune.dispatch.status_repair.completion_mutation_blocked_fresh",
        lambda *_: True,
        raising=False,
    )
    command = SimpleNamespace(subject_id="10", code="status.primary-status-conflict")
    config = SimpleNamespace(apply=True, run_state_path="state", resolved_forge=Mock())
    result = execute_status_repair_command(
        command, {}, completion_evidence=Mock(), config=config
    )
    assert result.status.value == "skipped"
    assert "completion" in " ".join(result.diagnostics)
    assert config.resolved_forge.mock_calls == []


def test_recovery_requeue_holds_worktree_free_pending_reservation():
    from orchestune.consistency.repairs.execution import COMMAND_REQUEUE
    from orchestune.dispatch.recovery import execute_recovery_requeue_command

    command = SimpleNamespace(subject_id="10", code=COMMAND_REQUEUE)
    snapshot = SimpleNamespace(tasks_by_issue={}, restorations=())
    config = SimpleNamespace(apply=True, run_state_path="state", resolved_forge=Mock())
    result = execute_recovery_requeue_command(command, _pending(), snapshot, config)
    assert result.status.value == "skipped"
    assert "completion" in " ".join(result.diagnostics)
    assert config.resolved_forge.mock_calls == []


def test_stale_gc_discard_preserves_pending_reservation(monkeypatch):
    from orchestune.dispatch.gc import _apply_stale_active_entry_discard

    active = _active()
    state = _pending(active)
    cleanup = Mock(return_value=True)
    monkeypatch.setattr(
        "orchestune.dispatch.gc._cleanup_stale_active_worktree", cleanup
    )
    assert not _apply_stale_active_entry_discard(
        state,
        "10",
        active,
        "stale",
        SimpleNamespace(apply=True, run_state_path="state"),
    )
    cleanup.assert_not_called()
    assert state.active_worktrees["10"] is active


@pytest.mark.parametrize("writer", ["claim", "gc", "status-repair"])
def test_competing_writer_waits_for_completion_lock_and_holds_pending(
    tmp_path, monkeypatch, writer
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.journal import completion_journal_lock
    from orchestune.complete.service import complete_task
    from orchestune.dispatch.status_repair import execute_status_repair_command
    from orchestune.ledger.completion_reservations import (
        completion_mutation_blocked_fresh,
    )
    from orchestune.ledger.run_state import load_run_state_readonly

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    barrier, observed = Barrier(2), Event()

    def inject(operation, after):
        if operation == "post" and not after:
            barrier.wait(timeout=5)
            assert not observed.is_set()
            raise OSError("leave pending publication")

    forge.inject = inject

    def compete():
        barrier.wait(timeout=5)
        with completion_journal_lock(request.state_path, 5):
            state = load_run_state_readonly(request.state_path)
            observed.set()
            if writer == "claim":
                outcome = _execute_claim_in_lock(
                    SimpleNamespace(issue_number=1110, resume_claim_id=None),
                    "token",
                    SimpleNamespace(run_state_path=request.state_path),
                    Mock(),
                    True,
                    None,
                    "main",
                )
                return not outcome.success
            if writer == "status-repair":
                command = SimpleNamespace(
                    subject_id="1110", code="status.primary-status-conflict"
                )
                config = SimpleNamespace(
                    apply=True, run_state_path=request.state_path, resolved_forge=Mock()
                )
                outcome = execute_status_repair_command(
                    command, {}, completion_evidence=Mock(), config=config
                )
                assert config.resolved_forge.mock_calls == []
                return outcome.status.value == "skipped"
            context = SimpleNamespace(
                completion_reserved=lambda _: completion_mutation_blocked_fresh(
                    state, 1110, request.state_path
                ),
                handoff_matches=lambda _: False,
                config=Mock(),
            )
            return (
                _resolve_completion(
                    context, "1110", state.active_worktrees["1110"], None
                ).state
                != "ready"
            )

    with ThreadPoolExecutor(2) as executor:
        completion = executor.submit(complete_task, request, forge=forge)
        competitor = executor.submit(compete)
        assert not completion.result(timeout=10).success
        assert competitor.result(timeout=10)


def test_review_pending_holds_dependencies_until_durable_policy_is_applied(
    tmp_path, monkeypatch
):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.journal import (
        CompletionJournalRecord,
        completion_journal_lock,
    )
    from orchestune.complete.publication import update_downstream_policy_locked
    from orchestune.complete.service import complete_task
    from orchestune.ledger.completion_reservations import dependency_completion_blocked
    from orchestune.ledger.run_state import load_run_state_readonly, save_run_state

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    with completion_journal_lock(request.state_path):
        state = load_run_state_readonly(request.state_path)
        state.active_worktrees["1110"].launch = replace(
            state.active_worktrees["1110"].launch, external_id="cloud-task"
        )
        save_run_state(state, request.state_path)
    assert complete_task(request, forge=forge).success
    state = load_run_state_readonly(request.state_path)
    assert dependency_completion_blocked(state, 1110)
    record = CompletionJournalRecord.from_dict(
        next(iter(state.completion_journal.values()))
    )
    policy = replace(
        record.downstream_policy_records[0],
        status="applied",
        metadata={"approval": "verified"},
    )
    with completion_journal_lock(request.state_path):
        update_downstream_policy_locked(request.state_path, record, policy)
    state = load_run_state_readonly(request.state_path)
    assert not dependency_completion_blocked(state, 1110)
    assert (
        state.completion_journal[record.journal_key]
        == state.completion_replay_receipts[record.receipt_key]
    )


def test_standalone_gc_holds_pending_even_when_terminal_label_is_visible(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    forge.inject = (
        lambda operation, after: (_ for _ in ()).throw(OSError("POST unavailable"))
        if operation == "post" and not after
        else None
    )
    assert not complete_task(request, forge=forge).success
    forge.labels.add("status:not-needed")
    workspace = SimpleNamespace(
        run_state_path=request.state_path,
        worktree_root=tmp_path / "worktrees",
        repository_identity="repo",
        lock_path=request.state_path.with_suffix(".lock"),
    )
    monkeypatch.setattr(
        "orchestune.dispatch.gc_service.resolve_claim_workspace", lambda **_: workspace
    )
    before = request.state_path.read_bytes()
    result = run_handoff_gc(
        GcRequest(state_path=request.state_path),
        forge_factory=lambda: pytest.fail("pending GC consulted Forge"),
    )
    assert result.exit_code == 0
    assert not result.receipts
    assert result.items and result.items[0].action == "held"
    assert request.state_path.read_bytes() == before


def test_dependency_guard_reloads_reservation_created_after_snapshot(
    tmp_path, monkeypatch
):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task
    from orchestune.ledger.completion_reservations import (
        dependency_completion_blocked_fresh,
    )
    from orchestune.ledger.run_state import load_run_state_readonly

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    stale = load_run_state_readonly(request.state_path)
    forge.inject = (
        lambda operation, after: (_ for _ in ()).throw(OSError("POST unavailable"))
        if operation == "post" and not after
        else None
    )
    assert not complete_task(request, forge=forge).success
    assert stale.completion_reservations == {}
    assert dependency_completion_blocked_fresh(stale, 1110, request.state_path)
