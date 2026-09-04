"""dispatch_cycle内の外部ロック同期（dispatch_locks.py関連）テスト。

`tests/test_dispatch_cycle.py`の肥大化解消のため、外部ロックの判定・適用と、
自己ロック回避のためのブランチ名正規化テストを分割している（#343）。
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import run_dispatch_cycle
from orchestune.dispatch.locks import (
    ExternalLockConflict,
    ExternalLockScanResult,
)
from orchestune.dispatch.phase_rebase import (
    _apply_external_lock_sync,
    _decide_external_lock_sync,
    _is_base_or_parent_branch,
)
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import (
    ActiveWorktree,
    RunState,
    save_run_state,
)
from orchestune.issue_notice import notice_marker, render_notice
from orchestune.models import PrRecord
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


class TestIsBaseOrParentBranch:
    """#677: 親ブランチ（parent/issue-*）・ベースブランチ除外の仕様固定。

    `_is_base_or_parent_branch`は`_decide_external_lock_sync`が外部タスク
    ブランチをスキャンする前段で、ベースブランチ自身や`--parent-issue`運用の
    親ブランチを誤って競合ブランチとして扱わないための除外判定。
    """

    @pytest.mark.parametrize(
        "branch_name",
        [
            "main",
            "master",
            "HEAD",
            "origin/main",
            "origin/HEAD",
        ],
    )
    def test_excludes_base_branches(self, branch_name):
        assert _is_base_or_parent_branch(branch_name) is True

    @pytest.mark.parametrize(
        "branch_name",
        [
            "parent/issue-181",
            "origin/parent/issue-181",
            "parent/issue-1",
        ],
    )
    def test_excludes_generic_parent_issue_branch(self, branch_name):
        assert _is_base_or_parent_branch(branch_name) is True

    def test_does_not_exclude_unrelated_branch(self):
        assert _is_base_or_parent_branch("feature/foo") is False
        assert _is_base_or_parent_branch("origin/someone-elses-branch") is False


class TestDecideExternalLockSync:
    """decide層: githubからの読み取りとscan_external_locksの純粋計算のみを行い、
    ラベルの書き込みは行わない。"""

    def test_no_bare_branches_means_no_locks(self):
        run_state = RunState(active_worktrees={})
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                return_value=[],
            ),
        ):
            result = _decide_external_lock_sync({}, [], run_state)
        assert result.to_lock == []
        assert result.to_unlock == []

    def test_ignores_invalid_remote_branch_name(self):
        run_state = RunState(active_worktrees={})
        task = _task(issue_number=1, footprint=("src/foo.py",))
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                return_value=["origin/feature/foo@bar"],
            ),
        ):
            result = _decide_external_lock_sync({1: task}, [], run_state)
        assert result.to_lock == []
        assert result.to_unlock == []

    def test_diff_failure_keeps_existing_lock_and_locks_queued_tasks(self):
        """#245: 差分取得不能（branch_changed_files=None）はfail closed。
        既存のexternal-lockは解除されず、footprintを持つqueued taskはlockされる。"""
        run_state = RunState(active_worktrees={})
        locked_task = _task(
            issue_number=1,
            footprint=("src/foo.py",),
            status_labels=("status:external-lock",),
        )
        queued_task = _task(issue_number=2, footprint=("src/bar.py",))
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                return_value=["origin/feat/x"],
            ),
            patch(
                "orchestune.dispatch.phase_rebase.branch_changed_files",
                return_value=None,
            ),
        ):
            result = _decide_external_lock_sync(
                {1: locked_task, 2: queued_task}, [], run_state
            )
        assert result.to_unlock == []
        assert [t.issue_number for t in result.to_lock] == [2]

    def test_parent_branch_diff_does_not_lock_overlapping_child_task(self, tmp_path):
        """#677: 先行サブタスクのマージで親ブランチ（parent/issue-*）に差分が
        生じても、footprintが重なる後続タスクをexternal-lockしない。

        `test_unrelated_external_branch_still_locks_overlapping_task`の対にな
        る陽性ケース: 同じ重なるfootprintでも、ブランチが親ブランチであれば
        ロックされないことを確認する。"""
        run_state = RunState(active_worktrees={})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl", parent_issue_number=181
        )
        queued_task = _task(issue_number=2, footprint=("src/shared.py",))
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                return_value=["origin/parent/issue-181"],
            ),
            patch(
                "orchestune.dispatch.phase_rebase.branch_changed_files"
            ) as mock_branch_files,
        ):
            result = _decide_external_lock_sync({2: queued_task}, [], run_state, config)
        mock_branch_files.assert_not_called()
        assert result.to_lock == []
        assert result.to_unlock == []


class TestApplyExternalLockSync:
    def test_unlocking_blocked_task_does_not_requeue_it(self, tmp_path, fake_forge):
        task = _task(status_labels=("status:blocked", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[task])

        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        _apply_external_lock_sync(
            lock_result,
            DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True),
        )

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        mock_add_label.assert_not_called()

    def test_unlocking_in_progress_task_does_not_requeue_it(self, tmp_path, fake_forge):
        task = _task(status_labels=("status:in-progress", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[task])

        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        _apply_external_lock_sync(
            lock_result,
            DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True),
        )

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        mock_add_label.assert_not_called()

    def test_unlocking_queued_task_requeues_it(self, tmp_path, fake_forge):
        task = _task(status_labels=("status:queued", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[task])

        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        _apply_external_lock_sync(
            lock_result,
            DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True),
        )

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        mock_add_label.assert_called_once_with(1, "status:queued")


class TestRunDispatchCycleBranchNormalization:
    """#194: リモートブランチ名のorigin/プレフィックス正規化。"""

    def test_does_not_self_lock_own_active_branch(self, tmp_path, fake_forge):
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
                        declared_footprint=("src/shared.py",),
                    )
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(2, footprint=("src/shared.py",))
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                return_value=["origin/claude/issue-1-task-a"],
            ),
            patch(
                "orchestune.dispatch.phase_rebase.branch_changed_files",
                return_value=["src/shared.py"],
            ),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch.rebase.check_footprint_deviation", return_value=[]
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.lock_changes["to_lock"] == []

    def test_excludes_branch_with_open_pr_multisegment_headref(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.return_value = []
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = [
            PrRecord(number=1, head_ref="feature/foo", changed_files=("src/x.py",))
        ]
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                return_value=["origin/feature/foo"],
            ),
            patch(
                "orchestune.dispatch.phase_rebase.branch_changed_files"
            ) as mock_branch_files,
        ):
            run_dispatch_cycle(config)

        mock_branch_files.assert_not_called()

    def test_parent_branch_diff_does_not_block_child_task_scheduling(
        self, tmp_path, fake_forge
    ):
        """#677: `--parent-issue`運用で先行サブタスクが親ブランチにマージされ
        差分が生じても、footprintが重なる後続の子タスクが
        `status:external-lock`で永久ブロックされず、正常にスケジュール対象
        であり続けることをエンドツーエンドで検証する回帰テスト。

        `config.parent_issue_number`は設定しない: `_is_base_or_parent_branch`の
        汎用`parent/issue-*`プレフィックス除外はconfig非依存で効くうえ、
        `parent_issue_number`を設定すると`_fetch_issues`がネイティブSub-issue
        API経由（`find_children_by_parent`）に切り替わり、本テストがモックして
        いる`list_issues_by_label`ベースの取得経路を素通りしてしまう。"""
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(2, footprint=("src/shared.py",), parent_number=181)
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                return_value=["origin/parent/issue-181"],
            ),
            patch(
                "orchestune.dispatch.phase_rebase.branch_changed_files"
            ) as mock_branch_files,
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        mock_branch_files.assert_not_called()
        assert report.lock_changes["to_lock"] == []
        assert report.lock_changes["to_unlock"] == []
        assert [t.issue_number for t in report.selected] == [2]

    def test_unrelated_external_branch_still_locks_overlapping_task(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(1, footprint=("src/shared.py",))
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                return_value=["origin/someone-elses-branch"],
            ),
            patch(
                "orchestune.dispatch.phase_rebase.branch_changed_files",
                return_value=["src/shared.py"],
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert [t.issue_number for t in report.lock_changes["to_lock"]] == [1]


class TestExternalLockNotice:
    """#787: ロック理由（衝突相手・衝突ファイル）を対象Issueのコメントへ残す。"""

    def _config(self, tmp_path, apply=True):
        return DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=apply)

    def test_posts_conflict_detail_for_newly_locked_task(self, tmp_path, fake_forge):
        task = _task(status_labels=("status:queued",))
        lock_result = ExternalLockScanResult(
            to_lock=[task],
            to_unlock=[],
            conflicts={
                1: (
                    ExternalLockConflict(
                        kind="branch",
                        source="fix/issue-777-branch-naming",
                        files=("tests/conftest.py",),
                    ),
                )
            },
        )

        _apply_external_lock_sync(lock_result, self._config(tmp_path))

        body = fake_forge.add_comment.call_args.args[1]
        assert notice_marker("external-lock") in body
        assert "fix/issue-777-branch-naming" in body
        assert "tests/conftest.py" in body

    def test_posts_conflict_detail_for_task_that_stays_locked(
        self, tmp_path, fake_forge
    ):
        """継続ロック中のタスクは`to_lock`に載らないが、理由は残す必要がある。"""
        lock_result = ExternalLockScanResult(
            to_lock=[],
            to_unlock=[],
            conflicts={
                695: (
                    ExternalLockConflict(
                        kind="branch",
                        source="fix/issue-777-branch-naming",
                        files=("tests/conftest.py",),
                    ),
                )
            },
        )

        _apply_external_lock_sync(lock_result, self._config(tmp_path))

        assert fake_forge.add_comment.call_args.args[0] == 695

    def test_does_not_post_when_not_applying(self, tmp_path, fake_forge):
        task = _task(status_labels=("status:queued",))
        lock_result = ExternalLockScanResult(
            to_lock=[task],
            to_unlock=[],
            conflicts={
                1: (
                    ExternalLockConflict(
                        kind="branch", source="feat/x", files=("a.py",)
                    )
                )
            },
        )

        _apply_external_lock_sync(lock_result, self._config(tmp_path, apply=False))

        fake_forge.add_comment.assert_not_called()

    def test_does_not_repost_unchanged_reason(self, tmp_path, fake_forge):
        conflicts = {
            1: (ExternalLockConflict(kind="branch", source="feat/x", files=("a.py",)),)
        }
        lock_result = ExternalLockScanResult(
            to_lock=[_task(status_labels=("status:queued",))],
            to_unlock=[],
            conflicts=conflicts,
        )
        posted: list[str] = []
        fake_forge.add_comment.side_effect = lambda number, body: posted.append(body)
        fake_forge.list_comments.side_effect = lambda number: [
            {"body": body} for body in posted
        ]

        config = self._config(tmp_path)
        _apply_external_lock_sync(lock_result, config)
        _apply_external_lock_sync(lock_result, config)

        assert len(posted) == 1

    def test_posts_release_notice_only_when_a_reason_was_recorded(
        self, tmp_path, fake_forge
    ):
        """ロック理由を書いたIssueにだけ解除を伝える。理由を書いていない
        Issue（本機能導入前のロックや`--no-apply`サイクル）には触れない。"""
        unlocked = _task(status_labels=("status:queued", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[unlocked])
        fake_forge.list_comments.return_value = []

        _apply_external_lock_sync(lock_result, self._config(tmp_path))
        fake_forge.add_comment.assert_not_called()

        fake_forge.list_comments.return_value = [
            {"body": render_notice("external-lock", "ロック中です")}
        ]
        _apply_external_lock_sync(lock_result, self._config(tmp_path))
        assert (
            notice_marker("external-lock") in (fake_forge.add_comment.call_args.args[1])
        )

    def test_renders_fail_closed_kinds_without_file_list(self, tmp_path, fake_forge):
        """差分を取得できないブランチは衝突ファイルを特定できないため、
        件数へ丸めて毎サイクル同じ本文になるようにする。"""
        lock_result = ExternalLockScanResult(
            to_lock=[_task(status_labels=("status:queued",))],
            to_unlock=[],
            conflicts={
                1: (
                    ExternalLockConflict(kind="branch-diff-unknown", source="feat/a"),
                    ExternalLockConflict(kind="branch-diff-unknown", source="feat/b"),
                )
            },
        )

        _apply_external_lock_sync(lock_result, self._config(tmp_path))

        body = fake_forge.add_comment.call_args.args[1]
        assert "2" in body
        assert "feat/a" not in body


class TestExternalLockReleaseNoticeRetry:
    """PR#789レビュー(Codex P2): 解除通知が一度失われても次サイクルで書き直す。

    ラベルは通知より先に外れるため、投稿に失敗したタスクは次サイクルの
    `to_unlock`には現れない。再試行キューが無いと、Issue上の最後の通知が
    「ロック中」のまま永久に取り残される。
    """

    def _config(self, tmp_path):
        return DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)

    def test_failed_release_notice_is_queued_for_retry(self, tmp_path, fake_forge):
        task = _task(status_labels=("status:queued", "status:external-lock"))
        run_state = RunState()
        fake_forge.list_comments.side_effect = RuntimeError("504 Gateway Timeout")

        _apply_external_lock_sync(
            ExternalLockScanResult(to_lock=[], to_unlock=[task]),
            self._config(tmp_path),
            run_state,
        )

        assert run_state.pending_lock_release_notices == [1]

    def test_queued_release_notice_is_retried_without_the_task_in_to_unlock(
        self, tmp_path, fake_forge
    ):
        run_state = RunState(pending_lock_release_notices=[1])
        fake_forge.list_comments.return_value = [
            {"body": render_notice("external-lock", "ロック中です")}
        ]

        _apply_external_lock_sync(
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
            self._config(tmp_path),
            run_state,
        )

        assert fake_forge.add_comment.call_args.args[0] == 1
        assert run_state.pending_lock_release_notices == []

    def test_issue_without_a_prior_notice_leaves_the_queue(self, tmp_path, fake_forge):
        """通知そのものが無いIssueは書く必要がない。永久に再試行しない。"""
        run_state = RunState(pending_lock_release_notices=[1])
        fake_forge.list_comments.return_value = []

        _apply_external_lock_sync(
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
            self._config(tmp_path),
            run_state,
        )

        fake_forge.add_comment.assert_not_called()
        assert run_state.pending_lock_release_notices == []

    def test_relocked_task_drops_out_of_the_queue(self, tmp_path, fake_forge):
        """再ロックされたタスクはロック理由の通知で本文が上書きされる。"""
        task = _task(status_labels=("status:queued",))
        run_state = RunState(pending_lock_release_notices=[1])
        fake_forge.list_comments.return_value = []

        _apply_external_lock_sync(
            ExternalLockScanResult(
                to_lock=[task],
                to_unlock=[],
                conflicts={
                    1: (
                        ExternalLockConflict(
                            kind="branch", source="feat/x", files=("a.py",)
                        ),
                    )
                },
            ),
            self._config(tmp_path),
            run_state,
        )

        assert run_state.pending_lock_release_notices == []
        assert "ロック中" in fake_forge.add_comment.call_args.args[1]
