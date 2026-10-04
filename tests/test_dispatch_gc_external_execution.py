"""#1154: 外部実行の停止未確認時は台帳・ハンドル・枠を保持する。"""

from unittest.mock import patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.external_execution import (
    ACTION_EXTERNAL_EXECUTION_HELD,
    hold_if_not_stopped,
    observe_runtime_state,
)
from orchestune.dispatch.gc import _apply_stale_active_entry_discard
from orchestune.dispatch.gc.zombies import (
    ZombieOrTimeoutReclaim,
    _apply_zombie_or_timeout_reclaim,
)
from orchestune.ledger.run_state import RunState
from tests.dispatch_gc_test_support import _active


def _config(tmp_path, fake_forge, **overrides) -> DispatcherConfig:
    values = {
        "events_log_path": tmp_path / "events.jsonl",
        "run_state_path": tmp_path / "run_state.json",
        "forge": fake_forge,
        "apply": True,
        "task_timeout_seconds": 60,
    }
    values.update(overrides)
    return DispatcherConfig(parent_issue_number=100, **values)


def _external(tmp_path):
    return _active(
        worktree_path=str(tmp_path / "missing"),
        pid=None,
        external_id="task_cloud_1",
        started_at=1.0,
    )


def _reclaim(active, *, is_timeout=True):
    return ZombieOrTimeoutReclaim(
        key="280",
        active=active,
        subtask_id="task-a",
        reason="timeout",
        is_timeout=is_timeout,
        process_alive=False,
        status_labels=("status:in-progress",),
    )


@pytest.mark.parametrize("state", ["running", "unknown"])
def test_timeout_reclaim_holds_slot_unless_stopped(tmp_path, fake_forge, state):
    active = _external(tmp_path)
    run_state = RunState(active_worktrees={"280": active})
    config = _config(tmp_path, fake_forge)
    with (
        patch.object(config.dispatch_target, "execution_status", return_value=state),
        patch("orchestune.dispatch.gc.zombies.os.kill") as kill,
        patch("orchestune.dispatch.gc.zombies.remove_worktree") as remove,
    ):
        event = _apply_zombie_or_timeout_reclaim(run_state, _reclaim(active), config)

    assert event.to_dict()["action"] == ACTION_EXTERNAL_EXECUTION_HELD
    assert event.to_dict()["runtime_state"] == state
    assert event.to_dict()["reason"] == "timeout"
    assert "280" in run_state.active_worktrees
    assert not run_state.task_reclaim_counts
    kill.assert_not_called()
    remove.assert_not_called()
    fake_forge.add_label.assert_any_call(280, "status:blocked-human-review")


def test_timeout_reclaim_hold_does_not_renotify_when_already_in_human_review(
    tmp_path, fake_forge
):
    active = _external(tmp_path)
    run_state = RunState(active_worktrees={"280": active})
    config = _config(tmp_path, fake_forge)
    reclaim = _reclaim(active)
    reclaim = ZombieOrTimeoutReclaim(
        **{
            **reclaim.__dict__,
            "status_labels": ("status:blocked-human-review",),
        }
    )
    with patch.object(
        config.dispatch_target, "execution_status", return_value="unknown"
    ):
        event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

    assert event.to_dict()["action"] == ACTION_EXTERNAL_EXECUTION_HELD
    fake_forge.add_label.assert_not_called()
    fake_forge.add_comment.assert_not_called()
    assert "280" in run_state.active_worktrees


def test_dry_run_reports_hold_without_writes(tmp_path, fake_forge):
    active = _external(tmp_path)
    run_state = RunState(active_worktrees={"280": active})
    config = _config(tmp_path, fake_forge, apply=False)
    with patch.object(
        config.dispatch_target, "execution_status", return_value="unknown"
    ):
        event = _apply_zombie_or_timeout_reclaim(run_state, _reclaim(active), config)

    assert event.to_dict()["action"] == ACTION_EXTERNAL_EXECUTION_HELD
    fake_forge.add_label.assert_not_called()
    fake_forge.remove_label.assert_not_called()
    fake_forge.add_comment.assert_not_called()
    assert "280" in run_state.active_worktrees


