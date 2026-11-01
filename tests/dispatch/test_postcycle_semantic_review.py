"""Layer-2 review/fix loop advanced from the Dispatcher post-cycle (#1362)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from orchestune.branch_naming import build_task_branch_name
from orchestune.dispatch import dispatcher
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.postcycle_semantic_review import (
    SemanticClaimClient,
    SemanticReviewCycleRunner,
    SemanticReviewOperations,
    advance_pending_semantic_reviews,
)
from orchestune.dispatch.pre_scheduling_integration import (
    _INTEGRATOR_SUCCESS_STATUSES,
    _advance_pending_semantic_reviews,
    _build_integrator_config,
)
from orchestune.dispatch.result import PhaseStatus
from orchestune.dispatch.semantic_review_adapters import (
    SemanticOperationResult,
    SemanticReviewPreflightResult,
    escalate_semantic_review_to_human,
    trigger_semantic_review_round,
)
from orchestune.integrator.semantic_claim import (
    ClaimResult,
    ClaimStatus,
    PushResult,
    PushStatus,
    SemanticClaim,
)
from orchestune.integrator.types import IntegrationStatus
from orchestune.models import PrRecord
from orchestune.review.semantic_gate import (
    ChildSourceRef,
    IntegrationAttemptIdentity,
    SemanticCiConclusion,
    SemanticCiObservation,
    SemanticFixResultV1,
    SemanticOperationKind,
    SemanticOperationStatus,
    SemanticReviewHumanOverrideV1,
    SemanticReviewPhase,
    SemanticReviewRetryV1,
    SemanticReviewStateV1,
    format_semantic_fix_result_marker,
    format_semantic_review_override_marker,
    format_semantic_review_retry_marker,
    format_semantic_review_state_marker,
    parse_semantic_review_state_marker,
)

P = SemanticReviewPhase
PARENT = 1033
PR = 500
BASE = "b" * 40
HEAD = "1" * 40
FIXED = "2" * 40
CHILDREN = (
    ChildSourceRef(1101, "task-a", "a" * 40),
    ChildSourceRef(1102, "task-b", "c" * 40),
)
IDENTITY = IntegrationAttemptIdentity.create(PARENT, BASE, CHILDREN)
ATTEMPT = IDENTITY.attempt_id
SELF = "orchestune[bot]"
REVIEWER = "chatgpt-codex-connector[bot]"
T0 = datetime(2026, 10, 22, tzinfo=UTC)


def _state_of(body: str) -> SemanticReviewStateV1 | None:
    try:
        return parse_semantic_review_state_marker(body)
    except ValueError:
        return None


class World:
    """GitHub, remote refs, claim store and job adapters of one parent."""

    def __init__(self, tmp_path: Path, **config: Any) -> None:
        self.tmp_path = tmp_path
        self.clock = T0
        self.comments: dict[int, list[dict[str, Any]]] = {PR: [], PARENT: []}
        self.labels: dict[int, set[str]] = {
            PARENT: {"status:in-progress"},
            1101: {"status:done"},
            1102: {"status:done"},
        }
        self.reviews: list[dict[str, Any]] = []
        self.pr_head = HEAD
        self.refs = {f"parent/issue-{PARENT}": BASE} | {
            build_task_branch_name(c.issue_number, c.subtask_id): c.source_sha
            for c in CHILDREN
        }
        self.claim: SemanticClaim | None = None
        self.lose_claim = False
        self.renewed_at: list[datetime] = []
        self.fail_save_after = -1
        self.dispatched: list[str] = []
        self.polls: dict[str, SemanticOperationResult] = {}
        self.preflight_ok = True
        self.preflight_calls = 0
        self.escalations: list[str] = []
        self.deleted: list[str] = []
        self.cleaned_worktrees: list[Path] = []
        self.ci: list[tuple[str, str]] = []
        self.ci_result: SemanticCiObservation | None | str = "success"
        self.ops_overrides: dict[str, Any] = {}
        self.claim_overrides: dict[str, Any] = {}
        self.config = DispatcherConfig(
            parent_issue_number=PARENT,
            forge=self.forge(),
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            dispatch_target=cast(Any, SimpleNamespace(target_name="claude-cli")),
            **({"apply": True} | config),
        )

    # -- forge ---------------------------------------------------------------
    def forge(self) -> MagicMock:
        forge = MagicMock()
        forge.list_open_prs.side_effect = lambda *a, **k: [
            PrRecord(
                number=PR,
                head_ref=IDENTITY.temp_branch,
                changed_files=(),
                base_ref=f"parent/issue-{PARENT}",
                is_cross_repository=False,
                head_sha=self.pr_head,
            )
        ]
        forge.list_comments.side_effect = lambda n: list(self.comments[int(n)])
        forge.list_all_issue_comments.side_effect = lambda n: list(
            self.comments[int(n)]
        )
        forge.add_comment.side_effect = lambda n, body: self.post(int(n), body)
        forge.create_issue_comment.side_effect = lambda n, body: self.post(int(n), body)
        forge.get_authenticated_user.return_value = SELF
        forge.list_notice_trusted_authors.return_value = frozenset({SELF})
        forge.get_actor_permission.side_effect = lambda u: (
            "write" if u.casefold() == "alice" else "read"
        )
        forge.get_issue_labels.side_effect = lambda n: tuple(self.labels[int(n)])
        forge.add_label.side_effect = lambda n, label: self.labels[int(n)].add(label)
        forge.remove_label.side_effect = lambda n, label: self.labels[int(n)].discard(
            label
        )
        forge.list_pull_request_reviews.side_effect = lambda n: list(self.reviews)
        forge.list_pull_request_review_comments.return_value = []
        return forge

    def post(self, number: int, body: str, author: str = SELF) -> dict[str, Any]:
        if author == SELF and _state_of(body) is not None:
            if self.fail_save_after == 0:
                raise RuntimeError("state save failed")
            self.fail_save_after -= 1
        self.clock += timedelta(seconds=1)
        comment = {
            "id": 1000 + sum(len(c) for c in self.comments.values()),
            "body": body,
            "user": {"login": author},
            "created_at": self.clock.isoformat().replace("+00:00", "Z"),
        }
        self.comments[number].append(comment)
        return comment

    def states(self) -> list[SemanticReviewStateV1]:
        parsed = [_state_of(c["body"]) for c in self.comments[PR]]
        return [state for state in parsed if state is not None]

    @property
    def state(self) -> SemanticReviewStateV1:
        return self.states()[-1]

    def seed(self, **changes: Any) -> SemanticReviewStateV1:
        state = SemanticReviewStateV1(
            ATTEMPT,
            PARENT,
            PR,
            P.NOT_STARTED,
            0,
            HEAD,
            BASE,
            CHILDREN,
            ci_verified_sha=HEAD,
        )
        state = replace(state, **changes)
        self.post(PR, format_semantic_review_state_marker(state))
        return state

    # -- claim, ops ------------------------------------------------------------
    def claims(self) -> SemanticClaimClient:
        def acquire(
            _root: Any, *, attempt_id: str, holder: str, now: datetime, **_: Any
        ) -> ClaimResult:
            current = self.claim
            if current and current.holder != holder and not current.is_expired(now):
                return ClaimResult(ClaimStatus.HELD_BY_OTHER, current)
            generation = (current.generation + 1) if current else 1
            self.claim = SemanticClaim(
                PARENT,
                attempt_id,
                holder,
                generation,
                now + timedelta(minutes=5),
                "0" * 40,
            )
            return ClaimResult(ClaimStatus.ACQUIRED, self.claim)

        def renew(
            _root: Any, claim: SemanticClaim, *, now: datetime, **_: Any
        ) -> PushResult:
            self.renewed_at.append(now)
            if self.lose_claim:
                return PushResult(PushStatus.REJECTED, None, "claim_lost")
            return PushResult(PushStatus.PUSHED, claim)

        def release(_root: Any, claim: SemanticClaim, *, now: datetime) -> PushResult:
            self.claim = replace(claim, expires_at=now)
            return PushResult(PushStatus.PUSHED, self.claim)

        def promote(
            _root: Any,
            claim: SemanticClaim,
            *,
            expected_head_sha: str,
            fix_branch: str,
            **_: Any,
        ) -> PushResult:
            assert self.pr_head == expected_head_sha
            self.pr_head = self.refs[fix_branch]
            return PushResult(PushStatus.PUSHED, claim)

        return SemanticClaimClient(
            acquire=acquire,
            renew=renew,
            release=release,
            promote=promote,
            delete_fix_branches=lambda _root, attempt: self.deleted.append(attempt),
        )

    def operations(self) -> SemanticReviewOperations:
        def preflight(*_: Any, **__: Any) -> SemanticReviewPreflightResult:
            self.preflight_calls += 1
            if self.preflight_ok:
                return SemanticReviewPreflightResult(True, "codex", "pr-comment")
            return SemanticReviewPreflightResult(
                False, None, "pr-comment", "fix_provider_unavailable", "x", "key-1"
            )

        def dispatch(
            _target: Any, *, operation_id: str, expected_head_sha: str, **_: Any
        ) -> SemanticOperationResult:
            self.dispatched.append(operation_id)
            return SemanticOperationResult(
                operation_id,
                SemanticOperationStatus.DISPATCHING,
                "h-1",
                input_sha=expected_head_sha,
            )

        def poll(*_: Any, operation_id: str, **__: Any) -> SemanticOperationResult:
            return self.polls.get(
                operation_id,
                SemanticOperationResult(operation_id, SemanticOperationStatus.RUNNING),
            )

        def escalate(forge: Any, **kwargs: Any) -> None:
            self.escalations.append(kwargs["reason"])
            escalate_semantic_review_to_human(forge, **kwargs)

        def cleanup_worktree(path: Path, repo_root: Any = None, **_k: Any) -> bool:
            self.cleaned_worktrees.append(path)
            return True

        return SemanticReviewOperations(
            preflight=preflight,
            trigger=trigger_semantic_review_round,
            dispatch_fix=dispatch,
            poll=poll,
            escalate=escalate,
            cleanup_fix_worktree=self.ops_overrides.get(
                "cleanup_fix_worktree", cleanup_worktree
            ),
        )

    def verify_ci(self, branch: str, sha: str) -> SemanticCiObservation | None:
        self.ci.append((branch, sha))
        self.clock += timedelta(minutes=20)  # a long CI run
        if self.ci_result == "success":
            return SemanticCiObservation(sha, SemanticCiConclusion.SUCCESS)
        if self.ci_result == "failure":
            return SemanticCiObservation(sha, SemanticCiConclusion.FAILURE)
        return None

    def cycle(self, *, worker: str = "w1", minutes: float = 1) -> dict[str, Any]:
        self.clock += timedelta(minutes=minutes)
        runner = SemanticReviewCycleRunner(
            self.config,
            forge=self.config.resolved_forge,
            repository_root=Path("."),
            now=self.clock,
            worker_id=worker,
            verify_ci_fn=self.verify_ci,
            operations=replace(self.operations(), **self.ops_overrides),
            claims=replace(self.claims(), **self.claim_overrides),
            remote_head_fn=self.refs.get,
            clock=lambda: self.clock,
        )
        reports = runner.run()
        assert len(reports) == 1
        return reports[0].to_dict()

    # -- external actors -------------------------------------------------------
    def review(self, body: str = "Found a cross-task defect in a.py.") -> int:
        return int(self.post(PR, body, REVIEWER)["id"])

    def finish_job(
        self,
        *,
        output: str | None,
        statuses: tuple[str, ...] = ("resolved",),
        sources: list[str] | None = None,
    ) -> None:
        state = self.state
        assert state.operation_id is not None
        rows = "".join(
            f"  - {{source: '{src}', location: a.py, judgment: adopt, status: {status},"
            " basis: fixed, evidence: tests}\n"
            for src, status in zip(sources or self.sources(), statuses, strict=False)
        )
        table = self.post(
            PR,
            f"```orchestune-review-judgments\nround: {state.round}\nfindings:\n{rows}```",
        )
        result = SemanticFixResultV1(
            ATTEMPT,
            state.operation_id,
            state.head_sha,
            output,
            table["id"],
            "completed",
        )
        self.post(PR, format_semantic_fix_result_marker(result))
        if output is not None:
            from orchestune.review.semantic_gate import semantic_fix_output_branch

            self.refs[semantic_fix_output_branch(state.operation_id)] = output
        self.polls[state.operation_id] = SemanticOperationResult(
            state.operation_id,
            SemanticOperationStatus.COMPLETED,
            input_sha=state.head_sha,
            output_sha=output,
            completed=True,
        )

    def sources(self) -> list[str]:
        return [
            f"issue_comment:{c['id']}"
            for c in self.comments[PR]
            if c["user"]["login"] == REVIEWER
        ][-1:]

    def triggers(self) -> list[str]:
        return [
            c["body"]
            for c in self.comments[PR]
            if "orchestune:review-trigger" in c["body"]
        ]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _phases(report: dict[str, Any]) -> tuple[str | None, str | None]:
    return report["previous_phase"], report["next_phase"]


class TestReviewFixLoop:
    def test_no_attempt_reports_no_pending_attempt(self, world: World) -> None:
        cast(MagicMock, world.config.resolved_forge).list_open_prs.side_effect = (
            lambda *a, **k: []
        )
        assert world.cycle()["status"] == "no_pending_attempt"

    def test_findings_fix_promotion_ci_and_re_review_reach_passed(
        self, world: World
    ) -> None:
        world.seed()
        report = world.cycle()
        assert _phases(report) == ("not_started", "review_pending")
        assert len(world.triggers()) == 1 and f"head {HEAD}" in world.triggers()[0]
        assert world.cycle()["status"] == "held"  # no review yet

        world.review()
        report = world.cycle()
        assert _phases(report) == ("review_pending", "fix_in_progress")
        assert world.dispatched == [world.state.operation_id]
        assert world.state.operation_status is SemanticOperationStatus.DISPATCHING

        world.finish_job(output=FIXED)
        report = world.cycle()
        assert _phases(report) == ("fix_in_progress", "ci_verification")
        assert world.pr_head == FIXED and world.state.head_sha == FIXED
        assert world.state.reviewed_sha is None and world.state.ci_verified_sha is None
        assert world.ci == []  # CI waits for the next cycle (one phase per cycle)

        report = world.cycle()
        assert _phases(report) == ("ci_verification", "review_pending")
        assert world.ci == [(IDENTITY.temp_branch, FIXED)]
        round_two = world.triggers()[-1]
        assert "orchestune:review-round 2" in round_two and FIXED in round_two
        assert "orchestune-review-judgments" in round_two

        world.review("No further findings.")
        assert _phases(world.cycle()) == ("review_pending", "fix_in_progress")
        world.finish_job(output=None)
        report = world.cycle()
        assert _phases(report) == ("fix_in_progress", "passed")
        assert world.state.ci_verified_sha == FIXED == world.state.reviewed_sha
        assert world.refs[f"parent/issue-{PARENT}"] == BASE  # never finalized here
        assert len(world.dispatched) == 2

        before = len(world.comments[PR])
        assert world.cycle()["status"] == "no_pending_attempt"  # passed is final here
        assert len(world.comments[PR]) == before

    def test_unchanged_head_with_settled_table_needs_ci_of_that_head(
        self, world: World
    ) -> None:
        world.seed(phase=P.NOT_STARTED, ci_verified_sha=None)
        world.cycle()
        world.review()
        world.cycle()
        world.finish_job(output=None)
        assert _phases(world.cycle()) == ("fix_in_progress", "ci_verification")
        assert _phases(world.cycle()) == ("ci_verification", "passed")

    def test_unresolved_judgments_retry_then_escalate(self, world: World) -> None:
        world.config.max_semantic_fix_retries = 1
        world.seed()
        world.cycle()
        world.review()
        world.cycle()
        world.finish_job(output=None, statuses=("unresolved",))
        report = world.cycle()
        assert report["details"]["reason"] == "unresolved_after_fix"
        assert world.state.fix_retries == 1 and len(world.dispatched) == 2
        world.finish_job(output=None, statuses=("unresolved",))
        report = world.cycle()
        assert report["next_phase"] == "escalated"
        assert world.state.escalation_reason == "fix_retry_limit"
        assert "status:blocked-human-review" in world.labels[PARENT]

    def test_deferred_judgment_never_passes(self, world: World) -> None:
        world.seed()
        world.cycle()
        world.review()
        world.cycle()
        state = world.state
        rows = f"  - {{source: '{world.sources()[0]}', location: a, judgment: needs_information, status: deferred, basis: b, evidence: e}}\n"
        table = world.post(
            PR, f"```orchestune-review-judgments\nround: 1\nfindings:\n{rows}```"
        )
        world.post(
            PR,
            format_semantic_fix_result_marker(
                SemanticFixResultV1(
                    ATTEMPT,
                    str(state.operation_id),
                    HEAD,
                    None,
                    table["id"],
                    "completed",
                )
            ),
        )
        world.polls[str(state.operation_id)] = SemanticOperationResult(
            str(state.operation_id),
            SemanticOperationStatus.COMPLETED,
            input_sha=HEAD,
            completed=True,
        )
        assert world.cycle()["next_phase"] == "escalated"
        assert world.state.escalation_reason == "deferred_judgments"

    def test_untrusted_trigger_does_not_move_the_review_window(
        self, world: World
    ) -> None:
        world.seed()
        world.cycle()
        world.review()
        world.post(PR, world.triggers()[-1], "mallory")  # a copied request
        assert _phases(world.cycle()) == ("review_pending", "fix_in_progress")

    def test_unknown_comment_trust_surfaces_as_inputs_unknown(
        self, world: World
    ) -> None:
        world.seed()
        world.cycle()
        cast(Any, world.config.forge).list_notice_trusted_authors.return_value = None
        world.post(PR, world.triggers()[-1], "someone")
        assert world.cycle()["status"] == "inputs_unknown"

    def test_review_before_the_trigger_or_of_another_head_is_not_evidence(
        self, world: World
    ) -> None:
        world.review("an old review")
        world.seed(
            phase=P.REVIEW_PENDING,
            round=1,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        world.post(
            PR,
            f"@codex review\n<!-- orchestune:review-trigger bot=codex -->\n<!-- orchestune:review-round 1 -->\n<!-- orchestune:review-head {FIXED} -->",
        )
        world.review("review of another head")
        assert world.cycle()["status"] == "held"
        assert world.dispatched == []


class TestLimitsAndEscalation:
    def test_unacknowledged_trigger_escalates_once(self, world: World) -> None:
        world.seed()
        world.cycle()
        report = world.cycle(minutes=31)
        assert report["next_phase"] == "escalated"
        assert world.state.escalation_reason == "review_not_acknowledged"
        assert "status:blocked-human-review" in world.labels[PARENT]
        parent_comments = len(world.comments[PARENT])
        assert world.cycle()["status"] == "held"
        assert world.escalations == ["review_not_acknowledged"]
        assert len(world.comments[PARENT]) == parent_comments

    def test_interrupted_escalation_is_completed_once(self, world: World) -> None:
        world.seed()
        world.cycle()
        world.cycle(minutes=31)
        world.labels[PARENT].discard("status:blocked-human-review")  # label failed
        assert world.cycle()["status"] == "advanced"
        assert "status:blocked-human-review" in world.labels[PARENT]
        parent_comments = len(world.comments[PARENT])
        assert world.cycle()["status"] == "held"
        assert len(world.comments[PARENT]) == parent_comments

    def test_max_rounds_escalates(self, world: World) -> None:
        world.seed(
            phase=P.CI_VERIFICATION,
            round=3,
            ci_verified_sha=None,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        assert world.cycle()["next_phase"] == "escalated"
        assert world.state.escalation_reason == "max_rounds"

    def test_attempt_deadline_is_not_extended(self, world: World) -> None:
        world.seed(
            phase=P.REVIEW_PENDING,
            round=1,
            attempt_started_at=T0.isoformat(),
            phase_started_at=(T0 + timedelta(minutes=110)).isoformat(),
        )
        world.post(
            PR,
            f"@codex review\n<!-- orchestune:review-trigger bot=codex -->\n<!-- orchestune:review-round 1 -->\n<!-- orchestune:review-head {HEAD} -->",
        )
        world.review()
        report = world.cycle(minutes=121)
        assert report["next_phase"] == "escalated"
        assert world.state.escalation_reason == "attempt_timeout"

    def test_ci_failure_retries_the_fix_job(self, world: World) -> None:
        world.seed(
            phase=P.CI_VERIFICATION,
            round=1,
            ci_verified_sha=None,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        world.ci_result = "failure"
        report = world.cycle()
        assert _phases(report) == ("ci_verification", "fix_in_progress")
        assert world.state.fix_retries == 1 and len(world.dispatched) == 1

    def test_claim_is_renewed_at_the_time_of_each_write(self, world: World) -> None:
        world.seed(
            phase=P.CI_VERIFICATION,
            round=1,
            ci_verified_sha=None,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        world.cycle()
        assert world.renewed_at[-1] - world.renewed_at[0] >= timedelta(minutes=20)

    def test_head_moving_around_ci_discards_the_result(self, world: World) -> None:
        world.seed(
            phase=P.CI_VERIFICATION,
            round=1,
            ci_verified_sha=None,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        world.ci_result = None
        before = len(world.states())
        assert world.cycle()["status"] == "ci_discarded"
        assert len(world.states()) == before


class TestClaimAndRecovery:
    def test_claim_held_by_another_worker_writes_nothing(self, world: World) -> None:
        world.seed()
        world.claim = SemanticClaim(
            PARENT, ATTEMPT, "w2", 1, T0 + timedelta(hours=1), "0" * 40
        )
        before = len(world.comments[PR])
        assert world.cycle()["status"] == "claimed_by_other"
        assert len(world.comments[PR]) == before

    def test_lost_claim_stops_before_any_write(self, world: World) -> None:
        world.seed()
        world.lose_claim = True
        before = len(world.comments[PR])
        assert world.cycle()["status"] == "claim_lost"
        assert len(world.comments[PR]) == before and world.dispatched == []

    def test_no_apply_only_plans(self, world: World) -> None:
        world.config.apply = False
        world.seed()
        before = len(world.comments[PR])
        report = world.cycle()
        assert (
            report["status"] == "planned" and report["next_phase"] == "review_pending"
        )
        assert len(world.comments[PR]) == before and world.claim is None
        assert world.preflight_calls == 0  # its claim-backend probe pushes refs

    def test_state_saved_by_another_worker_meanwhile_is_not_overwritten(
        self, world: World
    ) -> None:
        world.seed()
        acquire = world.claims().acquire

        def racing_acquire(*args: Any, **kwargs: Any) -> ClaimResult:
            world.seed(phase=P.REVIEW_PENDING, round=1, state_revision=1)
            return acquire(*args, **kwargs)

        world.claim_overrides["acquire"] = racing_acquire
        assert world.cycle()["status"] == "state_changed"
        assert [s.state_revision for s in world.states()] == [0, 1]
        assert world.triggers() == [] and world.claim is not None
        assert world.claim.is_expired(world.clock)  # released

    def test_unknown_claim_outcome_writes_nothing(self, world: World) -> None:
        world.seed()
        world.claim_overrides["acquire"] = lambda *a, **k: ClaimResult(
            ClaimStatus.UNKNOWN
        )
        before = len(world.comments[PR])
        assert world.cycle()["status"] == "claim_unknown"
        assert len(world.comments[PR]) == before

    def test_unknown_trigger_author_does_not_advance(self, world: World) -> None:
        world.seed()
        world.ops_overrides["trigger"] = lambda *a, **k: SemanticOperationResult(
            "op", SemanticOperationStatus.INDETERMINATE
        )
        assert world.cycle()["status"] == "trigger_indeterminate"
        assert world.state.phase is P.NOT_STARTED

    def test_fix_output_not_descending_from_the_head_escalates(
        self, world: World
    ) -> None:
        world.seed()
        world.cycle()
        world.review()
        world.cycle()
        world.finish_job(output=FIXED)
        world.claim_overrides["promote"] = lambda *a, **k: PushResult(
            PushStatus.REJECTED, None, "fix_not_descendant"
        )
        assert world.cycle()["next_phase"] == "escalated"
        assert world.state.escalation_reason == "fix_output_not_descendant"
        assert world.pr_head == HEAD

    def test_crash_after_launch_never_dispatches_twice(self, world: World) -> None:
        world.seed()
        world.cycle()
        world.review()
        world.fail_save_after = 1  # the reservation saves, the launch record does not
        assert world.cycle()["status"] == "error"
        assert world.state.operation_status is SemanticOperationStatus.RESERVED
        assert len(world.dispatched) == 1
        world.fail_save_after = -1
        assert world.cycle()["status"] == "held"
        assert len(world.dispatched) == 1

    def test_unknown_launch_escalates_at_the_deadline_without_relaunch(
        self, world: World
    ) -> None:
        world.seed(
            phase=P.FIX_IN_PROGRESS,
            round=1,
            operation_id=f"{ATTEMPT[:12]}-g0-r1-fix-0",
            operation_kind=SemanticOperationKind.FIX,
            operation_status=SemanticOperationStatus.INDETERMINATE,
            fix_input_sha=HEAD,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        assert world.cycle()["status"] == "held"
        assert world.cycle(minutes=31)["next_phase"] == "escalated"
        assert world.dispatched == []

    def test_crash_after_promotion_resumes_from_the_moved_head(
        self, world: World
    ) -> None:
        world.seed()
        world.cycle()
        world.review()
        world.cycle()
        world.finish_job(output=FIXED)
        world.pr_head = FIXED  # promoted, then the state save was lost
        report = world.cycle()
        assert _phases(report) == ("fix_in_progress", "ci_verification")
        assert world.state.head_sha == FIXED

    def test_corrupt_latest_state_holds_without_side_effects(
        self, world: World
    ) -> None:
        world.seed(phase=P.PASSED)
        world.post(PR, "<!-- orchestune:semantic-review-state:v1 {broken -->")
        before = len(world.comments[PR])
        assert world.cycle()["status"] == "state_unreadable"
        assert len(world.comments[PR]) == before


class TestInvalidationOverrideAndRetry:
    def test_base_change_invalidates_and_deletes_fix_outputs(
        self, world: World
    ) -> None:
        world.seed(
            phase=P.REVIEW_PENDING,
            round=1,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        world.refs[f"parent/issue-{PARENT}"] = "9" * 40
        assert world.cycle()["next_phase"] == "invalidated"
        assert world.deleted == [ATTEMPT]

    def test_dropped_child_invalidates(self, world: World) -> None:
        world.seed()
        world.labels[1102] = {"status:queued"}
        assert world.cycle()["next_phase"] == "invalidated"

    def test_running_job_of_a_stale_attempt_goes_to_a_human(self, world: World) -> None:
        world.seed(
            phase=P.FIX_IN_PROGRESS,
            round=1,
            operation_id=f"{ATTEMPT[:12]}-g0-r1-fix-0",
            operation_kind=SemanticOperationKind.FIX,
            operation_status=SemanticOperationStatus.DISPATCHING,
            fix_input_sha=HEAD,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        world.refs[build_task_branch_name(1101, "task-a")] = "8" * 40
        report = world.cycle()
        assert report["next_phase"] == "escalated" and world.deleted == []
        assert world.state.escalation_reason == "invalidation_unconfirmed"
        assert world.cycle()["status"] == "held"

    def test_override_skips_even_after_preflight_failure(self, world: World) -> None:
        world.preflight_ok = False
        world.seed()
        assert world.cycle()["next_phase"] == "escalated"
        assert world.escalations == ["fix_provider_unavailable"]
        override = SemanticReviewHumanOverrideV1(ATTEMPT, HEAD, "manual check done")
        world.post(PR, format_semantic_review_override_marker(override), "mallory")
        assert world.cycle()["status"] == "held"  # no write permission
        world.post(PR, format_semantic_review_override_marker(override), "alice")
        calls = world.preflight_calls
        assert world.cycle()["next_phase"] == "skipped_by_human"
        assert world.preflight_calls == calls

    def test_mode_off_skips_with_a_recorded_reason(self, tmp_path: Path) -> None:
        world = World(tmp_path, semantic_review_mode="off")
        world.seed()
        report = world.cycle()
        assert report["next_phase"] == "skipped_by_human"
        assert report["details"]["reason"] == "mode_off"
        assert world.preflight_calls == 0
        assert any("mode_off" in c["body"] for c in world.comments[PR])

    def test_retry_keeps_consumed_limits(self, world: World) -> None:
        started = T0.isoformat()
        world.seed(
            phase=P.ESCALATED,
            escalated_from=P.FIX_IN_PROGRESS,
            escalation_reason="fix_in_progress_timeout",
            round=2,
            fix_retries=1,
            attempt_started_at=started,
            phase_started_at=started,
        )
        request = SemanticReviewRetryV1(
            ATTEMPT, HEAD, "req-1", "retry", "provider fixed"
        )
        world.post(PR, format_semantic_review_retry_marker(request), "alice")
        report = world.cycle()
        assert _phases(report) == ("escalated", "fix_in_progress")
        state = world.state
        assert (state.round, state.fix_retries, state.restart_count) == (2, 1, 1)
        assert state.attempt_started_at == started and len(world.dispatched) == 1
        assert world.preflight_calls == 1

    def test_retry_resets_limits_only_when_requested(self, world: World) -> None:
        world.seed(
            phase=P.ESCALATED,
            escalated_from=P.CI_VERIFICATION,
            escalation_reason="max_rounds",
            round=3,
            fix_retries=2,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        request = SemanticReviewRetryV1(
            ATTEMPT, HEAD, "req-2", "retry", "go", reset_limits=True
        )
        world.post(PR, format_semantic_review_retry_marker(request), "alice")
        world.cycle()
        assert (world.state.round, world.state.fix_retries) == (1, 0)
        assert world.state.phase is P.REVIEW_PENDING

    def test_reset_round_requests_a_new_review_of_the_same_head(
        self, world: World
    ) -> None:
        world.seed()
        world.cycle()
        world.review("round 1 review of the first run")
        world.seed(
            phase=P.ESCALATED,
            escalated_from=P.CI_VERIFICATION,
            escalation_reason="max_rounds",
            round=3,
            state_revision=5,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        request = SemanticReviewRetryV1(ATTEMPT, HEAD, "r", "retry", "go", True)
        world.post(PR, format_semantic_review_retry_marker(request), "alice")
        world.cycle(minutes=10)
        assert world.state.round == 1 and len(world.triggers()) == 2
        assert world.cycle()["status"] == "held"  # the old review is not evidence
        world.review("fresh review")
        assert world.cycle()["next_phase"] == "fix_in_progress"


class TestPostcycleWiring:
    def test_pending_status_is_success_and_settings_propagate(
        self, tmp_path: Path
    ) -> None:
        assert IntegrationStatus.SEMANTIC_REVIEW_PENDING in _INTEGRATOR_SUCCESS_STATUSES
        world = World(
            tmp_path, semantic_review_mode="advisory", force_integration_review=True
        )
        integrator = _build_integrator_config(world.config, False)
        assert integrator.semantic_review_mode == "advisory"
        assert integrator.force_integration_review is True

    def test_runner_step_precedes_the_integrator_and_ignores_the_legacy_flag(
        self,
    ) -> None:
        steps = dispatcher._pre_scheduling_integration_steps(MagicMock(), False, None)
        names = [name for name, _, _ in steps]
        index = names.index("advance_pending_semantic_reviews")
        assert names[index + 1] == "run_semantic_integrator"
        assert steps[index][2] is True

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("no_pending_attempt", PhaseStatus.SUCCESS),
            ("claimed_by_other", PhaseStatus.SUCCESS),
            ("claim_unknown", PhaseStatus.WARNING),
            ("trigger_indeterminate", PhaseStatus.WARNING),
            ("error", PhaseStatus.RETRYABLE_FAILURE),
            ("promotion_failed", PhaseStatus.RETRYABLE_FAILURE),
        ],
    )
    def test_phase_status(self, status: str, expected: PhaseStatus) -> None:
        report = {"attempts": [{"status": status}]}
        with patch(
            "orchestune.dispatch.pre_scheduling_integration.advance_pending_semantic_reviews",
            return_value=report,
        ):
            assert _advance_pending_semantic_reviews(MagicMock()).status is expected

    def test_entrypoint_reports_each_attempt(self, world: World) -> None:
        world.seed()
        result = advance_pending_semantic_reviews(
            world.config,
            now=T0 + timedelta(minutes=1),
            worker_id="w1",
            verify_ci_fn=world.verify_ci,
            operations=world.operations(),
            claims=world.claims(),
            remote_head_fn=world.refs.get,
        )
        assert result["status"] == "advanced"
        assert result["attempts"][0]["next_phase"] == "review_pending"

    def test_fix_worktree_cleaned_up_on_terminal_status(self, tmp_path: Path) -> None:
        world = World(tmp_path, worktree_root=tmp_path / "worktrees")
        world.seed(
            phase=P.FIX_IN_PROGRESS,
            round=1,
            operation_id="test-op-1",
            operation_status=SemanticOperationStatus.RUNNING,
            ci_verified_sha=None,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        wt_dir = tmp_path / "worktrees" / "semantic-fix-test-op-1"
        wt_dir.mkdir(parents=True, exist_ok=True)
        world.finish_job(output=None)
        world.cycle()
        assert wt_dir in world.cleaned_worktrees
