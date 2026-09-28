"""Journal proof gates, review launch recovery and the two physical GC paths."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from orchestune.dispatch.gc.policies import process_completion_policies
from orchestune.dispatch.gc.policy_discovery import confirmed_records, update_record
from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.completion_reservations import dependency_completion_blocked
from orchestune.ledger.run_state import load_run_state_readonly, save_run_state
from orchestune.targets.contracts import DispatchHandle
from tests.test_dispatch_gc_policies import policy_case


def make_review(state, config):
    record = confirmed_records(state)[0]
    # Missing/local-worktree-free context requires independent review.
    record = replace(record, prepublication_policy_evidence={"context": {}})
    update_record(state, record)
    with run_state_lock(config.run_state_path.with_suffix(".lock")):
        save_run_state(state, config.run_state_path)
    target = Mock()
    target.fire_text.return_value = DispatchHandle(external_id="review-session")
    target.lookup_launch_attempt.return_value = None
    config.dispatch_target = target
    return target


def test_unknown_review_launch_holds_without_duplicate(tmp_path):
    state, config, forge, _, _ = policy_case(tmp_path, result="not-needed")
    target = make_review(state, config)
    target.fire_text.side_effect = OSError("response lost")
    process_completion_policies(state, config, now=100)
    process_completion_policies(state, config, now=200)
    target.fire_text.assert_called_once()
    assert dependency_completion_blocked(state, 250)
    forge.close_issue.assert_not_called()
    assert state.completion_replay_receipts


@pytest.mark.parametrize("effect", ["label", "comment", "save"])
def test_unknown_review_launch_escalates_once_after_timeout(
    tmp_path, monkeypatch, effect
):
    from orchestune.dispatch.gc import policies

    state, config, forge, labels, comments = policy_case(tmp_path, result="not-needed")
    target = make_review(state, config)
    target.fire_text.side_effect = OSError("response lost")
    config.not_needed_review_timeout_seconds = 10
    process_completion_policies(state, config, now=100)
    process_completion_policies(state, config, now=109)
    assert labels == ["status:not-needed"]
    real_save = policies.save_run_state

    if effect == "label":

        def lose_label(issue, label):
            labels.append(label)
            raise OSError("label response lost")

        forge.add_label.side_effect = lose_label
    elif effect == "comment":

        def lose_comment(issue, body):
            comments.append({"body": body})
            raise OSError("comment response lost")

        forge.add_comment.side_effect = lose_comment
    else:

        def lose_save(current, path):
            policy = confirmed_records(current)[0].downstream_policy_records[0]
            if policy.metadata.get("review_timed_out"):
                raise OSError("save failed")
            real_save(current, path)

        monkeypatch.setattr(policies, "save_run_state", lose_save)

    process_completion_policies(state, config, now=110)
    forge.add_label.side_effect = lambda issue, label: labels.append(label)
    forge.add_comment.side_effect = lambda issue, body: comments.append({"body": body})
    monkeypatch.setattr(policies, "save_run_state", real_save)
    process_completion_policies(state, config, now=111)
    process_completion_policies(state, config, now=112)
    target.fire_text.assert_called_once()
    assert labels == ["status:blocked-human-review"]
    assert len(comments) == 2
    assert "起動結果" in comments[-1]["body"]
    persisted = load_run_state_readonly(config.run_state_path)
    policy = confirmed_records(persisted)[0].downstream_policy_records[0]
    assert policy.metadata["review_timed_out"]
    assert policy.status == "pending"
    assert dependency_completion_blocked(persisted, 250)
    forge.close_issue.assert_not_called()


def test_review_approval_required_before_close_and_dependency(tmp_path):
    state, config, forge, _, comments = policy_case(tmp_path, result="not-needed")
    target = make_review(state, config)
    process_completion_policies(state, config, now=100)
    process_completion_policies(state, config, now=101)
    target.fire_text.assert_called_once()
    assert dependency_completion_blocked(state, 250)
    record = confirmed_records(state)[0]
    policy = record.downstream_policy_records[0]
    comments.append(
        {
            "body": f"<!-- orchestune:policy-review {policy.metadata['operation_id']} verdict=passed -->"
        }
    )
    forge.close_issue.side_effect = lambda *args: setattr(
        forge.get_issue_state, "return_value", "CLOSED"
    )
    process_completion_policies(state, config, now=102)
    process_completion_policies(state, config, now=103)
    forge.close_issue.assert_called_once()
    assert not dependency_completion_blocked(state, 250)


@pytest.mark.parametrize("verdict", ["timeout", "failed"])
def test_unapproved_review_removes_not_needed_label_without_unlocking(
    tmp_path, verdict
):
    state, config, forge, labels, comments = policy_case(tmp_path, result="not-needed")
    make_review(state, config)
    config.not_needed_review_timeout_seconds = 10
    process_completion_policies(state, config, now=100)
    policy = confirmed_records(state)[0].downstream_policy_records[0]
    if verdict == "failed":
        comments.append(
            {
                "body": f"<!-- orchestune:policy-review {policy.metadata['operation_id']} verdict=failed -->"
            }
        )
    process_completion_policies(state, config, now=111)
    process_completion_policies(state, config, now=112)
    assert labels == [
        "status:queued" if verdict == "failed" else "status:blocked-human-review"
    ]
    assert dependency_completion_blocked(state, 250)
    forge.close_issue.assert_not_called()


def test_missing_receipt_and_pending_label_never_mutate(tmp_path):
    state, config, forge, _, _ = policy_case(tmp_path)
    state.completion_replay_receipts.clear()
    with run_state_lock(config.run_state_path.with_suffix(".lock")):
        save_run_state(state, config.run_state_path)
    assert not process_completion_policies(state, config)
    forge.add_label.assert_not_called()
    record = next(iter(state.completion_journal.values()))
    record["stage"] = "label_confirmed"
    with run_state_lock(config.run_state_path.with_suffix(".lock")):
        save_run_state(state, config.run_state_path)
    assert not process_completion_policies(state, config)
    forge.add_comment.assert_not_called()


def test_new_claim_never_replays_old_policy(tmp_path):
    state, config, forge, _, _ = policy_case(tmp_path)
    state.active_worktrees["250"] = replace(
        state.active_worktrees["250"], claim_id="new-generation"
    )
    with run_state_lock(config.run_state_path.with_suffix(".lock")):
        save_run_state(state, config.run_state_path)
    assert not process_completion_policies(state, config)
    forge.add_label.assert_not_called()


def test_gc_persists_pending_review_before_physical_release(tmp_path, monkeypatch):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    state, config, forge, _, _ = policy_case(tmp_path, result="not-needed")
    make_review(state, config)
    monkeypatch.chdir(config.worktree_root.parent)
    result = run_handoff_gc(
        GcRequest(state_path=config.run_state_path), forge_factory=lambda: forge
    )
    assert result.exit_code == 0
    persisted = load_run_state_readonly(config.run_state_path)
    assert "250" not in persisted.active_worktrees
    assert persisted.completion_replay_receipts
    assert dependency_completion_blocked(persisted, 250)
    assert confirmed_records(persisted)[0].downstream_policy_records
    forge.close_issue.assert_not_called()


def test_two_stale_gc_snapshots_do_not_consume_retry_twice(tmp_path):
    state, config, forge, labels, _ = policy_case(tmp_path)
    stale = load_run_state_readonly(config.run_state_path)
    process_completion_policies(state, config, now=100)
    process_completion_policies(stale, config, now=200)
    assert stale.task_reclaim_counts[250].review_timeout_retry_count == 1
    assert stale.task_reclaim_counts[250].review_timeout_retry_at == 160
    assert forge.add_comment.call_count == 1
    assert labels == ["status:queued"]


def test_dispatcher_and_standalone_reverse_order_keep_review_receipt(
    tmp_path, monkeypatch
):
    from orchestune.dispatch.gc.confirmed import collect_confirmed_completion
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc
    from orchestune.dispatch.rules import _RuleExecutionContext

    state, config, forge, _, _ = policy_case(tmp_path, result="not-needed")
    target = make_review(state, config)
    active = state.active_worktrees["250"]
    ctx = _RuleExecutionContext(run_state=state, queries=Mock(), config=config)
    monkeypatch.chdir(config.worktree_root.parent)
    outcome = collect_confirmed_completion(
        state, config, "250", active, ctx.record_completion, None
    )
    assert outcome.completion_event["action"] == "completion_handoff_released"
    assert "250" not in state.active_worktrees
    run_handoff_gc(
        GcRequest(state_path=config.run_state_path), forge_factory=lambda: forge
    )
    process_completion_policies(state, config, now=101)
    assert dependency_completion_blocked(state, 250)
    target.fire_text.assert_called_once()
    ctx.queries.record_completion.assert_not_called()


def test_comment_response_loss_reconciles_marker(tmp_path):
    state, config, forge, _, comments = policy_case(tmp_path)

    def lose_comment(issue, body):
        comments.append({"body": body})
        raise OSError("reply lost")

    forge.add_comment.side_effect = lose_comment
    process_completion_policies(state, config, now=100)
    process_completion_policies(state, config, now=200)
    forge.add_comment.assert_called_once()
    assert state.task_reclaim_counts[250].review_timeout_retry_count == 1


def test_preview_creates_no_policy_or_lock(tmp_path):
    state, config, forge, _, _ = policy_case(tmp_path)
    config.apply = False
    before = config.run_state_path.read_bytes()
    process_completion_policies(state, config)
    assert config.run_state_path.read_bytes() == before
    assert not config.run_state_path.with_suffix(".lock").exists()
    forge.add_label.assert_not_called()


def test_cloud_policy_review_uses_single_attempt_transport(tmp_path):
    from orchestune.targets.cloud_routine import ClaudeCodeCloudRoutineDispatchTarget

    state, config, _, _, _ = policy_case(tmp_path, result="not-needed")
    make_review(state, config)
    target = ClaudeCodeCloudRoutineDispatchTarget("routine", "test-token")
    target._fire = Mock(return_value={"claude_code_session_id": "review-1"})
    config.dispatch_target = target
    process_completion_policies(state, config, now=100)
    target._fire.assert_called_once()
    assert target._fire.call_args.kwargs["retry"] is False


def test_save_failure_after_remote_effects_recovers_pending_without_duplicate(
    tmp_path, monkeypatch
):
    import orchestune.dispatch.gc.policies as policies

    state, config, forge, labels, _ = policy_case(tmp_path)
    original = policies.save_run_state
    calls = 0

    def fail_applied(state, path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("applied save failed")
        original(state, path)

    monkeypatch.setattr(policies, "save_run_state", fail_applied)
    process_completion_policies(state, config, now=100)
    assert state.task_reclaim_counts[250].review_timeout_retry_pending
    process_completion_policies(state, config, now=200)
    assert labels == ["status:queued"]
    assert not state.task_reclaim_counts[250].review_timeout_retry_pending
    forge.add_comment.assert_called_once()


@pytest.mark.parametrize("result", ["done", "blocked", "not-needed"])
@pytest.mark.parametrize("owner", ["interactive", "dispatch"])
@pytest.mark.parametrize("first", ["standalone", "dispatcher"])
def test_dual_gc_result_owner_order_matrix(tmp_path, monkeypatch, result, owner, first):
    from orchestune.dispatch.gc.confirmed import collect_confirmed_completion
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc
    from orchestune.dispatch.rules import _RuleExecutionContext

    state, config, forge, labels = matrix_case(tmp_path, result, owner)
    monkeypatch.chdir(config.worktree_root.parent)
    queries = Mock()

    def dispatcher():
        current = load_run_state_readonly(config.run_state_path)
        process_completion_policies(current, config)
        if "250" in current.active_worktrees:
            ctx = _RuleExecutionContext(
                run_state=current, queries=queries, config=config
            )
            collect_confirmed_completion(
                current,
                config,
                "250",
                current.active_worktrees["250"],
                ctx.record_completion,
                None,
            )

    def standalone():
        return run_handoff_gc(
            GcRequest(state_path=config.run_state_path), forge_factory=lambda: forge
        )

    if first == "standalone":
        standalone()
        dispatcher()
    else:
        dispatcher()
        standalone()
    persisted = load_run_state_readonly(config.run_state_path)
    assert "250" not in persisted.active_worktrees
    assert persisted.completion_replay_receipts
    assert len(persisted.completed_worktrees) == (1 if result == "done" else 0)
    if result == "done":
        assert labels == ["status:done"]
        forge.add_label.assert_not_called()
    if result == "blocked":
        assert labels == ["status:queued"]
        assert persisted.task_reclaim_counts[250].review_timeout_retry_count == 1
    if result == "not-needed":
        forge.close_issue.assert_called_once()
    assert queries.record_completion.call_count <= (1 if result == "done" else 0)


def matrix_case(tmp_path, result, owner):
    from tests.test_dispatch_gc_handoff_integration import _forge

    state, config, forge, labels, comments = policy_case(tmp_path, result=result)
    active = replace(state.active_worktrees["250"], owner_kind=owner)
    state.active_worktrees["250"] = active
    # Freeze the final active ownership in durable policy context.
    record = confirmed_records(state)[0]
    record = replace(
        record,
        prepublication_policy_evidence={
            "decision": "allowed",
            "context": {"active": {"external_id": None}},
        },
    )
    update_record(state, record)
    with run_state_lock(config.run_state_path.with_suffix(".lock")):
        save_run_state(state, config.run_state_path)
    merged = _forge(comments[0], active.branch, active.base_sha)
    forge.get_pull_request.return_value = merged.pr
    forge.is_merge_commit_reachable_from.return_value = True
    forge.close_issue.side_effect = lambda *args: setattr(
        forge.get_issue_state, "return_value", "CLOSED"
    )
    return state, config, forge, labels


def test_repository_mismatch_holds_policy_effects(tmp_path):
    state, config, forge, _, _ = policy_case(tmp_path)
    # Copying a ledger to another checkout must not mutate that repository's Issue.
    other = tmp_path / "other"
    from tests.test_dispatch_gc_handoff_integration import _create_repo

    other.mkdir()
    other_repo, _, _ = _create_repo(other)
    config.worktree_root = other_repo / "worktrees"
    events = process_completion_policies(state, config)
    assert events[0]["action"] == "completion_policy_hold"
    forge.add_label.assert_not_called()
    forge.add_comment.assert_not_called()


def test_receipt_reclaims_token_only_after_active_release(tmp_path):
    from orchestune.claim.ownership import owner_token_digest
    from orchestune.complete.journal_models import CompletionReservation
    from orchestune.dispatch.gc.policy_discovery import reclaim_completed_tokens
    from orchestune.infra.private_tokens import _write_owner_token

    state, config, _, _, _ = policy_case(tmp_path)
    record = confirmed_records(state)[0]
    digest = owner_token_digest("test-owned-token")
    record = replace(record, owner_token_digest=digest)
    state.active_worktrees["250"].owner_token_digest = digest
    update_record(state, record)
    state.completion_reservations[record.reservation_key] = (
        CompletionReservation.from_journal(record).to_dict()
    )
    directory = config.run_state_path.parent / ".orchestune" / "claim-tokens"
    _write_owner_token(directory, record.generation_id, "test-owned-token")
    path = directory / f"{record.generation_id}.token"
    with run_state_lock(config.run_state_path.with_suffix(".lock")):
        save_run_state(state, config.run_state_path)
        reclaim_completed_tokens(state, config.run_state_path, record.repository_id)
        assert path.exists()
        state.active_worktrees.pop("250")
        save_run_state(state, config.run_state_path)
        reclaim_completed_tokens(state, config.run_state_path, record.repository_id)
        assert not path.exists()
        reclaim_completed_tokens(state, config.run_state_path, record.repository_id)
        assert state.completion_replay_receipts


def test_simultaneous_gc_paths_keep_single_done_history(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from orchestune.dispatch.gc.confirmed import collect_confirmed_completion
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc
    from tests.test_dispatch_gc_handoff_integration import _forge

    state, config, forge, _, comments = policy_case(tmp_path, result="done")
    active = state.active_worktrees["250"]
    merged = _forge(comments[0], active.branch, active.base_sha)
    forge.get_pull_request.return_value = merged.pr
    forge.is_merge_commit_reachable_from.return_value = True
    monkeypatch.chdir(config.worktree_root.parent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                run_handoff_gc,
                GcRequest(state_path=config.run_state_path, timeout_seconds=5),
                forge_factory=lambda: forge,
            ),
            pool.submit(
                collect_confirmed_completion, state, config, "250", active, Mock(), None
            ),
        ]
        for future in futures:
            future.result()
    persisted = load_run_state_readonly(config.run_state_path)
    assert "250" not in persisted.active_worktrees
    assert len(persisted.completed_worktrees) == 1
    assert persisted.completion_replay_receipts


def test_stale_dispatcher_cannot_resurrect_active_after_standalone_gc(
    tmp_path, monkeypatch
):
    from orchestune.dispatch.gc.confirmed import collect_confirmed_completion
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc
    from tests.test_dispatch_gc_handoff_integration import _forge

    state, config, forge, _, comments = policy_case(tmp_path, result="done")
    active = state.active_worktrees["250"]
    merged = _forge(comments[0], active.branch, active.base_sha)
    forge.get_pull_request.return_value = merged.pr
    forge.is_merge_commit_reachable_from.return_value = True
    monkeypatch.chdir(config.worktree_root.parent)
    run_handoff_gc(
        GcRequest(state_path=config.run_state_path), forge_factory=lambda: forge
    )
    original_history = load_run_state_readonly(
        config.run_state_path
    ).completed_worktrees
    record_completion = Mock()
    collect_confirmed_completion(state, config, "250", active, record_completion, None)
    record_completion.assert_not_called()
    persisted = load_run_state_readonly(config.run_state_path)
    assert persisted.completed_worktrees == original_history
    assert "250" not in persisted.active_worktrees
