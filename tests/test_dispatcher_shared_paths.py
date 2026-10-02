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


@pytest.mark.parametrize("value", ["relative/result.json", "absolute"])
def test_report_env_resolves_from_explicit_cwd(tmp_path, monkeypatch, value):
    import json

    from orchestune.dispatch.config_loader import load_and_resolve_config

    report_path = tmp_path / "absolute.json" if value == "absolute" else Path(value)
    monkeypatch.setenv("ORCHESTUNE_DISPATCH_REPORT_PATH", str(report_path))
    with (
        patch(
            "orchestune.dispatch.config_loader._resolve_checkout_roots",
            return_value=(tmp_path / "primary", tmp_path),
        ),
        patch(
            "orchestune.dispatch.config_loader._default_resolve_dispatch_shared_paths",
            return_value=(tmp_path / "state.json", tmp_path / "worktrees"),
        ),
    ):
        config = load_and_resolve_config(
            ["-p", "100", "--no-apply"],
            cwd=tmp_path,
            load_config_fn=lambda _: {"report-dir": "configured"},
            build_target_fn=lambda _: None,
        )
    assert config.report_dir == tmp_path / "primary/configured"
    assert config.report_path == (
        report_path if report_path.is_absolute() else tmp_path / report_path
    )
    with (
        patch(
            "orchestune.dispatch.dispatcher.load_and_resolve_config",
            return_value=config,
        ),
        patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle",
            return_value=_empty_report(),
        ),
    ):
        assert main([], cwd=tmp_path) == 0
    assert json.loads(config.report_path.read_text())["post_cycle_results"] == []


@pytest.mark.parametrize(
    "value", ["", " ", ".", "state.json", "state.lock", "events.jsonl"]
)
def test_invalid_report_env_is_config_error(tmp_path, monkeypatch, value):
    from orchestune.dispatch.config_loader import ConfigError, load_and_resolve_config

    monkeypatch.setenv("ORCHESTUNE_DISPATCH_REPORT_PATH", value)
    with (
        patch(
            "orchestune.dispatch.config_loader._resolve_checkout_roots",
            return_value=(tmp_path, tmp_path),
        ),
        patch(
            "orchestune.dispatch.config_loader._default_resolve_dispatch_shared_paths",
            return_value=(tmp_path / "state.json", tmp_path / "worktrees"),
        ),
        pytest.raises(ConfigError),
    ):
        load_and_resolve_config(
            ["-p", "100", "--no-apply"],
            cwd=tmp_path,
            load_config_fn=lambda _: {},
            build_target_fn=lambda _: None,
        )


@pytest.mark.parametrize("cwd_suffix", [Path("."), Path("orchestune")])
def test_shared_relative_paths_use_primary_repository_root(cwd_suffix):
    worktree_root, repository_root = _test_workspace_roots()
    with (
        patch(
            "orchestune.dispatch.dispatcher.load_config_file",
            return_value={
                "run-state-path": "shared/state.json",
                "worktree-root": "shared/worktrees",
                "events-log-path": str(repository_root / "shared/events.jsonl"),
            },
        ),
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
            ],
            cwd=worktree_root / cwd_suffix,
        )

    config = mock_run.call_args.args[0]
    assert config.run_state_path == (repository_root / "shared/state.json").resolve()
    assert config.worktree_root == (repository_root / "shared/worktrees").resolve()
    assert config.report_dir == repository_root / ".orchestune/reports/dispatch"


def test_absolute_shared_paths_are_preserved(tmp_path):
    _, repository_root = _test_workspace_roots()
    state_path = (tmp_path / "state.json").resolve()
    worktree_root = (tmp_path / "worktrees").resolve()
    with (
        patch(
            "orchestune.dispatch.dispatcher.load_config_file",
            return_value={
                "run-state-path": str(state_path),
                "worktree-root": str(worktree_root),
                "events-log-path": str(tmp_path / "events.jsonl"),
            },
        ),
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
        main(["-p", "100", "--no-apply"], cwd=repository_root)

    config = mock_run.call_args.args[0]
    assert (
        config.run_state_path == (repository_root / "configured/state.json").resolve()
    )
    assert config.worktree_root == (repository_root / "configured/worktrees").resolve()


def test_dispatch_from_outside_repository_fails_closed(tmp_path):
    state_path = tmp_path / "state.json"
    worktree_root = tmp_path / "worktrees"
    with (
        patch(
            "orchestune.dispatch.dispatcher.load_config_file",
            return_value={
                "run-state-path": str(state_path),
                "worktree-root": str(worktree_root),
            },
        ),
        pytest.raises(SystemExit) as error,
    ):
        main(
            [
                "--parent-issue",
                "100",
                "--no-apply",
            ],
            cwd=tmp_path,
        )

    assert error.value.code == 2
    assert not state_path.exists()
    assert not worktree_root.exists()
