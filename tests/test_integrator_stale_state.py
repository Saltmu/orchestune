"""`integrator_stale_state.py`: 親branch更新の連続陳腐化カウントの永続化。"""

from __future__ import annotations

from pathlib import Path

from orchestune.integrator_stale_state import (
    StaleRetryState,
    load_stale_retry_state,
    parent_branch_stale_key,
    record_parent_branch_stale_failure,
    reset_parent_branch_stale_count,
    save_stale_retry_state,
)


class TestParentBranchStaleKey:
    def test_uses_issue_number_when_parent_issue_number_present(self):
        assert parent_branch_stale_key(100) == "issue-100"

    def test_uses_fixed_key_for_flat_mode(self):
        assert parent_branch_stale_key(None) == "flat"


class TestLoadSaveRoundTrip:
    def test_missing_file_returns_empty_state(self, tmp_path: Path):
        state = load_stale_retry_state(tmp_path / "missing.json")
        assert state.counts == {}

    def test_save_then_load_round_trips(self, tmp_path: Path):
        path = tmp_path / "stale.json"
        save_stale_retry_state(StaleRetryState(counts={"issue-100": 2}), path)

        loaded = load_stale_retry_state(path)

        assert loaded.counts == {"issue-100": 2}


class TestRecordAndReset:
    def test_record_increments_across_calls(self, tmp_path: Path):
        path = tmp_path / "stale.json"

        first = record_parent_branch_stale_failure(path, "issue-100")
        second = record_parent_branch_stale_failure(path, "issue-100")

        assert first == 1
        assert second == 2

    def test_record_tracks_keys_independently(self, tmp_path: Path):
        path = tmp_path / "stale.json"

        record_parent_branch_stale_failure(path, "issue-100")
        other = record_parent_branch_stale_failure(path, "issue-200")

        assert other == 1
        assert load_stale_retry_state(path).counts == {
            "issue-100": 1,
            "issue-200": 1,
        }

    def test_reset_clears_existing_count(self, tmp_path: Path):
        path = tmp_path / "stale.json"
        record_parent_branch_stale_failure(path, "issue-100")

        reset_parent_branch_stale_count(path, "issue-100")

        assert load_stale_retry_state(path).counts == {}

    def test_reset_without_prior_failure_does_not_create_file(self, tmp_path: Path):
        path = tmp_path / "stale.json"

        reset_parent_branch_stale_count(path, "issue-100")

        assert not path.exists()
