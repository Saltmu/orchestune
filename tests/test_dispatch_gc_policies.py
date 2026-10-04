"""Durable downstream policy operations survive GC order and response loss."""

from unittest.mock import Mock

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.ledger.run_state import load_run_state_readonly
from orchestune.outcome_record import OutcomeRecord
from tests.dispatch_test_support import replace_flat
from tests.test_dispatch_gc_handoff_integration import (
    _create_repo,
    _make_active,
    _write_state,
)


def policy_case(tmp_path, result="blocked", reason="review-timeout", attempt=None):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    outcome = OutcomeRecord(
        result=result,
        pr=125 if result == "done" else None,
        issue=250,
        reason=reason if result == "blocked" else None,
        attempt=attempt,
        claim_id=active.claim.claim_id,
        completion_id=active.completion.completion_id,
        head_sha=active.claim.base_sha,
    )
    active = replace_flat(
        active,
        completion_result=result,
        completion_payload={"outcome": outcome.render()},
    )
    comment["body"] = outcome.render()
    path = _write_state(repo, active)
    forge = Mock()
    labels = [f"status:{result}"]
    comments = [comment]
    forge.get_issue_labels.side_effect = lambda issue: tuple(labels)
    forge.add_label.side_effect = lambda issue, label: labels.append(label)
    forge.remove_label.side_effect = (
        lambda issue, label: labels.remove(label) if label in labels else None
    )
    forge.list_all_issue_comments.side_effect = lambda issue: list(comments)
    forge.add_comment.side_effect = lambda issue, body: comments.append({"body": body})
    forge.get_issue_state.return_value = "OPEN"
    config = DispatcherConfig(
        parent_issue_number=1059,
        apply=True,
        run_state_path=path,
        worktree_root=repo / "worktrees",
        events_log_path=repo / "events.jsonl",
        log_dir=repo / "logs",
        not_needed_review_state_path=repo / "not_needed_review_state.json",
        forge=forge,
    )
    return load_run_state_readonly(path), config, forge, labels, comments


def test_retry_response_loss_consumes_one_slot_and_one_comment(tmp_path):
    from orchestune.dispatch.gc.policies import process_completion_policies

    state, config, forge, labels, comments = policy_case(tmp_path)

    def lost_response(issue, label):
        labels.append(label)
        raise OSError("response lost")

    forge.add_label.side_effect = lost_response
    process_completion_policies(state, config, now=100)
    persisted = load_run_state_readonly(config.run_state_path)
    assert persisted.task_reclaim_counts[250].review_timeout_retry_count == 1
    assert persisted.task_reclaim_counts[250].review_timeout_retry_pending
    forge.add_label.side_effect = lambda issue, label: labels.append(label)
    process_completion_policies(persisted, config, now=200)
    process_completion_policies(persisted, config, now=300)
    assert persisted.task_reclaim_counts[250].review_timeout_retry_count == 1
    assert not persisted.task_reclaim_counts[250].review_timeout_retry_pending
    assert labels == ["status:queued"]
    assert forge.add_label.call_count == 1
    assert forge.add_comment.call_count == 1
    assert persisted.completion_replay_receipts


def test_policy_runs_after_active_removal(tmp_path):
    from orchestune.dispatch.gc.policies import process_completion_policies
    from orchestune.infra.process_utils import run_state_lock
    from orchestune.ledger.run_state import save_run_state

    state, config, forge, labels, _ = policy_case(tmp_path)
    state.active_worktrees.clear()
    with run_state_lock(config.run_state_path.with_suffix(".lock")):
        save_run_state(state, config.run_state_path)
    process_completion_policies(state, config, now=100)
    assert labels == ["status:queued"]
    assert state.task_reclaim_counts[250].review_timeout_retry_count == 1


@pytest.mark.parametrize(
    "attempt,target", [(1, "status:blocked"), (3, "status:blocked-human-review")]
)
def test_base_red_applies_once(tmp_path, attempt, target):
    from orchestune.dispatch.gc.policies import process_completion_policies

    state, config, forge, labels, _ = policy_case(
        tmp_path, reason="base-branch-red", attempt=attempt
    )
    process_completion_policies(state, config)
    process_completion_policies(state, config)
    assert target in labels
    assert ("ci:base-branch-red" in labels) == (attempt < 3)
    assert forge.add_comment.call_count == 1


