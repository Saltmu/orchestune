"""Reclaim counts, lock-release notices, and usage-limit ledger records."""

from __future__ import annotations

import json

from orchestune.ledger.run_state import (
    MAX_PENDING_LOCK_RELEASE_NOTICES,
    RunState,
    TaskReclaimRecord,
    load_run_state,
    prune_run_state,
)
from tests.dispatch_test_support import flat_active_worktree
from tests.dispatch_test_support import save_locked_run_state as save_run_state


class TestTaskReclaimCounts:
    """#512: ゾンビ/タイムアウト回収回数の台帳の永続化と有界化。"""

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700000000.0
        state = RunState(
            task_reclaim_counts={
                280: TaskReclaimRecord(
                    count=2,
                    last_reclaimed_at=now,
                    review_timeout_retry_count=1,
                    review_timeout_retry_at=now + 60.0,
                    review_timeout_retry_pending=True,
                ),
            },
        )
        save_run_state(state, path, now=now)

        loaded = load_run_state(path)

        assert loaded.task_reclaim_counts == {
            280: TaskReclaimRecord(
                count=2,
                last_reclaimed_at=now,
                review_timeout_retry_count=1,
                review_timeout_retry_at=now + 60.0,
                review_timeout_retry_pending=True,
            )
        }

    def test_review_timeout_retry_fields_corrupted_fallback(self, tmp_path):
        path = tmp_path / "run_state.json"
        payload = {
            "active_worktrees": {},
            "launch_history": [],
            "task_reclaim_counts": {
                "280": {
                    "count": 1,
                    "review_timeout_retry_count": "invalid",
                    "review_timeout_retry_at": "invalid",
                    "review_timeout_retry_pending": "not-a-bool",
                }
            },
        }
        path.write_text(json.dumps(payload))
        loaded = load_run_state(path)
        record = loaded.task_reclaim_counts[280]
        assert record.review_timeout_retry_count == 0
        assert record.review_timeout_retry_at == 0.0
        assert record.review_timeout_retry_pending is False

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
                "10": flat_active_worktree(
                    issue_number=10,
                    branch="claude/issue-10-x",
                    worktree_path="worktrees/claude-issue-10-x",
                    pid=4_200_001,
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


class TestUsageLimitLedger:
    """#1270: dedicated session-limit retry budget and per-target cooldown."""

    def test_usage_limit_retry_fields_round_trip(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700000000.0
        record = TaskReclaimRecord(
            count=1,
            last_reclaimed_at=now,
            usage_limit_retry_count=2,
            usage_limit_retry_at=now + 900.0,
            usage_limit_retry_pending=True,
            usage_limit_retry_run="claim-10:attempt-3",
        )
        save_run_state(RunState(task_reclaim_counts={280: record}), path, now=now)

        assert load_run_state(path).task_reclaim_counts == {280: record}

    def test_records_without_usage_limit_fields_load_with_defaults(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(
            json.dumps({"task_reclaim_counts": {"280": {"count": 1}}}), encoding="utf-8"
        )

        loaded = load_run_state(path).task_reclaim_counts[280]

        assert loaded.usage_limit_retry_count == 0
        assert loaded.usage_limit_retry_at == 0.0
        assert loaded.usage_limit_retry_pending is False
        assert loaded.usage_limit_retry_run is None

    def test_corrupted_usage_limit_fields_fall_back_without_dropping_the_record(
        self, tmp_path
    ):
        path = tmp_path / "run_state.json"
        payload = {
            "task_reclaim_counts": {
                "280": {
                    "count": 2,
                    "usage_limit_retry_count": "bad",
                    "usage_limit_retry_at": float("inf"),
                    "usage_limit_retry_pending": "yes",
                    "usage_limit_retry_run": 7,
                }
            }
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

        loaded = load_run_state(path).task_reclaim_counts[280]

        assert loaded.count == 2
        assert loaded.usage_limit_retry_count == 0
        assert loaded.usage_limit_retry_at == 0.0
        assert loaded.usage_limit_retry_pending is False
        assert loaded.usage_limit_retry_run is None

    def test_cooldowns_round_trip_and_survive_pruning(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700000000.0
        state = RunState(usage_limit_cooldowns={"claude-cli": now + 3600.0})

        save_run_state(state, path, now=now)

        assert load_run_state(path).usage_limit_cooldowns == {
            "claude-cli": now + 3600.0
        }
        assert prune_run_state(state, now=now).usage_limit_cooldowns == {
            "claude-cli": now + 3600.0
        }

    def test_empty_cooldowns_are_not_written_so_old_ledgers_stay_identical(
        self, tmp_path
    ):
        path = tmp_path / "run_state.json"
        save_run_state(RunState(), path, now=1700000000.0)

        assert "usage_limit_cooldowns" not in json.loads(path.read_text("utf-8"))
        assert load_run_state(path).usage_limit_cooldowns == {}

    def test_broken_cooldown_entries_are_ignored(self, tmp_path):
        path = tmp_path / "run_state.json"
        payload = {
            "usage_limit_cooldowns": {
                "claude-cli": 1700003600.0,
                "": 5.0,
                "codex-cli": "soon",
                "agy-cli": float("nan"),
                "local": True,
            }
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

        assert load_run_state(path).usage_limit_cooldowns == {
            "claude-cli": 1700003600.0
        }

    def test_non_object_cooldowns_load_as_empty(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(json.dumps({"usage_limit_cooldowns": [1, 2]}), encoding="utf-8")

        assert load_run_state(path).usage_limit_cooldowns == {}
