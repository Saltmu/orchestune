"""dispatcherのCLI引数パース・設定ファイル読み込み・post-cycleフェーズテスト。

`tests/test_dispatcher.py`の肥大化解消のため分割している（#349）。
`run_dispatch_cycle`自体のディスパッチループ統合テストは
`test_dispatcher_pipeline.py`へ分割し、本ファイルには`_build_arg_parser`/
`_config_defaults`/`main`のCLI引数・設定ファイル関連の挙動、および
`main`がオーケストレーションするpost-cycleのベストエフォート処理
（`_run_best_effort_phase`とその利用箇所）のテストを残している。
"""

import argparse
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import CycleReport, run_dispatch_cycle
from orchestune.dispatch_result import PhaseResult, PhaseStatus
from orchestune.dispatch_state import RunState, load_run_state, save_run_state
from orchestune.dispatch_targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    LocalProcessDispatchTarget,
)
from orchestune.dispatcher import (
    _decide_semantic_review_enabled,
    _poll_pending_not_needed_reviews,
    _process_parent_completion,
    _run_best_effort_phase,
    _run_semantic_integrator,
    main,
)
from orchestune.forge import ForgeAuthError
from orchestune.models import IssueRecord

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


@pytest.fixture(autouse=True)
def _stub_forge_check_auth_by_default():
    """テスト環境において GitHubForge.check_auth() が実際の gh 認証エラーを
    投げないように、デフォルトで pass するようにスタブする。"""
    with patch("orchestune.forge.GitHubForge.check_auth") as mock_check:
        yield mock_check


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default():
    """#119で追加したactor権限検証ステップが、既存の大半のテストで実際の
    `gh api`呼び出しを行わないよう、デフォルトで許可された actor/permission を
    返すようスタブする。検証ロジック自体のテストは
    tests/test_dispatch_actor_verification.py に集約する。"""
    with (
        patch(
            "orchestune.forge.GitHubForge.get_label_actor",
            return_value="trusted-actor",
        ),
        patch(
            "orchestune.forge.GitHubForge.get_actor_permission",
            return_value="write",
        ),
    ):
        yield


def _issue(
    number,
    labels=("status:queued",),
    footprint=("src/foo.py",),
    symbols=("foo.Foo",),
    subtask_id="task-a",
    depends_on=(),
    created_at="2026-01-01T00:00:00+00:00",
    parent_number=181,
):
    footprint_lines = "\n".join(f"  - {f}" for f in footprint) if footprint else "  []"
    symbols_lines = "\n".join(f"  - {s}" for s in symbols) if symbols else "  []"
    depends_on_lines = (
        "\n".join(f"  - {d}" for d in depends_on) if depends_on else "  []"
    )
    body = (
        "## Footprint\n"
        "```yaml\n"
        f"subtask_id: {subtask_id}\n"
        "footprint:\n"
        f"{footprint_lines}\n"
        "symbols:\n"
        f"{symbols_lines}\n"
        "depends_on:\n"
        f"{depends_on_lines}\n"
        "```\n"
    )
    parent = {"number": parent_number} if parent_number is not None else None
    return IssueRecord(
        number=number,
        title="t",
        body=body,
        labels=labels,
        created_at=created_at,
        parent=parent,
    )


