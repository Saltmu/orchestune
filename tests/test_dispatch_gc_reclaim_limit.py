"""#512: ゾンビ／タイムアウト回収による再投入の回数上限テスト。

`--task-timeout-seconds` に正の値を設定していても、回収されたタスクは
タスクごとのカウンタを持たないまま `status:queued` へ戻され続けていた
（終端のない経路）。上限を超えた場合に `status:blocked-human-review`
へ遷移して停止することを検証する。
"""

from unittest.mock import MagicMock, patch

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle_context import (
    IssuesByStatus,
    discard_reclaim_counts_for_closed_issues,
)
from orchestune.dispatch_gc import _rule_completed, _rule_not_needed
from orchestune.dispatch_gc_zombies import (
    ZombieOrTimeoutReclaim,
    _apply_zombie_or_timeout_reclaim,
    _collect_zombies_and_timeouts,
    _decide_zombie_or_timeout_reclaims,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import (
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
    for _ in range(cycles):
        run_state.active_worktrees["280"] = _active(
            worktree_path=str((tmp_path or config.log_dir) / "missing-280")
        )
        with (
            patch("orchestune.dispatch_gc_zombies.time.time", return_value=_NOW),
            patch(
                "orchestune.forge.GitHubForge.add_label",
                side_effect=lambda issue, label: labels.append(("add", label)),
            ),
            patch(
                "orchestune.forge.GitHubForge.remove_label",
                side_effect=lambda issue, label: labels.append(("remove", label)),
            ),
            patch(
                "orchestune.forge.GitHubForge.add_comment",
                side_effect=lambda issue, body: comments.append(body),
            ),
        ):
            cycle_events = _collect_zombies_and_timeouts(run_state, {280: task}, config)
        assert len(cycle_events) == 1
        events.append(cycle_events[0])
    return events, labels, comments


class TestReclaimRetryBound:
    def test_repeated_timeout_reclaims_stop_at_the_limit(self, tmp_path):
        """再現テスト(#512): 既定の上限を超えて回収されたタスクは
        `status:queued` へ再投入されず `status:blocked-human-review` で停止する。"""
        run_state = RunState(active_worktrees={})
        config = _config(tmp_path)

        events, labels, _ = _run_timeout_cycles(
            run_state, config, cycles=5, tmp_path=tmp_path
        )

        assert [event["action"] for event in events] == [
            "gc_reclaimed",
            "gc_reclaimed",
            "gc_reclaimed",
            "escalated_reclaim_limit_exceeded",
            "escalated_reclaim_limit_exceeded",
        ]
        assert ("add", "status:blocked-human-review") in labels
        # 上限超過後はもうqueuedへ戻されない
        assert labels.count(("add", "status:queued")) == 3

    def test_reclaims_up_to_the_limit_are_still_requeued(self, tmp_path):
        """境界値: 上限ちょうどまでは従来通りstatus:queuedへ再投入される。"""
        run_state = RunState(active_worktrees={})
        config = _config(tmp_path, max_task_reclaims=2)

        events, labels, _ = _run_timeout_cycles(
            run_state, config, cycles=3, tmp_path=tmp_path
        )

        assert [event["action"] for event in events] == [
            "gc_reclaimed",
            "gc_reclaimed",
            "escalated_reclaim_limit_exceeded",
        ]
        assert [event["reclaim_count"] for event in events] == [1, 2, 3]
        assert labels.count(("add", "status:queued")) == 2
        assert run_state.task_reclaim_counts[280] == TaskReclaimRecord(
            count=3, last_reclaimed_at=_NOW
        )

    def test_zero_limit_escalates_on_the_first_reclaim(self, tmp_path):
        """`max_task_reclaims=0`は「1回目の回収で即エスカレーション」を意味する。"""
        run_state = RunState(active_worktrees={})
        config = _config(tmp_path, max_task_reclaims=0)

        events, labels, _ = _run_timeout_cycles(
            run_state, config, cycles=1, tmp_path=tmp_path
        )

        assert events[0]["action"] == "escalated_reclaim_limit_exceeded"
        assert ("add", "status:queued") not in labels

    def test_escalation_transitions_labels_and_reports_reason(self, tmp_path):
        """エスカレーション時に回収回数と理由がIssueへコメントされること。"""
        run_state = RunState(active_worktrees={})
        config = _config(tmp_path, max_task_reclaims=1)

        _, labels, comments = _run_timeout_cycles(
            run_state, config, cycles=2, tmp_path=tmp_path
        )

        assert ("add", "status:blocked-human-review") in labels
        assert ("remove", "status:in-progress") in labels
        escalation_comment = comments[-1]
        assert "2回目" in escalation_comment
        assert "max_task_reclaims=1" in escalation_comment
        assert "timeout exceeded" in escalation_comment

    def test_zombie_reclaims_share_the_same_counter(self, tmp_path):
        """タイムアウトだけでなくゾンビ回収も同じカウンタで拘束される。"""
        run_state = RunState(active_worktrees={})
        config = _config(tmp_path, task_timeout_seconds=0, max_task_reclaims=1)
        actions = []
        for _ in range(2):
            # pid不在・worktree不在・started_at不明はゾンビ相当（#383）
            run_state.active_worktrees["280"] = _active(
                started_at=None, worktree_path=str(tmp_path / "missing-280")
            )
            with (
                patch("orchestune.forge.GitHubForge.add_label"),
                patch("orchestune.forge.GitHubForge.remove_label"),
                patch("orchestune.forge.GitHubForge.add_comment"),
            ):
                events = _collect_zombies_and_timeouts(
                    run_state, {280: _task()}, config
                )
            assert events[0]["reason"] == "process disappeared"
            actions.append(events[0]["action"])

        assert actions == ["gc_reclaimed", "escalated_reclaim_limit_exceeded"]

    def test_already_escalated_task_is_not_counted(self, tmp_path):
        """既に人間の確認待ちのタスクは再投入されないため回数にも数えない。"""
        active = _active(worktree_path=str(tmp_path / "missing-280"))
        run_state = RunState(active_worktrees={"280": active})
        config = _config(tmp_path)
        reclaim = ZombieOrTimeoutReclaim(
            key="280",
            active=active,
            subtask_id="task-a",
            reason="timeout exceeded",
            is_timeout=True,
            process_alive=False,
            status_labels=("status:blocked-human-review", "status:in-progress"),
            reclaim_count=1,
            now=_NOW,
        )

        with (
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        assert event is not None
        assert event["action"] == "gc_reclaimed"
        assert event["reclaim_count"] == 0
        assert run_state.task_reclaim_counts == {}

    def test_dry_run_reports_escalation_without_touching_the_ledger(self, tmp_path):
        """--no-applyでは台帳を変更しないが、イベントは適用時と同じ判定を返す。"""
        active = _active(worktree_path=str(tmp_path / "missing-280"))
        run_state = RunState(
            active_worktrees={"280": active},
            task_reclaim_counts={
                280: TaskReclaimRecord(count=3, last_reclaimed_at=1.0)
            },
        )
        config = _config(tmp_path, apply=False)

        with patch("orchestune.dispatch_gc_zombies.time.time", return_value=_NOW):
            events = _collect_zombies_and_timeouts(run_state, {280: _task()}, config)

        assert events[0]["action"] == "escalated_reclaim_limit_exceeded"
        assert events[0]["reclaim_count"] == 4
        assert run_state.task_reclaim_counts[280] == TaskReclaimRecord(
            count=3, last_reclaimed_at=1.0
        )
        assert run_state.active_worktrees == {"280": active}

    def test_reclaim_count_is_persisted_before_the_label_transition(self, tmp_path):
        """PR#520レビュー2巡目対応(Codex P2): `status:queued`を露出させる前に、
        回収回数がディスクへ書かれていること（ローカル先行）。

        逆順だと、ラベル遷移後・サイクル終端の保存前に落ちた場合に
        `run_state.json`へ回数が残らず、次サイクルのstaleエントリ破棄経由で
        回収を数えないまま再起動できてしまう。
        """
        active = _active(worktree_path=str(tmp_path / "missing-280"))
        run_state = RunState(active_worktrees={"280": active})
        config = _config(tmp_path)
        persisted: list[dict] = []

        def _record_persisted(issue_number, label):
            persisted.append(load_run_state(config.run_state_path).task_reclaim_counts)

        with (
            patch("orchestune.dispatch_gc_zombies.time.time", return_value=_NOW),
            patch(
                "orchestune.forge.GitHubForge.add_label", side_effect=_record_persisted
            ),
            patch(
                "orchestune.forge.GitHubForge.remove_label",
                side_effect=_record_persisted,
            ),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            _collect_zombies_and_timeouts(run_state, {280: _task()}, config)

        assert persisted
        assert persisted[0] == {280: TaskReclaimRecord(count=1, last_reclaimed_at=_NOW)}

    def test_reclaim_is_skipped_when_the_ledger_cannot_be_persisted(
        self, tmp_path, capsys
    ):
        """永続化に失敗した回収はfail-closedで見送り、プロセスkill・worktree削除・
        ラベル遷移のいずれの副作用も起こさない（記録が先、破壊は後）。"""
        worktree = tmp_path / "wt-280"
        worktree.mkdir()
        active = _active(worktree_path=str(worktree), pid=4242)
        run_state = RunState(active_worktrees={"280": active})
        config = _config(tmp_path)

        with (
            patch("orchestune.dispatch_gc_zombies.time.time", return_value=_NOW),
            patch("orchestune.dispatch_gc_zombies.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_gc_zombies.save_run_state",
                side_effect=OSError("no space left on device"),
            ),
            patch("orchestune.dispatch_gc_zombies.os.kill") as mock_kill,
            patch("orchestune.dispatch_gc_zombies.backup_wip_commit") as mock_backup,
            patch(
                "orchestune.dispatch_gc_zombies.remove_worktree"
            ) as mock_remove_worktree,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
        ):
            events = _collect_zombies_and_timeouts(run_state, {280: _task()}, config)

        assert events == []
        mock_kill.assert_not_called()
        mock_backup.assert_not_called()
        mock_remove_worktree.assert_not_called()
        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_add_comment.assert_not_called()
        # 次サイクルでやり直せるよう、台帳もactiveエントリもそのまま残す
        assert run_state.task_reclaim_counts == {}
        assert run_state.active_worktrees == {"280": active}
        assert "no space left on device" in capsys.readouterr().err

    def test_failed_persist_restores_a_previous_reclaim_count(self, tmp_path):
        """永続化に失敗しても、既存の回数をインクリメント後の値で汚さない。"""
        active = _active(worktree_path=str(tmp_path / "missing-280"))
        previous = TaskReclaimRecord(count=1, last_reclaimed_at=1.0)
        run_state = RunState(
            active_worktrees={"280": active},
            task_reclaim_counts={280: previous},
        )
        config = _config(tmp_path)

        with (
            patch("orchestune.dispatch_gc_zombies.time.time", return_value=_NOW),
            patch(
                "orchestune.dispatch_gc_zombies.save_run_state",
                side_effect=OSError("boom"),
            ),
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            _collect_zombies_and_timeouts(run_state, {280: _task()}, config)

        assert run_state.task_reclaim_counts == {280: previous}

    def _reclaim_with_failing_backup(self, tmp_path, run_state, config):
        worktree = tmp_path / "wt-280"
        worktree.mkdir(exist_ok=True)
        run_state.active_worktrees["280"] = _active(worktree_path=str(worktree))
        comments: list[str] = []
        with (
            patch("orchestune.dispatch_gc_zombies.time.time", return_value=_NOW),
            patch(
                "orchestune.dispatch_gc_zombies.backup_wip_commit",
                return_value="fatal: unable to write new index file",
            ),
            patch(
                "orchestune.dispatch_gc_zombies.remove_worktree"
            ) as mock_remove_worktree,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch(
                "orchestune.forge.GitHubForge.add_comment",
                side_effect=lambda issue, body: comments.append(body),
            ),
        ):
            events = _collect_zombies_and_timeouts(run_state, {280: _task()}, config)
        return events, comments, mock_add_label, mock_remove_worktree

    def test_repeated_wip_backup_failures_escalate_at_the_limit(self, tmp_path):
        """PR#520レビュー3巡目対応(Codex P1): WIPバックアップが失敗し続けるタスクも
        上限で終端する（status:in-progressのままクオータを占有し続けない）。"""
        run_state = RunState(active_worktrees={})
        config = _config(tmp_path, max_task_reclaims=1)

        # 1巡目: 従来どおりスキップ（コメントのみ、帳簿エントリは保持）
        events, comments, mock_add_label, mock_remove_worktree = (
            self._reclaim_with_failing_backup(tmp_path, run_state, config)
        )
        assert events == []
        mock_add_label.assert_not_called()
        mock_remove_worktree.assert_not_called()
        assert "一時スキップ" in comments[0]
        assert "280" in run_state.active_worktrees

        # 2巡目: 上限超過につき人間の確認へ回す
        events, comments, mock_add_label, mock_remove_worktree = (
            self._reclaim_with_failing_backup(tmp_path, run_state, config)
        )
        assert [event["action"] for event in events] == [
            "escalated_reclaim_limit_exceeded"
        ]
        mock_add_label.assert_called_once_with(280, "status:blocked-human-review")
        # 未コミットの作業データを守るため、worktreeは削除しない
        mock_remove_worktree.assert_not_called()
        assert (tmp_path / "wt-280").exists()
        # 帳簿エントリは解放し、クオータを占有し続けない
        assert run_state.active_worktrees == {}
        assert "worktreeは削除せずに残しています" in comments[0]
        assert "fatal: unable to write new index file" in comments[0]

    def test_github_failure_leaves_the_entry_reclaimable_next_cycle(self, tmp_path):
        """PR#520レビュー7巡目対応(Codex P1): GitHubへの反映が失敗しても、
        帳簿エントリがスロットを占有し続けない。

        worktreeは削除済みなので、タイムアウト判定が無効な構成では
        `started_at`を未知へ落として#383のゾンビ復旧経路に載せる。
        """
        worktree = tmp_path / "wt-280"
        worktree.mkdir()
        active = _active(worktree_path=str(worktree))
        run_state = RunState(active_worktrees={"280": active})
        config = _config(tmp_path, task_timeout_seconds=0, max_task_reclaims=0)

        with (
            patch("orchestune.dispatch_gc_zombies.time.time", return_value=_NOW),
            patch(
                "orchestune.dispatch_gc_zombies.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.dispatch_gc_zombies.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_gc_zombies.backup_wip_commit", return_value=None
            ),
            patch("orchestune.dispatch_gc_zombies.remove_worktree"),
            patch(
                "orchestune.forge.GitHubForge.add_label",
                side_effect=RuntimeError("gh: API is unavailable"),
            ),
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            events = _collect_zombies_and_timeouts(run_state, {280: _task()}, config)

        assert events == []
        # エントリは残したまま、次サイクルのゾンビ判定で拾える形にする
        assert set(run_state.active_worktrees) == {"280"}
        assert run_state.active_worktrees["280"].started_at is None
        # 回数は永続化済みのまま（二重には数えない）
        persisted = load_run_state(config.run_state_path)
        assert persisted.task_reclaim_counts[280].count == 1
        assert persisted.active_worktrees["280"].started_at is None

    def test_github_failure_marks_for_retry_even_with_timeouts_enabled(self, tmp_path):
        """PR#520レビュー8巡目対応(Codex P2): タイムアウトが有効な構成でも印を付ける。

        started_atを残すと、GCフェーズより先に走る`_rule_completed`が
        「プロセス停止 + worktree不在」を完了とみなし、記録済みの回収を再試行せず
        `completed_no_commits`として即エスカレーションしてしまう。
        """
        worktree = tmp_path / "wt-280"
        worktree.mkdir()
        active = _active(worktree_path=str(worktree))
        run_state = RunState(active_worktrees={"280": active})
        config = _config(tmp_path, task_timeout_seconds=60, max_task_reclaims=0)

        with (
            patch("orchestune.dispatch_gc_zombies.time.time", return_value=_NOW),
            patch(
                "orchestune.dispatch_gc_zombies.backup_wip_commit", return_value=None
            ),
            patch("orchestune.dispatch_gc_zombies.remove_worktree"),
            patch(
                "orchestune.forge.GitHubForge.add_label",
                side_effect=RuntimeError("gh: API is unavailable"),
            ),
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            events = _collect_zombies_and_timeouts(run_state, {280: _task()}, config)

        assert events == []
        assert run_state.active_worktrees["280"].started_at is None

    def test_github_failure_keeps_started_at_while_the_process_is_alive(self, tmp_path):
        """killに失敗してプロセスが生き残っている場合は開始時刻を壊さない。

        ゾンビ判定はプロセス停止が前提で拾えないため、started_atを消すと
        タイムアウト判定まで無効化してしまう。
        """
        worktree = tmp_path / "wt-280"
        worktree.mkdir()
        active = _active(worktree_path=str(worktree), pid=4242)
        run_state = RunState(active_worktrees={"280": active})
        config = _config(tmp_path, task_timeout_seconds=60, max_task_reclaims=0)

        with (
            patch("orchestune.dispatch_gc_zombies.time.time", return_value=_NOW),
            patch("orchestune.dispatch_gc_zombies.is_process_alive", return_value=True),
            patch("orchestune.dispatch_gc_zombies.os.kill"),
            patch(
                "orchestune.dispatch_gc_zombies.backup_wip_commit", return_value=None
            ),
            patch("orchestune.dispatch_gc_zombies.remove_worktree"),
            patch(
                "orchestune.forge.GitHubForge.add_label",
                side_effect=RuntimeError("gh: API is unavailable"),
            ),
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            events = _collect_zombies_and_timeouts(run_state, {280: _task()}, config)

        assert events == []
        assert run_state.active_worktrees["280"].started_at == active.started_at

    def test_decide_layer_does_not_mutate_the_ledger(self, tmp_path):
        """decide層は副作用を持たない（台帳の更新はapply層の責務）。"""
        active = _active(worktree_path=str(tmp_path / "missing-280"))
        run_state = RunState(active_worktrees={"280": active})
        config = _config(tmp_path)

        reclaims = _decide_zombie_or_timeout_reclaims(
            run_state, {280: _task()}, config, None, _NOW
        )

        assert len(reclaims) == 1
        assert reclaims[0].reclaim_count == 1
        assert reclaims[0].escalate is False
        assert run_state.task_reclaim_counts == {}


class TestReclaimCounterLifecycle:
    """#512: 回収回数の台帳から記録が破棄される条件。

    PR#520レビュー6巡目対応(Codex P2): 破棄の根拠は「GitHub上でIssueが
    クローズされたこと」ただ一つ。`status:done`（ワーカー完了）の時点では
    Integratorの仮マージCI失敗で`status:queued`へ差し戻され得るため破棄しない。
    """

    def _completed_ctx(self, action="completed"):
        ctx = _ctx()
        ctx.config.apply = True
        active = ctx.run_state.active_worktrees.setdefault(
            "1", _active(issue_number=280, worktree_path="worktrees/w1")
        )
        ctx.run_state.task_reclaim_counts[280] = TaskReclaimRecord(
            count=2, last_reclaimed_at=_NOW
        )
        task = _task(status_labels=("status:in-progress",))
        with (
            patch("orchestune.dispatch_gc._is_worktree_complete", return_value=True),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": action, "commit_sha": "abc123d"},
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)
        return ctx, outcome

    def test_worker_completion_alone_keeps_the_reclaim_count(self):
        """ワーカー完了（status:done）だけでは破棄しない。

        `_finalize_completed_worktree`はIssueをクローズしないため、この時点で
        破棄すると、Integratorの仮マージCI失敗（`handle_merge_failure`）で
        `status:queued`へ差し戻されたタスクが回数0から再開してしまい、
        「GC回収 → ワーカー完了 → 統合失敗」の繰り返しで上限を素通りできる。
        """
        ctx, outcome = self._completed_ctx()

        assert outcome is not None
        assert ctx.run_state.task_reclaim_counts == {
            280: TaskReclaimRecord(count=2, last_reclaimed_at=_NOW)
        }

    def test_token_limit_escalation_keeps_the_reclaim_count(self):
        ctx, outcome = self._completed_ctx(action="escalated_token_limit_exceeded")

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
            "orchestune.dispatch_gc._finalize_not_needed_worktree",
            return_value={"action": "not_needed", "issue_number": 280},
        ):
            outcome = _rule_not_needed(ctx, "1", active, task)

        assert outcome is not None
        assert ctx.run_state.task_reclaim_counts == {280: record}


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
        config.forge.list_issues_by_label.side_effect = lambda label, state="open": (
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

    def test_empty_ledger_makes_no_api_call(self, tmp_path):
        config = self._config(tmp_path)

        removed = discard_reclaim_counts_for_closed_issues(
            RunState(), self._issues([(280, "CLOSED")]), config
        )

        assert removed == []
        config.forge.get_issue_state.assert_not_called()
        config.forge.list_issues_by_label.assert_not_called()
