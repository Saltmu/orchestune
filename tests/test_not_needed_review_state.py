from __future__ import annotations

from orchestune.not_needed_review_state import (
    NotNeededReviewState,
    PendingNotNeededReview,
    load_not_needed_review_state,
    save_not_needed_review_state,
)


class TestNotNeededReviewStateRoundTrip:
    def test_load_missing_file_returns_empty_state(self, tmp_path):
        state = load_not_needed_review_state(tmp_path / "does-not-exist.json")
        assert state.pending == []

    def test_save_then_load_round_trips(self, tmp_path):
        path = tmp_path / "not_needed_review_state.json"
        state = NotNeededReviewState(
            pending=[
                PendingNotNeededReview(
                    issue_number=250,
                    subtask_id="plot-api-routes",
                    dispatched_at=1234.5,
                    session_external_id="sess-1",
                    session_external_url="https://claude.ai/code/s/sess-1",
                )
            ]
        )

        save_not_needed_review_state(state, path)
        loaded = load_not_needed_review_state(path)

        assert len(loaded.pending) == 1
        entry = loaded.pending[0]
        assert entry.issue_number == 250
        assert entry.subtask_id == "plot-api-routes"
        assert entry.dispatched_at == 1234.5
        assert entry.session_external_id == "sess-1"
        assert entry.session_external_url == "https://claude.ai/code/s/sess-1"

    def test_save_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "state.json"
        save_not_needed_review_state(NotNeededReviewState(), path)
        assert path.exists()

    def test_optional_session_fields_default_none_on_load(self, tmp_path):
        path = tmp_path / "state.json"
        state = NotNeededReviewState(
            pending=[
                PendingNotNeededReview(
                    issue_number=1,
                    subtask_id="task-a",
                    dispatched_at=1.0,
                )
            ]
        )
        save_not_needed_review_state(state, path)
        loaded = load_not_needed_review_state(path)
        assert loaded.pending[0].session_external_id is None
        assert loaded.pending[0].session_external_url is None
