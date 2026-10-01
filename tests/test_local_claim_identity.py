"""Real Git/worktree regression coverage for local claim generations."""

from dataclasses import replace

import pytest

from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.run_state import ActiveWorktree, RunState, save_run_state
from orchestune.worktree_ops.claim_marker import write_claim_marker


@pytest.fixture
def local_claim(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.test"],
    ):
        run_git(args, cwd=repo)
    (repo / "file").write_text("base")
    run_git(["add", "file"], cwd=repo)
    run_git(["commit", "-m", "base"], cwd=repo)
    worktree = repo / "worktrees" / "task"
    run_git(["worktree", "add", "-b", "task", str(worktree)], cwd=repo)
    workspace = resolve_claim_workspace(repo)
    base = run_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    active = ActiveWorktree(
        7,
        "task",
        str(worktree),
        None,
        None,
        (),
        claim_id="claim-original",
        claim_stage="completed",
        base_ref="main",
        base_sha=base,
        repository_id=workspace.repository_identity,
        owner_kind="interactive",
        owner_token_digest="a" * 64,
    )
    write_claim_marker(
        worktree,
        claim_id=active.claim_id,
        branch="task",
        base_sha=base,
        branch_created=True,
    )
    with run_state_lock(workspace.lock_path):
        save_run_state(
            RunState(active_worktrees={"7": active}), workspace.run_state_path
        )
    return workspace, active, worktree


def test_local_generation_needs_no_token(local_claim):
    from orchestune.claim.local_identity import validate_local_claim

    workspace, active, worktree = local_claim
    validate_local_claim(
        active, active.claim_id, cwd=worktree, state_path=workspace.run_state_path
    )


@pytest.mark.parametrize(
    "problem", ["generation", "marker", "branch", "repository", "caller"]
)
def test_stale_or_wrong_worktree_rejected(local_claim, problem):
    from orchestune.claim.local_identity import validate_local_claim
    from orchestune.worktree_ops.claim_marker import claim_marker_path

    workspace, active, worktree = local_claim
    caller = worktree
    if problem == "generation":
        active = replace(active, claim_id="claim-new")
    elif problem == "marker":
        claim_marker_path(worktree).unlink()
    elif problem == "branch":
        run_git(["checkout", "-b", "other"], cwd=worktree)
    elif problem == "repository":
        active = replace(active, repository_id="other")
    else:
        caller = workspace.repository_root
    with pytest.raises(ValueError):
        validate_local_claim(
            active, "claim-original", cwd=caller, state_path=workspace.run_state_path
        )


def test_atomic_marker_failure_preserves_previous_generation(local_claim, monkeypatch):
    import pytest

    from orchestune.worktree_ops import claim_marker

    _, active, worktree = local_claim
    before = claim_marker.claim_marker_path(worktree).read_bytes()
    monkeypatch.setattr(
        claim_marker,
        "write_json_atomic",
        lambda *_: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError):
        claim_marker.write_claim_marker(
            worktree,
            claim_id="replacement",
            branch=active.branch,
            base_sha=active.base_sha,
            branch_created=False,
        )
    assert claim_marker.claim_marker_path(worktree).read_bytes() == before
