"""#512: ゾンビ／タイムアウト回収による再投入の回数上限テスト。

`--task-timeout-seconds` に正の値を設定していても、回収されたタスクは
タスクごとのカウンタを持たないまま `status:queued` へ戻され続けていた
（終端のない経路）。上限を超えた場合に `status:blocked-human-review`
へ遷移して停止することを検証する。
"""

from dataclasses import fields, replace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_context import (
    IssuesByStatus,
    discard_reclaim_counts_for_closed_issues,
)
from orchestune.dispatch.cycle_events import (
    TaskWorktreeCompletion,
    WorktreeCompletion,
    WorktreeCompletionHold,
)
from orchestune.dispatch.gc import _rule_completed, _rule_not_needed
from orchestune.dispatch.gc.cloud_completion import _reserve_cloud_reclaim_record
from orchestune.ledger.run_state import (
    RunState,
    TaskReclaimRecord,
    load_run_state,
)
from orchestune.models import IssueRecord
from tests.dispatch_gc_test_support import _in_progress_task as _task
from tests.dispatch_gc_test_support import _reclaim_active as _active
from tests.dispatch_gc_test_support import _rule_ctx as _ctx
from tests.dispatch_gc_test_support import (
    run_gc_reclaims as _collect_zombies_and_timeouts,
)
from tests.verification_contract_test_support import expect_violation, require

_NOW = 2_000.0


def _config(tmp_path, **overrides):
    defaults = dict(
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        apply=True,
        task_timeout_seconds=60,
    )
    defaults.update(overrides)
    return DispatcherConfig(parent_issue_number=100, **defaults)


def _run_timeout_cycles(run_state, config, cycles, task=None, tmp_path=None):
    """毎サイクル「起動 → タイムアウト → GC回収」を繰り返し、eventを返す。"""
    task = task or _task()
    events = []
    labels: list[tuple[str, str]] = []
    comments: list[str] = []
    forge = config.resolved_forge
    forge.add_label.side_effect = lambda issue, label: labels.append(("add", label))
    forge.remove_label.side_effect = lambda issue, label: labels.append(
        ("remove", label)
    )
    forge.add_comment.side_effect = lambda issue, body: comments.append(body)
    for _ in range(cycles):
        run_state.active_worktrees["280"] = _active(
            worktree_path=str((tmp_path or config.log_dir) / "missing-280")
        )
        with patch("orchestune.dispatch.phase_gc.time.time", return_value=_NOW):
            cycle_events = _collect_zombies_and_timeouts(run_state, {280: task}, config)
        assert len(cycle_events) == 1
        events.append(cycle_events[0])
    return events, labels, comments


