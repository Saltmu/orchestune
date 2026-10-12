"""#1306: PR freshness is decided by one L0 rule shared by targets and GC."""

import math
from unittest.mock import MagicMock

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc.completion import _local_pr_completion_status
from orchestune.dispatch.targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    DispatchHandle,
)
from orchestune.models import PrRecord
from orchestune.pr_freshness import parse_github_timestamp, should_exclude_pr
from tests.dispatch_test_support import make_test_active_worktree

START = 1_767_225_600.0  # 2026-01-01T00:00:00Z


class TestParseGithubTimestamp:
    def test_z_suffix_and_explicit_utc_offset_are_the_same_instant(self):
        assert parse_github_timestamp("2026-01-01T00:00:00Z") == START
        assert parse_github_timestamp("2026-01-01T00:00:00+00:00") == START
        assert parse_github_timestamp("2026-01-01T09:00:00+09:00") == START

    def test_fractional_seconds_are_preserved(self):
        assert parse_github_timestamp("2026-01-01T00:00:00.500Z") == START + 0.5

    @pytest.mark.parametrize(
        "value",
        ["", "not-a-date", "2026-13-45T00:00:00Z", "2026-01-01T00:00:00"],
        ids=["empty", "garbage", "out-of-range", "no-timezone"],
    )
    def test_unusable_values_are_none(self, value):
        assert parse_github_timestamp(value) is None

    def test_timezone_less_value_never_depends_on_the_host_timezone(self, monkeypatch):
        import time

        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
        try:
            assert parse_github_timestamp("2026-01-01T00:00:00") is None
        finally:
            monkeypatch.undo()
            time.tzset()


class TestShouldExcludePr:
    def test_unset_started_at_never_excludes_even_with_unusable_created_at(self):
        assert should_exclude_pr(created_at="", started_at=None) is False
        assert should_exclude_pr(created_at="garbage", started_at=None) is False
        assert should_exclude_pr(
            created_at="2000-01-01T00:00:00Z", started_at=None
        ) is (False)

    @pytest.mark.parametrize("created_at", ["", "garbage", "2026-01-01T00:00:00"])
    def test_unusable_created_at_is_excluded_when_started_at_is_known(self, created_at):
        assert should_exclude_pr(created_at=created_at, started_at=START) is True

    @pytest.mark.parametrize("started_at", [math.nan, math.inf, -math.inf])
    def test_non_finite_started_at_excludes_every_pr(self, started_at):
        assert (
            should_exclude_pr(created_at="2026-01-01T00:00:00Z", started_at=started_at)
            is True
        )
        assert should_exclude_pr(created_at="", started_at=started_at) is True

    def test_pr_created_before_the_start_second_is_excluded(self):
        assert (
            should_exclude_pr(created_at="2025-12-31T23:59:59Z", started_at=START)
            is True
        )

    def test_pr_created_in_the_start_second_is_kept_despite_fractional_start(self):
        assert (
            should_exclude_pr(created_at="2026-01-01T00:00:00Z", started_at=START + 0.9)
            is False
        )

    def test_pr_created_one_second_before_a_fractional_start_is_excluded(self):
        assert (
            should_exclude_pr(created_at="2025-12-31T23:59:59Z", started_at=START + 0.9)
            is True
        )

    def test_pr_created_after_the_start_is_kept(self):
        assert (
            should_exclude_pr(created_at="2026-01-01T00:00:01Z", started_at=START)
            is False
        )

    def test_offset_notation_does_not_change_the_comparison(self):
        assert (
            should_exclude_pr(
                created_at="2026-01-01T09:00:00+09:00", started_at=START + 0.9
            )
            is False
        )
        assert (
            should_exclude_pr(
                created_at="2026-01-01T08:59:59+09:00", started_at=START + 0.9
            )
            is True
        )