@pytest.mark.parametrize("state", ["running", "unknown"])
def test_stale_discard_holds_external_entry(tmp_path, fake_forge, state):
    active = _external(tmp_path)
    run_state = RunState(active_worktrees={"280": active})
    config = _config(tmp_path, fake_forge)
    events: list[dict] = []
    with patch.object(config.dispatch_target, "execution_status", return_value=state):
        discarded = _apply_stale_active_entry_discard(
            run_state,
            "280",
            active,
            "label removed",
            config,
            status_labels=("status:queued",),
            events=events,
        )

    assert discarded is False
    assert "280" in run_state.active_worktrees
    assert [e.to_dict()["action"] for e in events] == [ACTION_EXTERNAL_EXECUTION_HELD]
    fake_forge.add_label.assert_any_call(280, "status:blocked-human-review")


def test_stale_discard_proceeds_when_provider_confirms_stopped(tmp_path, fake_forge):
    active = _external(tmp_path)
    run_state = RunState(active_worktrees={"280": active})
    config = _config(tmp_path, fake_forge)
    with patch.object(
        config.dispatch_target, "execution_status", return_value="stopped"
    ):
        discarded = _apply_stale_active_entry_discard(
            run_state, "280", active, "label removed", config
        )

    assert discarded is True
    assert "280" not in run_state.active_worktrees


def test_local_execution_is_not_held_by_external_guard(tmp_path, fake_forge):
    active = _active(worktree_path=str(tmp_path / "missing"), pid=999)
    config = _config(tmp_path, fake_forge)
    assert hold_if_not_stopped(active, config, "stale") is None


def test_default_target_without_runtime_support_is_unknown(tmp_path, fake_forge):
    active = _external(tmp_path)
    config = _config(tmp_path, fake_forge)
    assert observe_runtime_state(active, config) == "unknown"


def test_runtime_status_exception_and_invalid_value_are_unknown(tmp_path, fake_forge):
    active = _external(tmp_path)
    config = _config(tmp_path, fake_forge)
    with patch.object(
        config.dispatch_target, "execution_status", side_effect=RuntimeError("boom")
    ):
        assert observe_runtime_state(active, config) == "unknown"
    with patch.object(config.dispatch_target, "execution_status", return_value="done"):
        assert observe_runtime_state(active, config) == "unknown"


class TestCompletedCloudHoldNotice:
    """#1154レビュー対応: 成果物完了でも停止未確認なら枠を保持し、理由をIssueへ残す。

    完了予約・結果ラベルを人間確認ラベルで上書きしないため、ラベルは変えない。
    """

    def _run(self, fake_forge, *, apply=True):
        from orchestune.dispatch.gc import _rule_completed
        from tests.dispatch_gc_test_support import _rule_ctx, _task

        active = _active(external_id="session-1")
        ctx = _rule_ctx(forge=fake_forge)
        ctx.config.apply = apply
        ctx.run_state.active_worktrees["1"] = active
        target = ctx.config.dispatch_target
        with (
            patch.object(
                target, "completion_status", return_value="completed", create=True
            ),
            patch.object(target, "execution_status", return_value="unknown"),
        ):
            outcome = _rule_completed(
                ctx, "1", active, _task(status_labels=("status:in-progress",))
            )
        return ctx, outcome

    def test_posts_notice_without_changing_labels(self, fake_forge):
        fake_forge.list_comments.return_value = []
        ctx, outcome = self._run(fake_forge)

        assert outcome is not None and outcome.terminal is True
        assert (
            outcome.completion_event.to_dict()["action"]
            == ACTION_EXTERNAL_EXECUTION_HELD
        )
        assert outcome.completion_event.to_dict()["reason"] == "completion"
        assert "1" in ctx.run_state.active_worktrees
        fake_forge.add_label.assert_not_called()
        fake_forge.remove_label.assert_not_called()
        fake_forge.add_comment.assert_called_once()
        issue_number, body = fake_forge.add_comment.call_args.args
        assert issue_number == 280
        assert "<!-- orchestune:notice:external-execution-held -->" in body
        assert "session-1" in body

    def test_does_not_repeat_unchanged_notice(self, fake_forge):
        fake_forge.list_comments.return_value = []
        self._run(fake_forge)
        posted = fake_forge.add_comment.call_args.args[1]
        fake_forge.add_comment.reset_mock()
        fake_forge.list_comments.return_value = [{"body": posted}]

        self._run(fake_forge)

        fake_forge.add_comment.assert_not_called()

    def test_dry_run_does_not_post(self, fake_forge):
        fake_forge.list_comments.return_value = []
        ctx, outcome = self._run(fake_forge, apply=False)

        assert (
            outcome.completion_event.to_dict()["action"]
            == ACTION_EXTERNAL_EXECUTION_HELD
        )
        fake_forge.add_comment.assert_not_called()
        fake_forge.list_comments.assert_not_called()
