"""Tests for LocalTrialWorkspace in orchestune.local_trial.workspace."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from orchestune.infra.git_cli import run_git
from orchestune.local_trial.contracts import (
    TrialError,
    TrialWorkspace,
)
from orchestune.local_trial.models import (
    AgentInfo,
    Budget,
    Checkpoint,
    DisciplineStatus,
    GitIntent,
    GitOperation,
    Observation,
    RepositoryIdentity,
    RequestSnapshot,
    TrialPhase,
    TrialPlan,
    TrialState,
    WorkspaceIdentity,
)
from orchestune.local_trial.workspace import LocalTrialWorkspace, workspace_digest


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-b", "main"], cwd=path)
    run_git(["config", "user.name", "Test User"], cwd=path)
    run_git(["config", "user.email", "test@example.com"], cwd=path)
    # Commit initial file
    initial_file = path / "README.md"
    initial_file.write_text("# Test Repo\n", encoding="utf-8")
    run_git(["add", "README.md"], cwd=path)
    run_git(["commit", "-m", "Initial commit"], cwd=path)


def _make_state(
    repo: RepositoryIdentity,
    ws: WorkspaceIdentity,
    *,
    trial_id: str = "a" * 32,
    head_sha: str,
    write_paths: tuple[str, ...] = ("foo.py",),
    approved_index_digest: str | None = None,
    approved_worktree_digest: str | None = None,
) -> TrialState:
    return TrialState(
        schema_version=1,
        trial_id=trial_id,
        repository=repo,
        workspace=ws,
        request=RequestSnapshot(text="Test", digest="d" * 64),
        agent=AgentInfo(name="codex", executable="codex", version="1.0"),
        plan=TrialPlan(
            goal="Test Goal",
            acceptance_criteria=("Done",),
            write_paths=write_paths,
            discipline_paths=(),
            discipline_notes=(),
            ci_argv=("pytest",),
            discipline_status=DisciplineStatus.COMPATIBLE,
        ),
        approved_plan_digest=None,
        approved_worktree_digest=approved_worktree_digest,
        approved_index_digest=approved_index_digest,
        phase=TrialPhase.IMPLEMENTING,
        checkpoint=Checkpoint.IMPLEMENT,
        reason=None,
        revision=1,
        budget=Budget(plan_revision=0, cycles_started=0, reviews_started=0),
        head_sha=head_sha,
        pending_attempt=None,
        pending_git=None,
        attempt_ids=(),
        ci_evidence=None,
        review_evidence=None,
        findings=(),
        responses=(),
        stop_confirmations=(),
        controller=None,
        observation=Observation(
            started_at="2026-11-07T12:00:00Z",
            updated_at="2026-11-07T12:00:00Z",
            elapsed_seconds=0.0,
        ),
        decision=None,
    )


def test_protocol_satisfaction() -> None:
    ws = LocalTrialWorkspace()
    assert isinstance(ws, TrialWorkspace)


def test_storage_roots_and_discover(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)

    ws_impl = LocalTrialWorkspace()
    primary, common = ws_impl.storage_roots(str(repo_path))
    assert Path(primary).resolve() == repo_path.resolve()
    assert Path(common).resolve() == (repo_path / ".git").resolve()

    repo_id = ws_impl.discover(str(repo_path))
    assert repo_id.source_root == str(repo_path.resolve())
    assert repo_id.primary_root == str(repo_path.resolve())
    assert repo_id.base_ref == "refs/heads/main"
    assert len(repo_id.base_sha) == 40


def test_discover_rejects_dirty_and_bare(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)

    # Dirty repo
    (repo_path / "dirty.txt").write_text("dirty", encoding="utf-8")
    ws_impl = LocalTrialWorkspace()
    with pytest.raises(TrialError) as exc:
        ws_impl.discover(str(repo_path))
    assert exc.value.code == "source_dirty"

    # Bare repo
    bare_path = tmp_path / "bare.git"
    run_git(["clone", "--bare", str(repo_path), str(bare_path)], cwd=tmp_path)
    with pytest.raises(TrialError) as exc:
        ws_impl.discover(str(bare_path))
    assert exc.value.code == "repository_invalid"


def test_ensure_storage_ignored_and_prepare(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))

    # ensure_storage_ignored
    ws_impl.ensure_storage_ignored(repo_id)
    # verify idempotency
    ws_impl.ensure_storage_ignored(repo_id)

    exclude_file = Path(repo_id.common_dir) / "info" / "exclude"
    assert "/.orchestune/" in exclude_file.read_text(encoding="utf-8")

    trial_id = "1" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    assert f"trial-{trial_id}" in paths.implementation_path
    assert f"trial/{trial_id}" == paths.branch

    # prepare worktree
    ws_impl.prepare(repo_id, paths)
    assert Path(paths.implementation_path).is_dir()

    # Re-prepare on clean worktree is idempotent
    ws_impl.prepare(repo_id, paths)


def test_inspect_and_digests(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "2" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)

    snap = ws_impl.inspect(repo_id, paths)
    assert snap.branch == paths.branch
    assert snap.head_sha == repo_id.base_sha
    assert len(snap.changes) == 0

    # Add a modified file and an untracked file
    impl_path = Path(paths.implementation_path)
    (impl_path / "foo.py").write_text("print('hello')\n", encoding="utf-8")

    snap2 = ws_impl.inspect(repo_id, paths)
    assert len(snap2.changes) == 1
    assert snap2.changes[0].path == "foo.py"
    assert snap2.content_digest != snap.content_digest


def test_two_stage_commit_and_reconcile(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "3" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)

    impl_path = Path(paths.implementation_path)
    (impl_path / "foo.py").write_text("def f(): pass\n", encoding="utf-8")

    state = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("foo.py",),
    )

    # Stage commit
    intent = ws_impl.stage_commit(state)
    assert intent.operation == GitOperation.COMMIT
    assert intent.pre_head_sha == repo_id.base_sha
    assert intent.expected_tree_sha is not None
    assert "foo.py" in intent.allowed_paths

    # Before finish_commit, reconcile returns None
    assert ws_impl.reconcile_commit(state, intent) is None

    # Finish commit
    new_head = ws_impl.finish_commit(state, intent)
    assert len(new_head) == 40
    assert new_head != repo_id.base_sha

    # After finish_commit, reconcile returns new_head
    reconciled = ws_impl.reconcile_commit(state, intent)
    assert reconciled == new_head


def test_stage_commit_rejects_out_of_scope_and_no_changes(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "4" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)

    impl_path = Path(paths.implementation_path)
    state = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("foo.py",),
    )

    # No changes
    with pytest.raises(TrialError) as exc:
        ws_impl.stage_commit(state)
    assert exc.value.code == "no_changes"

    # Out of scope file
    (impl_path / "unapproved.py").write_text("bad\n", encoding="utf-8")
    with pytest.raises(TrialError) as exc:
        ws_impl.stage_commit(state)
    assert exc.value.code == "out_of_scope"


def test_review_worktree_and_includes_head(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "5" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)

    impl_path = Path(paths.implementation_path)
    (impl_path / "foo.py").write_text("def f(): pass\n", encoding="utf-8")
    state0 = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("foo.py",),
    )
    intent = ws_impl.stage_commit(state0)
    new_head = ws_impl.finish_commit(state0, intent)

    state1 = _make_state(
        repo_id, paths, trial_id=trial_id, head_sha=new_head, write_paths=("foo.py",)
    )

    # prepare_review
    ws_impl.prepare_review(state1)
    assert Path(paths.review_path).is_dir()

    snap_rev = ws_impl.inspect_review(state1)
    assert snap_rev.branch is None
    assert snap_rev.head_sha == new_head

    # includes_head (new_head is not yet merged into main)
    assert ws_impl.includes_head(state1) is False

    # diff & working_diff
    diff_text = ws_impl.diff(state1)
    assert "foo.py" in diff_text
    working_diff_text = ws_impl.working_diff(state1)
    assert isinstance(working_diff_text, str)


def test_paths_validation(tmp_path: Path) -> None:
    ws_impl = LocalTrialWorkspace()
    repo = RepositoryIdentity(
        source_root=str(tmp_path),
        primary_root=str(tmp_path),
        common_dir=str(tmp_path / ".git"),
        base_ref="refs/heads/main",
        base_sha="0" * 40,
    )
    with pytest.raises(TrialError) as exc:
        ws_impl.paths(repo, "invalid-id")
    assert exc.value.code == "invalid_input"


def test_prepare_existing_mismatch_and_dirty(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "6" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)

    # Corrupt existing worktree branch
    impl_path = Path(paths.implementation_path)
    run_git(["checkout", "-b", "different-branch"], cwd=impl_path)
    with pytest.raises(TrialError) as exc:
        ws_impl.prepare(repo_id, paths)
    assert exc.value.code == "identity_mismatch"

    # Switch back and make it dirty
    run_git(["checkout", paths.branch], cwd=impl_path)
    (impl_path / "dirty.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(TrialError) as exc:
        ws_impl.prepare(repo_id, paths)
    assert exc.value.code == "worktree_dirty"


def test_commit_identity_and_path_validation(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "7" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)

    # Invalid write paths
    impl_path = Path(paths.implementation_path)
    (impl_path / "foo.py").write_text("x = 1\n", encoding="utf-8")

    state_glob = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("*.py",),
    )
    with pytest.raises(TrialError) as exc:
        ws_impl.stage_commit(state_glob)
    assert exc.value.code == "out_of_scope"

    state_reserved = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=(".git/config",),
    )
    with pytest.raises(TrialError) as exc:
        ws_impl.stage_commit(state_reserved)
    assert exc.value.code == "out_of_scope"

    # Empty git user config
    run_git(["config", "user.name", ""], cwd=impl_path)
    state_valid = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("foo.py",),
    )
    with pytest.raises(TrialError) as exc:
        ws_impl.stage_commit(state_valid)
    assert exc.value.code == "commit_identity_missing"


def test_finish_and_reconcile_commit_errors(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "8" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)
    impl_path = Path(paths.implementation_path)
    (impl_path / "foo.py").write_text("x = 1\n", encoding="utf-8")

    state = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("foo.py",),
    )
    intent = ws_impl.stage_commit(state)

    # Head changed before finish_commit
    bad_intent = GitIntent(
        operation=GitOperation.COMMIT,
        pre_head_sha="f" * 40,
        expected_tree_sha=intent.expected_tree_sha,
        target_sha=intent.target_sha,
        allowed_paths=intent.allowed_paths,
    )
    with pytest.raises(TrialError) as exc:
        ws_impl.finish_commit(state, bad_intent)
    assert exc.value.code == "head_changed"

    # Finish commit normally
    assert len(ws_impl.finish_commit(state, intent)) == 40

    # Reconcile with mismatching tree sha
    bad_reconcile_intent = GitIntent(
        operation=GitOperation.COMMIT,
        pre_head_sha=repo_id.base_sha,
        expected_tree_sha="e" * 40,
        target_sha=intent.target_sha,
        allowed_paths=intent.allowed_paths,
    )
    with pytest.raises(TrialError) as exc:
        ws_impl.reconcile_commit(state, bad_reconcile_intent)
    assert exc.value.code == "head_changed"


def test_review_worktree_reuse_and_dirty(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "9" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)
    impl_path = Path(paths.implementation_path)
    (impl_path / "foo.py").write_text("x = 1\n", encoding="utf-8")
    state0 = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("foo.py",),
    )
    intent = ws_impl.stage_commit(state0)
    new_head = ws_impl.finish_commit(state0, intent)
    state1 = _make_state(
        repo_id, paths, trial_id=trial_id, head_sha=new_head, write_paths=("foo.py",)
    )

    ws_impl.prepare_review(state1)
    # Re-prepare clean review worktree is idempotent
    ws_impl.prepare_review(state1)

    # Dirty review worktree is rejected
    review_path = Path(paths.review_path)
    (review_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(TrialError) as exc:
        ws_impl.prepare_review(state1)
    assert exc.value.code == "worktree_dirty"


def test_storage_roots_invalid_repo(tmp_path: Path) -> None:
    ws_impl = LocalTrialWorkspace()
    with pytest.raises(TrialError) as exc:
        ws_impl.storage_roots(str(tmp_path / "not-a-repo"))
    assert exc.value.code == "repository_invalid"


def test_stage_commit_approved_index_digest(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "a" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)
    impl_path = Path(paths.implementation_path)
    (impl_path / "foo.py").write_text("x = 1\n", encoding="utf-8")

    # Manually stage foo.py
    run_git(["add", "foo.py"], cwd=impl_path)
    ls_res = run_git(["ls-files", "--stage", "-z"], cwd=impl_path)
    approved_digest = hashlib.sha256(ls_res.stdout.encode("utf-8")).hexdigest()

    # Without approved_index_digest, stage_commit rejects preexisting staged changes
    state_no_approval = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("foo.py",),
    )
    with pytest.raises(TrialError) as exc:
        ws_impl.stage_commit(state_no_approval)
    assert exc.value.code == "worktree_dirty"

    # With matching approved_index_digest and approved_worktree_digest, stage_commit accepts
    snap = ws_impl.inspect(repo_id, paths)
    state_approved = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("foo.py",),
        approved_index_digest=approved_digest,
        approved_worktree_digest=workspace_digest(snap),
    )
    intent = ws_impl.stage_commit(state_approved)
    assert intent.expected_tree_sha is not None


def test_includes_head_failure_code(tmp_path: Path) -> None:
    ws_impl = LocalTrialWorkspace()
    repo_id = RepositoryIdentity(
        source_root=str(tmp_path),
        primary_root=str(tmp_path),
        common_dir=str(tmp_path / ".git"),
        base_ref="refs/heads/main",
        base_sha="0" * 40,
    )
    paths = WorkspaceIdentity(
        implementation_path=str(tmp_path),
        review_path=str(tmp_path),
        branch="trial/1",
    )
    state = _make_state(repo_id, paths, head_sha="0" * 40)
    # Running outside git repo causes git merge-base to exit with 128
    with pytest.raises(TrialError) as exc:
        ws_impl.includes_head(state)
    assert exc.value.code == "repository_invalid"


def test_inspect_with_rename(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "b" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)
    impl_path = Path(paths.implementation_path)

    # Initial file committed in implementation worktree
    (impl_path / "origin.txt").write_text("orig\n", encoding="utf-8")
    run_git(["add", "origin.txt"], cwd=impl_path)
    run_git(["commit", "-m", "add origin"], cwd=impl_path)

    # Rename origin.txt -> renamed.txt
    run_git(["mv", "origin.txt", "renamed.txt"], cwd=impl_path)

    snap = ws_impl.inspect(repo_id, paths)
    rename_change = next((c for c in snap.changes if c.old_path is not None), None)
    assert rename_change is not None
    assert rename_change.path == "renamed.txt"
    assert rename_change.old_path == "origin.txt"


def test_finish_commit_tree_mismatch(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "c" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)
    impl_path = Path(paths.implementation_path)
    (impl_path / "foo.py").write_text("x = 1\n", encoding="utf-8")

    state = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("foo.py",),
    )
    intent = ws_impl.stage_commit(state)

    bad_intent = GitIntent(
        operation=GitOperation.COMMIT,
        pre_head_sha=state.head_sha,
        expected_tree_sha="1" * 40,
        target_sha=intent.target_sha,
        allowed_paths=intent.allowed_paths,
    )
    with pytest.raises(TrialError) as exc:
        ws_impl.finish_commit(state, bad_intent)
    assert exc.value.code == "head_changed"


def test_reconcile_commit_mismatches(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "d" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)
    impl_path = Path(paths.implementation_path)
    (impl_path / "foo.py").write_text("x = 1\n", encoding="utf-8")

    state = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("foo.py",),
    )
    intent = ws_impl.stage_commit(state)

    # Commit with message WITHOUT Orchestune-Trial trailer
    run_git(["commit", "-m", "manual commit without trailer"], cwd=impl_path)
    with pytest.raises(TrialError) as exc:
        ws_impl.reconcile_commit(state, intent)
    assert exc.value.code == "head_changed"


def test_stage_commit_verifies_branch(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "e" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)
    impl_path = Path(paths.implementation_path)
    (impl_path / "foo.py").write_text("x = 1\n", encoding="utf-8")

    state = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("foo.py",),
    )

    # Switch to unrelated branch
    run_git(["checkout", "-b", "unrelated-branch"], cwd=impl_path)
    with pytest.raises(TrialError) as exc:
        ws_impl.stage_commit(state)
    assert exc.value.code == "identity_mismatch"


def test_stage_commit_stale_worktree_digest(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "f" * 32
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)
    impl_path = Path(paths.implementation_path)
    (impl_path / "foo.py").write_text("x = 1\n", encoding="utf-8")

    # Stage foo.py
    run_git(["add", "foo.py"], cwd=impl_path)
    snap = ws_impl.inspect(repo_id, paths)
    ls_res = run_git(["ls-files", "--stage", "-z"], cwd=impl_path)
    approved_index = hashlib.sha256(ls_res.stdout.encode("utf-8")).hexdigest()
    approved_ws = workspace_digest(snap)

    # Now modify foo.py unstaged
    (impl_path / "foo.py").write_text("x = 2\n", encoding="utf-8")

    # The current workspace digest no longer matches approved_ws
    state = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("foo.py",),
        approved_index_digest=approved_index,
        approved_worktree_digest=approved_ws,
    )
    with pytest.raises(TrialError) as exc:
        ws_impl.stage_commit(state)
    assert exc.value.code == "worktree_dirty"


def test_stage_commit_with_deleted_path(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "1" * 31 + "a"
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)
    impl_path = Path(paths.implementation_path)

    # Initial file committed in implementation worktree
    (impl_path / "to_delete.txt").write_text("delete me\n", encoding="utf-8")
    run_git(["add", "to_delete.txt"], cwd=impl_path)
    run_git(["commit", "-m", "add to_delete"], cwd=impl_path)
    new_base = run_git(["rev-parse", "HEAD"], cwd=impl_path).stdout.strip()

    # git rm to_delete.txt
    run_git(["rm", "to_delete.txt"], cwd=impl_path)

    state = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=new_base,
        write_paths=("to_delete.txt",),
        approved_index_digest=hashlib.sha256(
            run_git(["ls-files", "--stage", "-z"], cwd=impl_path).stdout.encode("utf-8")
        ).hexdigest(),
        approved_worktree_digest=workspace_digest(ws_impl.inspect(repo_id, paths)),
    )
    # stage_commit should succeed without failing on missing pathspec
    intent = ws_impl.stage_commit(state)
    assert "to_delete.txt" in intent.allowed_paths


def test_stage_commit_dangling_symlink(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "2" * 31 + "b"
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)
    impl_path = Path(paths.implementation_path)

    # Create dangling symlink
    link_path = impl_path / "dangling.txt"
    link_path.symlink_to("missing_target.txt")

    state = _make_state(
        repo_id,
        paths,
        trial_id=trial_id,
        head_sha=repo_id.base_sha,
        write_paths=("dangling.txt",),
    )
    intent = ws_impl.stage_commit(state)
    assert "dangling.txt" in intent.allowed_paths
    # finish_commit should succeed and commit the symlink
    new_head = ws_impl.finish_commit(state, intent)
    assert new_head != repo_id.base_sha


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file permission bits are not supported on Windows",
)
def test_compute_content_digest_executable_mode(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    ws_impl = LocalTrialWorkspace()
    repo_id = ws_impl.discover(str(repo_path))
    ws_impl.ensure_storage_ignored(repo_id)

    trial_id = "3" * 31 + "c"
    paths = ws_impl.paths(repo_id, trial_id)
    ws_impl.prepare(repo_id, paths)
    impl_path = Path(paths.implementation_path)

    script_path = impl_path / "run.sh"
    script_path.write_text("#!/bin/sh\necho hello\n", encoding="utf-8")
    script_path.chmod(0o755)

    snap1 = ws_impl.inspect(repo_id, paths)

    # Change to non-executable
    script_path.chmod(0o644)
    snap2 = ws_impl.inspect(repo_id, paths)

    assert snap1.content_digest != snap2.content_digest
