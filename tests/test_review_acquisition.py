"""The installed package and historical script share exactly one implementation."""

from orchestune.review import acquisition
from scripts import review_verdict


def test_script_reexports_packaged_acquisition():
    assert review_verdict.extract_review_result is acquisition.extract_review_result
    assert review_verdict.collect_review_state is acquisition.collect_review_state


def test_current_round_acquisition_ignores_bot_authored_trigger():
    state = {
        "issue_comments": [
            dict(
                id=1,
                body="@claude review",
                user={"login": "claude[bot]"},
                created_at="2026-10-03T00:00:00Z",
            ),
        ]
    }
    result = acquisition.collect_review_state(
        state, "claude", exclude_ids={1}, round_started_at="2026-10-03T00:00:00Z"
    )
    assert result["acquisition_status"] == "unavailable"