FAKE_BRANCH = "claude/issue-1-task-1"
LAUNCH = 1_800_000_000.0  # 2027-01-15T08:00:00Z
BEFORE_LAUNCH = "2026-01-01T00:00:00Z"
AFTER_LAUNCH = "2029-01-01T00:00:00Z"
CLOSED_AFTER_LAUNCH = "2030-01-01T00:00:00Z"


def _pr(state, created_at, closed_at="", number=1):
    return PrRecord(
        number=number,
        head_ref=FAKE_BRANCH,
        changed_files=(),
        closes_issue_numbers=(1,),
        state=state,
        created_at=created_at,
        closed_at=closed_at,
    )


def _statuses(tmp_path, prs, *, started_at=LAUNCH):
    """Run the same PR list through the target path and the GC path."""
    forge = MagicMock()
    forge.list_prs.return_value = prs
    forge.list_comments.return_value = []
    handle = DispatchHandle(
        external_id="session_1",
        branch_name=FAKE_BRANCH,
        issue_number=1,
        started_at=started_at,
    )
    target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
    config = DispatcherConfig(
        parent_issue_number=100,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        forge=forge,
    )
    active = make_test_active_worktree(
        1,
        branch=FAKE_BRANCH,
        worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
        pid=123,
        started_at=started_at,
    )
    return (
        target.completion_status(handle, forge=forge),
        _local_pr_completion_status(active, config),
    )


class TestTargetAndGcShareTheFreshnessRule:
    """#1306: one candidate PR is judged the same way on both completion paths."""

    @pytest.mark.parametrize("state", ["OPEN", "MERGED", "CLOSED"])
    def test_pr_created_before_launch_is_excluded_in_every_state(self, tmp_path, state):
        closed_at = CLOSED_AFTER_LAUNCH if state != "OPEN" else ""
        prs = [_pr(state, BEFORE_LAUNCH, closed_at)]
        assert _statuses(tmp_path, prs) == ("pending", "pending")

    @pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
    @pytest.mark.parametrize("created_at", ["", "garbage", "2029-01-01T00:00:00"])
    def test_unusable_created_at_is_no_evidence_when_started_at_is_known(
        self, tmp_path, state, created_at
    ):
        prs = [_pr(state, created_at, CLOSED_AFTER_LAUNCH)]
        assert _statuses(tmp_path, prs) == ("pending", "pending")

    @pytest.mark.parametrize(
        ("state", "expected"),
        [("MERGED", "completed"), ("CLOSED", "abandoned"), ("OPEN", "pending")],
    )
    def test_pr_created_after_launch_keeps_its_state_meaning(
        self, tmp_path, state, expected
    ):
        closed_at = CLOSED_AFTER_LAUNCH if state != "OPEN" else ""
        prs = [_pr(state, AFTER_LAUNCH, closed_at)]
        assert _statuses(tmp_path, prs) == (expected, expected)

    def test_current_pr_is_used_when_stale_prs_are_mixed_in(self, tmp_path):
        prs = [
            _pr("MERGED", BEFORE_LAUNCH, CLOSED_AFTER_LAUNCH, number=1),
            _pr("CLOSED", AFTER_LAUNCH, CLOSED_AFTER_LAUNCH, number=2),
        ]
        assert _statuses(tmp_path, prs) == ("abandoned", "abandoned")

    def test_without_started_at_every_pr_counts(self, tmp_path):
        prs = [_pr("MERGED", "garbage")]
        assert _statuses(tmp_path, prs, started_at=None) == ("completed", "completed")

    def test_same_second_pr_is_kept_and_one_second_earlier_is_excluded(self, tmp_path):
        started_at = 1_767_225_600.9
        assert _statuses(
            tmp_path, [_pr("MERGED", "2026-01-01T00:00:00Z")], started_at=started_at
        ) == ("completed", "completed")
        assert _statuses(
            tmp_path, [_pr("MERGED", "2025-12-31T23:59:59Z")], started_at=started_at
        ) == ("pending", "pending")
