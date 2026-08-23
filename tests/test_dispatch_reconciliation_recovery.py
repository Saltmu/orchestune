"""dispatch_reconciliation.py の復元・整合性修復に関する境界値テスト (#337)。

`_collect_active_conflict_subtask_ids` / `_decide_blocked_promotions` /
`_handle_blocked_recompute_recovery` は既存の `tests/test_dispatch_cycle.py`
では実質未検証だったため、本ファイルで単体テストとして完結させる。
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_reconciliation import (
    BaseBranchRedRecoveryDecision,
    _apply_base_branch_red_recovery,
    _decide_base_branch_red_recovery,
    _handle_base_branch_red_recovery,
    _reconcile_recovery_counters,
    _resolve_base_branch_for_task,
    _restore_launch_history,
    _self_heal_launch_history,
    _self_heal_run_state,
)
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.models import IssueRecord
from orchestune.outcome_record import OutcomeRecord

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-reconciliation-"))


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


def _active(**overrides):
    defaults = dict(
        issue_number=1,
        branch="claude/issue-1-task-a",
        worktree_path="worktrees/w1",
        pid=111,
        started_at=1_699_999_000.0,
        declared_footprint=(),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


def _ctx(**overrides):
    defaults = dict(
        run_state=RunState(active_worktrees={}),
        tasks_by_issue={},
        issue_number_by_subtask_id={},
        done_subtask_ids=set(),
        ci_passed_pr_subtask_ids=set(),
        changes_requested_subtask_ids=set(),
        subtask_branch_map={},
        prs=[],
        pr_by_branch={},
        config=DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        ),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


def _issue(number, labels=(), state="OPEN"):
    return IssueRecord(
        number=number,
        title=f"Issue {number}",
        body="",
        labels=labels,
        created_at="2026-01-01T00:00:00+00:00",
        state=state,
    )


class _IssuesStub:
    """`_handle_blocked_recompute_recovery`が要求する`.all()`のみを持つ最小スタブ。"""

    def __init__(self, issues):
        self._issues = list(issues)

    def all(self):
        return list(self._issues)


class TestSelfHealRunState:
    def test_persists_recovered_state_when_run_state_missing(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )
        run_state = RunState(active_worktrees={})

        with (
            patch(
                "orchestune.forge.GitHubForge.list_issues_by_label",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch_reconciliation.recover_run_state",
                return_value=True,
            ),
            patch("orchestune.dispatch_reconciliation.save_run_state") as mock_save,
        ):
            _self_heal_run_state(run_state, config)

        mock_save.assert_called_once_with(
            run_state,
            config.run_state_path,
            launch_window_seconds=config.window_seconds,
        )

    def test_does_not_persist_when_recovery_reports_no_change(self, tmp_path):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )
        run_state = RunState(active_worktrees={})

        with (
            patch(
                "orchestune.forge.GitHubForge.list_issues_by_label",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch_reconciliation.recover_run_state",
                return_value=False,
            ),
            patch("orchestune.dispatch_reconciliation.save_run_state") as mock_save,
        ):
            _self_heal_run_state(run_state, config)

        mock_save.assert_not_called()


class TestReconcileRecoveryCounters:
    """#516: `_reconcile_stale_recovery_counters`をrun_state.json有無に
    関わらず毎サイクル呼び出す（再3巡目レビュー指摘）。再4巡目レビュー指摘
    により以下2点を追加で担保する:
    (1) --parent-issue指定時のスコープ済みIssue一覧に依存せず、常に
        リポジトリ全体のstatus:in-progress Issueを独自に読み直す（#156と
        同じ理由: run_stateは複数親Issueにまたがって共有されうる）。
    (2) 早期保存がopen_prsを渡さずにsave_run_stateを呼ぶと、30日超の
        completed_worktrees保護（open PRのlast_completed）が通常の
        サイクル終端保存より先に無条件で刈り込まれてしまうため、
        変更があった場合のみopen_prsを取得して渡す。
    """

    def test_persists_with_open_prs_when_reconciliation_reports_a_change(
        self, tmp_path
    ):
        run_state_path = tmp_path / "run_state.json"
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )
        run_state = RunState(active_worktrees={})
        open_prs = [MagicMock()]

        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=open_prs),
            patch(
                "orchestune.dispatch_reconciliation._reconcile_stale_recovery_counters",
                return_value=True,
            ),
            patch("orchestune.dispatch_reconciliation.save_run_state") as mock_save,
        ):
            _reconcile_recovery_counters(run_state, config)

        mock_save.assert_called_once_with(
            run_state,
            config.run_state_path,
            launch_window_seconds=config.window_seconds,
            open_prs=open_prs,
        )

    def test_does_not_fetch_open_prs_or_save_when_no_change(self, tmp_path):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )
        run_state = RunState(active_worktrees={})

        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs") as mock_list_prs,
            patch(
                "orchestune.dispatch_reconciliation._reconcile_stale_recovery_counters",
                return_value=False,
            ),
            patch("orchestune.dispatch_reconciliation.save_run_state") as mock_save,
        ):
            _reconcile_recovery_counters(run_state, config)

        mock_list_prs.assert_not_called()
        mock_save.assert_not_called()

    def test_is_a_noop_when_apply_is_false(self, tmp_path):
        """dry-run（--no-apply）では、DAG再計算等の他の副作用と同様に
        再照合そのものを行わない（他のconfig.apply分岐と同じ流儀）。"""
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=False,
        )
        run_state = RunState(active_worktrees={})

        with (
            patch(
                "orchestune.forge.GitHubForge.list_issues_by_label"
            ) as mock_list_issues,
            patch(
                "orchestune.dispatch_reconciliation._reconcile_stale_recovery_counters"
            ) as mock_reconcile,
            patch("orchestune.dispatch_reconciliation.save_run_state") as mock_save,
        ):
            _reconcile_recovery_counters(run_state, config)

        mock_list_issues.assert_not_called()
        mock_reconcile.assert_not_called()
        mock_save.assert_not_called()

    def test_reconciles_repository_wide_regardless_of_parent_scope(self, tmp_path):
        """再現テスト（#516再4巡目）: --parent-issue指定時、`_fetch_issues`は
        parent-scopedなfast pathを使うため、その結果をそのまま渡すと他の親
        Issue配下のactive worktreeが再照合対象から漏れる。`config.parent_issue_number`
        にこのactive worktreeとは無関係な親（999）を設定していても、
        リポジトリ全体を独自に読み直すことで正しく再照合されなければ
        ならない。"""
        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}", encoding="utf-8")
        active = ActiveWorktree(
            issue_number=101,
            branch="claude/issue-101-task-a",
            worktree_path="worktrees/w1",
            pid=None,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            recompute_count=2,
            forced_serial=False,
        )
        run_state = RunState(active_worktrees={"101": active})
        issue = IssueRecord(
            number=101,
            title="t",
            body=(
                "```yaml\nsubtask_id: task-a\nrecompute_count: 2\n"
                "forced_serial: true\n```\n"
            ),
            labels=(),
            created_at="2026-01-01T00:00:00+00:00",
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
            parent_issue_number=999,  # 別の親（B）を対象にディスパッチ中
        )

        with (
            patch(
                "orchestune.forge.GitHubForge.list_issues_by_label",
                return_value=[issue],
            ),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
        ):
            _reconcile_recovery_counters(run_state, config)

        assert run_state.active_worktrees["101"].forced_serial is True


class TestRestoreLaunchHistory:
    """#514: run_state.json消失時、親Issue本文からlaunch_historyを復元する。

    `--parent-issue` 未指定（フラットモード）は対象外（永続化先の親Issueが
    存在しないため、Issue #514 のスコープ決定に従う）。
    """

    def _config(self, tmp_path, **overrides):
        defaults = dict(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            parent_issue_number=100,
            window_seconds=3600,
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def _parent_issue(self, timestamps):
        entries = "\n".join(f"- {t}" for t in timestamps)
        return IssueRecord(
            number=100,
            title="[EPIC] t",
            body=(
                "EPIC\n\n<!-- orchestune:launch-history -->\n"
                f"```yaml\nlaunch_history:\n{entries}\n```\n"
            ),
            labels=(),
            created_at="2026-01-01T00:00:00+00:00",
        )

    def test_restores_in_window_timestamps_when_run_state_is_empty(self, tmp_path):
        """再現テスト: run_state.jsonが消えてlaunch_historyが空でも、
        親Issue本文のウィンドウ内タイムスタンプが復元されること。"""
        now = 10_000.0
        run_state = RunState(active_worktrees={}, launch_history=[])
        config = self._config(tmp_path)
        issue = self._parent_issue([now - 60, now - 120])

        with patch("orchestune.forge.GitHubForge.get_issue", return_value=issue):
            changed = _restore_launch_history(run_state, config, now=now)

        assert changed is True
        assert sorted(run_state.launch_history) == [now - 120, now - 60]

    def test_drops_timestamps_outside_the_window(self, tmp_path):
        """`prune_run_state`と同じ意味論で、ウィンドウ外は復元しない。"""
        now = 10_000.0
        run_state = RunState(active_worktrees={}, launch_history=[])
        config = self._config(tmp_path)
        issue = self._parent_issue([now - 60, now - 7200])

        with patch("orchestune.forge.GitHubForge.get_issue", return_value=issue):
            _restore_launch_history(run_state, config, now=now)

        assert run_state.launch_history == [now - 60]

    def test_does_not_shrink_an_existing_local_history(self, tmp_path):
        """本文側が古い場合に、ローカルの進捗（より多くの起動）を巻き戻さない。"""
        now = 10_000.0
        run_state = RunState(active_worktrees={}, launch_history=[now - 30, now - 60])
        config = self._config(tmp_path)
        issue = self._parent_issue([now - 60])

        with patch("orchestune.forge.GitHubForge.get_issue", return_value=issue):
            _restore_launch_history(run_state, config, now=now)

        assert sorted(run_state.launch_history) == [now - 60, now - 30]

    def test_preserves_duplicate_timestamps_as_a_multiset(self, tmp_path):
        """#519レビュー指摘(P1): 同一サイクルで複数タスクが起動すると、
        いずれもサイクル共通の`now`をappendするため、重複タイムスタンプは
        「別々の起動」を表す正当なデータ。集合和で畳むと起動数を過少に
        数え、上限に余剰スロットが生まれてしまう。多重集合として
        （各値の出現回数の最大を採って）マージすること。"""
        now = 10_000.0
        run_state = RunState(active_worktrees={}, launch_history=[])
        config = self._config(tmp_path)
        issue = self._parent_issue([now - 60, now - 60])

        with patch("orchestune.forge.GitHubForge.get_issue", return_value=issue):
            _restore_launch_history(run_state, config, now=now)

        assert run_state.launch_history == [now - 60, now - 60]

    def test_takes_the_larger_occurrence_count_from_either_side(self, tmp_path):
        """ローカルに2件・本文に1件なら2件（巻き戻さない）。逆なら本文の2件。"""
        now = 10_000.0
        run_state = RunState(active_worktrees={}, launch_history=[now - 60, now - 60])
        config = self._config(tmp_path)
        issue = self._parent_issue([now - 60])

        with patch("orchestune.forge.GitHubForge.get_issue", return_value=issue):
            _restore_launch_history(run_state, config, now=now)

        assert run_state.launch_history == [now - 60, now - 60]

    def test_restoration_feeds_the_global_quota_counter(self, tmp_path):
        """#519レビュー指摘(P2)の明文化: 実行時のクオータ判定
        （`quota_available`）は`run_state.launch_history`を**グローバルに**
        数える既存挙動のままで、本PRはそれを変更しない。永続化ストアだけが
        親ごと（親Issue本文が唯一の置き場所のため）。

        したがって永続ディスクで複数の親を運用する場合、親Bの復元分は親Aの
        ローカル履歴と合算されて数えられる。マージは和集合の最大回数を採る
        片方向なので、上限は緩む方向へは壊れない（安全側）。
        """
        from orchestune.dispatch_scoring import quota_available

        now = 10_000.0
        parent_a_launch = now - 30.0
        run_state = RunState(active_worktrees={}, launch_history=[parent_a_launch])
        config = self._config(tmp_path)
        issue = self._parent_issue([now - 60])  # 親Bの永続履歴

        with patch("orchestune.forge.GitHubForge.get_issue", return_value=issue):
            _restore_launch_history(run_state, config, now=now)

        # 親Aのローカル分と親Bの復元分が両方残る（片方向マージ）
        assert run_state.launch_history == [now - 60, parent_a_launch]
        # そしてクオータ判定はその全体を数える（＝グローバル意味論）
        assert (
            quota_available(
                run_state,
                now,
                max_concurrent=5,
                max_launches_per_window=2,
                window_seconds=3600,
            )
            == 0
        )

    def test_is_a_noop_in_flat_mode(self, tmp_path):
        """#514スコープ決定: --parent-issue未指定では永続化・復元とも行わない。"""
        run_state = RunState(active_worktrees={}, launch_history=[])
        config = self._config(tmp_path, parent_issue_number=None)

        with patch("orchestune.forge.GitHubForge.get_issue") as mock_get:
            changed = _restore_launch_history(run_state, config, now=10_000.0)

        mock_get.assert_not_called()
        assert changed is False

    def test_restores_in_memory_even_when_apply_is_false(self, tmp_path):
        """#519レビュー7巡目(P2): dry-runは「適用したら何が起きるか」の
        previewなので、永続履歴を無視してはならない。

        ステートレスなランナーで`--no-apply`を実行すると、以前は本文の
        読み取りごとスキップされ、ローカル履歴が空のまま
        `quota_available`が「起動する」と表示していた。直後に実適用すると
        復元が効いて1件も起動しない、という食い違いになる。本文の読み取り
        自体に副作用は無いため、復元はメモリ上で両モードとも行う
        （run_stateへの書き戻しだけがapply限定——`TestSelfHealLaunchHistory`）。
        """
        now = 10_000.0
        run_state = RunState(active_worktrees={}, launch_history=[])
        config = self._config(tmp_path, apply=False)
        issue = self._parent_issue([now - 60])

        with patch("orchestune.forge.GitHubForge.get_issue", return_value=issue):
            changed = _restore_launch_history(run_state, config, now=now)

        assert changed is True
        assert run_state.launch_history == [now - 60]

    def test_keeps_a_slightly_future_timestamp_unchanged(self, tmp_path):
        """#519レビュー7巡目(P2): ランナー間の軽微なクロックずれで数秒未来に
        書かれた正当な記録は捨てない（捨てると起動数の過少カウント＝上限を
        緩める危険側へ倒れる）。"""
        now = 10_000.0
        run_state = RunState(active_worktrees={}, launch_history=[])
        config = self._config(tmp_path)
        issue = self._parent_issue([now + 5, now - 60])

        with patch("orchestune.forge.GitHubForge.get_issue", return_value=issue):
            _restore_launch_history(run_state, config, now=now)

        assert run_state.launch_history == [now - 60, now + 5]

    def test_repeated_restoration_does_not_manufacture_entries(self, tmp_path):
        """#519レビュー8巡目(P2)の再現テスト: 未来のタイムスタンプを`now`へ
        クランプして正規化すると、クランプ先が毎サイクル動くため、同じ1回の
        起動が別々のエントリとして増え続ける。

        このマージはタイムスタンプ値を同一性のキーにした多重集合なので、
        本文の`1005`が`now=1000`→`[1000]`、`now=1001`→`[1000, 1001]`、
        `now=1002`→`[1000, 1001, 1002]`と増殖し、1回の起動が複数スロットを
        消費してしまう（`max_launches_per_window > 1`のとき顕在化）。
        正規化で値を動かさないこと。
        """
        launched_at = 10_005.0
        run_state = RunState(active_worktrees={}, launch_history=[])
        config = self._config(tmp_path)
        issue = self._parent_issue([launched_at])

        for now in (10_000.0, 10_001.0, 10_002.0, 10_010.0):
            with patch("orchestune.forge.GitHubForge.get_issue", return_value=issue):
                _restore_launch_history(run_state, config, now=now)
            assert run_state.launch_history == [launched_at]

    def test_discards_an_implausibly_future_timestamp(self, tmp_path):
        """#519レビュー7巡目(P2): 本文は人間が編集できるため、有限だが遥か
        未来のタイムスタンプが入りうる。`math.isfinite`は`.inf`しか弾けず、
        `999999999999`はそのまま復元されて`quota_available`の
        `now - t < window_seconds`が何年も真であり続ける。

        1ウィンドウより先は過去の起動の記録としてあり得ないため破棄する。
        クランプで済ませると毎サイクル新しい`now`へクランプし直され、
        `.inf`と同じ「永久に1スロットを食い潰す」症状が有限値で再現する。
        """
        now = 10_000.0
        run_state = RunState(active_worktrees={}, launch_history=[])
        config = self._config(tmp_path)
        issue = self._parent_issue([999_999_999_999.0, now - 60])

        with patch("orchestune.forge.GitHubForge.get_issue", return_value=issue):
            _restore_launch_history(run_state, config, now=now)

        assert run_state.launch_history == [now - 60]

    def test_an_implausible_timestamp_does_not_block_dispatch_forever(self, tmp_path):
        """破棄の要点: 本文を修正しなくても、次サイクル以降ディスパッチが
        止まらないこと（クランプのみだと毎サイクル復活してしまう）。"""
        from orchestune.dispatch_scoring import quota_available

        run_state = RunState(active_worktrees={}, launch_history=[])
        config = self._config(tmp_path)
        issue = self._parent_issue([999_999_999_999.0])

        for now in (10_000.0, 20_000.0):
            run_state.launch_history = []
            with patch("orchestune.forge.GitHubForge.get_issue", return_value=issue):
                _restore_launch_history(run_state, config, now=now)
            assert run_state.launch_history == []
            assert (
                quota_available(
                    run_state,
                    now,
                    max_concurrent=5,
                    max_launches_per_window=1,
                    window_seconds=3600,
                )
                == 1
            )

    def test_returns_false_when_parent_issue_has_no_block(self, tmp_path):
        """本フィールド導入前の親Issue（ブロック欠落）への後方互換。"""
        run_state = RunState(active_worktrees={}, launch_history=[])
        config = self._config(tmp_path)
        issue = IssueRecord(
            number=100,
            title="[EPIC] t",
            body="EPIC only",
            labels=(),
            created_at="2026-01-01T00:00:00+00:00",
        )

        with patch("orchestune.forge.GitHubForge.get_issue", return_value=issue):
            changed = _restore_launch_history(run_state, config, now=10_000.0)

        assert changed is False
        assert run_state.launch_history == []


