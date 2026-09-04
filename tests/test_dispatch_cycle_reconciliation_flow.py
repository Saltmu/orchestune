"""dispatch_cycle内の状態調整・整合性修復（dispatch_reconciliation.py関連）テスト。

`tests/test_dispatch_cycle.py`の肥大化解消のため、二重ステータス整合・
blocked昇格・自己修復・footprint逸脱recompute後の自動復帰系を分割している
（#343）。
"""

import subprocess
from contextlib import ExitStack, contextmanager
from unittest.mock import ANY, patch

import pytest

from orchestune.consistency.invariants.status import (
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
    PRIMARY_STATUS_CONFLICT,
)
from orchestune.consistency.repairs.status import (
    COMMAND_REMOVE_LABEL,
    COMMAND_TRANSITION_LABEL,
)
from orchestune.consistency.supervisor import ConsistencyMode
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import (
    run_dispatch_cycle,
)
from orchestune.dispatch.locks import ExternalLockScanResult
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import (
    ActiveWorktree,
    RunState,
    save_run_state,
)
from orchestune.dispatch.status_repair import (
    execute_status_repair_command as execute_status_repair_command_real,
)
from orchestune.models import IssueRecord
from orchestune.outcome_record import OutcomeRecord
from tests.conftest import make_issue


def _task(**overrides):
    defaults = dict(
        issue_number=1,
        subtask_id="task-a",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:in-progress",),
        created_at="2026-01-01T00:00:00+00:00",
        depends_on=(),
    )
    defaults.update(overrides)
    return Task(**defaults)


def _full_issue(
    number,
    labels=("status:queued",),
    footprint=("src/foo.py",),
    symbols=("foo.Foo",),
    subtask_id="task-a",
    depends_on=(),
    created_at="2026-01-01T00:00:00+00:00",
    parent_number=181,
):
    """`_issue()`より詳細なFootprint YAMLブロックを持つIssueRecordを作る。

    `run_dispatch_cycle`をエンドツーエンドで駆動する系のテスト（旧
    `test_dispatcher.py`の`TestRunDispatchCycle*`群）が要求するフィールド
    （footprint/symbols/subtask_id/depends_on/parent_number）を持つため、
    より単純な`_issue()`とは別名にし、`tests/conftest.py`の`make_issue`に
    委譲する薄いラッパーにしている。
    """
    parent = {"number": parent_number} if parent_number is not None else None
    return make_issue(
        number,
        title="t",
        labels=labels,
        footprint=footprint,
        symbols=symbols,
        subtask_id=subtask_id,
        depends_on=depends_on,
        created_at=created_at,
        parent=parent,
    )


def _track_forge_labels(fake_forge, *issues: IssueRecord) -> None:
    """Make label mutation mocks observable by fresh status precondition probes."""
    labels = {issue.number: list(issue.labels) for issue in issues}
    states = {issue.number: issue.state for issue in issues}

    def add_label(issue_number, label):
        current = labels.setdefault(int(issue_number), [])
        if label not in current:
            current.append(label)

    def remove_label(issue_number, label):
        current = labels.setdefault(int(issue_number), [])
        labels[int(issue_number)] = [item for item in current if item != label]

    fake_forge.add_label.side_effect = add_label
    fake_forge.remove_label.side_effect = remove_label
    fake_forge.get_issue_labels.side_effect = lambda issue_number: tuple(
        labels.get(int(issue_number), ())
    )
    fake_forge.get_issue_state.side_effect = lambda issue_number: states.get(
        int(issue_number), "OPEN"
    )


def _install_mutable_issue_snapshot(fake_forge, specs):
    labels_by_issue = {
        issue_number: list(labels) for issue_number, _, labels, _ in specs
    }

    def current_issues(label, **_):
        return [
            _full_issue(
                issue_number,
                labels=tuple(labels_by_issue[issue_number]),
                subtask_id=subtask_id,
                depends_on=depends_on,
                parent_number=None,
            )
            for issue_number, subtask_id, _, depends_on in specs
            if label in labels_by_issue[issue_number]
        ]

    def add_label(issue_number, label):
        if label not in labels_by_issue[issue_number]:
            labels_by_issue[issue_number].append(label)

    def remove_label(issue_number, label):
        labels_by_issue[issue_number].remove(label)

    fake_forge.list_issues_by_label.side_effect = current_issues
    fake_forge.get_issue_labels.side_effect = lambda issue_number: tuple(
        labels_by_issue[issue_number]
    )
    fake_forge.get_issue_state.return_value = "OPEN"
    fake_forge.add_label.side_effect = add_label
    fake_forge.remove_label.side_effect = remove_label
    fake_forge.list_open_prs.return_value = []
    return labels_by_issue


