"""#512: ゾンビ／タイムアウト回収による再投入の回数上限テスト。

`--task-timeout-seconds` に正の値を設定していても、回収されたタスクは
タスクごとのカウンタを持たないまま `status:queued` へ戻され続けていた
（終端のない経路）。上限を超えた場合に `status:blocked-human-review`
へ遷移して停止することを検証する。
"""

from unittest.mock import MagicMock, patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_context import (
    IssuesByStatus,
    discard_reclaim_counts_for_closed_issues,
)
from orchestune.dispatch.gc import _rule_completed, _rule_not_needed
from orchestune.dispatch.gc.zombies import (
    _collect_zombies_and_timeouts,
)
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import (
    ActiveWorktree,
    RunState,
    TaskReclaimRecord,
    load_run_state,
)
from orchestune.models import IssueRecord
from tests.dispatch_gc_test_support import _ctx

_NOW = 2_000.0


def _active(**overrides):
    defaults = dict(
        issue_number=280,
        branch="claude/issue-280-task-a",
        worktree_path="worktrees/missing-280",
        pid=None,
        started_at=1_000.0,
        declared_footprint=("src/foo.py",),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


def _task(**overrides):
    defaults = dict(
        issue_number=280,
        subtask_id="task-a",
        footprint=("src/foo.py",),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:in-progress",),
        created_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _config(tmp_path, **overrides):
    defaults = dict(
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        apply=True,
        task_timeout_seconds=60,
    )
    defaults.update(overrides)
    return DispatcherConfig(**defaults)


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
        with patch("orchestune.dispatch.gc.zombies.time.time", return_value=_NOW):
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
            patch("orchestune.dispatch.gc._is_worktree_complete", return_value=True),
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                return_value={"action": action, "commit_sha": "abc123d"},
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
            return_value={"action": "not_needed", "issue_number": 280},
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
            patch("orchestune.dispatch.gc._is_worktree_complete", return_value=True),
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                return_value={
                    "action": "completion_skipped_dirty_worktree",
                    "issue_number": 280,
                    "worktree_path": active.worktree_path,
                },
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
            actions.append(outcome.completion_event["action"])

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
            patch("orchestune.dispatch.gc._is_worktree_complete", return_value=True),
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                return_value={
                    "action": "completion_skipped_dirty_worktree",
                    "issue_number": 280,
                    "worktree_path": active.worktree_path,
                },
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.completion_event["action"] == "completion_skipped_dirty_worktree"
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
            patch("orchestune.dispatch.gc._is_worktree_complete", return_value=True),
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                return_value={
                    "action": "completion_skipped_dirty_worktree",
                    "issue_number": 280,
                    "worktree_path": active.worktree_path,
                },
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.completion_event["action"] == "escalated_reclaim_limit_exceeded"
        assert ctx.run_state.active_worktrees == {}
        assert load_run_state(ctx.config.run_state_path).active_worktrees == {}

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
        assert outcome.completion_event["action"] == "completion_skipped_dirty_worktree"
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
        return DispatcherConfig(**defaults)

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
