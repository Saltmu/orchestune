import json

import pytest

from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.run_state import (
    CompletedWorktree,
    RunState,
    load_run_state,
    prune_run_state,
)
from orchestune.ledger.run_state import save_run_state as save_run_state_unlocked
from tests.dispatch_test_support import flat_active_worktree
from tests.dispatch_test_support import save_locked_run_state as save_run_state


def _serialized_current_active(**overrides):
    active = {
        "issue_number": 10,
        "branch": "claude/issue-10-x",
        "worktree_path": "worktrees/claude-issue-10-x",
        "pid": 4_212_345,
        "started_at": 1700000000.0,
        "declared_footprint": [],
        "owner_kind": "dispatch",
        "claim_id": "claim-10",
        "claim_stage": "completed",
        "base_ref": "origin/main",
        "base_sha": "a" * 40,
        "reservation_kind": "footprint",
        "repository_id": "Saltmu/orchestune",
        "claimed_at": 1700000001.0,
        "owner_token_digest": "sha256:abc123",
    }
    active.update(overrides)
    return active


def _current_active_worktree(**overrides):
    fields = {
        "issue_number": 10,
        "branch": "claude/issue-10-x",
        "worktree_path": "worktrees/claude-issue-10-x",
        "pid": 4_212_345,
        "started_at": 1700000000.0,
        "declared_footprint": ("src/foo.py",),
        "owner_kind": "dispatch",
        "claim_id": "claim-10",
        "claim_stage": "completed",
        "base_ref": "origin/main",
        "base_sha": "a" * 40,
        "reservation_kind": "footprint",
        "repository_id": "Saltmu/orchestune",
        "claimed_at": 1700000001.0,
        "owner_token_digest": "sha256:abc123",
    }
    fields.update(overrides)
    return flat_active_worktree(**fields)