@contextmanager
def _patch_gc_process_alive(*, return_value: bool):
    """Patch every consumer split from the former dispatch_gc dependency."""
    with ExitStack() as stack:
        for target in (
            "orchestune.dispatch.execution_repair.is_process_alive",
            "orchestune.dispatch.gc.is_process_alive",
            "orchestune.dispatch.gc.completion.is_process_alive",
            "orchestune.dispatch.gc.zombies.is_process_alive",
        ):
            stack.enter_context(patch(target, return_value=return_value))
        yield


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default(fake_forge):
    """#119で追加したactor権限検証ステップが、既存の大半のテストで実際の
    `gh api`呼び出しを行わないよう、デフォルトで許可された actor/permission を
    返すようスタブする。検証ロジック自体のテストは
    tests/test_dispatch_actor_verification.py に集約する。"""
    fake_forge.get_label_actor.reset_mock(side_effect=True)
    fake_forge.get_label_actor.return_value = "trusted-actor"
    fake_forge.get_actor_permission.reset_mock(side_effect=True)
    fake_forge.get_actor_permission.return_value = "write"
    yield


class TestRunDispatchCycleBlockedPromotion:
    """#193: 依存解決によるstatus:blocked → status:queued昇格。"""

    def _config(self, tmp_path, **overrides):
        defaults = dict(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def test_promotes_blocked_task_when_dependency_already_done(
        self, tmp_path, fake_forge
    ):
        config = self._config(tmp_path)
        done_issue = _full_issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        _track_forge_labels(fake_forge, done_issue, blocked_issue)
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):

            def _list(label, **_):
                if label == "status:done":
                    return [done_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]

    def test_resolves_depends_on_from_blocked_by(self, tmp_path, fake_forge):
        config = self._config(tmp_path)
        done_issue = _full_issue(1, labels=("status:done",), subtask_id="task-a")
        blocked = _full_issue(
            2, labels=("status:blocked",), subtask_id="task-b", depends_on=()
        )
        blocked_issue = IssueRecord(
            number=blocked.number,
            title=blocked.title,
            body=blocked.body,
            labels=blocked.labels,
            created_at=blocked.created_at,
            blocked_by=(1,),
        )
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        _track_forge_labels(fake_forge, done_issue, blocked_issue)
        with patch(
            "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
        ):

            def _list(label, **_):
                if label == "status:done":
                    return [done_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]

    def test_promotes_blocked_task_when_dependency_done_and_closed(
        self, tmp_path, fake_forge
    ):
        """#236: 完了Issueが通常のGitHub運用でCloseされていても、
        status:done検索がstate="all"で呼ばれる限り依存解決できる。"""
        config = self._config(tmp_path)
        done_issue = _full_issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        _track_forge_labels(fake_forge, done_issue, blocked_issue)
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):

            def _list(label, state="open"):
                # closedなIssueもstatus:done検索に含まれるのはstate="all"の
                # 呼び出しのみ（実際のgh issue list --state open/allの挙動を模す）。
                if label == "status:done" and state == "all":
                    return [done_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]

    def test_does_not_promote_when_dependency_unresolved(self, tmp_path, fake_forge):
        config = self._config(tmp_path)
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [blocked_issue] if label == "status:blocked" else []
            )
            report = run_dispatch_cycle(config)

        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        assert report.promotion_events == []

    def test_promotes_when_dependency_completes_in_same_cycle(
        self, tmp_path, fake_forge
    ):
        """依存先が同一サイクル内で完了検知された場合も即座に昇格させる。"""
        run_state_path = tmp_path / "run_state.json"
        save_run_state(
            RunState(
                active_worktrees={
                    "1": ActiveWorktree(
                        issue_number=1,
                        branch="claude/issue-1-task-a",
                        worktree_path=str(tmp_path / "w1"),
                        pid=111,
                        started_at=1_699_999_000.0,
                        declared_footprint=("src/foo.py",),
                    )
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = self._config(tmp_path, run_state_path=run_state_path)
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        outcome = OutcomeRecord(result="done", issue=1)
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.list_prs.reset_mock(side_effect=True)
        fake_forge.list_prs.return_value = []
        fake_forge.list_comments.reset_mock(side_effect=True)
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        _track_forge_labels(fake_forge, in_progress_issue, blocked_issue)
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree"),
        ):

            def _list(label, **_):
                if label == "status:in-progress":
                    return [in_progress_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert {"issue_number": 2, "subtask_id": "task-b"} in report.promotion_events

    def test_dry_run_promotion_does_not_call_github(self, tmp_path, fake_forge):
        config = self._config(tmp_path, apply=False)
        done_issue = _full_issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):

            def _list(label, **_):
                if label == "status:done":
                    return [done_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]

    def test_status_repairs_use_typed_executor_once_at_ordered_boundaries(
        self, tmp_path, fake_forge
    ):
        labels = _install_mutable_issue_snapshot(
            fake_forge,
            (
                (1, "task-a", ("status:done",), ()),
                (2, "task-b", ("status:blocked",), ("task-a",)),
                (3, "task-c", ("status:done", "status:queued"), ()),
            ),
        )
        config = self._config(
            tmp_path,
            max_concurrent=0,
            consistency_mode=ConsistencyMode.REPAIR,
            consistency_repair_allowlist=frozenset(
                {
                    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
                    PRIMARY_STATUS_CONFLICT,
                }
            ),
        )
        order = []

        def execute(command, *args, **kwargs):
            order.append(command.code)
            return execute_status_repair_command_real(command, *args, **kwargs)

        def sync_locks(*_args, **_kwargs):
            order.append("external-lock-sync")
            return ExternalLockScanResult(to_lock=[], to_unlock=[])

        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch.cycle.execute_status_repair_command",
                side_effect=execute,
            ),
            patch(
                "orchestune.dispatch.cycle._sync_external_locks",
                side_effect=sync_locks,
            ),
        ):
            report = run_dispatch_cycle(config)

        assert order == [
            COMMAND_TRANSITION_LABEL,
            "external-lock-sync",
            COMMAND_REMOVE_LABEL,
        ]
        assert labels[2] == ["status:queued"]
        assert labels[3] == ["status:queued"]
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]

    def test_yaml_error_transitions_to_blocked(self, tmp_path, fake_forge):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-invalid\n"
            "footprint:\n"
            "  - [invalid-yaml-structure:\n"
            "```\n"
        )
        issue = IssueRecord(
            number=9,
            title="t",
            body=body,
            labels=("status:queued",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        fake_forge.add_comment.reset_mock(side_effect=True)
        mock_add_comment = fake_forge.add_comment
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue] if label == "status:queued" else []
            )

            report = run_dispatch_cycle(config)

            assert report.selected == []
            mock_remove_label.assert_any_call(9, "status:queued")
            mock_add_label.assert_any_call(9, "status:blocked")
            mock_add_comment.assert_called_once_with(9, ANY)

    def test_worktree_launch_failure_transitions_to_blocked(self, tmp_path, fake_forge):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        issue = _full_issue(1)
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        fake_forge.add_comment.reset_mock(side_effect=True)
        mock_add_comment = fake_forge.add_comment
        with (
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue] if label == "status:queued" else []
            )
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=128,
                cmd="git worktree add",
            )
            report = run_dispatch_cycle(config)

            assert report.selected == []
            mock_remove_label.assert_any_call(1, "status:queued")
            mock_add_label.assert_any_call(1, "status:blocked")
            mock_add_comment.assert_called_once_with(1, ANY)


