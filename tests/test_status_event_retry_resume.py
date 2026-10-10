"""Production retry paths stopped and resumed at every point (#1266, #1219 §3).

The harness (`tests/status_event_retry_harness.py`) keeps `run_state.json` and the
Forge across a stop and rebuilds everything else. Every expectation below is
written from the specification (the #1219 budget table and the production
docstrings), not computed by `plan_retry`, `exceeds_limit` or
`_resolve_reclaim_count`:

* a reservation that reached disk is reused by the resumed run: the count is not
  consumed twice and `retry_at` keeps its first value;
* a reservation lost before its save is planned again from the persisted state;
* an over-budget reclaim escalates exactly once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestune.dependencies.resolution import build_legacy_dag_inputs
from orchestune.dispatch import launch, rebase, recovery
from orchestune.dispatch.gc import policies
from orchestune.issue_parsing import recovery_counters_from_body
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import RunState
from orchestune.ledger.status_events import (
    BackoffState,
    ReclaimState,
    RetryStates,
)
from orchestune.outcome_record import (
    REASON_BASE_BRANCH_RED,
    RESULT_BLOCKED,
    OutcomeRecord,
    calculate_blocked_attempt,
)
from tests.conftest import FakeForge, make_issue
from tests.dispatch_test_support import (
    make_test_active_worktree,
    make_test_dispatcher_config,
    make_test_task,
)
from tests.status_event_retry_harness import (
    STOP_POINTS,
    Crash,
    PolicyWorld,
    backoff_world,
    gc_backoff_once,
    gc_reclaim_once,
    reclaim_world,
)
from tests.status_event_test_support import NOW
from tests.status_transition_callsite_drivers import ISSUE, _fake
from tests.test_dispatch_gc_policies import policy_case
from tests.verification_contract_test_support import (
    ContractViolation,
    pinned_defect,
    require,
)

P = StatusLabel.IN_PROGRESS
Q = StatusLabel.QUEUED
H = StatusLabel.BLOCKED_HUMAN_REVIEW
#: Default `max_task_reclaims` (#1219 budget table).
MAX_RECLAIMS = 3
#: Default early-death / review-timeout backoff base (`retry_policy`).
BACKOFF = 60
RESUMED = NOW + 5
#: Production defects found by this harness, split out with their counterexamples.
RECLAIM_RECORD_ISSUE = "#1279"
_PRESERVED = "P3B-PERSISTENT-BUDGET-PRESERVED"
RECOMPUTE_RELAUNCH_ISSUE = "#1280"


def _reclaim(world, stop=None, now=NOW):
    return world.run(lambda state: gc_reclaim_once(world, state, now), stop)


# ---- GC reclaim --------------------------------------------------------------

#: Labels left after a stop at each point and the resumed reclaim, within budget.
#: The requeue path adds queued, removes in-progress, then settles; a resumed run
#: finishes whatever is left, so every point converges to `{queued}`.
_REQUEUE_LABELS = dict.fromkeys(STOP_POINTS, frozenset({Q}))

#: Over budget the escalation settles right after adding blocked-human-review
#: (`apply_human_review_escalation(on_label_applied=...)`). Once the label is on
#: the Issue, a resumed GC treats the task as already escalated: it settles
#: without counting or relabelling, and the stale in-progress label is left to
#: status repair (#1218), as the model's restart predicts.
_ESCALATION_LABELS = {
    "reserve-save:before": frozenset({H}),
    "reserve-save:after": frozenset({H}),
    "add:before": frozenset({H}),
    "add:after": frozenset({P, H}),
    "settle-save:before": frozenset({P, H}),
    "settle-save:after": frozenset({P, H}),
    "remove:before": frozenset({P, H}),
    "remove:after": frozenset({H}),
}


class TestGcReclaimResume:
    def test_uninterrupted_reclaim_counts_once_and_settles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = reclaim_world(tmp_path, monkeypatch)
        assert _reclaim(world)
        assert world.retries().reclaim == ReclaimState(count=1, pending=False)
        assert world.labels == {Q}
        assert world.active_released() and world.escalations() == 0

    @pytest.mark.parametrize("stop", STOP_POINTS)
    def test_within_budget_resume_reuses_the_reservation(
        self, stop: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = reclaim_world(tmp_path, monkeypatch)
        assert not _reclaim(world, stop), f"{stop} was not reached"
        assert _reclaim(world, now=RESUMED)
        assert world.retries().reclaim == ReclaimState(count=1, pending=False)
        assert world.labels == _REQUEUE_LABELS[stop]
        assert world.active_released() and world.escalations() == 0

    @pytest.mark.parametrize("stop", STOP_POINTS)
    def test_over_budget_resume_escalates_exactly_once(
        self, stop: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prior = RetryStates(reclaim=ReclaimState(count=MAX_RECLAIMS))
        world = reclaim_world(tmp_path, monkeypatch, retries=prior)
        assert not _reclaim(world, stop), f"{stop} was not reached"
        assert _reclaim(world, now=RESUMED)
        expected = ReclaimState(count=MAX_RECLAIMS + 1, pending=False)
        assert world.retries().reclaim == expected
        assert world.labels == _ESCALATION_LABELS[stop]
        assert world.active_released() and world.escalations() == 1

    def test_repeated_stops_never_consume_a_second_slot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = reclaim_world(tmp_path, monkeypatch)
        for stop in ("reserve-save:after", "add:after", "remove:after"):
            assert not _reclaim(world, stop)
            assert world.retries().reclaim == ReclaimState(count=1, pending=True)
        assert _reclaim(world, now=RESUMED)
        assert world.retries().reclaim == ReclaimState(count=1, pending=False)


# ---- GC early death / review timeout ----------------------------------------

#: First backoff requeue at `NOW`: one slot, `retry_at = now + base * 2**0`.
_FIRST_RETRY = BackoffState(count=1, retry_at=NOW + BACKOFF, pending=False)
#: A stop before the reservation reached disk loses it; the resumed run plans
#: again from the persisted state at its own clock.
_REPLANNED_RETRY = BackoffState(count=1, retry_at=RESUMED + BACKOFF, pending=False)

#: The backoff requeue settles in the label-added callback, before removing
#: in-progress (`_publish_requeue(on_label_added=...)`). After the settle save the
#: ledger no longer holds the execution, so nothing resumes it and the stale
#: in-progress label is left to status repair (#1218).
_BACKOFF_LABELS = {
    "reserve-save:before": frozenset({Q}),
    "reserve-save:after": frozenset({Q}),
    "add:before": frozenset({Q}),
    "add:after": frozenset({Q}),
    "settle-save:before": frozenset({Q}),
    "settle-save:after": frozenset({P, Q}),
    "remove:before": frozenset({P, Q}),
    "remove:after": frozenset({Q}),
}


def _backoff_field(kind: str, retries: RetryStates) -> BackoffState:
    return retries.early_death if kind == "early_death" else retries.review_timeout


@pytest.mark.parametrize("kind", ["early_death", "review_timeout"])
class TestGcBackoffResume:
    def test_uninterrupted_requeue_reserves_one_slot(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = backoff_world(tmp_path, monkeypatch)
        assert world.run(lambda s: gc_backoff_once(world, s, kind), None)
        assert _backoff_field(kind, world.retries()) == _FIRST_RETRY
        assert world.labels == {Q} and world.active_released()

    @pytest.mark.parametrize("stop", STOP_POINTS)
    def test_resume_keeps_the_persisted_reservation_and_retry_at(
        self, kind: str, stop: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = backoff_world(tmp_path, monkeypatch)
        assert not world.run(lambda s: gc_backoff_once(world, s, kind), stop), stop
        assert world.run(lambda s: gc_backoff_once(world, s, kind, RESUMED), None)
        expected = _REPLANNED_RETRY if stop == "reserve-save:before" else _FIRST_RETRY
        assert _backoff_field(kind, world.retries()) == expected
        assert world.labels == _BACKOFF_LABELS[stop]
        assert world.active_released()


# ---- review-timeout completion policy ---------------------------------------

_POLICY_NOW, _POLICY_RESUMED = 100.0, 200.0
#: `policy_case` starts from `status:blocked` with a review-timeout Outcome.
_POLICY_RETRY = (1, _POLICY_NOW + BACKOFF)
_POLICY_REPLANNED = (1, _POLICY_RESUMED + BACKOFF)


def _retry_comments(world: PolicyWorld) -> int:
    marker = "AIレビュー待機タイムアウトのため自動再投入します"
    return sum(marker in comment["body"] for comment in world.comments)


class TestReviewTimeoutPolicyResume:
    @pytest.mark.parametrize("stop", STOP_POINTS)
    def test_resume_reuses_the_operation_reservation(
        self, stop: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = PolicyWorld.create(tmp_path, monkeypatch)
        assert not world.run(stop, _POLICY_NOW), f"{stop} was not reached"
        assert world.run(None, _POLICY_RESUMED)
        retry = world.retry()
        assert retry is not None
        expected = _POLICY_REPLANNED if stop == "reserve-save:before" else _POLICY_RETRY
        assert (retry.review_timeout_retry_count, retry.review_timeout_retry_at) == (
            expected
        )
        assert not retry.review_timeout_retry_pending
        assert world.labels == [Q]
        assert _retry_comments(world) == 1


# ---- footprint-deviation recompute (Issue-body recovery counters) ------------


def _recompute_world(tmp_path: Path) -> tuple[FakeForge, Any, Any]:
    forge = FakeForge()
    forge.issues[ISSUE] = make_issue(ISSUE, labels=(P,))
    config = make_test_dispatcher_config(tmp_path, forge=forge, apply=True)
    return forge, config, make_test_task(ISSUE, status_labels=(P,))


def _deviate(active: Any, config: Any, task: Any) -> str:
    deviated = ["src/deviated.py"]
    decision = rebase._decide_footprint_deviation_outcome(
        active, deviated, {ISSUE: task}, config, build_legacy_dag_inputs((task,))
    )
    rebase._apply_footprint_deviation_outcome(active, deviated, decision, {}, config)
    return decision.action


def _body_counters(forge: FakeForge) -> tuple[int, bool]:
    return recovery_counters_from_body(forge.issues[ISSUE].body)


#: Default `max_recompute_retries` = 2: two recomputations, then force-serial
#: once, then no further change (#1219 budget table).
_RECOMPUTE_STEPS = (
    ("recomputed", (1, False)),
    ("recomputed", (2, False)),
    ("forced_serial", (2, True)),
    ("already_forced_serial", (2, True)),
)


class TestRecomputeBudget:
    def test_budget_boundary_is_persisted_to_the_issue_body(
        self, tmp_path: Path
    ) -> None:
        forge, config, task = _recompute_world(tmp_path)
        active = make_test_active_worktree(ISSUE, pid=None)
        for action, counters in _RECOMPUTE_STEPS:
            assert _deviate(active, config, task) == action
            assert _body_counters(forge) == counters
        assert StatusLabel.FORCE_SERIAL in forge.get_issue_labels(ISSUE)

    def test_ledger_loss_restores_the_budget_from_the_issue_body(
        self, tmp_path: Path
    ) -> None:
        forge, config, task = _recompute_world(tmp_path)
        active = make_test_active_worktree(ISSUE, pid=None)
        _deviate(active, config, task)
        _deviate(active, config, task)
        assert _body_counters(forge) == (2, False)
        issue = forge.get_issue(ISSUE)
        assert issue is not None
        count, serial = recovery._recovery_counters_for_issue(issue)
        restored = make_test_active_worktree(
            ISSUE, pid=None, recompute_count=count, forced_serial=serial
        )
        assert _deviate(restored, config, task) == "forced_serial"

    def test_stop_after_the_body_write_does_not_consume_twice(
        self, tmp_path: Path
    ) -> None:
        forge, config, task = _recompute_world(tmp_path)
        written = forge.update_issue_body

        def stop_after_write(issue: int | str, body: str) -> None:
            written(issue, body)
            raise Crash("body-save:after")

        setattr(forge, "update_issue_body", stop_after_write)  # noqa: B010
        # The ledger copy of the execution is rebuilt from disk (count 0).
        with pytest.raises(Crash):
            _deviate(make_test_active_worktree(ISSUE, pid=None), config, task)
        setattr(forge, "update_issue_body", written)  # noqa: B010
        assert _deviate(make_test_active_worktree(ISSUE, pid=None), config, task)
        assert _body_counters(forge) == (1, False)

    @pytest.mark.xfail(
        reason=f"production bug {RECOMPUTE_RELAUNCH_ISSUE}",
        strict=True,
        raises=ContractViolation,
    )
    def test_relaunch_keeps_the_persisted_recompute_budget(
        self, tmp_path: Path
    ) -> None:
        forge, config, task = _recompute_world(tmp_path)
        active = make_test_active_worktree(ISSUE, pid=None)
        for _ in range(3):
            _deviate(active, config, task)
        assert _body_counters(forge) == (2, True)
        relaunched = _fresh_launch(forge, config)
        with pinned_defect("P3B-PERSISTENT-BUDGET-PRESERVED"):
            result = _deviate(relaunched, config, task)
            require(_PRESERVED, result == "already_forced_serial", result)
            require(_PRESERVED, _body_counters(forge) == (2, True), result)


def _fresh_launch(forge: FakeForge, config: Any) -> Any:
    """The active record a normal launch builds after a requeue."""
    task = make_test_task(ISSUE, status_labels=tuple(forge.get_issue_labels(ISSUE)))
    reservation = RunState(
        active_worktrees={str(ISSUE): make_test_active_worktree(ISSUE)}
    )
    launched = _fake(
        pid=2,
        dispatch_started_at=NOW,
        external_id=None,
        external_url=None,
        launch_attempt_id="attempt-2",
        branch=f"claude/issue-{ISSUE}-task-a",
        worktree_path="worktrees/w2",
        base_ref="main",
    )
    plan = _fake(execution_selection=None, base_branch_for_state="main")
    return launch._build_active_worktree_from_launch(
        task, plan, launched, reservation, NOW, config
    )


# ---- base-branch-red (Outcome records on the Issue) ---------------------------


def _blocked_outcome(attempt: int, claim: str, head: str) -> dict[str, str]:
    record = OutcomeRecord(
        result=RESULT_BLOCKED,
        issue=ISSUE,
        reason=REASON_BASE_BRANCH_RED,
        attempt=attempt,
        claim_id=claim,
        head_sha=head,
    )
    return {"body": record.render(), "created_at": f"2026-01-0{attempt}T00:00:00Z"}


class TestBaseBranchRedBudget:
    def test_attempts_are_counted_from_issue_comments(self) -> None:
        comments: list[dict[str, str]] = []

        def next_attempt(claim: str, head: str) -> int:
            return calculate_blocked_attempt(
                comments, issue_number=ISSUE, claim_id=claim, head_sha=head
            )

        assert next_attempt("c1", "h1") == 1
        comments.append(_blocked_outcome(1, "c1", "h1"))
        # A resend of the same work attempt reuses its number.
        assert next_attempt("c1", "h1") == 1
        assert next_attempt("c2", "h2") == 2
        comments.append(_blocked_outcome(2, "c2", "h2"))
        # Losing the local ledger does not matter: only the Issue is read.
        assert next_attempt("c3", "h3") == 3

    @pytest.mark.parametrize(
        ("attempt", "labels"),
        [
            (1, {"status:blocked", "ci:base-branch-red"}),
            (2, {"status:blocked", "ci:base-branch-red"}),
            (3, {"status:blocked-human-review"}),
        ],
    )
    def test_the_third_attempt_escalates(
        self, attempt: int, labels: set[str], tmp_path: Path
    ) -> None:
        state, config, _, observed, _ = policy_case(
            tmp_path, reason="base-branch-red", attempt=attempt
        )
        policies.process_completion_policies(state, config, now=_POLICY_NOW)
        assert set(observed) == labels


# ---- defects pinned for their own Issues --------------------------------------


@pytest.mark.xfail(
    reason=f"production bug {RECLAIM_RECORD_ISSUE}",
    strict=True,
    raises=ContractViolation,
)
def test_reclaim_keeps_backoff_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reclaim budget is a separate slot from early death / review timeout."""
    backoff = RetryStates(
        early_death=BackoffState(count=2, retry_at=5.0),
        review_timeout=BackoffState(count=1, retry_at=7.0),
    )
    world = reclaim_world(tmp_path, monkeypatch, retries=backoff)
    assert _reclaim(world)
    with pinned_defect("P3B-PERSISTENT-BUDGET-PRESERVED"):
        retries = world.retries()
        require(
            _PRESERVED,
            retries
            == RetryStates(
                reclaim=ReclaimState(count=1),
                early_death=backoff.early_death,
                review_timeout=backoff.review_timeout,
            ),
            retries,
        )
