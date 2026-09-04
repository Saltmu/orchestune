import json

from orchestune.dispatch.state import (
    MAX_PENDING_LOCK_RELEASE_NOTICES,
    ActiveWorktree,
    CompletedWorktree,
    RunState,
    TaskReclaimRecord,
    load_run_state,
    prune_run_state,
    save_run_state,
)


class TestRunState:
    def test_load_missing_file_returns_empty_state(self, tmp_path):
        state = load_run_state(tmp_path / "run_state.json")
        assert state.active_worktrees == {}
        assert state.launch_history == []

    def test_load_corrupted_file_falls_back_to_empty_state_and_quarantines(
        self, tmp_path
    ):
        path = tmp_path / "run_state.json"
        path.write_text("{broken json", encoding="utf-8")

        state = load_run_state(path)

        assert state.active_worktrees == {}
        assert state.launch_history == []
        assert state.completed_worktrees == []
        # 破損ファイルは元の場所から退避され、パスは"未存在"扱いになる
        # (typed recovery bookkeepingがForgeの正本から復元できるように)。
        assert not path.exists()
        assert list(tmp_path.glob("run_state.json.corrupt.*"))

    def test_save_after_corrupted_load_overwrites_cleanly(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text("{broken json", encoding="utf-8")
        state = load_run_state(path)

        state.active_worktrees["1"] = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-x",
            worktree_path="worktrees/claude-issue-1-x",
            pid=1,
            started_at=1.0,
            declared_footprint=(),
        )
        save_run_state(state, path)

        reloaded = load_run_state(path)
        assert reloaded.active_worktrees["1"].branch == "claude/issue-1-x"

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700000000.0
        state = RunState(
            active_worktrees={
                "10": ActiveWorktree(
                    issue_number=10,
                    branch="claude/issue-10-x",
                    worktree_path="worktrees/claude-issue-10-x",
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=("src/foo.py",),
                    estimated_tokens=400,
                    token_estimate_recorded=True,
                )
            },
            launch_history=[1700000000.0],
        )
        save_run_state(state, path, now=now)
        loaded = load_run_state(path)
        assert loaded.active_worktrees["10"].branch == "claude/issue-10-x"
        assert loaded.active_worktrees["10"].estimated_tokens == 400
        assert loaded.active_worktrees["10"].token_estimate_recorded is True
        assert loaded.launch_history == [1700000000.0]

    def test_old_active_worktree_without_token_estimate_loads_compatibly(
        self, tmp_path
    ):
        path = tmp_path / "run_state.json"
        path.write_text(
            json.dumps(
                {
                    "active_worktrees": {
                        "10": {
                            "issue_number": 10,
                            "branch": "claude/issue-10-x",
                            "worktree_path": "worktrees/claude-issue-10-x",
                            "pid": 12345,
                            "started_at": 1700000000.0,
                            "declared_footprint": [],
                        }
                    },
                    "launch_history": [],
                }
            ),
            encoding="utf-8",
        )

        active = load_run_state(path).active_worktrees["10"]
        assert active.estimated_tokens is None
        assert active.token_estimate_recorded is False

        save_run_state(RunState(active_worktrees={"10": active}), path)
        assert (
            load_run_state(path).active_worktrees["10"].token_estimate_recorded is False
        )

    def test_save_and_load_roundtrip_with_execution_profile_fields(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700000000.0
        state = RunState(
            active_worktrees={
                "10": ActiveWorktree(
                    issue_number=10,
                    branch="claude/issue-10-x",
                    worktree_path="worktrees/claude-issue-10-x",
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=("src/foo.py",),
                    profile="deep",
                    model="claude-3-7-sonnet-20250219",
                    reasoning_effort="high",
                    selection_reason="profile 'deep' resolved for target 'claude-cli'",
                )
            },
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=11,
                    subtask_id="task-b",
                    branch="claude/issue-11-task-b",
                    started_at=1700000000.0,
                    completed_at=1700003600.0,
                    profile="balanced",
                    model="gemini-2.5-pro",
                    reasoning_effort="medium",
                    selection_reason="default profile 'balanced' applied",
                )
            ],
            launch_history=[1700000000.0],
        )
        save_run_state(state, path, now=now)
        loaded = load_run_state(path)
        active = loaded.active_worktrees["10"]
        assert active.profile == "deep"
        assert active.model == "claude-3-7-sonnet-20250219"
        assert active.reasoning_effort == "high"
        assert (
            active.selection_reason == "profile 'deep' resolved for target 'claude-cli'"
        )

        completed = loaded.completed_worktrees[0]
        assert completed.profile == "balanced"
        assert completed.model == "gemini-2.5-pro"
        assert completed.reasoning_effort == "medium"
        assert completed.selection_reason == "default profile 'balanced' applied"

    def test_old_data_without_execution_profile_fields_loads_as_none(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(
            json.dumps(
                {
                    "active_worktrees": {
                        "10": {
                            "issue_number": 10,
                            "branch": "claude/issue-10-x",
                            "worktree_path": "worktrees/claude-issue-10-x",
                            "pid": 12345,
                            "started_at": 1700000000.0,
                            "declared_footprint": [],
                        }
                    },
                    "completed_worktrees": [
                        {
                            "issue_number": 11,
                            "subtask_id": "task-b",
                            "branch": "claude/issue-11-task-b",
                            "started_at": 1700000000.0,
                            "completed_at": 1700003600.0,
                        }
                    ],
                    "launch_history": [],
                }
            ),
            encoding="utf-8",
        )

        loaded = load_run_state(path)
        active = loaded.active_worktrees["10"]
        assert active.profile is None
        assert active.model is None
        assert active.reasoning_effort is None
        assert active.selection_reason is None

        completed = loaded.completed_worktrees[0]
        assert completed.profile is None
        assert completed.model is None
        assert completed.reasoning_effort is None
        assert completed.selection_reason is None

    def test_save_and_load_roundtrip_with_unknown_active_start_time(self, tmp_path):
        path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={
                "10": ActiveWorktree(
                    issue_number=10,
                    branch="claude/issue-10-x",
                    worktree_path="worktrees/claude-issue-10-x",
                    pid=None,
                    started_at=None,
                    declared_footprint=("src/foo.py",),
                )
            }
        )

        save_run_state(state, path)

        assert load_run_state(path).active_worktrees["10"].started_at is None

    def test_save_and_load_roundtrip_with_completed_worktrees(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700003600.0
        state = RunState(
            active_worktrees={},
            launch_history=[],
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=11,
                    subtask_id="task-b",
                    branch="claude/issue-11-task-b",
                    started_at=1700000000.0,
                    completed_at=1700003600.0,
                    recompute_count=1,
                    forced_serial=False,
                )
            ],
        )
        save_run_state(state, path, now=now)
        loaded = load_run_state(path)
        assert loaded.completed_worktrees == state.completed_worktrees

    def test_completed_worktree_preserves_unknown_start_time(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700003600.0
        state = RunState(
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=11,
                    subtask_id="task-b",
                    branch="claude/issue-11-task-b",
                    started_at=None,
                    completed_at=1700003600.0,
                )
            ]
        )

        save_run_state(state, path, now=now)

        assert load_run_state(path).completed_worktrees[0].started_at is None

    def test_load_missing_completed_worktrees_key_defaults_to_empty(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(json.dumps({"active_worktrees": {}, "launch_history": []}))
        loaded = load_run_state(path)
        assert loaded.completed_worktrees == []

    def test_prune_run_state(self):
        from orchestune.dispatch.state import prune_run_state

        now = 5000000.0
        # launch_window = 86400 -> min_launch_time = 4913600
        # completed_retention = 30 * 86400 (2592000) -> min_completed_time = 2408000
        state = RunState(
            launch_history=[
                4000000.0,
                4920000.0,
                4990000.0,
            ],  # 4000000.0 is older than 24h
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=1,
                    subtask_id="old",
                    branch="b1",
                    started_at=1000000.0,
                    completed_at=1000000.0,  # 1000000 < 2408000 (very old > 30 days)
                ),
                CompletedWorktree(
                    issue_number=2,
                    subtask_id="recent",
                    branch="b2",
                    started_at=4900000.0,
                    completed_at=4950000.0,  # 4950000 > 2408000 (recent)
                ),
            ],
        )

        pruned = prune_run_state(
            state,
            now=now,
            launch_window_seconds=86400.0,
            completed_retention_seconds=2592000.0,
        )

        assert pruned.launch_history == [4920000.0, 4990000.0]
        # open PR のない古い Issue 1 のレコードは削除され、直近30日以内の Issue 2 のみが残る
        assert len(pruned.completed_worktrees) == 1
        assert pruned.completed_worktrees[0].subtask_id == "recent"

    def test_prune_run_state_bounded_when_many_old_issues(self):
        from orchestune.dispatch.state import prune_run_state

        now = 5000000.0
        # 30日以上前(500.0)の CompletedWorktree が 1000 個ある
        many_old_worktrees = [
            CompletedWorktree(
                issue_number=i,
                subtask_id=f"t{i}",
                branch=f"b{i}",
                started_at=100.0,
                completed_at=500.0,
            )
            for i in range(1, 1001)
        ]
        state = RunState(completed_worktrees=many_old_worktrees)

        # open PR なし、上限500
        pruned = prune_run_state(
            state,
            now=now,
            launch_window_seconds=86400.0,
            completed_retention_seconds=2592000.0,
            max_completed_worktrees=500,
        )

        # 古い履歴はすべて削除され 0 件（有界かつ無駄に保持されない）
        assert len(pruned.completed_worktrees) == 0

    def test_prune_run_state_preserves_open_pr_latest_completed_worktree(self):
        from orchestune.dispatch.state import prune_run_state
        from orchestune.models import PrRecord

        now = 5000000.0  # min_completed_time = 2408000
        state = RunState(
            completed_worktrees=[
                # Issue 10: 30日以上前だが、現在 Open PR #101 (closes #10) が存在する
                CompletedWorktree(
                    issue_number=10,
                    subtask_id="t1",
                    branch="b10",
                    started_at=100.0,
                    completed_at=500.0,
                ),
                # Issue 20: 30日以上前で、Open PR なし (closed/merged 済み)
                CompletedWorktree(
                    issue_number=20,
                    subtask_id="t2",
                    branch="b20",
                    started_at=100.0,
                    completed_at=500.0,
                ),
                # Issue 30: 30日以内の最新 (Open PR なしでも保持)
                CompletedWorktree(
                    issue_number=30,
                    subtask_id="t3",
                    branch="b30",
                    started_at=4900000.0,
                    completed_at=4950000.0,
                ),
            ],
        )

        open_prs = [
            PrRecord(
                number=101,
                head_ref="b10",
                changed_files=(),
                review_decision="",
                is_ci_passing=True,
                closes_issue_numbers=(10,),
            )
        ]

        pruned = prune_run_state(
            state,
            now=now,
            launch_window_seconds=86400.0,
            completed_retention_seconds=2592000.0,
            open_prs=open_prs,
        )

        # Issue 10 (open PRありで保護) と Issue 30 (30日以内) が残り、Issue 20 は削除される
        assert len(pruned.completed_worktrees) == 2
        issues_in_pruned = {cw.issue_number for cw in pruned.completed_worktrees}
        assert issues_in_pruned == {10, 30}

    def test_prune_run_state_preserves_old_protected_record_over_new_unprotected_history(
        self,
    ):
        from orchestune.dispatch.state import prune_run_state
        from orchestune.models import PrRecord

        now = 5000000.0  # min_completed_time = 2408000
        # 古い保護レコード (30日以上前の open PR 用)
        old_protected = CompletedWorktree(
            issue_number=10,
            subtask_id="t10",
            branch="b10",
            started_at=100.0,
            completed_at=500.0,
        )
        # 30日以内の新しい非保護レコード 500 件
        new_unprotected = [
            CompletedWorktree(
                issue_number=1000 + i,
                subtask_id=f"t{1000 + i}",
                branch=f"b{1000 + i}",
                started_at=4900000.0 + i,
                completed_at=4900000.0 + i,
            )
            for i in range(500)
        ]
        state = RunState(completed_worktrees=[old_protected] + new_unprotected)

        open_prs = [
            PrRecord(
                number=101,
                head_ref="b10",
                changed_files=(),
                review_decision="",
                is_ci_passing=True,
                closes_issue_numbers=(10,),
            )
        ]

        pruned = prune_run_state(
            state,
            now=now,
            launch_window_seconds=86400.0,
            completed_retention_seconds=2592000.0,
            open_prs=open_prs,
            max_completed_worktrees=500,
        )

        assert len(pruned.completed_worktrees) == 500
        issues = {cw.issue_number for cw in pruned.completed_worktrees}
        # 古い保護対象の Issue 10 が削られずに確実に残っていること
        assert 10 in issues

    def test_save_run_state_prunes_automatically(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 5000000.0
        state = RunState(
            launch_history=[4000000.0, 4950000.0],  # 4000000.0 is too old for 24h
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=1,
                    subtask_id="old_closed",
                    branch="b1",
                    started_at=1000.0,
                    completed_at=1000.0,  # 古い
                ),
            ],
        )

        save_run_state(state, path, now=now)
        loaded = load_run_state(path)
        assert loaded.launch_history == [4950000.0]
        assert loaded.completed_worktrees == []

    def test_last_reconciled_at_defaults_to_none(self, tmp_path):
        state = load_run_state(tmp_path / "run_state.json")
        assert state.last_reconciled_at is None

    def test_save_and_load_roundtrip_with_last_reconciled_at(self, tmp_path):
        path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={}, launch_history=[], last_reconciled_at=1700003600.0
        )
        save_run_state(state, path)
        loaded = load_run_state(path)
        assert loaded.last_reconciled_at == 1700003600.0

    def test_load_missing_last_reconciled_at_key_defaults_to_none(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(json.dumps({"active_worktrees": {}, "launch_history": []}))
        loaded = load_run_state(path)
        assert loaded.last_reconciled_at is None

    def test_load_backwards_compatibility_for_base_branch(self, tmp_path):
        path = tmp_path / "run_state.json"
        old_data = {
            "active_worktrees": {
                "10": {
                    "issue_number": 10,
                    "branch": "claude/issue-10-x",
                    "worktree_path": "worktrees/claude-issue-10-x",
                    "pid": 12345,
                    "started_at": 1700000000.0,
                    "declared_footprint": ["src/foo.py"],
                }
            },
            "launch_history": [],
            "completed_worktrees": [
                {
                    "issue_number": 11,
                    "subtask_id": "task-b",
                    "branch": "claude/issue-11-task-b",
                    "started_at": 1700000000.0,
                    "completed_at": 1700003600.0,
                }
            ],
        }
        path.write_text(json.dumps(old_data))
        loaded = load_run_state(path)
        assert loaded.active_worktrees["10"].base_branch == "origin/main"
        assert loaded.completed_worktrees[0].base_branch == "origin/main"


class TestTaskReclaimCounts:
    """#512: ゾンビ/タイムアウト回収回数の台帳の永続化と有界化。"""

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700000000.0
        state = RunState(
            task_reclaim_counts={
                280: TaskReclaimRecord(count=2, last_reclaimed_at=now),
            },
        )
        save_run_state(state, path, now=now)

        loaded = load_run_state(path)

        assert loaded.task_reclaim_counts == {
            280: TaskReclaimRecord(count=2, last_reclaimed_at=now)
        }

    def test_missing_key_defaults_to_empty_ledger(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(json.dumps({"active_worktrees": {}, "launch_history": []}))

        loaded = load_run_state(path)

        assert loaded.task_reclaim_counts == {}

    def test_pending_flag_round_trips(self, tmp_path):
        """#512: 予約中フラグ（pending）も永続化・復元される。"""
        path = tmp_path / "run_state.json"
        now = 1700000000.0
        state = RunState(
            task_reclaim_counts={
                280: TaskReclaimRecord(count=1, last_reclaimed_at=now, pending=True)
            }
        )
        save_run_state(state, path, now=now)

        loaded = load_run_state(path)

        assert loaded.task_reclaim_counts[280].pending is True

    def test_missing_pending_flag_defaults_to_false(self, tmp_path):
        """本フィールド導入前のrun_state.jsonは「予約中ではない」として扱う。"""
        path = tmp_path / "run_state.json"
        path.write_text(
            json.dumps(
                {
                    "active_worktrees": {},
                    "launch_history": [],
                    "task_reclaim_counts": {
                        "280": {"count": 1, "last_reclaimed_at": 1700000000.0}
                    },
                }
            )
        )

        loaded = load_run_state(path)

        assert loaded.task_reclaim_counts[280].pending is False

    def test_lookup_cursor_round_trips_and_defaults_to_zero(self, tmp_path):
        """#512: 走査カーソルの永続化と、欠落・壊れた値のフォールバック。"""
        path = tmp_path / "run_state.json"
        save_run_state(RunState(task_reclaim_lookup_cursor=120), path)
        assert load_run_state(path).task_reclaim_lookup_cursor == 120

        path.write_text(json.dumps({"active_worktrees": {}, "launch_history": []}))
        assert load_run_state(path).task_reclaim_lookup_cursor == 0

        path.write_text(
            json.dumps(
                {
                    "active_worktrees": {},
                    "launch_history": [],
                    "task_reclaim_lookup_cursor": -3,
                }
            )
        )
        assert load_run_state(path).task_reclaim_lookup_cursor == 0

    def test_broken_entries_are_ignored(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(
            json.dumps(
                {
                    "active_worktrees": {},
                    "launch_history": [],
                    "task_reclaim_counts": {
                        "not-an-issue-number": {"count": 5},
                        "281": {"count": -1},
                        "282": {"count": True},
                        "283": "not-a-record",
                        "284": {"count": 1, "last_reclaimed_at": "nan"},
                        "285": {"count": 1},
                        "286": {"count": 2, "last_reclaimed_at": 1700000000.0},
                    },
                }
            )
        )

        loaded = load_run_state(path)

        # 壊れた値は「まだ回収していない」（＝台帳に載せない）へ倒す。
        # 時刻だけが壊れている場合は回数を活かし、時刻を0.0へ倒す。
        assert loaded.task_reclaim_counts == {
            284: TaskReclaimRecord(count=1, last_reclaimed_at=0.0),
            285: TaskReclaimRecord(count=1, last_reclaimed_at=0.0),
            286: TaskReclaimRecord(count=2, last_reclaimed_at=1700000000.0),
        }

    def test_non_finite_timestamp_is_normalized(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(
            '{"active_worktrees": {}, "launch_history": [], '
            '"task_reclaim_counts": {"280": {"count": 1, "last_reclaimed_at": Infinity}}}'
        )

        loaded = load_run_state(path)

        assert loaded.task_reclaim_counts == {
            280: TaskReclaimRecord(count=1, last_reclaimed_at=0.0)
        }

    def test_prune_keeps_records_regardless_of_age(self):
        """PR#520レビュー5巡目対応(Codex P2): 経過時間でも刈り込まない。

        起動レートに対してバックログが大きい場合、回収から次の起動までが
        保持期間を超え得る。そこでカウンタが落ちると次の回収が1回目から
        やり直しになり、`max_task_reclaims`を素通りできてしまう。
        """
        now = 1700000000.0
        state = RunState(
            active_worktrees={
                "10": ActiveWorktree(
                    issue_number=10,
                    branch="claude/issue-10-x",
                    worktree_path="worktrees/claude-issue-10-x",
                    pid=1,
                    started_at=now,
                    declared_footprint=(),
                )
            },
            task_reclaim_counts={
                10: TaskReclaimRecord(count=1, last_reclaimed_at=now - 100 * 86400.0),
                # activeでなく、保持期間（既定30日）よりはるかに古くても残す
                11: TaskReclaimRecord(count=1, last_reclaimed_at=now - 100 * 86400.0),
                12: TaskReclaimRecord(count=1, last_reclaimed_at=now - 86400.0),
            },
        )

        pruned = prune_run_state(state, now=now)

        assert set(pruned.task_reclaim_counts) == {10, 11, 12}

    def test_prune_does_not_alias_the_original_ledger(self):
        """刈り込み結果は元のdictと別インスタンス（意図しない共有変更を避ける）。"""
        state = RunState(
            task_reclaim_counts={10: TaskReclaimRecord(count=1, last_reclaimed_at=1.0)}
        )

        pruned = prune_run_state(state, now=1700000000.0)
        pruned.task_reclaim_counts.pop(10)

        assert set(state.task_reclaim_counts) == {10}

    def test_prune_keeps_every_record_regardless_of_count(self):
        """PR#520レビュー4巡目対応(Codex P2): 件数上限で古い順に追い出さない。

        追い出すと、未完了のまま繰り返し失敗しているタスクのカウンタが次の試行の
        前に消え、毎回1回目からやり直しになって`max_task_reclaims`を素通り
        できてしまう（本Issueが塞ごうとしている終端の無い経路そのもの）。
        """
        now = 1700000000.0
        state = RunState(
            task_reclaim_counts={
                issue_number: TaskReclaimRecord(
                    count=1, last_reclaimed_at=now - issue_number
                )
                for issue_number in range(1_000)
            },
        )

        pruned = prune_run_state(state, now=now)

        assert len(pruned.task_reclaim_counts) == 1_000


class TestPendingLockReleaseNotices:
    """#787: 解除通知の再試行キューの永続化と、壊れた値への耐性。"""

    def test_round_trips_through_run_state_json(self, tmp_path):
        path = tmp_path / "run_state.json"
        save_run_state(RunState(pending_lock_release_notices=[695, 696]), path)
        assert load_run_state(path).pending_lock_release_notices == [695, 696]

    def test_missing_field_loads_as_empty(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(json.dumps({"active_worktrees": {}}), encoding="utf-8")
        assert load_run_state(path).pending_lock_release_notices == []

    def test_corrupt_entries_are_dropped(self, tmp_path):
        """通知を書き直す機会を失うだけで、ディスパッチの判断には影響しない。"""
        path = tmp_path / "run_state.json"
        path.write_text(
            json.dumps(
                {
                    "active_worktrees": {},
                    "pending_lock_release_notices": [1, "x", True, -3, 0, 1, 2],
                }
            ),
            encoding="utf-8",
        )
        assert load_run_state(path).pending_lock_release_notices == [1, 2]

    def test_queue_is_bounded(self, tmp_path):
        """クローズされたIssue等で永久に投稿できない記録が溜まっても膨らまない。"""
        path = tmp_path / "run_state.json"
        oversized = list(range(1, MAX_PENDING_LOCK_RELEASE_NOTICES + 51))
        save_run_state(RunState(pending_lock_release_notices=oversized), path)
        restored = load_run_state(path).pending_lock_release_notices
        assert len(restored) == MAX_PENDING_LOCK_RELEASE_NOTICES
        assert restored[-1] == oversized[-1]
