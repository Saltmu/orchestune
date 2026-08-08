"""dispatch cycle後のベストエフォート後処理オーケストレーション（dispatch_postcycle.py）のテスト。

`tests/test_dispatcher.py`の肥大化解消のため分割した`test_dispatcher_cli.py`
（#349）から、`dispatcher.py`のCLI配線とは独立して検証すべきpost-cycle
フェーズ本体（`_run_best_effort_phase`とその利用箇所）のテストをさらに
`dispatch_postcycle`モジュールの新設（アーキテクチャ層L3への切り出し）に
合わせて分離している。
"""

import argparse
import tempfile
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import CycleReport
from orchestune.dispatch_postcycle import (
    _decide_semantic_review_enabled,
    _poll_pending_not_needed_reviews,
    _post_event_log_comment,
    _process_parent_completion,
    _run_best_effort_phase,
    _run_semantic_integrator,
)
from orchestune.dispatch_result import PhaseResult, PhaseStatus
from orchestune.dispatch_targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    LocalProcessDispatchTarget,
)
from orchestune.forge import ForgeAuthError

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


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
            "orchestune.dispatch_postcycle.process_pending_not_needed_reviews",
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
            "orchestune.dispatch_postcycle.process_pending_not_needed_reviews",
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
                "orchestune.dispatch_postcycle.Integrator", return_value=mock_instance
            ) as mock_integrator_cls,
            patch(
                "orchestune.dispatch_postcycle.IntegrationCoordinator"
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
            "orchestune.dispatch_postcycle.Integrator", return_value=mock_instance
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
            "orchestune.dispatch_postcycle.Integrator", return_value=mock_instance
        ) as mock_integrator_cls:
            result = _run_semantic_integrator(config, semantic_review_enabled=True)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        integrator_config = mock_integrator_cls.call_args.args[0]
        assert integrator_config.enable_semantic_review is False

    def test_propagates_ci_command_from_dispatcher_config(self, tmp_path):
        """#394: `DispatcherConfig.ci_command`が`IntegratorConfig.ci_command`
        へそのまま伝播すること。"""
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=LocalProcessDispatchTarget(log_dir=tmp_path / "logs"),
            ci_command=["make", "ci"],
        )
        mock_instance = MagicMock()
        mock_instance.run.return_value = {"status": "success", "ok": True}
        with patch(
            "orchestune.dispatch_postcycle.Integrator", return_value=mock_instance
        ) as mock_integrator_cls:
            _run_semantic_integrator(config, semantic_review_enabled=False)

        integrator_config = mock_integrator_cls.call_args.args[0]
        assert integrator_config.ci_command == ["make", "ci"]

    def test_ci_command_none_when_unset(self, tmp_path):
        """#394: `DispatcherConfig.ci_command`未設定時は`IntegratorConfig.ci_command`
        も`None`のままで、Integrator側の既定値フォールバックに委ねる。"""
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=LocalProcessDispatchTarget(log_dir=tmp_path / "logs"),
        )
        mock_instance = MagicMock()
        mock_instance.run.return_value = {"status": "success", "ok": True}
        with patch(
            "orchestune.dispatch_postcycle.Integrator", return_value=mock_instance
        ) as mock_integrator_cls:
            _run_semantic_integrator(config, semantic_review_enabled=False)

        integrator_config = mock_integrator_cls.call_args.args[0]
        assert integrator_config.ci_command is None

    def test_returns_none_and_warns_on_failure(self, capsys):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        with patch(
            "orchestune.dispatch_postcycle.Integrator", side_effect=RuntimeError("boom")
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
        with patch(
            "orchestune.dispatch_postcycle.Integrator", return_value=mock_instance
        ):
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
        with patch(
            "orchestune.dispatch_postcycle.Integrator", return_value=mock_instance
        ):
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
        with patch(
            "orchestune.dispatch_postcycle.Integrator", return_value=mock_instance
        ):
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
            "orchestune.dispatch_postcycle.process_parent_completion",
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
            "orchestune.dispatch_postcycle.process_parent_completion",
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


class TestPostEventLogComment:
    """#396: ディスパッチサイクルの意思決定ログを親Issueへコメント投稿する。

    当初はGitHub Actions artifactへのアップロードを想定していたが、
    ローカル実行・Codex Cloud等Actions以外の環境では成立しないため、
    全実行環境で共通の`gh`（Forge.add_comment）経由で親Issueへ投稿する
    方式に変更した（issue #396のコメント参照）。ベストエフォート処理。
    """

    def _report_with_events(self) -> CycleReport:
        return CycleReport(
            selected=[],
            quota_slots_available=1,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[{"subtask_id": "a", "reason": "footprint deviation"}],
            completion_events=[],
            promotion_events=[],
            applied=True,
        )

    def _empty_report(self) -> CycleReport:
        return CycleReport(
            selected=[],
            quota_slots_available=1,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=True,
        )

    def test_posts_comment_when_cycle_has_events(self):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            parent_issue_number=100,
            apply=True,
            forge=MagicMock(),
        )

        result = _post_event_log_comment(config, self._report_with_events())

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        config.forge.add_comment.assert_called_once()
        posted_issue_number, posted_body = config.forge.add_comment.call_args.args
        assert posted_issue_number == 100
        assert "footprint deviation" in posted_body

    def test_skips_comment_when_cycle_has_no_events(self):
        """頻繁なディスパッチサイクルで親Issueが空コメントに埋め尽くされる
        のを防ぐため、何も起きなかったサイクルではコメントを投稿しない。"""
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            parent_issue_number=100,
            apply=True,
            forge=MagicMock(),
        )

        result = _post_event_log_comment(config, self._empty_report())

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        config.forge.add_comment.assert_not_called()
        assert result.report is not None
        assert result.report["posted"] is False

    def test_returns_retryable_failure_when_add_comment_raises(self, capsys):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            parent_issue_number=100,
            apply=True,
            forge=MagicMock(),
        )
        config.forge.add_comment.side_effect = RuntimeError("boom")

        result = _post_event_log_comment(config, self._report_with_events())

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

        result = _post_event_log_comment(
            config,
            self._report_with_events(),
            auth_error=ForgeAuthError("auth-failed"),
        )

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.FATAL_FAILURE
        assert result.retryable is False
        assert "auth-failed" in result.error_message
        assert "auth-failed" in capsys.readouterr().err
