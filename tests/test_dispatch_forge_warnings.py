"""#787: Forge API障害で判定を保留したことを、無言にせず警告として出す。"""

from __future__ import annotations

from unittest.mock import MagicMock

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc import _completion_forge_error_hold
from orchestune.dispatch.gc.completion import (
    _cloud_worktree_completion_status,
    _fetch_outcome_for_active,
    _local_pr_completion_status,
)
from orchestune.dispatch.state import ActiveWorktree
from orchestune.dispatch.summary import WARN_PREFIX


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
        errors: list[str] = []

        _local_pr_completion_status(
            _active(tmp_path), _config(tmp_path, forge), error_sink=errors
        )

        assert errors == ["RuntimeError: 504 Gateway Timeout"]

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