class TestReclaimCounterLifecycle:
    """#512: 回収回数の台帳から記録が破棄される条件。

    PR#520レビュー6巡目対応(Codex P2): 破棄の根拠は「GitHub上でIssueが
    クローズされたこと」ただ一つ。`status:done`（ワーカー完了）の時点では
    Integratorの仮マージCI失敗で`status:queued`へ差し戻され得るため破棄しない。
    """

    def _completed_ctx(self, fake_forge, action="completed"):
        ctx = _ctx(forge=fake_forge)
        ctx.config.apply = True
        active = ctx.run_state.active_worktrees.setdefault(
            "1", _active(issue_number=280, worktree_path="worktrees/w1")
        )
        ctx.run_state.task_reclaim_counts[280] = TaskReclaimRecord(
            count=2, last_reclaimed_at=_NOW
        )
        task = _task(status_labels=("status:in-progress",))
        with (
            patch(
                "orchestune.dispatch.gc._is_worktree_complete",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                autospec=True,
                return_value=WorktreeCompletion(
                    issue_number=280,
                    worktree_path=active.core.worktree_path,
                    action=action,
                    commit_sha="abc123d",
                ),
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)
        return ctx, outcome

    def test_worker_completion_alone_keeps_the_reclaim_count(self, fake_forge):
        """ワーカー完了（status:done）だけでは破棄しない。

        `_finalize_completed_worktree`はIssueをクローズしないため、この時点で
        破棄すると、Integratorの仮マージCI失敗（`handle_merge_failure`）で
        `status:queued`へ差し戻されたタスクが回数0から再開してしまい、
        「GC回収 → ワーカー完了 → 統合失敗」の繰り返しで上限を素通りできる。
        """
        ctx, outcome = self._completed_ctx(fake_forge)

        assert outcome is not None
        assert ctx.run_state.task_reclaim_counts == {
            280: TaskReclaimRecord(count=2, last_reclaimed_at=_NOW)
        }

    def test_token_limit_escalation_keeps_the_reclaim_count(self, fake_forge):
        ctx, outcome = self._completed_ctx(
            fake_forge, action="escalated_token_limit_exceeded"
        )

        assert outcome is not None
        assert ctx.run_state.task_reclaim_counts == {
            280: TaskReclaimRecord(count=2, last_reclaimed_at=_NOW)
        }

    def test_not_needed_completion_keeps_the_count_until_the_issue_is_closed(self):
        """`status:not-needed`経路でも、破棄はクローズ確認まで待つ。"""
        ctx = _ctx()
        ctx.config.apply = True
        active = ctx.run_state.active_worktrees.setdefault(
            "1", _active(issue_number=280, worktree_path="worktrees/w1")
        )
        record = TaskReclaimRecord(count=2, last_reclaimed_at=_NOW)
        ctx.run_state.task_reclaim_counts[280] = record
        task = _task(status_labels=("status:not-needed",))

        with patch(
            "orchestune.dispatch.gc._finalize_not_needed_worktree",
            autospec=True,
            return_value=TaskWorktreeCompletion(
                issue_number=280,
                subtask_id=task.subtask_id,
                worktree_path=active.core.worktree_path,
                action="not_needed",
            ),
        ):
            outcome = _rule_not_needed(ctx, "1", active, task)

        assert outcome is not None
        assert ctx.run_state.task_reclaim_counts == {280: record}


class TestDirtyWorktreeHoldLimit:
    """#512/PR#520レビュー11巡目対応(Codex P1): #212のdirty worktree保留も上限で終端する。

    保留中のworktreeは`run_gc_phase`がGC対象から除外するため、ゾンビ/タイムアウト
    回収の上限判定には到達しない。保留自体を同じ台帳で数えないと、対象タスクは
    `status:in-progress`のままクオータを占有し続ける。
    """

    def _hold_cycle(self, ctx, active, task):
        with (
            patch(
                "orchestune.dispatch.gc._is_worktree_complete",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                autospec=True,
                return_value=WorktreeCompletionHold(
                    issue_number=280,
                    worktree_path=active.core.worktree_path,
                    action="completion_skipped_dirty_worktree",
                ),
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)
        return (
            outcome,
            ctx.config.resolved_forge.add_label,
            ctx.config.resolved_forge.add_comment,
        )

    def test_repeated_holds_escalate_at_the_limit(self, tmp_path, fake_forge):
        ctx = _ctx(forge=fake_forge)
        ctx.config.apply = True
        ctx.config.run_state_path = tmp_path / "run_state.json"
        ctx.config.max_task_reclaims = 2
        active = ctx.run_state.active_worktrees.setdefault(
            "1", _active(issue_number=280, worktree_path="worktrees/w1")
        )
        task = _task(status_labels=("status:in-progress",))

        actions = []
        for _ in range(3):
            ctx.run_state.active_worktrees.setdefault("1", active)
            outcome, mock_add_label, _ = self._hold_cycle(ctx, active, task)
            assert outcome is not None
            actions.append(outcome.completion_event.to_dict()["action"])

        assert actions == [
            "completion_skipped_dirty_worktree",
            "completion_skipped_dirty_worktree",
            "escalated_reclaim_limit_exceeded",
        ]
        mock_add_label.assert_called_once_with(280, "status:blocked-human-review")
        # 未コミットの作業を守るためworktreeは残し、帳簿エントリのみ解放する
        assert ctx.run_state.active_worktrees == {}
        assert load_run_state(ctx.config.run_state_path).active_worktrees == {}

    def test_hold_count_shares_the_reclaim_ledger(self, tmp_path, fake_forge):
        """回収回数と同じ台帳を共有する（保留と回収を通算して上限に到達する）。"""
        ctx = _ctx(forge=fake_forge)
        ctx.config.apply = True
        ctx.config.run_state_path = tmp_path / "run_state.json"
        ctx.config.max_task_reclaims = 3
        active = ctx.run_state.active_worktrees.setdefault(
            "1", _active(issue_number=280, worktree_path="worktrees/w1")
        )
        ctx.run_state.task_reclaim_counts[280] = TaskReclaimRecord(
            count=1, last_reclaimed_at=_NOW
        )
        task = _task(status_labels=("status:in-progress",))

        self._hold_cycle(ctx, active, task)

        assert ctx.run_state.task_reclaim_counts[280].count == 2
        assert ctx.run_state.task_reclaim_counts[280].pending is False

    def test_escalation_failure_does_not_abort_the_cycle(self, tmp_path, fake_forge):
        """PR#520レビュー12巡目対応(Codex P1): エスカレーションの失敗で
        サイクル全体を止めない（1タスクが全体のスケジューリングを妨げない）。

        ラベル遷移すら失敗した場合は保留のまま次サイクルで再試行し、回数は
        GitHubへ触れる前に永続化済みであることを確認する。
        """
        ctx = _ctx(forge=fake_forge)
        fake_forge.add_label.side_effect = RuntimeError("gh: API is unavailable")
        ctx.config.apply = True
        ctx.config.run_state_path = tmp_path / "run_state.json"
        ctx.config.max_task_reclaims = 0
        active = ctx.run_state.active_worktrees.setdefault(
            "1", _active(issue_number=280, worktree_path="worktrees/w1")
        )
        task = _task(status_labels=("status:in-progress",))

        with (
            patch(
                "orchestune.dispatch.gc._is_worktree_complete",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                autospec=True,
                return_value=WorktreeCompletionHold(
                    issue_number=280,
                    worktree_path=active.core.worktree_path,
                    action="completion_skipped_dirty_worktree",
                ),
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert (
            outcome.completion_event.to_dict()["action"]
            == "completion_skipped_dirty_worktree"
        )
        assert set(ctx.run_state.active_worktrees) == {"1"}
        # 回数はGitHubへ触れる前にディスクへ載っている
        assert (
            load_run_state(ctx.config.run_state_path).task_reclaim_counts[280].count
            == 1
        )

    def test_comment_failure_after_escalation_still_releases_the_entry(
        self, tmp_path, fake_forge
    ):
        """ラベル遷移が成功していればコメント失敗でもエントリを解放する。"""
        ctx = _ctx(forge=fake_forge)
        fake_forge.add_comment.side_effect = RuntimeError("gh: comment failed")
        ctx.config.apply = True
        ctx.config.run_state_path = tmp_path / "run_state.json"
        ctx.config.max_task_reclaims = 0
        active = ctx.run_state.active_worktrees.setdefault(
            "1", _active(issue_number=280, worktree_path="worktrees/w1")
        )
        task = _task(status_labels=("status:in-progress",))

        with (
            patch(
                "orchestune.dispatch.gc._is_worktree_complete",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                autospec=True,
                return_value=WorktreeCompletionHold(
                    issue_number=280,
                    worktree_path=active.core.worktree_path,
                    action="completion_skipped_dirty_worktree",
                ),
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert (
            outcome.completion_event.to_dict()["action"]
            == "escalated_reclaim_limit_exceeded"
        )
        assert ctx.run_state.active_worktrees == {}
        assert load_run_state(ctx.config.run_state_path).active_worktrees == {}

    def test_escalation_is_reported_as_the_canonical_escalated_event(
        self, tmp_path, fake_forge
    ):
        """#1349: castで偽装せず`DirtyWorktreeEscalatedCompletion`を返す。

        field・出力順・省略規則は従来のholdイベントの`replace`と同一でなければ
        ならない（events.jsonlの既存出力を維持する）。
        """
        from orchestune.dispatch.cycle_events import DirtyWorktreeEscalatedCompletion

        ctx = _ctx(forge=fake_forge)
        ctx.config.apply = True
        ctx.config.run_state_path = tmp_path / "run_state.json"
        ctx.config.max_task_reclaims = 0
        active = ctx.run_state.active_worktrees.setdefault(
            "1", _active(issue_number=280, worktree_path="worktrees/w1")
        )
        task = _task(status_labels=("status:in-progress",))
        held = WorktreeCompletionHold(
            issue_number=280,
            worktree_path=active.core.worktree_path,
            action="completion_skipped_dirty_worktree",
            operation="list_prs",
            error="boom",
        )

        with (
            patch(
                "orchestune.dispatch.gc._is_worktree_complete",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                autospec=True,
                return_value=held,
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        event = outcome.completion_event
        assert isinstance(event, DirtyWorktreeEscalatedCompletion)
        assert event.to_dict() == {
            "issue_number": 280,
            "worktree_path": active.core.worktree_path,
            "action": "escalated_reclaim_limit_exceeded",
            "operation": "list_prs",
            "error": "boom",
        }
        assert list(event.to_dict()) == [
            "issue_number",
            "worktree_path",
            "action",
            "operation",
            "error",
        ]

    def test_dry_run_does_not_count_the_hold(self, tmp_path, fake_forge):
        ctx = _ctx(forge=fake_forge)
        ctx.config.apply = False
        ctx.config.run_state_path = tmp_path / "run_state.json"
        active = ctx.run_state.active_worktrees.setdefault(
            "1", _active(issue_number=280, worktree_path="worktrees/w1")
        )
        task = _task(status_labels=("status:in-progress",))

        outcome, _, _ = self._hold_cycle(ctx, active, task)

        assert outcome is not None
        assert (
            outcome.completion_event.to_dict()["action"]
            == "completion_skipped_dirty_worktree"
        )
        assert ctx.run_state.task_reclaim_counts == {}


class TestDiscardReclaimCountsForClosedIssues:
    """#512: クローズ済みIssueの回収回数を台帳から破棄する単一の規則。"""

    def _issue_record(self, number, state):
        return IssueRecord(
            number=number,
            title=f"#{number}",
            body="",
            labels=("status:done",),
            created_at="2026-01-01T00:00:00+00:00",
            state=state,
        )

    def _issues(self, records):
        issues = [self._issue_record(number, state) for number, state in records]
        return IssuesByStatus(
            queued=[],
            locked=[],
            in_progress=[],
            blocked=[],
            done=issues,
            not_needed=[],
        )

    def _config(self, tmp_path, **overrides):
        defaults = dict(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            forge=MagicMock(),
        )
        defaults.update(overrides)
        return DispatcherConfig(parent_issue_number=100, **defaults)

    def test_closed_issue_loses_its_reclaim_count(self, tmp_path):
        run_state = RunState(
            task_reclaim_counts={
                280: TaskReclaimRecord(count=2, last_reclaimed_at=_NOW),
                281: TaskReclaimRecord(count=1, last_reclaimed_at=_NOW),
            }
        )
        config = self._config(tmp_path)

        removed = discard_reclaim_counts_for_closed_issues(
            run_state, self._issues([(280, "CLOSED"), (281, "OPEN")]), config
        )

        assert removed == [280]
        assert set(run_state.task_reclaim_counts) == {281}
        # 一覧で状態が分かった分は追加の問い合わせをしない
        config.forge.get_issue_state.assert_not_called()

    def test_terminal_label_issues_are_resolved_in_bulk(self, tmp_path):
        """PR#520レビュー8巡目対応(Codex P2): `status:blocked-human-review`のまま
        閉じられたIssueは、1件ずつではなくラベル単位の一括取得で解決する。

        エスカレーション済みタスクは本機能自身が作り、クローズされるまで台帳に
        残るため、1件1リクエストだと毎サイクルのAPI呼び出しが線形に増えてしまう。
        """
        run_state = RunState(
            task_reclaim_counts={
                number: TaskReclaimRecord(count=1, last_reclaimed_at=_NOW)
                for number in range(280, 285)
            }
        )
        config = self._config(tmp_path)
        config.forge.list_issues_by_label.side_effect = (
            lambda label, state="open", limit=1000: (
                [
                    self._issue_record(280, "CLOSED"),
                    self._issue_record(281, "OPEN"),
                    self._issue_record(282, "CLOSED"),
                    self._issue_record(283, "OPEN"),
                    self._issue_record(284, "CLOSED"),
                ]
                if label == "status:blocked-human-review"
                else []
            )
        )

        removed = discard_reclaim_counts_for_closed_issues(
            run_state, self._issues([]), config
        )

        assert removed == [280, 282, 284]
        assert set(run_state.task_reclaim_counts) == {281, 283}
        # 一括取得で解決できた分は1件ずつ問い合わせない
        config.forge.get_issue_state.assert_not_called()
        assert config.forge.list_issues_by_label.call_count == 1

    def test_issue_outside_every_listing_is_queried_directly(self, tmp_path):
        """どの一覧にも現れない記録（status:*ラベルを持たない等）だけを直接問い合わせる。"""
        run_state = RunState(
            task_reclaim_counts={
                280: TaskReclaimRecord(count=2, last_reclaimed_at=_NOW),
                281: TaskReclaimRecord(count=1, last_reclaimed_at=_NOW),
            }
        )
        config = self._config(tmp_path)
        config.forge.list_issues_by_label.return_value = []
        config.forge.get_issue_state.side_effect = lambda number: (
            "CLOSED" if number == 280 else "OPEN"
        )

        removed = discard_reclaim_counts_for_closed_issues(
            run_state, self._issues([]), config
        )

        assert removed == [280]
        assert set(run_state.task_reclaim_counts) == {281}

    def test_bulk_lookup_failure_falls_back_to_direct_queries(self, tmp_path, capsys):
        run_state = RunState(
            task_reclaim_counts={
                280: TaskReclaimRecord(count=2, last_reclaimed_at=_NOW)
            }
        )
        config = self._config(tmp_path)
        config.forge.list_issues_by_label.side_effect = RuntimeError(
            "gh: search failed"
        )
        config.forge.get_issue_state.return_value = "CLOSED"

        removed = discard_reclaim_counts_for_closed_issues(
            run_state, self._issues([]), config
        )

        assert removed == [280]
        assert "gh: search failed" in capsys.readouterr().err

    def test_unverifiable_issue_keeps_its_count(self, tmp_path, capsys):
        """問い合わせに失敗した記録は保持する（回数を失う方が危険）。"""
        run_state = RunState(
            task_reclaim_counts={
                280: TaskReclaimRecord(count=2, last_reclaimed_at=_NOW)
            }
        )
        config = self._config(tmp_path)
        config.forge.list_issues_by_label.return_value = []
        config.forge.get_issue_state.side_effect = RuntimeError("gh: API rate limited")

        removed = discard_reclaim_counts_for_closed_issues(
            run_state, self._issues([]), config
        )

        assert removed == []
        assert set(run_state.task_reclaim_counts) == {280}
        assert "API rate limited" in capsys.readouterr().err

    def test_bulk_lookup_requests_enough_results_for_the_ledger(self, tmp_path):
        """PR#520レビュー14巡目対応(Codex P2): 既定の1000件で頭打ちにしない。"""
        run_state = RunState(
            task_reclaim_counts={
                number: TaskReclaimRecord(count=1, last_reclaimed_at=_NOW)
                for number in range(2_000)
            }
        )
        config = self._config(tmp_path)
        config.forge.list_issues_by_label.return_value = []
        config.forge.get_issue_state.return_value = "OPEN"

        discard_reclaim_counts_for_closed_issues(run_state, self._issues([]), config)

        assert (
            config.forge.list_issues_by_label.call_args_list[0].kwargs["limit"] >= 2_000
        )

    def test_direct_lookups_are_bounded_per_cycle(self, tmp_path):
        """個別問い合わせは1サイクルあたりの上限で頭打ちにする。

        台帳に件数上限を設けていないため、ここを無制限にすると大規模リポジトリで
        毎サイクル数千回の逐次API呼び出しになり、ディスパッチ全体が詰まる。
        未解決の記録は保持され、次サイクル以降で順次確認される。
        """
        run_state = RunState(
            task_reclaim_counts={
                number: TaskReclaimRecord(count=1, last_reclaimed_at=_NOW)
                for number in range(500)
            }
        )
        config = self._config(tmp_path)
        config.forge.list_issues_by_label.return_value = []
        config.forge.get_issue_state.return_value = "CLOSED"

        removed = discard_reclaim_counts_for_closed_issues(
            run_state, self._issues([]), config
        )

        assert config.forge.get_issue_state.call_count == 50
        assert len(removed) == 50
        assert len(run_state.task_reclaim_counts) == 450

    def test_capped_direct_lookups_advance_a_persisted_cursor(self, tmp_path):
        """PR#520レビュー15/16巡目対応(Codex P2): 走査位置をカーソルで順送りする。

        常に若い番号の50件だけを見ると、その後ろでクローズされた記録の回数が
        永久に台帳へ残る。壁時計によるローテーションでも、一定周期で起動される
        ディスパッチャーでは同じ位置ばかり見る組み合わせが生じるため、
        `run_state`のカーソルで進める。
        """
        config = self._config(tmp_path)
        config.forge.list_issues_by_label.return_value = []
        config.forge.get_issue_state.return_value = "OPEN"
        run_state = RunState(
            task_reclaim_counts={
                number: TaskReclaimRecord(count=1, last_reclaimed_at=_NOW)
                for number in range(500)
            }
        )

        seen: list[set[int]] = []
        for _ in range(10):
            config.forge.get_issue_state.reset_mock()
            discard_reclaim_counts_for_closed_issues(
                run_state, self._issues([]), config
            )
            seen.append(
                {call.args[0] for call in config.forge.get_issue_state.call_args_list}
            )

        # 10サイクル（50件 x 10）で全500件を一巡し、重複なく進む
        assert all(len(batch) == 50 for batch in seen)
        assert set().union(*seen) == set(range(500))
        assert run_state.task_reclaim_lookup_cursor == 0

    def test_empty_ledger_makes_no_api_call(self, tmp_path):
        config = self._config(tmp_path)

        removed = discard_reclaim_counts_for_closed_issues(
            RunState(), self._issues([(280, "CLOSED")]), config
        )

        assert removed == []
        config.forge.get_issue_state.assert_not_called()
        config.forge.list_issues_by_label.assert_not_called()


_RECLAIM_FIELDS = frozenset({"count", "last_reclaimed_at", "pending"})


def _budgeted(**overrides) -> TaskReclaimRecord:
    """#1279: 回収以外の独立予算（early-death・review-timeout・usage-limit）を
    すべて使用済みにした台帳エントリ。

    新しい予算フィールドが追加されたら、ここで非既定値にしないとテストが
    失敗する（置き換えによる取りこぼしを検知し続けるため）。
    """
    values: dict[str, Any] = dict(
        count=1,
        last_reclaimed_at=100.0,
        pending=False,
        early_death_retry_count=2,
        early_death_retry_at=5.0,
        early_death_retry_pending=True,
        review_timeout_retry_count=1,
        review_timeout_retry_at=7.0,
        review_timeout_retry_pending=True,
        usage_limit_retry_count=1,
        usage_limit_retry_at=9.0,
        usage_limit_retry_pending=True,
        usage_limit_retry_run="claim-1",
    )
    values.update(overrides)
    record = TaskReclaimRecord(**values)
    default = TaskReclaimRecord()
    for field in fields(record):
        if field.name not in _RECLAIM_FIELDS:
            assert getattr(record, field.name) != getattr(
                default, field.name
            ), f"_budgeted must set {field.name} to a non-default value"
    return record


def _independent(record: TaskReclaimRecord) -> dict[str, object]:
    return {
        field.name: getattr(record, field.name)
        for field in fields(record)
        if field.name not in _RECLAIM_FIELDS
    }


class TestReclaimKeepsIndependentBudgets:
    """#1279: 回収の予約・保留は、early-death / review-timeout / usage-limit の
    予算を巻き込まない（既存エントリを更新し、置き換えない）。"""

    @pytest.mark.parametrize(
        ("pending", "expected_count"), [(False, 2), (True, 1)], ids=["new", "reused"]
    )
    def test_zombie_reclaim_keeps_the_independent_budgets(
        self, tmp_path, fake_forge, pending, expected_count
    ):
        previous = _budgeted(pending=pending)
        run_state = RunState(task_reclaim_counts={280: previous})
        config = _config(tmp_path, max_task_reclaims=5, forge=fake_forge)

        _run_timeout_cycles(run_state, config, cycles=1, tmp_path=tmp_path)

        saved = load_run_state(config.run_state_path).task_reclaim_counts[280]
        for record in (run_state.task_reclaim_counts[280], saved):
            assert _independent(record) == _independent(previous)
            assert record.count == expected_count
            assert record.pending is False
        assert _independent(previous) == _independent(_budgeted(pending=pending))

    def test_zombie_reclaim_reservation_keeps_the_budgets_on_disk(
        self, tmp_path, fake_forge
    ):
        """予約（pending=True）の時点で、すでに読み戻せる形で保持されている。"""
        previous = _budgeted()
        run_state = RunState(task_reclaim_counts={280: previous})
        config = _config(tmp_path, max_task_reclaims=5, forge=fake_forge)
        run_state.active_worktrees["280"] = _active(
            worktree_path=str(tmp_path / "missing-280")
        )
        persisted: list[TaskReclaimRecord] = []

        def _record_persisted(issue_number, label):
            persisted.append(
                load_run_state(config.run_state_path).task_reclaim_counts[280]
            )

        with (
            patch("orchestune.dispatch.phase_gc.time.time", return_value=_NOW),
            patch.object(
                config.resolved_forge, "add_label", side_effect=_record_persisted
            ),
        ):
            _collect_zombies_and_timeouts(run_state, {280: _task()}, config)

        assert persisted
        assert persisted[0].pending is True
        assert _independent(persisted[0]) == _independent(previous)

    @pytest.mark.parametrize(
        ("pending", "expected_count"), [(False, 2), (True, 1)], ids=["new", "reused"]
    )
    def test_cloud_reservation_keeps_the_independent_budgets(
        self, pending, expected_count
    ):
        previous = _budgeted(pending=pending)
        run_state = RunState(task_reclaim_counts={280: previous})
        seen: list[TaskReclaimRecord] = []

        count = _reserve_cloud_reclaim_record(
            280, run_state, lambda: seen.append(run_state.task_reclaim_counts[280])
        )

        reserved = run_state.task_reclaim_counts[280]
        assert count == expected_count == reserved.count
        assert reserved.pending is True
        assert _independent(reserved) == _independent(previous)
        assert seen == [reserved]
        assert reserved is not previous

    def test_cloud_reservation_without_a_record_starts_from_the_defaults(self):
        run_state = RunState()

        count = _reserve_cloud_reclaim_record(280, run_state)

        reserved = run_state.task_reclaim_counts[280]
        assert count == reserved.count == 1
        assert reserved.pending is True
        assert _independent(reserved) == _independent(TaskReclaimRecord())

    def test_cloud_reservation_failure_restores_the_whole_record(self):
        previous = _budgeted()
        run_state = RunState(task_reclaim_counts={280: previous})

        def fail():
            raise RuntimeError("persist failed")

        with pytest.raises(RuntimeError, match="persist failed"):
            _reserve_cloud_reclaim_record(280, run_state, fail)

        assert run_state.task_reclaim_counts[280] is previous
        assert previous == _budgeted()

    def test_cloud_reservation_failure_forgets_a_record_that_did_not_exist(self):
        run_state = RunState()

        def fail():
            raise RuntimeError("persist failed")

        with pytest.raises(RuntimeError):
            _reserve_cloud_reclaim_record(280, run_state, fail)

        assert run_state.task_reclaim_counts == {}

    @pytest.mark.parametrize("pending", [False, True], ids=["idle", "reserved"])
    def test_dirty_hold_keeps_the_pending_flag_and_budgets(
        self, tmp_path, fake_forge, pending
    ):
        """保留は回収予約を新設も解除もしない（pending は既存の担当箇所が解除する）。"""
        ctx = _ctx(forge=fake_forge)
        ctx.config.apply = True
        ctx.config.run_state_path = tmp_path / "run_state.json"
        ctx.config.max_task_reclaims = 5
        active = ctx.run_state.active_worktrees.setdefault(
            "1", _active(issue_number=280, worktree_path="worktrees/w1")
        )
        previous = _budgeted(pending=pending)
        ctx.run_state.task_reclaim_counts[280] = previous

        TestDirtyWorktreeHoldLimit()._hold_cycle(
            ctx, active, _task(status_labels=("status:in-progress",))
        )

        saved = load_run_state(ctx.config.run_state_path).task_reclaim_counts[280]
        for record in (ctx.run_state.task_reclaim_counts[280], saved):
            assert record.count == 2
            assert record.pending is pending
            assert _independent(record) == _independent(previous)
        assert previous == _budgeted(pending=pending)

    def test_dirty_hold_without_a_record_counts_from_zero(self, tmp_path, fake_forge):
        ctx = _ctx(forge=fake_forge)
        ctx.config.apply = True
        ctx.config.run_state_path = tmp_path / "run_state.json"
        active = ctx.run_state.active_worktrees.setdefault(
            "1", _active(issue_number=280, worktree_path="worktrees/w1")
        )

        TestDirtyWorktreeHoldLimit()._hold_cycle(
            ctx, active, _task(status_labels=("status:in-progress",))
        )

        record = ctx.run_state.task_reclaim_counts[280]
        assert record.count == 1
        assert record.pending is False
        assert _independent(record) == _independent(TaskReclaimRecord())

    def test_dirty_hold_persist_failure_keeps_the_in_memory_update(
        self, tmp_path, fake_forge, capsys
    ):
        """保留の保存は best-effort のまま（今回 fail-closed にしない）。"""
        ctx = _ctx(forge=fake_forge)
        ctx.config.apply = True
        ctx.config.run_state_path = tmp_path / "run_state.json"
        ctx.config.max_task_reclaims = 5
        active = ctx.run_state.active_worktrees.setdefault(
            "1", _active(issue_number=280, worktree_path="worktrees/w1")
        )
        previous = _budgeted()
        ctx.run_state.task_reclaim_counts[280] = previous

        with patch(
            "orchestune.dispatch.gc.save_run_state",
            autospec=True,
            side_effect=OSError("disk full"),
        ):
            TestDirtyWorktreeHoldLimit()._hold_cycle(
                ctx, active, _task(status_labels=("status:in-progress",))
            )

        record = ctx.run_state.task_reclaim_counts[280]
        assert record.count == 2
        assert _independent(record) == _independent(previous)
        assert "disk full" in capsys.readouterr().err


def _scenario_cloud_reservation(*_unused) -> None:
    previous = _budgeted()
    run_state = RunState(task_reclaim_counts={280: previous})
    _reserve_cloud_reclaim_record(280, run_state)
    reserved = run_state.task_reclaim_counts[280]
    require(
        "P3B-RECLAIM-BUDGET-PRESERVED",
        _independent(reserved) == _independent(previous),
        reserved,
    )


def _scenario_dirty_hold(tmp_path, fake_forge) -> None:
    ctx = _ctx(forge=fake_forge)
    ctx.config.apply = True
    ctx.config.run_state_path = tmp_path / "run_state.json"
    ctx.config.max_task_reclaims = 5
    active = ctx.run_state.active_worktrees.setdefault(
        "1", _active(issue_number=280, worktree_path="worktrees/w1")
    )
    previous = _budgeted(pending=True)
    ctx.run_state.task_reclaim_counts[280] = previous
    TestDirtyWorktreeHoldLimit()._hold_cycle(
        ctx, active, _task(status_labels=("status:in-progress",))
    )
    saved = load_run_state(ctx.config.run_state_path).task_reclaim_counts[280]
    require(
        "P3B-RECLAIM-BUDGET-PRESERVED",
        _independent(saved) == _independent(previous),
        saved,
    )
    require("P3B-RECLAIM-BUDGET-PRESERVED", saved.pending is True, saved)


class TestReclaimRecordReplacementIsDetected:
    """#1279: 旧式（新しいレコードで置き換える書き込み）を注入すると、cloud 予約と
    dirty 保留の経路でも `P3B-RECLAIM-BUDGET-PRESERVED` の違反になる。"""

    @staticmethod
    def _replacing(real):
        def replacing(obj, **changes):
            if isinstance(obj, TaskReclaimRecord):
                return TaskReclaimRecord(**changes)
            return real(obj, **changes)

        return replacing

    def test_control_cloud_reservation_replacement_is_detected(self, monkeypatch):
        _scenario_cloud_reservation()
        monkeypatch.setattr(
            "orchestune.dispatch.gc.cloud_completion.replace", self._replacing(replace)
        )
        with expect_violation("P3B-RECLAIM-BUDGET-PRESERVED"):
            _scenario_cloud_reservation()

    def test_control_dirty_hold_replacement_is_detected(
        self, monkeypatch, tmp_path, fake_forge
    ):
        _scenario_dirty_hold(tmp_path / "clean", fake_forge)
        monkeypatch.setattr("orchestune.dispatch.gc.replace", self._replacing(replace))
        with expect_violation("P3B-RECLAIM-BUDGET-PRESERVED"):
            _scenario_dirty_hold(tmp_path / "faulty", fake_forge)