class TestSelfHealLaunchHistory:
    """#514: 復元を生産コードの配線から呼び出す層。

    PR #516 の3巡目レビューで学んだ通り、`_self_heal_run_state`は
    `run_state.json`欠落時にしか動作しないため、そこに相乗りさせると
    「ファイルは存在するがlaunch_historyだけ古い」ケースへ到達できない。
    ファイル有無を問わず毎サイクル呼び出す。
    """

    def test_persists_when_restoration_reports_a_change(self, tmp_path):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            parent_issue_number=100,
        )
        run_state = RunState(active_worktrees={})
        open_prs = [MagicMock()]

        with (
            patch(
                "orchestune.dispatch_reconciliation._restore_launch_history",
                return_value=True,
            ),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=open_prs),
            patch("orchestune.dispatch_reconciliation.save_run_state") as mock_save,
        ):
            _self_heal_launch_history(run_state, config, now=1000.0)

        mock_save.assert_called_once_with(
            run_state,
            config.run_state_path,
            launch_window_seconds=config.window_seconds,
            open_prs=open_prs,
        )

    def test_does_not_save_when_nothing_restored(self, tmp_path):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            parent_issue_number=100,
        )
        run_state = RunState(active_worktrees={})

        with (
            patch(
                "orchestune.dispatch_reconciliation._restore_launch_history",
                return_value=False,
            ),
            patch("orchestune.forge.GitHubForge.list_open_prs") as mock_list_prs,
            patch("orchestune.dispatch_reconciliation.save_run_state") as mock_save,
        ):
            _self_heal_launch_history(run_state, config, now=1000.0)

        mock_list_prs.assert_not_called()
        mock_save.assert_not_called()

    def test_restores_even_though_run_state_file_exists(self, tmp_path):
        """再現テスト: run_state.jsonが存在する通常サイクルでも復元されること
        （`_self_heal_run_state`のファイル有無ゲートに影響されない）。"""
        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}", encoding="utf-8")
        now = 10_000.0
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
            parent_issue_number=100,
            window_seconds=3600,
        )
        run_state = RunState(active_worktrees={}, launch_history=[])
        issue = IssueRecord(
            number=100,
            title="[EPIC] t",
            body=(
                "EPIC\n\n<!-- orchestune:launch-history -->\n"
                f"```yaml\nlaunch_history:\n- {now - 60}\n```\n"
            ),
            labels=(),
            created_at="2026-01-01T00:00:00+00:00",
        )

        with (
            patch("orchestune.forge.GitHubForge.get_issue", return_value=issue),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.dispatch_reconciliation.save_run_state"),
        ):
            _self_heal_launch_history(run_state, config, now=now)

        assert run_state.launch_history == [now - 60]


