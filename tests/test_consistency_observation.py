from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from orchestune.consistency import (
    ConsistencyScope,
    Observation,
    ObservationCertainty,
    ObservedRepositoryState,
    ScopedObservations,
)
from orchestune.consistency.observation import (
    EXECUTION_KIND_CLOUD,
    EXECUTION_KIND_LOCAL,
    EXECUTION_KIND_NONE,
    EXECUTION_KIND_UNKNOWN,
    FACT_BRANCH_EXISTS,
    FACT_BRANCH_NAME,
    FACT_CHILD_ISSUE_NUMBERS,
    FACT_EXECUTION_COUNT,
    FACT_EXECUTION_EXTERNAL_ID,
    FACT_EXECUTION_EXTERNAL_STATUS,
    FACT_EXECUTION_KIND,
    FACT_EXECUTION_PID,
    FACT_EXECUTION_PROCESS_ALIVE,
    FACT_FORGE_REACHABLE,
    FACT_ISSUE_COUNT,
    FACT_ISSUE_LABELS,
    FACT_ISSUE_NUMBER,
    FACT_ISSUE_STATE,
    FACT_ISSUE_STATUS_LABELS,
    FACT_PARENT_ISSUE_NUMBER,
    FACT_PARENT_STATE,
    FACT_PULL_REQUEST_COUNT,
    FACT_PULL_REQUEST_NUMBER,
    FACT_PULL_REQUEST_STATE,
    FACT_WORKTREE_EXISTS,
    FACT_WORKTREE_PATH,
    ExecutionRecord,
    ForgeSnapshot,
    ObservationCollector,
    build_observed_repository_state,
)
from orchestune.models import IssueRecord, PrRecord

REPOSITORY_ID = "Saltmu/orchestune"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
FETCHED_AT = datetime(2026, 8, 28, 11, 59, tzinfo=UTC)


