"""#787: Forge API障害で判定を保留したことを、無言にせず警告として出す。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc import (
    _completion_forge_error_hold,
    _resolve_local_completion,
)
from orchestune.dispatch.gc.completion import (
    CompletedWorktreeDecision,
    ForgeFailure,
    _apply_completed_worktree_outcome,
    _cloud_worktree_completion_status,
    _decide_completed_worktree_outcome,
    _fetch_outcome_for_active,
    _local_pr_completion_status,
)
from orchestune.dispatch.state import ActiveWorktree
from orchestune.dispatch.summary import WARN_PREFIX
from orchestune.models import PrRecord


def _active(tmp_path, **overrides):
    defaults = dict(
        issue_number=702,
        branch="claude/issue-702-task-a",
        worktree_path=str(tmp_path / "w1"),
        pid=123,
        started_at=1_700_000_000.0,
        declared_footprint=(),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


def _config(tmp_path, forge):
    return DispatcherConfig(
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        forge=forge,
    )


class TestLocalPrCompletionStatus:
    def test_warns_and_holds_when_list_prs_fails(self, tmp_path, capsys):
        forge = MagicMock()
        forge.list_prs.side_effect = RuntimeError("504 Gateway Timeout")

        assert (
            _local_pr_completion_status(_active(tmp_path), _config(tmp_path, forge))
            == "unknown"
        )

        captured = capsys.readouterr().err
        assert WARN_PREFIX in captured
        assert "list_prs" in captured
        assert "#702" in captured
        assert "RuntimeError" in captured

    def test_collects_the_error_description_for_the_cycle_report(self, tmp_path):
        forge = MagicMock()
        forge.list_prs.side_effect = RuntimeError("504 Gateway Timeout")
        failures: list[ForgeFailure] = []

        _local_pr_completion_status(
            _active(tmp_path), _config(tmp_path, forge), error_sink=failures
        )

        assert failures == [
            ForgeFailure("list_prs", "RuntimeError: 504 Gateway Timeout")
        ]

    def test_warning_is_ascii_only(self, tmp_path, capsys):
        forge = MagicMock()
        forge.list_prs.side_effect = RuntimeError("504 Gateway Timeout")
        _local_pr_completion_status(_active(tmp_path), _config(tmp_path, forge))
        capsys.readouterr().err.encode("ascii")

    def test_stays_silent_on_success(self, tmp_path, capsys):
        forge = MagicMock()
        forge.list_prs.return_value = []
        _local_pr_completion_status(_active(tmp_path), _config(tmp_path, forge))
        assert WARN_PREFIX not in capsys.readouterr().err


class TestCloudWorktreeCompletionStatus:
    def test_warns_when_the_dispatch_target_lookup_fails(self, tmp_path, capsys):
        target = MagicMock()
        target.completion_status.side_effect = RuntimeError("connection reset")
        config = _config(tmp_path, MagicMock())
        config.dispatch_target = target
        active = _active(tmp_path, external_id="cloud-1")

        assert _cloud_worktree_completion_status(active, config) == "unknown"

        captured = capsys.readouterr().err
        assert WARN_PREFIX in captured
        assert "#702" in captured


class TestFetchOutcomeForActive:
    def test_warns_when_comments_cannot_be_read(self, tmp_path, capsys):
        forge = MagicMock()
        forge.list_comments.side_effect = RuntimeError("502 Bad Gateway")

        assert _fetch_outcome_for_active(_active(tmp_path), forge) == "error"

        captured = capsys.readouterr().err
        assert WARN_PREFIX in captured
        assert "list_comments" in captured


class TestCompletionForgeErrorHold:
    def test_event_carries_the_operation_and_error(self, tmp_path):
        outcome = _completion_forge_error_hold(
            _active(tmp_path),
            operation="list_prs",
            error="RuntimeError: 504 Gateway Timeout",
        )
        assert outcome.completion_event["operation"] == "list_prs"
        assert outcome.completion_event["error"] == "RuntimeError: 504 Gateway Timeout"

    def test_operation_and_error_are_optional(self, tmp_path):
        outcome = _completion_forge_error_hold(_active(tmp_path))
        assert outcome.completion_event["action"] == "completion_skipped_forge_error"


class TestCompletedWorktreeDecisionCarriesForgeError:
    """PR#789レビュー(Codex P2): stderrが失われた後も、保存されたレポートから
    どのForge呼び出しがなぜ失敗したのかを特定できるようにする。"""

    def test_comment_fetch_failure_reaches_the_completion_event(self, tmp_path):
        forge = MagicMock()
        forge.list_comments.side_effect = RuntimeError("502 Bad Gateway")
        worktree = tmp_path / "w1"
        worktree.mkdir()

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=True,
            ),
        ):
            decision = _decide_completed_worktree_outcome(
                _active(tmp_path, worktree_path=str(worktree)), None, forge=forge
            )

        assert decision.action == "completion_skipped_forge_error"
        assert decision.operation == "list_comments"
        assert decision.error == "RuntimeError: 502 Bad Gateway"

    def test_event_renders_the_operation_and_error(self, tmp_path):
        config = _config(tmp_path, MagicMock())
        event = _apply_completed_worktree_outcome(
            _active(tmp_path),
            CompletedWorktreeDecision(
                action="completion_skipped_forge_error",
                operation="list_comments",
                error="RuntimeError: 502 Bad Gateway",
            ),
            config,
        )

        assert event["operation"] == "list_comments"
        # 同一の障害で複数の呼び出しが落ちても、説明は畳んで1つに保つ。
        assert event["error"] == "RuntimeError: 502 Bad Gateway"


class TestOpenPrCommentFailures:
    """PR#789レビュー(Codex P2): オープンPR経路のコメント取得失敗も、どの呼び出しが
    失敗したのかを警告とレポートへ伝える。"""

    def _open_pr_config(self, tmp_path):
        forge = MagicMock()
        forge.list_prs.return_value = [
            PrRecord(
                number=1,
                head_ref="claude/issue-702-task-a",
                changed_files=(),
                state="OPEN",
            )
        ]
        forge.list_comments.side_effect = RuntimeError("502 Bad Gateway")
        return forge, _config(tmp_path, forge)

    def test_warns_and_names_list_comments_not_list_prs(self, tmp_path, capsys):
        forge, config = self._open_pr_config(tmp_path)
        failures: list[ForgeFailure] = []

        status = _local_pr_completion_status(_active(tmp_path), config, failures)

        assert status == "unknown"
        # Issue側とPR側の2回の`list_comments`が落ちるので記録も2件になる。
        assert {failure.operation for failure in failures} == {"list_comments"}
        assert failures[0].description == "RuntimeError: 502 Bad Gateway"
        captured = capsys.readouterr().err
        assert WARN_PREFIX in captured
        assert "list_comments" in captured

    def test_hold_event_names_the_call_that_actually_failed(self, tmp_path):
        forge, config = self._open_pr_config(tmp_path)
        ctx = SimpleNamespace(config=config)
        active = _active(tmp_path)

        with patch("orchestune.dispatch.gc._is_worktree_complete", return_value=True):
            resolution = _resolve_local_completion(ctx, "702", active, None)

        event = resolution.rule_outcome.completion_event
        assert event["operation"] == "list_comments"
        # 同一の障害で複数の呼び出しが落ちても、説明は畳んで1つに保つ。
        assert event["error"] == "RuntimeError: 502 Bad Gateway"


class TestWarningEncoding:
    def test_non_ascii_exception_text_does_not_break_the_warning(
        self, tmp_path, capsys
    ):
        """cp932のコンソールでも壊れないよう、stderrへ出す前にASCIIへ落とす。"""
        forge = MagicMock()
        forge.list_prs.side_effect = RuntimeError("接続に失敗しました")
        failures: list[ForgeFailure] = []

        _local_pr_completion_status(
            _active(tmp_path), _config(tmp_path, forge), failures
        )

        capsys.readouterr().err.encode("ascii")
        # レポートはUTF-8で書かれるため、原文をそのまま残す。
        assert failures[0].description == "RuntimeError: 接続に失敗しました"
