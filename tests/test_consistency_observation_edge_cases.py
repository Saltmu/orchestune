"""Cross-cutting completeness, freshness, and identity edge cases."""

from datetime import datetime, timedelta

from test_consistency_observation import (
    FETCHED_AT,
    NOW,
    _collector,
    _execution,
    _fact,
    _issue,
    _pr,
    _task_fact,
)

from orchestune.consistency import ConsistencyScope, ObservationCertainty
from orchestune.consistency.observation import (
    FACT_CHILD_ISSUE_NUMBERS,
    FACT_ISSUE_COUNT,
    FACT_ISSUE_LABELS,
    FACT_ISSUE_STATE,
    FACT_ISSUE_STATUS_LABELS,
    FACT_PARENT_ISSUE_NUMBER,
    FACT_PULL_REQUEST_COUNT,
    FACT_PULL_REQUEST_NUMBER,
    ForgeSnapshot,
)


def test_filtered_repository_counts_keep_evidence_but_stay_unknown() -> None:
    """Filtered cycle inputs prove the observed lower bound, not a total."""
    snapshot = ForgeSnapshot(
        issues=(_issue(703), _issue(704)),
        pull_requests=(_pr(720, "claude/issue-703"),),
        fetched_at=FETCHED_AT,
    )

    state = _collector().collect(forge=snapshot)

    issue_count = _fact(state, ConsistencyScope.REPOSITORY, None, FACT_ISSUE_COUNT)
    assert issue_count.value == 2
    assert issue_count.certainty is ObservationCertainty.UNKNOWN
    assert issue_count.diagnostics == (
        "the reused Issue snapshot is filtered, so the observed issue count "
        "is not a complete repository count",
    )
    pull_request_count = _fact(
        state, ConsistencyScope.REPOSITORY, None, FACT_PULL_REQUEST_COUNT
    )
    assert pull_request_count.value == 1
    assert pull_request_count.certainty is ObservationCertainty.UNKNOWN
    assert pull_request_count.diagnostics == (
        "the reused pull request snapshot is filtered, so the observed pull "
        "request count is not a complete repository count",
    )


def test_a_nested_parent_is_also_observed_as_a_task() -> None:
    """An Issue can be a parent and a child; its child link makes it a task."""
    snapshot = ForgeSnapshot(
        issues=(
            _issue(600, labels=()),
            _issue(700, parent={"number": 600, "state": "OPEN"}),
            _issue(703, parent={"number": 700, "state": "OPEN"}),
        ),
        fetched_at=FETCHED_AT,
    )

    state = _collector().collect(forge=snapshot)

    assert _task_fact(state, 700, FACT_PARENT_ISSUE_NUMBER).value == 600
    assert _task_fact(state, 703, FACT_PARENT_ISSUE_NUMBER).value == 700


def test_a_nested_parent_with_conflicting_links_stays_a_task() -> None:
    snapshot = ForgeSnapshot(
        issues=(
            _issue(700, parent={"number": 600, "state": "OPEN"}),
            _issue(700, parent={"number": 601, "state": "OPEN"}),
            _issue(703, parent={"number": 700, "state": "OPEN"}),
        ),
        fetched_at=FETCHED_AT,
    )

    state = _collector().collect(forge=snapshot)

    parent = _task_fact(state, 700, FACT_PARENT_ISSUE_NUMBER)
    assert parent.certainty is ObservationCertainty.UNKNOWN
    assert parent.value is None


def test_snapshot_without_a_budget_does_not_compare_naive_timestamps() -> None:
    fetched_at = datetime(2026, 8, 28, 11, 59)
    snapshot = ForgeSnapshot(issues=(_issue(703),), fetched_at=fetched_at)

    state = _collector().collect(forge=snapshot)

    issue_state = _task_fact(state, 703, FACT_ISSUE_STATE)
    assert issue_state.certainty is ObservationCertainty.KNOWN
    assert issue_state.observed_at == fetched_at


def test_incomparable_snapshot_clock_has_unknown_freshness() -> None:
    snapshot = ForgeSnapshot(
        issues=(_issue(703),), fetched_at=datetime(2026, 8, 28, 11, 59)
    )

    state = _collector(freshness_budget=timedelta(minutes=5)).collect(forge=snapshot)

    issue_state = _task_fact(state, 703, FACT_ISSUE_STATE)
    assert issue_state.certainty is ObservationCertainty.UNKNOWN
    assert issue_state.observed_at == NOW
    assert issue_state.diagnostics == (
        "forge snapshot timestamp cannot be compared with the collector clock",
    )


def test_snapshot_from_the_future_has_unknown_freshness() -> None:
    snapshot = ForgeSnapshot(
        issues=(_issue(703),), fetched_at=NOW + timedelta(minutes=1)
    )

    state = _collector(freshness_budget=timedelta(minutes=5)).collect(forge=snapshot)

    issue_state = _task_fact(state, 703, FACT_ISSUE_STATE)
    assert issue_state.certainty is ObservationCertainty.UNKNOWN
    assert issue_state.observed_at == NOW
    assert issue_state.diagnostics == (
        "forge snapshot timestamp is 0:01:00 in the future",
    )


def test_stale_filtered_snapshot_keeps_both_diagnostic_reasons() -> None:
    snapshot = ForgeSnapshot(
        issues=(_issue(703, parent={"number": 700, "state": "OPEN"}),),
        fetched_at=NOW - timedelta(minutes=30),
    )

    state = _collector(freshness_budget=timedelta(minutes=5)).collect(forge=snapshot)

    children = _fact(state, ConsistencyScope.PARENT, "700", FACT_CHILD_ISSUE_NUMBERS)
    assert children.certainty is ObservationCertainty.UNKNOWN
    assert children.value == (703,)
    assert children.diagnostics == (
        "forge snapshot is 0:30:00 old, past the 0:05:00 freshness budget",
        "the reused Issue snapshot is filtered, so these are the children "
        "observed in it, not necessarily every child",
    )


def test_issue_missing_from_a_complete_snapshot_is_a_known_absence() -> None:
    snapshot = ForgeSnapshot(
        issues=(_issue(704),), fetched_at=FETCHED_AT, issues_complete=True
    )

    state = _collector().collect(forge=snapshot, executions=(_execution(703),))

    for name in (
        FACT_ISSUE_STATE,
        FACT_ISSUE_LABELS,
        FACT_ISSUE_STATUS_LABELS,
        FACT_PARENT_ISSUE_NUMBER,
    ):
        fact = _task_fact(state, 703, name)
        assert fact.certainty is ObservationCertainty.KNOWN, name
        assert fact.value is None, name
        assert fact.diagnostics == (), name


def test_exact_duplicate_pull_request_records_collapse_to_one_candidate() -> None:
    pull_request = _pr(720, "claude/issue-703")
    snapshot = ForgeSnapshot(
        issues=(_issue(703),),
        pull_requests=(pull_request, pull_request),
        fetched_at=FETCHED_AT,
        pull_requests_complete=True,
    )

    state = _collector().collect(forge=snapshot, executions=(_execution(703),))

    number = _task_fact(state, 703, FACT_PULL_REQUEST_NUMBER)
    assert number.certainty is ObservationCertainty.KNOWN
    assert number.value == 720
    count = _fact(state, ConsistencyScope.REPOSITORY, None, FACT_PULL_REQUEST_COUNT)
    assert count.certainty is ObservationCertainty.KNOWN
    assert count.value == 1