class TestRunState:
    def test_new_claim_ownership_fields_round_trip_without_owner_token(self, tmp_path):
        path = tmp_path / "run_state.json"
        active = flat_active_worktree(
            issue_number=10,
            branch="claim/10",
            worktree_path="worktrees/claim-10",
            pid=4_200_123,
            started_at=1.0,
            declared_footprint=(),
            owner_kind="interactive",
            claim_id="claim-10",
            claim_stage="active_saved",
            base_ref="parent/issue-893",
            base_sha="a" * 40,
            reservation_kind="repository",
            repository_id="Saltmu/orchestune",
            claimed_at=2.0,
            owner_token_digest="sha256:abc123",
        )

        with run_state_lock(path.with_suffix(".lock")):
            save_run_state_unlocked(RunState(active_worktrees={"10": active}), path)

        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert "owner_token" not in persisted["active_worktrees"]["10"]
        assert persisted["active_worktrees"]["10"]["reservation_kind"] == "repository"
        assert load_run_state(path).active_worktrees["10"] == active

    def test_load_run_state_canonicalizes_declared_footprint(self, tmp_path):
        path = tmp_path / "run_state.json"
        raw_active = _serialized_current_active(
            declared_footprint=["./src/a.py", "src/a.py", r"src\b.py"]
        )
        path.write_text(json.dumps({"active_worktrees": {"10": raw_active}}))
        loaded = load_run_state(path)
        assert loaded.active_worktrees["10"].core.declared_footprint == (
            "src/a.py",
            "src/b.py",
        )

    @pytest.mark.parametrize(
        "field,value",
        [
            ("owner_kind", "unknown-owner"),
            ("reservation_kind", "unknown-reservation"),
            ("claim_stage", "unknown-stage"),
            ("claim_id", None),
            ("base_ref", None),
            ("missing:base_sha", None),
            ("repository_id", None),
            ("claimed_at", None),
            ("owner_token_digest", None),
        ],
    )
    def test_rejects_incomplete_or_unknown_serialized_active_worktree(
        self, tmp_path, field, value
    ):
        path = tmp_path / "run_state.json"
        active = {
            "issue_number": 10,
            "branch": "claude/issue-10-x",
            "worktree_path": "worktrees/claude-issue-10-x",
            "pid": 4_200_123,
            "started_at": 1.0,
            "declared_footprint": [],
            "owner_kind": "dispatch",
            "claim_id": "claim-10",
            "claim_stage": "active_saved",
            "base_ref": "origin/main",
            "base_sha": "a" * 40,
            "reservation_kind": "footprint",
            "repository_id": "Saltmu/orchestune",
            "claimed_at": 2.0,
            "owner_token_digest": "sha256:abc123",
        }
        if field.startswith("missing:"):
            del active[field.removeprefix("missing:")]
        else:
            active[field] = value
        path.write_text(
            json.dumps({"active_worktrees": {"10": active}}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="active_worktrees\\[10\\]"):
            load_run_state(path)

    def test_rejects_wrong_type_in_serialized_active_worktree(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(
            json.dumps(
                {
                    "active_worktrees": {
                        "10": {
                            "issue_number": True,
                            "branch": "claude/issue-10-x",
                            "worktree_path": "worktrees/claude-issue-10-x",
                            "pid": 4_200_123,
                            "started_at": 1.0,
                            "declared_footprint": [],
                            "owner_kind": "dispatch",
                            "claim_id": "claim-10",
                            "claim_stage": "active_saved",
                            "base_ref": "origin/main",
                            "base_sha": "a" * 40,
                            "reservation_kind": "footprint",
                            "repository_id": "Saltmu/orchestune",
                            "claimed_at": 2.0,
                            "owner_token_digest": "sha256:abc123",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="active_worktrees\\[10\\]"):
            load_run_state(path)

    @pytest.mark.uses_run_state_lock_assertion
    def test_save_run_state_rejects_write_without_sibling_lock(self, tmp_path):
        path = tmp_path / "run_state.json"

        with pytest.raises(RuntimeError, match="run_state lock must be held"):
            save_run_state_unlocked(RunState(), path)

    def test_load_missing_file_returns_empty_state(self, tmp_path):
        state = load_run_state(tmp_path / "run_state.json")
        assert state.active_worktrees == {}
        assert state.launch_history == []

    def test_load_corrupted_file_rejects_and_quarantines(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text("{broken json", encoding="utf-8")

        with pytest.raises(ValueError, match="ledger could not be read"):
            load_run_state(path)

        assert not path.exists()
        assert list(tmp_path.glob("run_state.json.corrupt.*"))

    def test_explicit_replacement_after_corrupted_load_overwrites_cleanly(
        self, tmp_path
    ):
        path = tmp_path / "run_state.json"
        path.write_text("{broken json", encoding="utf-8")
        with pytest.raises(ValueError, match="ledger could not be read"):
            load_run_state(path)

        state = RunState(
            active_worktrees={
                "1": _current_active_worktree(
                    issue_number=1,
                    branch="claude/issue-1-x",
                    worktree_path="worktrees/claude-issue-1-x",
                    pid=4_200_001,
                    started_at=1.0,
                    declared_footprint=(),
                    claim_id="claim-1",
                )
            }
        )
        save_run_state(state, path)

        reloaded = load_run_state(path)
        assert reloaded.active_worktrees["1"].core.branch == "claude/issue-1-x"

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700000000.0
        state = RunState(
            active_worktrees={
                "10": _current_active_worktree(
                    estimated_tokens=400,
                    token_estimate_recorded=True,
                )
            },
            launch_history=[1700000000.0],
        )
        save_run_state(state, path, now=now)
        loaded = load_run_state(path)
        assert loaded.active_worktrees["10"].core.branch == "claude/issue-10-x"
        assert loaded.active_worktrees["10"].launch.estimated_tokens == 400
        assert loaded.active_worktrees["10"].launch.token_estimate_recorded is True
        assert loaded.launch_history == [1700000000.0]

    def test_current_active_worktree_without_token_estimate_loads_compatibly(
        self, tmp_path
    ):
        path = tmp_path / "run_state.json"
        path.write_text(
            json.dumps(
                {
                    "active_worktrees": {"10": _serialized_current_active()},
                    "launch_history": [],
                }
            ),
            encoding="utf-8",
        )

        active = load_run_state(path).active_worktrees["10"]
        assert active.launch.estimated_tokens is None
        assert active.launch.token_estimate_recorded is False

        save_run_state(RunState(active_worktrees={"10": active}), path)
        assert (
            load_run_state(path).active_worktrees["10"].launch.token_estimate_recorded
            is False
        )

    def test_save_and_load_roundtrip_with_execution_profile_fields(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700000000.0
        state = RunState(
            active_worktrees={
                "10": _current_active_worktree(
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
        assert active.launch.profile == "deep"
        assert active.launch.model == "claude-3-7-sonnet-20250219"
        assert active.launch.reasoning_effort == "high"
        assert (
            active.launch.selection_reason
            == "profile 'deep' resolved for target 'claude-cli'"
        )

        completed = loaded.completed_worktrees[0]
        assert completed.profile == "balanced"
        assert completed.model == "gemini-2.5-pro"
        assert completed.reasoning_effort == "medium"
        assert completed.selection_reason == "default profile 'balanced' applied"

    def test_launch_attribution_round_trips_and_legacy_records_load_as_unknown(
        self, tmp_path
    ):
        path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={
                "10": _current_active_worktree(
                    launch_target="claude-cli",
                    launch_log_path="logs/claude-issue-10-x.log",
                    launch_log_offset=2048,
                ),
                "11": _current_active_worktree(
                    issue_number=11, branch="claude/issue-11-y"
                ),
            }
        )
        save_run_state(state, path, now=1700000000.0)
        raw = json.loads(path.read_text(encoding="utf-8"))["active_worktrees"]
        assert raw["10"]["launch_log_offset"] == 2048
        # Absent attribution stays absent in the JSON (legacy-identical encoding).
        assert "launch_target" not in raw["11"]

        loaded = load_run_state(path)
        attributed = loaded.active_worktrees["10"].launch
        assert (attributed.launch_target, attributed.launch_log_offset) == (
            "claude-cli",
            2048,
        )
        legacy = loaded.active_worktrees["11"].launch
        assert (
            legacy.launch_target,
            legacy.launch_log_path,
            legacy.launch_log_offset,
        ) == (
            None,
            None,
            None,
        )

    def test_old_data_without_execution_profile_fields_loads_as_none(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(
            json.dumps(
                {
                    "active_worktrees": {"10": _serialized_current_active()},
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
        assert active.launch.profile is None
        assert active.launch.model is None
        assert active.launch.reasoning_effort is None
        assert active.launch.selection_reason is None

        completed = loaded.completed_worktrees[0]
        assert completed.profile is None
        assert completed.model is None
        assert completed.reasoning_effort is None
        assert completed.selection_reason is None

    def test_completion_journal_fields_default_to_none_and_false(self, tmp_path):
        path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={"10": _current_active_worktree()})

        save_run_state(state, path)
        active = load_run_state(path).active_worktrees["10"]

        assert active.completion.completion_id is None
        assert active.completion.completion_result is None
        assert active.completion.completion_stage is None
        assert active.completion.completion_payload is None
        assert active.completion.completion_comment_id is None
        assert active.completion.completion_comment_url is None
        assert active.completion.completion_handoff_ready is False

    def test_save_and_load_roundtrip_with_completion_journal_fields(self, tmp_path):
        path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={
                "10": _current_active_worktree(
                    completion_id="completion-abc123",
                    completion_result="done",
                    completion_stage="handed_off_to_gc",
                    completion_payload={"pr": 42, "review": {"bot": "codex"}},
                    completion_comment_id="999",
                    completion_comment_url=(
                        "https://github.com/Saltmu/orchestune/issues/10"
                        "#issuecomment-999"
                    ),
                    completion_handoff_ready=True,
                )
            }
        )

        save_run_state(state, path)
        active = load_run_state(path).active_worktrees["10"]

        assert active.completion.completion_id == "completion-abc123"
        assert active.completion.completion_result == "done"
        assert active.completion.completion_stage == "handed_off_to_gc"
        assert active.completion.completion_payload == {
            "pr": 42,
            "review": {"bot": "codex"},
        }
        assert active.completion.completion_comment_id == "999"
        assert active.completion.completion_comment_url.endswith("999")
        assert active.completion.completion_handoff_ready is True

    def test_old_data_without_completion_journal_fields_loads_compatibly(
        self, tmp_path
    ):
        path = tmp_path / "run_state.json"
        path.write_text(
            json.dumps(
                {
                    "active_worktrees": {"10": _serialized_current_active()},
                    "launch_history": [],
                }
            ),
            encoding="utf-8",
        )

        active = load_run_state(path).active_worktrees["10"]
        assert active.completion.completion_id is None
        assert active.completion.completion_handoff_ready is False

        save_run_state(RunState(active_worktrees={"10": active}), path)
        assert (
            load_run_state(path)
            .active_worktrees["10"]
            .completion.completion_handoff_ready
            is False
        )

    @pytest.mark.parametrize(
        "field,value",
        [
            ("completion_id", 123),
            ("completion_result", []),
            ("completion_stage", 4.2),
            ("completion_payload", ["not", "a", "dict"]),
            ("completion_comment_id", True),
            ("completion_comment_url", {}),
            ("completion_handoff_ready", "yes"),
        ],
    )
    def test_rejects_wrong_type_in_completion_journal_fields(
        self, tmp_path, field, value
    ):
        path = tmp_path / "run_state.json"
        active = _serialized_current_active(**{field: value})
        path.write_text(
            json.dumps({"active_worktrees": {"10": active}}), encoding="utf-8"
        )

        with pytest.raises(ValueError, match="active_worktrees\\[10\\]"):
            load_run_state(path)

    def test_save_and_load_roundtrip_with_unknown_active_start_time(self, tmp_path):
        path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={
                "10": _current_active_worktree(
                    pid=None,
                    started_at=None,
                )
            }
        )

        save_run_state(state, path)

        assert load_run_state(path).active_worktrees["10"].launch.started_at is None

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

    def test_rejects_legacy_active_worktree_even_when_completed_history_is_valid(
        self, tmp_path
    ):
        path = tmp_path / "run_state.json"
        old_data = {
            "active_worktrees": {
                "10": {
                    "issue_number": 10,
                    "branch": "claude/issue-10-x",
                    "worktree_path": "worktrees/claude-issue-10-x",
                    "pid": 4_212_345,
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
        with pytest.raises(ValueError, match="missing required fields"):
            load_run_state(path)