class TestBaseBranchRedRecovery:
    def test_decide_requeue_when_base_sha_advances_and_no_pending_deps(self):
        issue = _issue(1, labels=("status:blocked", "ci:base-branch-red"))
        task = _task(issue_number=1, subtask_id="task-a", depends_on=())
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="1111111111111111111111111111111111111111",
            attempt=1,
        )
        decisions = _decide_base_branch_red_recovery(
            base_branch_red_issues=[issue],
            tasks_by_issue={1: task},
            done_subtask_ids=set(),
            current_base_shas={1: "2222222222222222222222222222222222222222"},
            outcomes_by_issue={1: outcome},
        )
        assert len(decisions) == 1
        assert decisions[0].action == "requeue"
        assert decisions[0].issue_number == 1
        assert decisions[0].subtask_id == "task-a"

    def test_decide_unmark_only_when_base_sha_advances_but_has_pending_deps(self):
        issue = _issue(1, labels=("status:blocked", "ci:base-branch-red"))
        task = _task(issue_number=1, subtask_id="task-a", depends_on=("task-dep",))
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="1111111111111111111111111111111111111111",
            attempt=1,
        )
        decisions = _decide_base_branch_red_recovery(
            base_branch_red_issues=[issue],
            tasks_by_issue={1: task},
            done_subtask_ids=set(),  # task-dep is not done
            current_base_shas={1: "2222222222222222222222222222222222222222"},
            outcomes_by_issue={1: outcome},
        )
        assert len(decisions) == 1
        assert decisions[0].action == "unmark_only"

    def test_decide_no_recovery_when_base_sha_has_not_advanced(self):
        issue = _issue(1, labels=("status:blocked", "ci:base-branch-red"))
        task = _task(issue_number=1, subtask_id="task-a", depends_on=())
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="1111111111111111111111111111111111111111",
            attempt=1,
        )
        decisions = _decide_base_branch_red_recovery(
            base_branch_red_issues=[issue],
            tasks_by_issue={1: task},
            done_subtask_ids=set(),
            current_base_shas={1: "1111111111111111111111111111111111111111"},
            outcomes_by_issue={1: outcome},
        )
        assert decisions == []

    def test_decide_escalate_when_attempt_3(self):
        issue = _issue(1, labels=("status:blocked", "ci:base-branch-red"))
        task = _task(issue_number=1, subtask_id="task-a", depends_on=())
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="1111111111111111111111111111111111111111",
            attempt=3,
        )
        decisions = _decide_base_branch_red_recovery(
            base_branch_red_issues=[issue],
            tasks_by_issue={1: task},
            done_subtask_ids=set(),
            current_base_shas={1: "2222222222222222222222222222222222222222"},
            outcomes_by_issue={1: outcome},
        )
        assert len(decisions) == 1
        assert decisions[0].action == "escalate"
        assert decisions[0].attempt == 3

    def test_apply_requeue_removes_marker_and_transitions_to_queued(self, tmp_path):
        decision = BaseBranchRedRecoveryDecision(
            issue_number=1,
            subtask_id="task-a",
            action="requeue",
            recorded_base_sha="1111111",
            current_base_sha="2222222",
            attempt=1,
        )
        fake_forge = MagicMock()
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
        )
        events = _apply_base_branch_red_recovery([decision], config)
        assert events == [{"issue_number": 1, "subtask_id": "task-a"}]
        fake_forge.remove_label.assert_any_call(1, "ci:base-branch-red")
        fake_forge.add_label.assert_called_once_with(1, "status:queued")
        fake_forge.add_comment.assert_called_once()

    def test_apply_escalate_transitions_to_blocked_human_review(self, tmp_path):
        decision = BaseBranchRedRecoveryDecision(
            issue_number=1,
            subtask_id="task-a",
            action="escalate",
            attempt=3,
        )
        fake_forge = MagicMock()
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
        )
        events = _apply_base_branch_red_recovery([decision], config)
        assert events == []
        fake_forge.add_label.assert_called_once_with(1, "status:blocked-human-review")
        fake_forge.remove_label.assert_any_call(1, "ci:base-branch-red")

    def test_handle_base_branch_red_recovery_empty_when_no_matching_issues(
        self, tmp_path
    ):
        issues_mock = MagicMock()
        issues_mock.all.return_value = [_issue(1, labels=("status:blocked",))]
        ctx = MagicMock()
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
        )
        events = _handle_base_branch_red_recovery(issues_mock, ctx, set(), config)
        assert events == []

    def test_handle_base_branch_red_recovery_success(self, tmp_path):
        issue = _issue(1, labels=("status:blocked", "ci:base-branch-red"))
        issues_mock = MagicMock()
        issues_mock.all.return_value = [issue]
        task = _task(issue_number=1, subtask_id="task-a", depends_on=())
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="1111111111111111111111111111111111111111",
            attempt=1,
        )
        fake_forge = MagicMock()
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:10Z"}
        ]
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
        )
        ctx = MagicMock()
        ctx.tasks_by_issue = {1: task}
        ctx.done_subtask_ids = set()
        ctx.subtask_branch_map = {}

        with patch(
            "orchestune.dispatch_reconciliation._get_branch_commit_sha",
            return_value="2222222222222222222222222222222222222222",
        ):
            events = _handle_base_branch_red_recovery(issues_mock, ctx, set(), config)

        assert events == [{"issue_number": 1, "subtask_id": "task-a"}]
        fake_forge.remove_label.assert_any_call(1, "ci:base-branch-red")
        fake_forge.add_label.assert_called_once_with(1, "status:queued")