def _clock() -> datetime:
    return NOW


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class RecordingProbe:
    """Base double that records every read call the collector makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []


class FakeForgeProbe(RecordingProbe):
    def __init__(self, snapshot: ForgeSnapshot | Exception) -> None:
        super().__init__()
        self._snapshot = snapshot

    def fetch_snapshot(self) -> ForgeSnapshot:
        self.calls.append(("fetch_snapshot", None))
        if isinstance(self._snapshot, Exception):
            raise self._snapshot
        return self._snapshot

    # Mutating operations a real Forge would offer.  The collector must never
    # reach for them; calling one fails the test that owns this double.
    def add_label(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("collector applied a repair: add_label")

    def create_pull_request(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("collector applied a repair: create_pull_request")

    def post_comment(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("collector applied a repair: post_comment")


class FakeGitProbe(RecordingProbe):
    def __init__(self, existing: set[str] | Exception) -> None:
        super().__init__()
        self._existing = existing

    def branch_exists(self, branch: str) -> bool:
        self.calls.append(("branch_exists", branch))
        if isinstance(self._existing, Exception):
            raise self._existing
        return branch in self._existing

    def delete_branch(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("collector applied a repair: delete_branch")


class FakeWorktreeProbe(RecordingProbe):
    def __init__(self, existing: set[str] | Exception) -> None:
        super().__init__()
        self._existing = existing

    def worktree_exists(self, path: str) -> bool:
        self.calls.append(("worktree_exists", path))
        if isinstance(self._existing, Exception):
            raise self._existing
        return path in self._existing

    def remove_worktree(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("collector applied a repair: remove_worktree")


class FakeProcessProbe(RecordingProbe):
    def __init__(self, alive: set[int] | Exception) -> None:
        super().__init__()
        self._alive = alive

    def is_alive(self, pid: int) -> bool:
        self.calls.append(("is_alive", pid))
        if isinstance(self._alive, Exception):
            raise self._alive
        return pid in self._alive

    def terminate(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("collector applied a repair: terminate")


class FakeExternalProbe(RecordingProbe):
    def __init__(self, statuses: dict[str, str] | Exception) -> None:
        super().__init__()
        self._statuses = statuses

    def status(self, external_id: str) -> str:
        self.calls.append(("status", external_id))
        if isinstance(self._statuses, Exception):
            raise self._statuses
        return self._statuses[external_id]

    def cancel(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("collector applied a repair: cancel")


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


def _issue(
    number: int,
    *,
    labels: tuple[str, ...] = ("status:in-progress",),
    state: str = "OPEN",
    parent: dict | None = None,
) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=f"task {number}",
        body="",
        labels=labels,
        created_at="2026-08-28T00:00:00Z",
        state=state,
        parent=parent,
    )


def _pr(number: int, head_ref: str, *, state: str = "OPEN") -> PrRecord:
    return PrRecord(
        number=number,
        head_ref=head_ref,
        changed_files=(),
        state=state,
    )


def _execution(
    issue_number: int,
    *,
    branch: str | None = None,
    worktree_path: str | None = "worktrees/703",
    pid: int | None = 4242,
    external_id: str | None = None,
) -> ExecutionRecord:
    return ExecutionRecord(
        issue_number=issue_number,
        branch=branch if branch is not None else f"claude/issue-{issue_number}",
        worktree_path=worktree_path,
        pid=pid,
        external_id=external_id,
    )


def _scoped(
    state: ObservedRepositoryState,
    scope: ConsistencyScope,
    subject_id: str | None = None,
) -> ScopedObservations:
    for scoped in state.observations:
        if scoped.scope is scope and scoped.subject_id == subject_id:
            return scoped
    raise AssertionError(f"no {scope} observations for subject {subject_id!r}")


def _fact(
    state: ObservedRepositoryState,
    scope: ConsistencyScope,
    subject_id: str | None,
    name: str,
) -> Observation:
    for observation in _scoped(state, scope, subject_id).facts:
        if observation.name == name:
            return observation
    raise AssertionError(f"no {name!r} fact for subject {subject_id!r}")


def _task_fact(
    state: ObservedRepositoryState, issue_number: int, name: str
) -> Observation:
    return _fact(state, ConsistencyScope.TASK, str(issue_number), name)


def _collector(**kwargs) -> ObservationCollector:
    kwargs.setdefault("repository_id", REPOSITORY_ID)
    kwargs.setdefault("clock", _clock)
    return ObservationCollector(**kwargs)


# --------------------------------------------------------------------------
# Happy path: explicit identifiers link issue, execution, branch/worktree, PR
# --------------------------------------------------------------------------


def test_task_observation_links_issue_execution_branch_worktree_and_pr() -> None:
    snapshot = ForgeSnapshot(
        issues=(_issue(703),),
        pull_requests=(_pr(720, "claude/issue-703"),),
        fetched_at=FETCHED_AT,
    )
    collector = _collector(
        git_probe=FakeGitProbe({"claude/issue-703"}),
        worktree_probe=FakeWorktreeProbe({"worktrees/703"}),
        process_probe=FakeProcessProbe({4242}),
    )

    state = collector.collect(forge=snapshot, executions=(_execution(703),))

    assert state.repository_id == REPOSITORY_ID
    assert state.observed_at == NOW
    assert _task_fact(state, 703, FACT_ISSUE_NUMBER).value == 703
    assert _task_fact(state, 703, FACT_ISSUE_STATE).value == "OPEN"
    assert _task_fact(state, 703, FACT_ISSUE_STATUS_LABELS).value == (
        "status:in-progress",
    )
    assert _task_fact(state, 703, FACT_EXECUTION_KIND).value == EXECUTION_KIND_LOCAL
    assert _task_fact(state, 703, FACT_EXECUTION_PID).value == 4242
    assert _task_fact(state, 703, FACT_EXECUTION_PROCESS_ALIVE).value is True
    assert _task_fact(state, 703, FACT_BRANCH_NAME).value == "claude/issue-703"
    assert _task_fact(state, 703, FACT_BRANCH_EXISTS).value is True
    assert _task_fact(state, 703, FACT_WORKTREE_PATH).value == "worktrees/703"
    assert _task_fact(state, 703, FACT_WORKTREE_EXISTS).value is True
    assert _task_fact(state, 703, FACT_PULL_REQUEST_NUMBER).value == 720
    assert _task_fact(state, 703, FACT_PULL_REQUEST_STATE).value == "OPEN"
    for name in (
        FACT_ISSUE_STATE,
        FACT_EXECUTION_PROCESS_ALIVE,
        FACT_BRANCH_EXISTS,
        FACT_PULL_REQUEST_NUMBER,
    ):
        assert (
            _task_fact(state, 703, name).certainty is ObservationCertainty.KNOWN
        ), name


def test_repository_scope_summarises_the_reused_snapshot() -> None:
    snapshot = ForgeSnapshot(
        issues=(_issue(703), _issue(704)),
        pull_requests=(_pr(720, "claude/issue-703"),),
        fetched_at=FETCHED_AT,
    )
    state = _collector().collect(forge=snapshot, executions=(_execution(703),))

    repo = ConsistencyScope.REPOSITORY
    assert _fact(state, repo, None, FACT_FORGE_REACHABLE).value is True
    assert _fact(state, repo, None, FACT_ISSUE_COUNT).value == 2
    assert _fact(state, repo, None, FACT_PULL_REQUEST_COUNT).value == 1
    assert _fact(state, repo, None, FACT_EXECUTION_COUNT).value == 1


def test_parent_scope_groups_children_by_explicit_parent_identifier() -> None:
    parent = {"number": 700, "state": "OPEN"}
    snapshot = ForgeSnapshot(
        issues=(_issue(703, parent=parent), _issue(704, parent=parent)),
        fetched_at=FETCHED_AT,
    )
    state = _collector().collect(forge=snapshot)

    children = _fact(state, ConsistencyScope.PARENT, "700", FACT_CHILD_ISSUE_NUMBERS)
    assert children.value == (703, 704)
    assert (
        _fact(state, ConsistencyScope.PARENT, "700", FACT_PARENT_STATE).value == "OPEN"
    )
    assert _task_fact(state, 703, FACT_PARENT_ISSUE_NUMBER).value == 700


def test_parent_state_prefers_the_parents_own_issue_record() -> None:
    snapshot = ForgeSnapshot(
        issues=(
            _issue(703, parent={"number": 700, "state": "OPEN"}),
            _issue(700, labels=(), state="CLOSED"),
        ),
        fetched_at=FETCHED_AT,
    )

    state = _collector().collect(forge=snapshot)

    parent_state = _fact(state, ConsistencyScope.PARENT, "700", FACT_PARENT_STATE)
    assert parent_state.value == "CLOSED"
    assert parent_state.certainty is ObservationCertainty.KNOWN


def test_parent_state_is_unknown_when_no_record_declares_it() -> None:
    snapshot = ForgeSnapshot(
        issues=(_issue(703, parent={"number": 700}),), fetched_at=FETCHED_AT
    )

    state = _collector().collect(forge=snapshot)

    parent_state = _fact(state, ConsistencyScope.PARENT, "700", FACT_PARENT_STATE)
    assert parent_state.certainty is ObservationCertainty.UNKNOWN
    assert parent_state.value is None
    assert parent_state.diagnostics == (
        "issue #700 is not present in the reused Forge snapshot",
    )


def test_a_parent_payload_without_a_usable_number_is_ignored() -> None:
    snapshot = ForgeSnapshot(
        issues=(_issue(703, parent={"number": "700", "state": "OPEN"}),),
        fetched_at=FETCHED_AT,
    )

    state = _collector().collect(forge=snapshot)

    assert [s.scope for s in state.observations] == [
        ConsistencyScope.REPOSITORY,
        ConsistencyScope.TASK,
    ]
    parent = _task_fact(state, 703, FACT_PARENT_ISSUE_NUMBER)
    assert parent.value is None
    assert parent.certainty is ObservationCertainty.KNOWN


def test_task_without_execution_reports_a_known_absence() -> None:
    snapshot = ForgeSnapshot(issues=(_issue(703),), fetched_at=FETCHED_AT)
    state = _collector().collect(forge=snapshot)

    kind = _task_fact(state, 703, FACT_EXECUTION_KIND)
    assert kind.value == EXECUTION_KIND_NONE
    assert kind.certainty is ObservationCertainty.KNOWN


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------


def test_stale_snapshot_degrades_certainty_and_preserves_fetch_time() -> None:
    snapshot = ForgeSnapshot(
        issues=(_issue(703),),
        pull_requests=(_pr(720, "claude/issue-703"),),
        fetched_at=NOW - timedelta(minutes=30),
    )
    collector = _collector(freshness_budget=timedelta(minutes=5))

    state = collector.collect(forge=snapshot, executions=(_execution(703),))

    issue_state = _task_fact(state, 703, FACT_ISSUE_STATE)
    assert issue_state.certainty is ObservationCertainty.STALE
    assert issue_state.observed_at == NOW - timedelta(minutes=30)
    assert issue_state.value == "OPEN"
    assert (
        _task_fact(state, 703, FACT_PULL_REQUEST_NUMBER).certainty
        is ObservationCertainty.STALE
    )
    # Freshness applies to Forge-derived facts only; run-state facts stay known.
    assert (
        _task_fact(state, 703, FACT_EXECUTION_PID).certainty
        is ObservationCertainty.KNOWN
    )


def test_fresh_snapshot_within_budget_stays_known() -> None:
    snapshot = ForgeSnapshot(issues=(_issue(703),), fetched_at=FETCHED_AT)
    collector = _collector(freshness_budget=timedelta(minutes=5))

    state = collector.collect(forge=snapshot)

    issue_state = _task_fact(state, 703, FACT_ISSUE_STATE)
    assert issue_state.certainty is ObservationCertainty.KNOWN
    assert issue_state.observed_at == FETCHED_AT


def test_snapshot_without_fetch_time_is_not_treated_as_stale() -> None:
    snapshot = ForgeSnapshot(issues=(_issue(703),))
    collector = _collector(freshness_budget=timedelta(minutes=5))

    state = collector.collect(forge=snapshot)

    assert (
        _task_fact(state, 703, FACT_ISSUE_STATE).certainty is ObservationCertainty.KNOWN
    )


# --------------------------------------------------------------------------
# Partial retrieval must not become a false absence
# --------------------------------------------------------------------------


def test_issue_missing_from_the_reused_snapshot_is_unknown_not_absent() -> None:
    snapshot = ForgeSnapshot(issues=(_issue(704),), fetched_at=FETCHED_AT)

    state = _collector().collect(forge=snapshot, executions=(_execution(703),))

    issue_state = _task_fact(state, 703, FACT_ISSUE_STATE)
    assert issue_state.certainty is ObservationCertainty.UNKNOWN
    assert issue_state.value is None
    assert any("snapshot" in note for note in issue_state.diagnostics)


def test_execution_only_task_is_still_observed() -> None:
    state = _collector().collect(executions=(_execution(703),))

    assert _task_fact(state, 703, FACT_EXECUTION_KIND).value == EXECUTION_KIND_LOCAL
    assert (
        _task_fact(state, 703, FACT_ISSUE_STATE).certainty
        is ObservationCertainty.UNKNOWN
    )


def test_branch_supplied_without_issue_or_execution_is_observed() -> None:
    state = _collector().collect(branches_by_issue={703: "claude/issue-703"})

    assert _task_fact(state, 703, FACT_BRANCH_NAME).value == "claude/issue-703"
    assert _task_fact(state, 703, FACT_EXECUTION_KIND).value == EXECUTION_KIND_NONE


# --------------------------------------------------------------------------
# Ambiguous correspondences are retained as diagnostic evidence
# --------------------------------------------------------------------------


def test_multiple_pull_requests_on_one_branch_are_unknown_with_candidates() -> None:
    snapshot = ForgeSnapshot(
        issues=(_issue(703),),
        pull_requests=(
            _pr(721, "claude/issue-703"),
            _pr(720, "claude/issue-703", state="CLOSED"),
        ),
        fetched_at=FETCHED_AT,
    )

    state = _collector().collect(forge=snapshot, executions=(_execution(703),))

    pr_number = _task_fact(state, 703, FACT_PULL_REQUEST_NUMBER)
    assert pr_number.certainty is ObservationCertainty.UNKNOWN
    assert pr_number.value is None
    assert pr_number.diagnostics == (
        "ambiguous pull request correspondence for branch "
        "'claude/issue-703': candidates 720, 721",
    )
    assert (
        _task_fact(state, 703, FACT_PULL_REQUEST_STATE).certainty
        is ObservationCertainty.UNKNOWN
    )


def test_task_without_a_matching_pull_request_is_a_known_absence() -> None:
    snapshot = ForgeSnapshot(
        issues=(_issue(703),),
        pull_requests=(_pr(720, "claude/issue-999"),),
        fetched_at=FETCHED_AT,
    )

    state = _collector().collect(forge=snapshot, executions=(_execution(703),))

    pr_number = _task_fact(state, 703, FACT_PULL_REQUEST_NUMBER)
    assert pr_number.certainty is ObservationCertainty.KNOWN
    assert pr_number.value is None


def test_pull_request_is_unknown_when_the_branch_itself_is_unknown() -> None:
    snapshot = ForgeSnapshot(issues=(_issue(703),), fetched_at=FETCHED_AT)

    state = _collector().collect(forge=snapshot)

    assert (
        _task_fact(state, 703, FACT_BRANCH_NAME).certainty
        is ObservationCertainty.UNKNOWN
    )
    assert (
        _task_fact(state, 703, FACT_PULL_REQUEST_NUMBER).certainty
        is ObservationCertainty.UNKNOWN
    )


def test_multiple_executions_for_one_issue_are_unknown_with_candidates() -> None:
    executions = (
        _execution(703, branch="claude/issue-703-b", pid=2),
        _execution(703, branch="claude/issue-703-a", pid=1),
    )

    state = _collector(process_probe=FakeProcessProbe({1, 2})).collect(
        executions=executions
    )

    kind = _task_fact(state, 703, FACT_EXECUTION_KIND)
    assert kind.certainty is ObservationCertainty.UNKNOWN
    assert kind.value is None
    assert kind.diagnostics == (
        "ambiguous execution correspondence for issue 703: "
        "branches 'claude/issue-703-a', 'claude/issue-703-b'",
    )
    for name in (FACT_EXECUTION_PID, FACT_EXECUTION_PROCESS_ALIVE, FACT_BRANCH_NAME):
        assert _task_fact(state, 703, name).certainty is ObservationCertainty.UNKNOWN


def test_conflicting_branch_records_are_unknown_with_both_candidates() -> None:
    state = _collector().collect(
        executions=(_execution(703, branch="claude/issue-703-old"),),
        branches_by_issue={703: "claude/issue-703-new"},
    )

    branch = _task_fact(state, 703, FACT_BRANCH_NAME)
    assert branch.certainty is ObservationCertainty.UNKNOWN
    assert branch.value is None
    assert branch.diagnostics == (
        "ambiguous branch correspondence for issue 703: "
        "declared 'claude/issue-703-new', execution 'claude/issue-703-old'",
    )


def test_ambiguous_execution_does_not_probe_a_process() -> None:
    process = FakeProcessProbe({1, 2})
    executions = (_execution(703, pid=1), _execution(703, pid=2))

    _collector(process_probe=process).collect(executions=executions)

    assert process.calls == []


# --------------------------------------------------------------------------
# Probe failures degrade to unknown, never to a false absence
# --------------------------------------------------------------------------


def test_forge_probe_failure_produces_unknown_repository_and_task_facts() -> None:
    forge = FakeForgeProbe(RuntimeError("gh: API rate limit exceeded"))

    state = _collector(forge_probe=forge).collect(executions=(_execution(703),))

    reachable = _fact(state, ConsistencyScope.REPOSITORY, None, FACT_FORGE_REACHABLE)
    assert reachable.certainty is ObservationCertainty.UNKNOWN
    assert reachable.value is None
    assert reachable.diagnostics == (
        "forge probe failed: RuntimeError: gh: API rate limit exceeded",
    )
    assert (
        _fact(state, ConsistencyScope.REPOSITORY, None, FACT_ISSUE_COUNT).certainty
        is ObservationCertainty.UNKNOWN
    )
    for name in (FACT_ISSUE_STATE, FACT_PULL_REQUEST_NUMBER):
        fact = _task_fact(state, 703, name)
        assert fact.certainty is ObservationCertainty.UNKNOWN
        assert fact.value is None
    # A Forge outage must not erase the locally observable execution.
    assert _task_fact(state, 703, FACT_EXECUTION_KIND).value == EXECUTION_KIND_LOCAL


def test_git_probe_failure_produces_unknown_branch_existence() -> None:
    git = FakeGitProbe(OSError("fatal: not a git repository"))

    state = _collector(git_probe=git).collect(executions=(_execution(703),))

    exists = _task_fact(state, 703, FACT_BRANCH_EXISTS)
    assert exists.certainty is ObservationCertainty.UNKNOWN
    assert exists.value is None
    assert exists.diagnostics == (
        "git probe failed: OSError: fatal: not a git repository",
    )
    # The identifier itself is still known; only its existence is not.
    assert (
        _task_fact(state, 703, FACT_BRANCH_NAME).certainty is ObservationCertainty.KNOWN
    )


def test_worktree_probe_failure_produces_unknown_existence() -> None:
    worktree = FakeWorktreeProbe(PermissionError("access denied"))

    state = _collector(worktree_probe=worktree).collect(executions=(_execution(703),))

    exists = _task_fact(state, 703, FACT_WORKTREE_EXISTS)
    assert exists.certainty is ObservationCertainty.UNKNOWN
    assert exists.value is None
    assert exists.diagnostics == (
        "worktree probe failed: PermissionError: access denied",
    )


def test_process_probe_failure_produces_unknown_liveness() -> None:
    process = FakeProcessProbe(OSError("permission denied"))

    state = _collector(process_probe=process).collect(executions=(_execution(703),))

    alive = _task_fact(state, 703, FACT_EXECUTION_PROCESS_ALIVE)
    assert alive.certainty is ObservationCertainty.UNKNOWN
    assert alive.value is None
    assert alive.diagnostics == ("process probe failed: OSError: permission denied",)


def test_external_probe_failure_produces_unknown_execution_status() -> None:
    external = FakeExternalProbe(TimeoutError("cloud API timed out"))
    execution = _execution(703, pid=None, external_id="codex-cloud:abc")

    state = _collector(external_probe=external).collect(executions=(execution,))

    status = _task_fact(state, 703, FACT_EXECUTION_EXTERNAL_STATUS)
    assert status.certainty is ObservationCertainty.UNKNOWN
    assert status.value is None
    assert status.diagnostics == (
        "external execution probe failed: TimeoutError: cloud API timed out",
    )
    assert _task_fact(state, 703, FACT_EXECUTION_KIND).value == EXECUTION_KIND_CLOUD


def test_unconfigured_probes_produce_unknown_not_false_absence() -> None:
    execution = _execution(703, external_id="codex-cloud:abc", pid=None)

    state = _collector().collect(executions=(execution,))

    for name, probe in (
        (FACT_BRANCH_EXISTS, "git"),
        (FACT_WORKTREE_EXISTS, "worktree"),
        (FACT_EXECUTION_EXTERNAL_STATUS, "external execution"),
    ):
        fact = _task_fact(state, 703, name)
        assert fact.certainty is ObservationCertainty.UNKNOWN, name
        assert fact.value is None, name
        assert fact.diagnostics == (f"{probe} probe is not configured",), name


def test_repository_facts_are_unknown_without_any_forge_input() -> None:
    state = _collector().collect(executions=(_execution(703),))

    reachable = _fact(state, ConsistencyScope.REPOSITORY, None, FACT_FORGE_REACHABLE)
    assert reachable.certainty is ObservationCertainty.UNKNOWN
    assert reachable.diagnostics == ("forge probe is not configured",)
    # Executions come from run state, so their count stays known.
    assert (
        _fact(state, ConsistencyScope.REPOSITORY, None, FACT_EXECUTION_COUNT).value == 1
    )


# --------------------------------------------------------------------------
# Cloud (pid=None) vs missing vs dead local process
# --------------------------------------------------------------------------


def test_cloud_execution_without_pid_is_not_reported_as_a_dead_process() -> None:
    external = FakeExternalProbe({"codex-cloud:abc": "running"})
    execution = _execution(703, pid=None, external_id="codex-cloud:abc")

    state = _collector(
        process_probe=FakeProcessProbe(set()), external_probe=external
    ).collect(executions=(execution,))

    assert _task_fact(state, 703, FACT_EXECUTION_KIND).value == EXECUTION_KIND_CLOUD
    assert _task_fact(state, 703, FACT_EXECUTION_EXTERNAL_ID).value == "codex-cloud:abc"
    assert _task_fact(state, 703, FACT_EXECUTION_EXTERNAL_STATUS).value == "running"
    alive = _task_fact(state, 703, FACT_EXECUTION_PROCESS_ALIVE)
    assert alive.certainty is ObservationCertainty.UNKNOWN
    assert alive.value is None
    assert alive.diagnostics == ("cloud execution has no local process",)
    pid = _task_fact(state, 703, FACT_EXECUTION_PID)
    assert pid.certainty is ObservationCertainty.KNOWN
    assert pid.value is None


def test_cloud_execution_never_probes_the_local_process() -> None:
    process = FakeProcessProbe(set())
    execution = _execution(703, pid=None, external_id="codex-cloud:abc")

    _collector(process_probe=process).collect(executions=(execution,))

    assert process.calls == []


def test_dead_local_process_is_a_known_negative() -> None:
    state = _collector(process_probe=FakeProcessProbe(set())).collect(
        executions=(_execution(703, pid=4242),)
    )

    alive = _task_fact(state, 703, FACT_EXECUTION_PROCESS_ALIVE)
    assert alive.certainty is ObservationCertainty.KNOWN
    assert alive.value is False
    assert _task_fact(state, 703, FACT_EXECUTION_KIND).value == EXECUTION_KIND_LOCAL


def test_local_execution_without_a_recorded_pid_is_unknown() -> None:
    execution = _execution(703, pid=None, external_id=None)

    state = _collector(process_probe=FakeProcessProbe({4242})).collect(
        executions=(execution,)
    )

    kind = _task_fact(state, 703, FACT_EXECUTION_KIND)
    assert kind.certainty is ObservationCertainty.UNKNOWN
    assert kind.value == EXECUTION_KIND_UNKNOWN
    assert kind.diagnostics == (
        "execution records neither a local pid nor an external execution id",
    )
    alive = _task_fact(state, 703, FACT_EXECUTION_PROCESS_ALIVE)
    assert alive.certainty is ObservationCertainty.UNKNOWN
    assert alive.value is None


def test_missing_worktree_path_is_unknown_rather_than_absent() -> None:
    execution = _execution(703, worktree_path=None)

    state = _collector(worktree_probe=FakeWorktreeProbe(set())).collect(
        executions=(execution,)
    )

    path = _task_fact(state, 703, FACT_WORKTREE_PATH)
    assert path.certainty is ObservationCertainty.UNKNOWN
    assert path.value is None
    assert (
        _task_fact(state, 703, FACT_WORKTREE_EXISTS).certainty
        is ObservationCertainty.UNKNOWN
    )


def test_external_status_is_absent_for_a_purely_local_execution() -> None:
    external = FakeExternalProbe({})

    state = _collector(external_probe=external).collect(
        executions=(_execution(703, pid=4242),)
    )

    status = _task_fact(state, 703, FACT_EXECUTION_EXTERNAL_STATUS)
    assert status.certainty is ObservationCertainty.KNOWN
    assert status.value is None
    assert external.calls == []


# --------------------------------------------------------------------------
# Determinism, immutability, and absence of repair side effects
# --------------------------------------------------------------------------


def _full_inputs() -> tuple[ForgeSnapshot, tuple[ExecutionRecord, ...]]:
    snapshot = ForgeSnapshot(
        issues=(
            _issue(704, parent={"number": 700, "state": "OPEN"}),
            _issue(703, parent={"number": 700, "state": "OPEN"}),
        ),
        pull_requests=(_pr(720, "claude/issue-703"),),
        fetched_at=FETCHED_AT,
    )
    executions = (
        _execution(704, pid=None, external_id="codex-cloud:abc"),
        _execution(703),
    )
    return snapshot, executions


def test_repeated_collection_over_identical_inputs_is_equal() -> None:
    snapshot, executions = _full_inputs()

    def collect() -> ObservedRepositoryState:
        return _collector(
            git_probe=FakeGitProbe({"claude/issue-703"}),
            worktree_probe=FakeWorktreeProbe({"worktrees/703"}),
            process_probe=FakeProcessProbe({4242}),
            external_probe=FakeExternalProbe({"codex-cloud:abc": "running"}),
        ).collect(forge=snapshot, executions=executions)

    assert collect() == collect()


def test_observations_are_ordered_deterministically() -> None:
    snapshot, executions = _full_inputs()

    state = _collector().collect(forge=snapshot, executions=executions)

    scopes = [(s.scope.value, s.subject_id) for s in state.observations]
    assert scopes == [
        ("repository", None),
        ("parent", "700"),
        ("task", "703"),
        ("task", "704"),
    ]
    for scoped in state.observations:
        names = [fact.name for fact in scoped.facts]
        assert names == sorted(names)


def test_input_order_does_not_change_the_snapshot() -> None:
    snapshot, executions = _full_inputs()
    reversed_snapshot = ForgeSnapshot(
        issues=tuple(reversed(snapshot.issues)),
        pull_requests=snapshot.pull_requests,
        fetched_at=snapshot.fetched_at,
    )

    first = _collector().collect(forge=snapshot, executions=executions)
    second = _collector().collect(
        forge=reversed_snapshot, executions=tuple(reversed(executions))
    )

    assert first == second


def test_collected_state_is_immutable() -> None:
    state = _collector().collect(executions=(_execution(703),))

    assert isinstance(state.observations, tuple)
    with pytest.raises(FrozenInstanceError):
        state.repository_id = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        state.observations[0].facts[0].name = "other"  # type: ignore[misc]


def test_inputs_are_frozen_and_left_unmodified() -> None:
    snapshot, executions = _full_inputs()
    snapshot_before = copy.deepcopy(snapshot)
    executions_before = copy.deepcopy(executions)

    _collector(
        git_probe=FakeGitProbe({"claude/issue-703"}),
        worktree_probe=FakeWorktreeProbe(set()),
        process_probe=FakeProcessProbe({4242}),
        external_probe=FakeExternalProbe({"codex-cloud:abc": "running"}),
    ).collect(forge=snapshot, executions=executions)

    assert snapshot == snapshot_before
    assert executions == executions_before
    with pytest.raises(FrozenInstanceError):
        executions[0].pid = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.fetched_at = NOW  # type: ignore[misc]


def test_collector_only_issues_read_probes() -> None:
    snapshot, executions = _full_inputs()
    git = FakeGitProbe({"claude/issue-703"})
    worktree = FakeWorktreeProbe({"worktrees/703"})
    process = FakeProcessProbe({4242})
    external = FakeExternalProbe({"codex-cloud:abc": "running"})

    _collector(
        git_probe=git,
        worktree_probe=worktree,
        process_probe=process,
        external_probe=external,
    ).collect(forge=snapshot, executions=executions)

    assert {name for name, _ in git.calls} == {"branch_exists"}
    assert {name for name, _ in worktree.calls} == {"worktree_exists"}
    assert {name for name, _ in process.calls} == {"is_alive"}
    assert {name for name, _ in external.calls} == {"status"}


def test_a_supplied_snapshot_is_reused_without_calling_the_forge_probe() -> None:
    snapshot, executions = _full_inputs()
    forge = FakeForgeProbe(snapshot)

    state = _collector(forge_probe=forge).collect(forge=snapshot, executions=executions)

    assert forge.calls == []
    assert _task_fact(state, 703, FACT_ISSUE_STATE).value == "OPEN"


def test_the_forge_probe_is_used_once_when_no_snapshot_is_supplied() -> None:
    snapshot, executions = _full_inputs()
    forge = FakeForgeProbe(snapshot)

    state = _collector(forge_probe=forge).collect(executions=executions)

    assert forge.calls == [("fetch_snapshot", None)]
    assert _task_fact(state, 703, FACT_ISSUE_STATE).value == "OPEN"


def test_labels_are_normalised_into_sorted_deduplicated_tuples() -> None:
    issue = _issue(703, labels=("priority:high", "status:in-progress", "priority:high"))
    snapshot = ForgeSnapshot(issues=(issue,), fetched_at=FETCHED_AT)

    state = _collector().collect(forge=snapshot)

    assert _task_fact(state, 703, FACT_ISSUE_LABELS).value == (
        "priority:high",
        "status:in-progress",
    )
    assert _task_fact(state, 703, FACT_ISSUE_STATUS_LABELS).value == (
        "status:in-progress",
    )


# --------------------------------------------------------------------------
# Module-level convenience entry point
# --------------------------------------------------------------------------


def test_the_default_clock_stamps_the_snapshot_with_the_current_time() -> None:
    before = datetime.now(UTC)

    state = ObservationCollector(repository_id=REPOSITORY_ID).collect()

    assert before <= state.observed_at <= datetime.now(UTC)


def test_build_observed_repository_state_matches_the_collector() -> None:
    snapshot, executions = _full_inputs()

    built = build_observed_repository_state(
        repository_id=REPOSITORY_ID,
        forge=snapshot,
        executions=executions,
        process_probe=FakeProcessProbe({4242}),
        clock=_clock,
    )
    collected = _collector(process_probe=FakeProcessProbe({4242})).collect(
        forge=snapshot, executions=executions
    )

    assert built == collected