class TestBuildArgParser:
    """#328: dispatch-cycleの既定挙動をapplyに変更（--no-applyでdry-run）。"""

    def test_apply_defaults_to_true(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.apply is True

    def test_no_apply_flag_disables_apply(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--no-apply"])
        assert args.apply is False

    def test_explicit_apply_flag_still_works(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--apply"])
        assert args.apply is True

    def test_dispatch_target_defaults_to_none_when_unspecified(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.dispatch_target is None

    def test_dispatch_target_explicit_local_is_preserved(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dispatch-target", "local"])
        assert args.dispatch_target == "local"

    def test_dispatch_target_explicit_auto_is_preserved(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dispatch-target", "auto"])
        assert args.dispatch_target == "auto"

    def test_dispatch_target_explicit_codex_cli_is_preserved(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dispatch-target", "codex-cli"])
        assert args.dispatch_target == "codex-cli"

    def test_dispatch_target_explicit_codex_cloud_is_preserved(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dispatch-target", "codex-cloud"])
        assert args.dispatch_target == "codex-cloud"

    def test_codex_cloud_env_option_is_parsed(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--codex-cloud-env", "env_123"])
        assert args.codex_cloud_env == "env_123"

    def test_task_timeout_seconds_defaults_to_zero(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.task_timeout_seconds == 0

    def test_task_timeout_seconds_arg_is_parsed(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--task-timeout-seconds", "3600"])
        assert args.task_timeout_seconds == 3600

    def test_zombie_gc_defaults_to_true(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.zombie_gc is True

    def test_no_zombie_gc_disables_zombie_gc(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--no-zombie-gc"])
        assert args.zombie_gc is False


class TestConfigDefaults:
    def test_config_defaults_load(self):
        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        config_data = {
            "task-timeout-seconds": 1200,
            "zombie-gc": False,
        }
        defaults = _config_defaults(parser, config_data)
        assert defaults["task_timeout_seconds"] == 1200
        assert defaults["zombie_gc"] is False

    def test_config_defaults_validation_error(self):
        import pytest

        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            _config_defaults(parser, {"task-timeout-seconds": -1})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"zombie-gc": "invalid"})


class TestMainDispatchTargetAutoDetection:
    """#121: --dispatch-target未指定時、mainが実行環境に応じた実ディスパッチ先を
    build_dispatch_targetへ渡すことを検証する（ダミー動作への誤フォールバック防止）。"""

    def _empty_report(self):
        return CycleReport(
            selected=[],
            quota_slots_available=0,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=False,
        )

    def test_defaults_to_auto_outside_github_actions(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
        ):
            main(["--no-apply", "--run-state-path", str(tmp_path / "rs.json")])

        assert mock_build.call_args.args[0] == "auto"

    def test_defaults_to_cloud_routine_in_github_actions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
        ):
            main(["--no-apply", "--run-state-path", str(tmp_path / "rs.json")])

        assert mock_build.call_args.args[0] == "cloud-routine"

    def test_explicit_local_wins_even_inside_github_actions(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
        ):
            main(
                [
                    "--no-apply",
                    "--dispatch-target",
                    "local",
                    "--run-state-path",
                    str(tmp_path / "rs.json"),
                ]
            )

        assert mock_build.call_args.args[0] == "local"


class TestDecideSemanticReviewEnabled:
    """#150: ORCHESTUNE_SEMANTIC_REVIEW環境変数によるON/OFF判定（副作用なし）。"""

    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ORCHESTUNE_SEMANTIC_REVIEW", raising=False)
        assert _decide_semantic_review_enabled() is True

    def test_disabled_when_env_var_is_zero(self, monkeypatch):
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW", "0")
        assert _decide_semantic_review_enabled() is False

    def test_enabled_when_env_var_is_nonzero(self, monkeypatch):
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW", "1")
        assert _decide_semantic_review_enabled() is True


class TestRunBestEffortPhase:
    """#232: 3つのベストエフォート後処理フェーズが共有する実行部の契約を検証する。"""

    def test_returns_success_with_report_when_work_succeeds(self):
        result = _run_best_effort_phase(
            phase_name="dummy_phase",
            report_label="Dummy Report",
            work=lambda: {"ok": True},
            auth_error=None,
            auth_error_message="authentication failed while doing dummy work",
            failure_message="failed to do dummy work",
        )

        assert isinstance(result, PhaseResult)
        assert result.phase_name == "dummy_phase"
        assert result.status == PhaseStatus.SUCCESS
        assert result.report == {"ok": True}
        assert result.retryable is False

    def test_returns_fatal_failure_on_forge_auth_error(self, capsys):
        result = _run_best_effort_phase(
            phase_name="dummy_phase",
            report_label="Dummy Report",
            work=lambda: {"ok": True},
            auth_error=ForgeAuthError("auth-failed"),
            auth_error_message="authentication failed while doing dummy work",
            failure_message="failed to do dummy work",
        )

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.FATAL_FAILURE
        assert result.retryable is False
        assert result.error_message == "auth-failed"
        err = capsys.readouterr().err
        assert "authentication failed while doing dummy work" in err
        assert "auth-failed" in err

    def test_returns_retryable_failure_when_work_raises(self, capsys):
        def _boom() -> dict:
            raise RuntimeError("boom")

        result = _run_best_effort_phase(
            phase_name="dummy_phase",
            report_label="Dummy Report",
            work=_boom,
            auth_error=None,
            auth_error_message="authentication failed while doing dummy work",
            failure_message="failed to do dummy work",
        )

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.RETRYABLE_FAILURE
        assert result.retryable is True
        assert result.error_message == "boom"
        err = capsys.readouterr().err
        assert "failed to do dummy work" in err
        assert "boom" in err

    def test_evaluate_report_overrides_status(self):
        result = _run_best_effort_phase(
            phase_name="dummy_phase",
            report_label="Dummy Report",
            work=lambda: {"status": "not_whitelisted"},
            auth_error=None,
            auth_error_message="authentication failed while doing dummy work",
            failure_message="failed to do dummy work",
            evaluate_report=lambda report: (PhaseStatus.RETRYABLE_FAILURE, True),
        )

        assert result.status == PhaseStatus.RETRYABLE_FAILURE
        assert result.retryable is True
        assert result.report == {"status": "not_whitelisted"}


class TestPollPendingNotNeededReviews:
    """#150/#282: 保留中のstatus:not-needed検証レビューのポーリング（ベストエフォート）。"""

    def test_returns_report_on_success(self, tmp_path):
        args = argparse.Namespace(not_needed_review_state_path=tmp_path / "s.json")
        with patch(
            "orchestune.dispatcher.process_pending_not_needed_reviews",
            return_value={"processed": 1},
        ) as mock_poll:
            result = _poll_pending_not_needed_reviews(args)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        assert result.report == {"processed": 1}
        mock_poll.assert_called_once_with(args.not_needed_review_state_path, forge=ANY)

    def test_returns_none_and_warns_on_failure(self, tmp_path, capsys):
        args = argparse.Namespace(not_needed_review_state_path=tmp_path / "s.json")
        with patch(
            "orchestune.dispatcher.process_pending_not_needed_reviews",
            side_effect=RuntimeError("boom"),
        ):
            result = _poll_pending_not_needed_reviews(args)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.RETRYABLE_FAILURE
        assert result.retryable is True
        assert "boom" in result.error_message
        assert "boom" in capsys.readouterr().err

    def test_returns_fatal_failure_on_forge_auth_error(self, tmp_path, capsys):
        args = argparse.Namespace(not_needed_review_state_path=tmp_path / "s.json")
        result = _poll_pending_not_needed_reviews(
            args, auth_error=ForgeAuthError("auth-failed")
        )

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.FATAL_FAILURE
        assert result.retryable is False
        assert "auth-failed" in result.error_message
        assert "auth-failed" in capsys.readouterr().err


class TestRunSemanticIntegrator:
    """#150: Integrator実行と、クラウドルーチン利用時のみ意味的レビューを
    有効化する分岐（ベストエフォート）。"""

    def test_enables_semantic_review_for_cloud_routine_target(self):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=ClaudeCodeCloudRoutineDispatchTarget("rid", "rtok"),
        )
        mock_instance = MagicMock()
        mock_instance.run.return_value = {"status": "success", "ok": True}
        with (
            patch(
                "orchestune.dispatcher.Integrator", return_value=mock_instance
            ) as mock_integrator_cls,
            patch(
                "orchestune.dispatcher.IntegrationCoordinator"
            ) as mock_coordinator_cls,
        ):
            result = _run_semantic_integrator(config, semantic_review_enabled=True)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        assert result.report == {"status": "success", "ok": True}
        integrator_config = mock_integrator_cls.call_args.args[0]
        assert integrator_config.enable_semantic_review is True
        mock_coordinator_cls.assert_called_once_with(config.dispatch_target)

    def test_disables_semantic_review_when_flag_off(self):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=ClaudeCodeCloudRoutineDispatchTarget("rid", "rtok"),
        )
        mock_instance = MagicMock()
        mock_instance.run.return_value = {"status": "success", "ok": True}
        with patch(
            "orchestune.dispatcher.Integrator", return_value=mock_instance
        ) as mock_integrator_cls:
            result = _run_semantic_integrator(config, semantic_review_enabled=False)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        integrator_config = mock_integrator_cls.call_args.args[0]
        assert integrator_config.enable_semantic_review is False

    def test_disables_semantic_review_for_non_cloud_routine_target(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=LocalProcessDispatchTarget(log_dir=tmp_path / "logs"),
        )
        mock_instance = MagicMock()
        mock_instance.run.return_value = {"status": "success", "ok": True}
        with patch(
            "orchestune.dispatcher.Integrator", return_value=mock_instance
        ) as mock_integrator_cls:
            result = _run_semantic_integrator(config, semantic_review_enabled=True)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        integrator_config = mock_integrator_cls.call_args.args[0]
        assert integrator_config.enable_semantic_review is False

    def test_returns_none_and_warns_on_failure(self, capsys):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        with patch(
            "orchestune.dispatcher.Integrator", side_effect=RuntimeError("boom")
        ):
            result = _run_semantic_integrator(config, semantic_review_enabled=False)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.RETRYABLE_FAILURE
        assert result.retryable is True
        assert "boom" in result.error_message
        assert "boom" in capsys.readouterr().err

    def test_returns_retryable_failure_when_report_has_failed_tasks(self):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        mock_instance = MagicMock()
        mock_instance.run.return_value = {"status": "failure", "failed": ["task-1"]}
        with patch("orchestune.dispatcher.Integrator", return_value=mock_instance):
            result = _run_semantic_integrator(config, semantic_review_enabled=False)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.RETRYABLE_FAILURE
        assert result.retryable is True
        assert result.report == {"status": "failure", "failed": ["task-1"]}

    @pytest.mark.parametrize(
        "error_status",
        [
            "failed_to_create_temp_worktree",
            "failed_to_create_temp_branch",
            "failed_to_push_temp_branch",
            "auto_merge_failed",
            "integration_branch_locked",
        ],
    )
    def test_returns_retryable_failure_for_pipeline_error_statuses_without_failed_key(
        self, error_status
    ):
        """#207: パイプラインが早期returnするエラーステータスは`failed`キーを
        伴わないため、ホワイトリスト方式（success/no_done_tasks以外は失敗）で
        判定されなければならない。"""
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        mock_instance = MagicMock()
        mock_instance.run.return_value = {"status": error_status, "error": "boom"}
        with patch("orchestune.dispatcher.Integrator", return_value=mock_instance):
            result = _run_semantic_integrator(config, semantic_review_enabled=False)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.RETRYABLE_FAILURE
        assert result.retryable is True
        assert result.report == {"status": error_status, "error": "boom"}

    @pytest.mark.parametrize("success_status", ["success", "no_done_tasks"])
    def test_returns_success_for_whitelisted_statuses(self, success_status):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        mock_instance = MagicMock()
        mock_instance.run.return_value = {"status": success_status}
        with patch("orchestune.dispatcher.Integrator", return_value=mock_instance):
            result = _run_semantic_integrator(config, semantic_review_enabled=False)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        assert result.retryable is False

    def test_returns_fatal_failure_on_forge_auth_error(self, capsys):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        result = _run_semantic_integrator(
            config,
            semantic_review_enabled=False,
            auth_error=ForgeAuthError("auth-failed"),
        )

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.FATAL_FAILURE
        assert result.retryable is False
        assert "auth-failed" in result.error_message
        assert "auth-failed" in capsys.readouterr().err


class TestProcessParentCompletion:
    """#170: 親Issue完了検知（best-effort）の配線を確認する。"""

    def test_returns_report_on_success(self):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            parent_issue_number=100,
            apply=True,
        )
        with patch(
            "orchestune.dispatcher.process_parent_completion",
            return_value={"status": "waiting_on_children", "open_children": [101]},
        ) as mock_process:
            result = _process_parent_completion(config)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        assert result.report == {
            "status": "waiting_on_children",
            "open_children": [101],
        }
        mock_process.assert_called_once_with(100, True, forge=ANY)

    def test_returns_none_and_warns_on_failure(self, capsys):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            parent_issue_number=100,
            apply=True,
        )
        with patch(
            "orchestune.dispatcher.process_parent_completion",
            side_effect=RuntimeError("boom"),
        ):
            result = _process_parent_completion(config)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.RETRYABLE_FAILURE
        assert result.retryable is True
        assert "boom" in result.error_message
        assert "boom" in capsys.readouterr().err

    def test_returns_fatal_failure_on_forge_auth_error(self, capsys):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            parent_issue_number=100,
            apply=True,
        )
        result = _process_parent_completion(
            config, auth_error=ForgeAuthError("auth-failed")
        )

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.FATAL_FAILURE
        assert result.retryable is False
        assert "auth-failed" in result.error_message
        assert "auth-failed" in capsys.readouterr().err


class TestDispatcherConfigLoading:
    def _empty_report(self):
        return CycleReport(
            selected=[],
            quota_slots_available=0,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=False,
        )

    def test_load_config_from_orchestune_toml(self, tmp_path):
        config_path = tmp_path / "orchestune.toml"
        config_path.write_text(
            "max-concurrent = 5\n"
            "dispatch-target = 'local'\n"
            "run-state-path = 'custom_state.json'\n",
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--no-apply"], cwd=tmp_path)

        mock_build.assert_called_once()
        assert mock_build.call_args.args[0] == "local"
        assert mock_run.called
        config_arg = mock_run.call_args.args[0]
        assert config_arg.max_concurrent == 5
        assert config_arg.run_state_path == Path("custom_state.json")

    def test_load_config_from_pyproject_toml(self, tmp_path):
        config_path = tmp_path / "pyproject.toml"
        config_path.write_text(
            "[tool.orchestune]\n"
            "max-concurrent = 7\n"
            "dispatch-target = 'claude-cli'\n",
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--no-apply"], cwd=tmp_path)

        mock_build.assert_called_once()
        assert mock_build.call_args.args[0] == "claude-cli"
        assert mock_run.called
        config_arg = mock_run.call_args.args[0]
        assert config_arg.max_concurrent == 7

    def test_cli_arg_overrides_config_file(self, tmp_path):
        config_path = tmp_path / "orchestune.toml"
        config_path.write_text(
            "max-concurrent = 5\n" "dispatch-target = 'local'\n",
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(
                [
                    "--no-apply",
                    "--max-concurrent",
                    "3",
                    "--dispatch-target",
                    "claude-cli",
                ],
                cwd=tmp_path,
            )

        mock_build.assert_called_once()
        assert mock_build.call_args.args[0] == "claude-cli"
        assert mock_run.called
        config_arg = mock_run.call_args.args[0]
        assert config_arg.max_concurrent == 3

    @pytest.mark.parametrize(
        ("config", "expected_error"),
        [
            ("dispatch-target = 'claude-clii'\n", "dispatch-target"),
            ("max-concurent = 5\n", "unknown key"),
            ('max-concurrent = "5"\n', "must be an integer"),
            ('apply = "false"\n', "must be a boolean"),
            ("max-concurrent = -1\n", "greater than or equal to 0"),
            ("window-seconds = 0\n", "greater than or equal to 1"),
            ("parent-issue = 0\n", "greater than or equal to 1"),
            ("run-state-path = 1\n", "must be a string path"),
        ],
    )
    def test_rejects_invalid_config_values(
        self, tmp_path, config, expected_error, capsys
    ):
        (tmp_path / "orchestune.toml").write_text(config, encoding="utf-8")

        with pytest.raises(SystemExit) as error:
            main(["--no-apply"], cwd=tmp_path)

        assert error.value.code == 2
        assert expected_error in capsys.readouterr().err

    def test_rejects_invalid_toml_without_falling_back_to_pyproject(
        self, tmp_path, capsys
    ):
        (tmp_path / "orchestune.toml").write_text(
            "max-concurrent = [\n", encoding="utf-8"
        )
        (tmp_path / "pyproject.toml").write_text(
            "[tool.orchestune]\nmax-concurrent = 5\n", encoding="utf-8"
        )

        with pytest.raises(SystemExit) as error:
            main(["--no-apply"], cwd=tmp_path)

        assert error.value.code == 2
        assert "failed to load" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "dispatch_target",
        [
            "local",
            "cloud-routine",
            "codex-cloud",
            "claude-cli",
            "agy-cli",
            "codex-cli",
            "auto",
        ],
    )
    def test_accepts_each_dispatch_target(self, tmp_path, dispatch_target):
        (tmp_path / "orchestune.toml").write_text(
            f"dispatch-target = '{dispatch_target}'\n", encoding="utf-8"
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
        ):
            main(["--no-apply"], cwd=tmp_path)

        assert mock_build.call_args.args[0] == dispatch_target

    def test_post_cycle_failures_in_main(self, tmp_path, capsys):
        r1 = PhaseResult("poll_pending_not_needed_reviews", PhaseStatus.SUCCESS)
        r2 = PhaseResult("run_semantic_integrator", PhaseStatus.SUCCESS)
        r2_retryable = PhaseResult(
            "run_semantic_integrator",
            PhaseStatus.RETRYABLE_FAILURE,
            error_message="retryable-error",
            retryable=True,
        )
        r2_fatal = PhaseResult(
            "run_semantic_integrator",
            PhaseStatus.FATAL_FAILURE,
            error_message="fatal-error",
        )
        r3 = PhaseResult("process_parent_completion", PhaseStatus.SUCCESS)

        # ケース1: すべて成功
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatcher._poll_pending_not_needed_reviews",
                return_value=r1,
            ),
            patch("orchestune.dispatcher._run_semantic_integrator", return_value=r2),
            patch("orchestune.dispatcher._process_parent_completion", return_value=r3),
        ):
            code = main(["--apply", "--parent-issue", "100"], cwd=tmp_path)
            assert code == 0
            out = json.loads(capsys.readouterr().out)
            assert "post_cycle_results" in out
            assert len(out["post_cycle_results"]) == 3
            assert out["post_cycle_results"][0]["status"] == "success"

        # ケース2: RETRYABLE_FAILURE
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatcher._poll_pending_not_needed_reviews",
                return_value=r1,
            ),
            patch(
                "orchestune.dispatcher._run_semantic_integrator",
                return_value=r2_retryable,
            ),
            patch("orchestune.dispatcher._process_parent_completion", return_value=r3),
        ):
            code = main(["--apply", "--parent-issue", "100"], cwd=tmp_path)
            assert code == 2
            out = json.loads(capsys.readouterr().out)
            assert out["post_cycle_results"][1]["status"] == "retryable_failure"
            assert out["post_cycle_results"][1]["error_message"] == "retryable-error"

        # ケース3: FATAL_FAILURE
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatcher._poll_pending_not_needed_reviews",
                return_value=r1,
            ),
            patch(
                "orchestune.dispatcher._run_semantic_integrator", return_value=r2_fatal
            ),
            patch("orchestune.dispatcher._process_parent_completion", return_value=r3),
        ):
            code = main(["--apply", "--parent-issue", "100"], cwd=tmp_path)
            assert code == 1
            out = json.loads(capsys.readouterr().out)
            assert out["post_cycle_results"][1]["status"] == "fatal_failure"

        # ケース4: main()のcheck_auth()自体がForgeAuthErrorを投げる場合
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.forge.GitHubForge.check_auth",
                side_effect=ForgeAuthError("main-auth-failed"),
            ),
        ):
            code = main(["--apply", "--parent-issue", "100"], cwd=tmp_path)
            assert code == 1
            out = json.loads(capsys.readouterr().out)
            assert "post_cycle_results" in out
            assert len(out["post_cycle_results"]) == 3
            for res in out["post_cycle_results"]:
                assert res["status"] == "fatal_failure"
                assert "main-auth-failed" in res["error_message"]

    def test_custom_window_seconds_preserves_launch_history_quota(self, tmp_path):
        now = time.time()
        # window_seconds = 172800 (48時間)
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=172800,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        # 36時間前 (129600秒前) の launch 記録（24時間前〜48時間前の間）
        launch_36h_ago = now - 129600.0
        launch_1h_ago = now - 3600.0

        save_run_state(
            RunState(
                launch_history=[launch_36h_ago, launch_1h_ago],
            ),
            config.run_state_path,
            launch_window_seconds=config.window_seconds,
        )

        # 48時間の window_seconds なので、36時間前の起動も記録に残っているはず
        loaded = load_run_state(config.run_state_path)
        assert len(loaded.launch_history) == 2

        # 2回起動済み（max_launches_per_window=2）のため新規起動がブロックされる
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
        ):
            mock_list.side_effect = lambda label, **_: (
                [_issue(10, subtask_id="t10")] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)
            # 48時間窓で2回に達しているため起動不可
            assert len(report.selected) == 0