class TestResolveBaseBranchForTask:
    def test_when_sole_dependency_is_done_returns_origin_main(self, tmp_path):
        task = _task(issue_number=2, subtask_id="task-b", depends_on=("task-a",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            parent_issue_number=None,
        )
        subtask_branch_map = {"task-a": "claude/issue-1-task-a"}
        done_subtask_ids = {"task-a"}

        base_branch = _resolve_base_branch_for_task(
            task, config, subtask_branch_map, done_subtask_ids
        )
        assert base_branch == "origin/main"

    def test_when_sole_dependency_is_done_with_parent_returns_parent_branch(
        self, tmp_path
    ):
        task = _task(issue_number=2, subtask_id="task-b", depends_on=("task-a",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            parent_issue_number=100,
        )
        subtask_branch_map = {"task-a": "claude/issue-1-task-a"}
        done_subtask_ids = {"task-a"}

        base_branch = _resolve_base_branch_for_task(
            task, config, subtask_branch_map, done_subtask_ids
        )
        assert base_branch == "parent/issue-100"

    def test_when_single_dependency_unresolved_returns_dep_branch(self, tmp_path):
        task = _task(issue_number=2, subtask_id="task-b", depends_on=("task-a",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            parent_issue_number=100,
        )
        subtask_branch_map = {"task-a": "claude/issue-1-task-a"}
        done_subtask_ids = set()

        base_branch = _resolve_base_branch_for_task(
            task, config, subtask_branch_map, done_subtask_ids
        )
        assert base_branch == "claude/issue-1-task-a"

    def test_when_multiple_dependencies_unresolved_returns_parent_or_main(
        self, tmp_path
    ):
        task = _task(
            issue_number=3,
            subtask_id="task-c",
            depends_on=("task-a", "task-b"),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            parent_issue_number=100,
        )
        subtask_branch_map = {
            "task-a": "claude/issue-1-task-a",
            "task-b": "claude/issue-2-task-b",
        }
        done_subtask_ids = set()

        base_branch = _resolve_base_branch_for_task(
            task, config, subtask_branch_map, done_subtask_ids
        )
        assert base_branch == "parent/issue-100"
