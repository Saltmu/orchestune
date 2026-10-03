"""Tokenless completion and retrospective merge evidence with a real worktree."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from orchestune.complete.contracts import CompleteRequest
from orchestune.complete.merged import validate_merged_completion
from orchestune.complete.service import complete_task
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.run_state import load_run_state_readonly, save_run_state
from orchestune.models import PrRecord
from tests.complete_lifecycle_test_support import PublicationForge

pytest_plugins = ["tests.test_local_claim_identity"]


@pytest.fixture
def completion_env(local_claim, monkeypatch):
    workspace, active, worktree = local_claim
    active.claim = replace(active.claim, claimed_at=0)
    active.completion = replace(
        active.completion, completion_policy_config={"max_tokens_per_task": None}
    )
    with run_state_lock(workspace.lock_path):
        save_run_state(
            load_with_active(workspace.run_state_path, active), workspace.run_state_path
        )
    head = run_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    forge = PublicationForge()
    forge.pr = PrRecord(
        42,
        "task",
        ("file",),
        closes_issue_numbers=(7,),
        base_ref="main",
        state="MERGED",
        head_sha=head,
        merged_at="2026-09-30T00:00:00Z",
        merge_commit_oid="b" * 40,
        is_cross_repository=False,
    )
    forge.is_merge_commit_reachable_from = lambda *_: True
    forge.get_issue_last_reopened_at = lambda _: None
    ci = SimpleNamespace(to_dict=lambda: {"head_sha": head})
    monkeypatch.setattr(
        "orchestune.complete.service.run_local_ci_if_needed", lambda _: ci
    )
    monkeypatch.setattr(
        "orchestune.complete.service.validate_ci_evidence", lambda _: ci
    )
    monkeypatch.chdir(worktree)
    request = CompleteRequest.done(
        7,
        42,
        reviewer="skip",
        claim_id=active.claim.claim_id,
        worktree_root=worktree,
        state_path=workspace.run_state_path,
    )
    return workspace, active, worktree, request, forge


def load_with_active(path, active):
    state = load_run_state_readonly(path)
    state.active_worktrees["7"] = active
    return state


def test_tokenless_merged_completion_and_replay(completion_env):
    workspace, active, worktree, request, forge = completion_env
    result = complete_task(request, forge=forge)
    assert result.success, result.failure
    assert forge.labels == {"status:done"}
    assert len(forge.comments) == 1
    assert complete_task(request, forge=forge).success
    assert len(forge.comments) == 1
    assert not list(
        workspace.run_state_path.parent.glob(".orchestune/claim-tokens/*.token")
    )
    state = load_run_state_readonly(workspace.run_state_path)
    assert (
        state.active_worktrees["7"].completion.completion_handoff_ready
        and state.completion_replay_receipts
    )


def test_pending_open_completion_resumes_after_merge(completion_env):
    _, _, _, request, forge = completion_env
    merged = forge.pr
    forge.pr = replace(merged, state="OPEN")
    forge.inject = lambda op, after: (
        (_ for _ in ()).throw(OSError("offline")) if op == "post" else None
    )
    first = complete_task(request, forge=forge)
    assert not first.success and first.completion_id
    forge.pr = merged
    forge.inject = lambda *_: None
    resumed = complete_task(
        replace(request, completion_id=first.completion_id), forge=forge
    )
    assert resumed.success, resumed.failure
    assert len(forge.comments) == 1


@pytest.mark.parametrize(
    "problem", ["head", "identity", "fork", "unreachable", "reopened", "old-merge"]
)
def test_merged_evidence_must_match_current_claim(completion_env, problem):
    _, active, _, request, forge = completion_env
    if problem == "head":
        forge.pr = replace(forge.pr, head_sha="c" * 40)
    elif problem == "identity":
        forge.pr = replace(forge.pr, closes_issue_numbers=(8,))
    elif problem == "fork":
        forge.pr = replace(forge.pr, is_cross_repository=True)
    elif problem == "unreachable":
        forge.is_merge_commit_reachable_from = lambda *_: False
    elif problem == "reopened":
        forge.get_issue_last_reopened_at = lambda _: "2026-10-01T00:00:00Z"
    else:
        active.claim = replace(active.claim, claimed_at=2000000000)
    assert validate_merged_completion(forge.pr, active, forge) is not None


def test_old_agent_cannot_complete_reassigned_claim(completion_env):
    workspace, active, worktree, request, forge = completion_env
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees["7"].claim = replace(
        state.active_worktrees["7"].claim, claim_id="new-generation"
    )
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    result = complete_task(request, forge=forge)
    assert not result.success and not forge.comments


def test_generation_is_rechecked_after_ci(completion_env, monkeypatch):
    workspace, active, worktree, request, forge = completion_env

    def ci(_):
        state = load_run_state_readonly(workspace.run_state_path)
        state.active_worktrees["7"].claim = replace(
            state.active_worktrees["7"].claim, claim_id="new-generation"
        )
        with run_state_lock(workspace.lock_path):
            save_run_state(state, workspace.run_state_path)
        head = run_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
        return SimpleNamespace(to_dict=lambda: {"head_sha": head})

    monkeypatch.setattr("orchestune.complete.service.run_local_ci_if_needed", ci)
    result = complete_task(request, forge=forge)
    assert not result.success and not forge.comments
