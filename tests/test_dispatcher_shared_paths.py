"""#966: dispatcher/claim shared workspace path contract tests."""

from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.dispatch.cycle import CycleReport
from orchestune.dispatch.dispatcher import main
from tests.dispatch_test_support import (
    stub_forge_check_auth,
    stub_label_actor_permission,
)


@pytest.fixture(autouse=True)
def _stub_forge_check_auth_by_default(fake_forge):
    return stub_forge_check_auth(fake_forge)


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default(fake_forge):
    stub_label_actor_permission(fake_forge)


def _empty_report() -> CycleReport:
    return CycleReport(
        selected=[],
        quota_slots_available=0,
        lock_changes={"to_lock": [], "to_unlock": []},
        deviation_events=[],
        completion_events=[],
        promotion_events=[],
        applied=False,
    )


def _test_workspace_roots() -> tuple[Path, Path]:
    workspace = resolve_claim_workspace(Path.cwd())
    return workspace.repository_root, workspace.common_dir.parent


@pytest.mark.parametrize("cwd_suffix", [Path("."), Path("orchestune")])
def test_shared_relative_paths_use_primary_repository_root(cwd_suffix):
    worktree_root, repository_root = _test_workspace_roots()
    with (
        patch("orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True),
        patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle",
            autospec=True,
            return_value=_empty_report(),
        ) as mock_run,
    ):
        main(
            [
                "--parent-issue",
                "100",
                "--no-apply",
                "--run-state-path",
                "shared/state.json",
                "--worktree-root",
                "shared/worktrees",
                "--events-log-path",
                str(repository_root / "shared/events.jsonl"),
            ],
            cwd=worktree_root / cwd_suffix,
        )

    config = mock_run.call_args.args[0]
    assert config.run_state_path == (repository_root / "shared/state.json").resolve()
    assert config.worktree_root == (repository_root / "shared/worktrees").resolve()


def test_absolute_shared_paths_are_preserved(tmp_path):
    _, repository_root = _test_workspace_roots()
    state_path = (tmp_path / "state.json").resolve()
    worktree_root = (tmp_path / "worktrees").resolve()
    with (
        patch("orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True),
        patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle",
            autospec=True,
            return_value=_empty_report(),
        ) as mock_run,
    ):
        main(
            [
                "--parent-issue",
                "100",
                "--no-apply",
                "--run-state-path",
                str(state_path),
                "--worktree-root",
                str(worktree_root),
                "--events-log-path",
                str(tmp_path / "events.jsonl"),
            ],
            cwd=repository_root,
        )

    config = mock_run.call_args.args[0]
    assert config.run_state_path == state_path
    assert config.worktree_root == worktree_root


def test_config_file_shared_paths_use_primary_repository_root():
    _, repository_root = _test_workspace_roots()
    with (
        patch(
            "orchestune.dispatch.dispatcher.load_config_file",
            return_value={
                "parent-issue": 100,
                "run-state-path": "configured/state.json",
                "worktree-root": "configured/worktrees",
                "events-log-path": "configured/events.jsonl",
            },
        ),
        patch("orchestune.dispatch.dispatcher.build_dispatch_target", autospec=True),
        patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle",
            autospec=True,
            return_value=_empty_report(),
        ) as mock_run,
    ):
        main(["--no-apply"], cwd=repository_root)

    config = mock_run.call_args.args[0]
    assert (
        config.run_state_path == (repository_root / "configured/state.json").resolve()
    )
    assert config.worktree_root == (repository_root / "configured/worktrees").resolve()


def test_dispatch_from_outside_repository_fails_closed(tmp_path):
    state_path = tmp_path / "state.json"
    worktree_root = tmp_path / "worktrees"
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--parent-issue",
                "100",
                "--no-apply",
                "--run-state-path",
                str(state_path),
                "--worktree-root",
                str(worktree_root),
            ],
            cwd=tmp_path,
        )

    assert error.value.code == 2
    assert not state_path.exists()
    assert not worktree_root.exists()
