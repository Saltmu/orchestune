from unittest.mock import patch

from orchestune.dispatch_actor_verification import (
    ActorVerificationDecision,
    _apply_actor_verification,
    _decide_actor_verification,
)
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_scoring import Task


def _task(issue_number=1, status_labels=("status:queued",)):
    return Task(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=status_labels,
        created_at="2026-01-01T00:00:00+00:00",
    )


class TestDecideActorVerification:
    """#119: status:queuedラベルを付与したactorのリポジトリ権限を判定する
    （読み取りのみで、ラベル等の書き換えは行わない）。"""

    def test_authorized_when_permission_is_write(self, fake_forge):
        task = _task(1)
        fake_forge.get_label_actor.return_value = "alice"
        fake_forge.get_actor_permission.return_value = "write"
        decisions = _decide_actor_verification([task], forge=fake_forge)
        assert decisions == [
            ActorVerificationDecision(
                task=task, actor="alice", permission="write", is_authorized=True
            )
        ]

    def test_authorized_for_triage_and_maintain_and_admin(self, fake_forge):
        task = _task(1)
        for permission in ("triage", "maintain", "admin"):
            fake_forge.get_label_actor.return_value = "alice"
            fake_forge.get_actor_permission.return_value = permission
            decisions = _decide_actor_verification([task], forge=fake_forge)
            assert decisions[0].is_authorized is True

    def test_unauthorized_when_permission_is_read(self, fake_forge):
        task = _task(1)
        fake_forge.get_label_actor.return_value = "mallory"
        fake_forge.get_actor_permission.return_value = "read"
        decisions = _decide_actor_verification([task], forge=fake_forge)
        assert decisions[0].is_authorized is False

    def test_unauthorized_when_permission_is_none(self, fake_forge):
        task = _task(1)
        fake_forge.get_label_actor.return_value = "mallory"
        fake_forge.get_actor_permission.return_value = "none"
        decisions = _decide_actor_verification([task], forge=fake_forge)
        assert decisions[0].is_authorized is False

    def test_looks_up_status_queued_label_specifically(self, fake_forge):
        task = _task(42)
        fake_forge.get_label_actor.return_value = "alice"
        fake_forge.get_actor_permission.return_value = "write"
        _decide_actor_verification([task], forge=fake_forge)
        fake_forge.get_label_actor.assert_called_once_with(42, "status:queued")

    def test_empty_candidate_list_returns_empty(self):
        assert _decide_actor_verification([]) == []

    def test_unauthorized_when_actor_is_empty_ghost_user(self, fake_forge):
        """#208: labeledイベント無し・author.loginも取得できないghostユーザーは
        空文字actorとして扱われ、権限`none`＝未認可として判定される。"""
        task = _task(1)
        fake_forge.get_label_actor.return_value = ""
        fake_forge.get_actor_permission.return_value = "none"
        decisions = _decide_actor_verification([task], forge=fake_forge)
        assert decisions == [
            ActorVerificationDecision(
                task=task, actor="", permission="none", is_authorized=False
            )
        ]

    def test_does_not_crash_when_get_actor_permission_raises(self, fake_forge):
        """#208: get_actor_permissionがValueErrorを送出しても
        サイクル全体をクラッシュさせず、当該タスクを未認可として扱う。"""
        task = _task(1)
        fake_forge.get_label_actor.return_value = ""
        fake_forge.get_actor_permission.side_effect = ValueError(
            "ユーザー名が不正です: ''"
        )
        decisions = _decide_actor_verification([task], forge=fake_forge)
        assert len(decisions) == 1
        assert decisions[0].is_authorized is False
        assert decisions[0].permission == "none"

    def test_continues_to_next_task_after_one_raises(self, fake_forge):
        """#208: 1件のタスクで例外が発生しても、後続タスクの判定は継続される。"""
        task_bad, task_ok = _task(1), _task(2)
        fake_forge.get_label_actor.side_effect = ["", "alice"]
        fake_forge.get_actor_permission.side_effect = [ValueError("boom"), "write"]
        decisions = _decide_actor_verification([task_bad, task_ok], forge=fake_forge)
        assert len(decisions) == 2
        assert decisions[0].is_authorized is False
        assert decisions[1] == ActorVerificationDecision(
            task=task_ok, actor="alice", permission="write", is_authorized=True
        )


class TestDecideActorVerificationWithFakeForge:
    """#293: `mock.patch`によるグローバルなクラスメソッド差し替えではなく、
    `forge`引数への注入だけでテストが書けることを示す。"""

    def test_uses_injected_fake_forge_instead_of_patching(self):
        from unittest.mock import MagicMock

        task = _task(1)
        fake_forge = MagicMock()
        fake_forge.get_label_actor.return_value = "alice"
        fake_forge.get_actor_permission.return_value = "write"

        decisions = _decide_actor_verification([task], forge=fake_forge)

        assert decisions == [
            ActorVerificationDecision(
                task=task, actor="alice", permission="write", is_authorized=True
            )
        ]
        fake_forge.get_label_actor.assert_called_once_with(1, "status:queued")
        fake_forge.get_actor_permission.assert_called_once_with("alice")


class TestApplyActorVerification:
    def test_authorized_task_stays_in_candidates(self, tmp_path):
        task = _task(1)
        decisions = [
            ActorVerificationDecision(
                task=task, actor="alice", permission="write", is_authorized=True
            )
        ]
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        with patch(
            "orchestune.dispatch_actor_verification.apply_human_review_escalation"
        ) as mock_escalate:
            result = _apply_actor_verification(decisions, config)
        assert result == [task]
        mock_escalate.assert_not_called()

    def test_unauthorized_task_is_excluded_and_escalated_when_apply_true(
        self, tmp_path
    ):
        task = _task(1, status_labels=("status:queued",))
        decisions = [
            ActorVerificationDecision(
                task=task, actor="mallory", permission="read", is_authorized=False
            )
        ]
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        with patch(
            "orchestune.dispatch_actor_verification.apply_human_review_escalation"
        ) as mock_escalate:
            result = _apply_actor_verification(decisions, config)
        assert result == []
        mock_escalate.assert_called_once()
        args = mock_escalate.call_args.args
        assert args[0] == 1
        assert args[1] == ("status:queued",)
        assert "mallory" in args[2]
        assert "read" in args[2]

    def test_unauthorized_task_excluded_but_not_escalated_when_apply_false(
        self, tmp_path
    ):
        task = _task(1)
        decisions = [
            ActorVerificationDecision(
                task=task, actor="mallory", permission="none", is_authorized=False
            )
        ]
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl", apply=False
        )
        with patch(
            "orchestune.dispatch_actor_verification.apply_human_review_escalation"
        ) as mock_escalate:
            result = _apply_actor_verification(decisions, config)
        assert result == []
        mock_escalate.assert_not_called()

    def test_mixed_decisions_keep_only_authorized(self, tmp_path):
        task_ok = _task(1)
        task_bad = _task(2)
        decisions = [
            ActorVerificationDecision(
                task=task_ok, actor="alice", permission="write", is_authorized=True
            ),
            ActorVerificationDecision(
                task=task_bad, actor="mallory", permission="read", is_authorized=False
            ),
        ]
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        with patch(
            "orchestune.dispatch_actor_verification.apply_human_review_escalation"
        ):
            result = _apply_actor_verification(decisions, config)
        assert result == [task_ok]