class TestBaseBranchRedCycleReconciliation:
    def test_base_branch_red_requeued_when_base_sha_advances(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        issue = _full_issue(
            1, labels=("status:blocked", "ci:base-branch-red"), parent_number=None
        )
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="1111111111111111111111111111111111111111",
            attempt=1,
        )
        comments = [{"body": outcome.render(), "created_at": "2026-01-01T00:00:10Z"}]

        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.list_comments.reset_mock(side_effect=True)
        fake_forge.list_comments.return_value = comments
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        fake_forge.add_comment.reset_mock(side_effect=True)
        mock_add_comment = fake_forge.add_comment
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            patch(
                "orchestune.dispatch.reconciliation._get_branch_commit_sha",
                return_value="2222222222222222222222222222222222222222",
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue] if label == "status:blocked" else []
            )
            report = run_dispatch_cycle(config)

            assert report.promotion_events == [
                {"issue_number": 1, "subtask_id": "task-a"}
            ]
            mock_remove_label.assert_any_call(1, "ci:base-branch-red")
            mock_remove_label.assert_any_call(1, "status:blocked")
            mock_add_label.assert_any_call(1, "status:queued")
            mock_add_comment.assert_called_once()

    def test_base_branch_red_escalated_when_3_attempts_reached(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        issue = _full_issue(
            1, labels=("status:blocked", "ci:base-branch-red"), parent_number=None
        )
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="1111111111111111111111111111111111111111",
            attempt=3,
        )
        comments = [{"body": outcome.render(), "created_at": "2026-01-01T00:00:10Z"}]

        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.list_comments.reset_mock(side_effect=True)
        fake_forge.list_comments.return_value = comments
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        fake_forge.add_comment.reset_mock(side_effect=True)
        mock_add_comment = fake_forge.add_comment
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            patch(
                "orchestune.dispatch.reconciliation._get_branch_commit_sha",
                return_value="2222222222222222222222222222222222222222",
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue] if label == "status:blocked" else []
            )
            report = run_dispatch_cycle(config)

            assert report.promotion_events == []
            mock_remove_label.assert_any_call(1, "ci:base-branch-red")
            mock_add_label.assert_any_call(1, "status:blocked-human-review")
            mock_add_comment.assert_called_once()