def test_save_failure_prevents_external_effects(tmp_path, monkeypatch):
    from orchestune.dispatch.gc.policies import process_completion_policies

    state, config, forge, _, _ = policy_case(tmp_path)
    monkeypatch.setattr(
        "orchestune.dispatch.gc.policies.save_run_state",
        Mock(side_effect=OSError("disk full")),
    )
    events = process_completion_policies(state, config)
    assert events[0].action == "completion_policy_hold"
    forge.add_label.assert_not_called()
    forge.add_comment.assert_not_called()
    assert 250 not in load_run_state_readonly(config.run_state_path).task_reclaim_counts


def test_retry_limit_escalates_without_consuming_extra_slot(tmp_path):
    from orchestune.dispatch.gc.policies import process_completion_policies
    from orchestune.infra.process_utils import run_state_lock
    from orchestune.ledger.run_state import TaskReclaimRecord, save_run_state

    state, config, forge, labels, _ = policy_case(tmp_path)
    state.task_reclaim_counts[250] = TaskReclaimRecord(review_timeout_retry_count=1)
    with run_state_lock(config.run_state_path.with_suffix(".lock")):
        save_run_state(state, config.run_state_path)
    process_completion_policies(state, config)
    process_completion_policies(state, config)
    assert labels == ["status:blocked-human-review"]
    assert state.task_reclaim_counts[250].review_timeout_retry_count == 1
    forge.add_comment.assert_called_once()


def test_close_response_loss_does_not_close_or_comment_twice(tmp_path):
    from orchestune.dispatch.gc.policies import process_completion_policies

    state, config, forge, _, _ = policy_case(tmp_path, result="not-needed")

    def close_then_fail(*args):
        forge.get_issue_state.return_value = "CLOSED"
        raise OSError("close response lost")

    forge.close_issue.side_effect = close_then_fail
    process_completion_policies(state, config)
    process_completion_policies(state, config)
    forge.close_issue.assert_called_once()
    forge.add_comment.assert_called_once()


def test_confirmed_records_skips_when_not_handoff_ready(tmp_path):
    from orchestune.dispatch.gc.policy_discovery import confirmed_records

    state, _, _, _, _ = policy_case(tmp_path)
    assert len(confirmed_records(state)) == 1

    # When completion_handoff_ready is False and stage is handed_off,
    # confirmed_records must skip the active entry.
    state.active_worktrees["250"] = replace_flat(
        state.active_worktrees["250"],
        completion_handoff_ready=False,
        completion_stage="handed_off",
    )
    assert confirmed_records(state) == []


def test_reserved_retry_is_not_recomputed_after_a_settings_change(tmp_path):
    """#1189: a reservation already saved keeps its count and time on restart."""
    from orchestune.dispatch.gc.policies import process_completion_policies

    state, config, forge, labels, _ = policy_case(tmp_path)

    def lost_response(issue, label):
        labels.append(label)
        raise OSError("response lost")

    forge.add_label.side_effect = lost_response
    process_completion_policies(state, config, now=100)
    reserved = load_run_state_readonly(config.run_state_path).task_reclaim_counts[250]
    assert (reserved.review_timeout_retry_count, reserved.review_timeout_retry_at) == (
        1,
        160.0,
    )

    forge.add_label.side_effect = lambda issue, label: labels.append(label)
    config.max_review_timeout_retries = 1  # would now be exhausted if re-evaluated
    config.review_timeout_backoff_seconds = 999
    persisted = load_run_state_readonly(config.run_state_path)
    process_completion_policies(persisted, config, now=500)

    record = persisted.task_reclaim_counts[250]
    assert record.review_timeout_retry_count == 1
    assert record.review_timeout_retry_at == 160.0
    assert not record.review_timeout_retry_pending
    assert "status:queued" in labels
