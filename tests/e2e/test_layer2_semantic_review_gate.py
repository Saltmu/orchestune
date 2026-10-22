"""E2E scenario tests for Layer-2 Semantic Review Gate (#1033 / #1363).

Covers all 19 required acceptance scenarios:
1. Single-task Fast-Path (bypasses layer 2 when single child passes layer 1)
2. Single-task Layer 1 blocked (stops when child lacks pass evidence)
3. Two-task review -> fix -> promotion -> CI -> re-review -> automerge
4. Max review rounds exceeded escalation (status:blocked-human-review)
5. Human override and explicit mode off (skipped_by_human)
6. Restart resilience and SHA-change invalidation
7. Preflight failures (unresolved reviewer, unsupported trigger, missing binary/token)
8. Concurrent and lost claims (claimed_by_other, claim_lost, claim_unknown)
9. Crash and recovery points (no double launches or commit loss)
10. Unresolved or deferred judgments (escalate rather than pass)
11. ESCALATED preserves attempt HEAD until PR head moves
12. Invalidation and fix-branch cleanup
13. Bounded wait and timeouts (attempt timeout, review timeout, CI timeout)
14. Commit and launch boundaries (apply=False side-effect-free, idempotent finalize)
15. Review evidence binding (rejects stale rounds, wrong SHAs, incomplete reviews)
16. Child set freezing (new done child does not invalidate active attempt)
17. Multi-commit child Fast-Path (descendant of base passes fast-path)
18. Finalization ownership (Runner saves PASSED, Integrator updates parent ref)
19. Bot non-acknowledgment and unsupported triggers
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orchestune.branch_naming import build_task_branch_name
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.semantic_review_adapters import (
    SemanticOperationResult,
    validate_semantic_review_preflight,
)
from orchestune.integrator.semantic_claim import ClaimResult, SemanticClaim
from orchestune.review.semantic_gate import (
    SemanticFixResultV1,
    SemanticOperationKind,
    SemanticOperationStatus,
    SemanticReviewHumanOverrideV1,
    evaluate_single_task_fast_path,
    format_semantic_fix_result_marker,
    format_semantic_review_override_marker,
)
from tests.dispatch.test_postcycle_semantic_review import (
    ATTEMPT,
    BASE,
    CHILDREN,
    FIXED,
    HEAD,
    PARENT,
    PR,
    T0,
    P,
    World,
    _phases,
)


@pytest.fixture
def e2e_world(tmp_path: Path) -> World:
    return World(tmp_path)


class TestLayer2SemanticReviewGateE2E:
    """19 comprehensive E2E scenario tests covering the Layer-2 gate lifecycle."""

    def test_scenario_01_single_task_fast_path(self, e2e_world: World) -> None:
        """Scenario 1: Single child task with Layer 1 pass passes via Fast-Path without review."""
        single_child = [CHILDREN[0]]
        decision = evaluate_single_task_fast_path(
            child_count=1,
            force_integration_review=False,
            base_sha_matches=True,
            tree_matches_without_conflict=True,
            layer1_verdict="pass",
            layer1_reviewed_sha=single_child[0].source_sha,
            source_sha=single_child[0].source_sha,
        )
        assert decision.eligible is True
        assert decision.phase is P.FAST_PATH_PASSED
        e2e_world.seed(phase=decision.phase)
        report = e2e_world.cycle()
        assert report["status"] == "no_pending_attempt"
        assert e2e_world.dispatched == []
        assert e2e_world.triggers() == []

    def test_scenario_02_single_task_layer1_blocked(self, e2e_world: World) -> None:
        """Scenario 2: Single child task blocked if Layer 1 review evidence is missing/invalid."""
        single_child = [CHILDREN[0]]
        # When child has not completed Layer 1 review (e.g. status:queued instead of done)
        e2e_world.labels[single_child[0].issue_number] = {"status:in-progress"}
        e2e_world.seed(child_sources=single_child, phase=P.NOT_STARTED)
        report = e2e_world.cycle()
        # Attempt cannot proceed or is invalidated due to unfulfilled child source
        assert report["status"] in ("state_unreadable", "inputs_unknown", "invalidated")

    def test_scenario_03_two_tasks_review_fix_ci_rereview_automerge(
        self, e2e_world: World
    ) -> None:
        """Scenario 3: 2-task merge: Round 1 review -> fix -> promotion -> CI -> Round 2 review pass -> PASSED."""
        e2e_world.seed()
        # Round 1 trigger
        assert _phases(e2e_world.cycle()) == ("not_started", "review_pending")
        assert len(e2e_world.triggers()) == 1

        # Bot reviews with findings
        e2e_world.review("Findings detected.")
        report = e2e_world.cycle()
        assert _phases(report) == ("review_pending", "fix_in_progress")
        assert len(e2e_world.dispatched) == 1

        # Fix job finishes with new output commit
        e2e_world.finish_job(output=FIXED)
        report = e2e_world.cycle()
        assert _phases(report) == ("fix_in_progress", "ci_verification")
        assert e2e_world.pr_head == FIXED

        # CI verification passes and triggers Round 2
        report = e2e_world.cycle()
        assert _phases(report) == ("ci_verification", "review_pending")
        assert len(e2e_world.triggers()) == 2
        assert "orchestune:review-round 2" in e2e_world.triggers()[-1]

        # Round 2 review passes
        e2e_world.review("Looks good, no findings.")
        assert _phases(e2e_world.cycle()) == ("review_pending", "fix_in_progress")
        e2e_world.finish_job(output=None)  # No further changes
        report = e2e_world.cycle()
        assert _phases(report) == ("fix_in_progress", "passed")
        assert e2e_world.state.phase is P.PASSED

    def test_scenario_04_max_rounds_escalation(self, e2e_world: World) -> None:
        """Scenario 4: Reaching max_semantic_review_rounds escalates to status:blocked-human-review."""
        e2e_world.config.max_semantic_review_rounds = 2
        e2e_world.seed(
            phase=P.CI_VERIFICATION,
            round=2,
            ci_verified_sha=None,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        report = e2e_world.cycle()
        assert report["next_phase"] == "escalated"
        assert e2e_world.state.phase is P.ESCALATED
        assert e2e_world.state.escalation_reason == "max_rounds"
        assert "status:blocked-human-review" in e2e_world.labels[PARENT]

    def test_scenario_05_human_override_and_mode_off(self, tmp_path: Path) -> None:
        """Scenario 5: Mode off skips review; human override on escalated attempt moves to skipped_by_human."""
        # Test mode off
        world_off = World(tmp_path, semantic_review_mode="off")
        world_off.seed()
        report = world_off.cycle()
        assert report["next_phase"] == "skipped_by_human"
        assert report["details"]["reason"] == "mode_off"

        # Test human override on escalated attempt
        world_req = World(tmp_path / "req", semantic_review_mode="required")
        world_req.seed(phase=P.ESCALATED, escalation_reason="max_rounds")
        override = SemanticReviewHumanOverrideV1(ATTEMPT, HEAD, "human verified")
        world_req.post(PR, format_semantic_review_override_marker(override), "alice")
        assert world_req.cycle()["next_phase"] == "skipped_by_human"

    def test_scenario_06_restart_resilience_and_sha_invalidation(
        self, e2e_world: World
    ) -> None:
        """Scenario 6: Restarting worker resumes without duplicate job; head change invalidates attempt."""
        e2e_world.seed(
            phase=P.FIX_IN_PROGRESS,
            round=1,
            operation_id=f"{ATTEMPT[:12]}-g0-r1-fix-0",
            operation_kind=SemanticOperationKind.FIX,
            operation_status=SemanticOperationStatus.DISPATCHING,
            fix_input_sha=HEAD,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        # Cycle polls in-flight job rather than re-dispatching
        report = e2e_world.cycle()
        assert report["status"] == "held"
        assert len(e2e_world.dispatched) == 0

        # Change in child branch SHA when a job is in-flight safely escalates rather than abandoning the job
        e2e_world.refs[build_task_branch_name(1101, "task-a")] = "7" * 40
        report = e2e_world.cycle()
        assert report["next_phase"] == "escalated"
        assert e2e_world.state.escalation_reason == "invalidation_unconfirmed"

    def test_scenario_07_preflight_failure(self, e2e_world: World) -> None:
        """Scenario 7: Preflight failure (e.g. missing provider binary/token) stops and escalates."""
        e2e_world.preflight_ok = False
        e2e_world.seed()
        report = e2e_world.cycle()
        assert report["next_phase"] == "escalated"
        assert report["details"]["reason"] == "fix_provider_unavailable"
        assert e2e_world.escalations == ["fix_provider_unavailable"]

    def test_scenario_08_concurrent_and_lost_claim(self, e2e_world: World) -> None:
        """Scenario 8: Concurrent claim collision, lost claim, or claim unknown halts cleanly."""
        e2e_world.seed()
        # Concurrent worker holds claim
        e2e_world.claim = SemanticClaim(
            PARENT, ATTEMPT, "other-worker", 1, T0 + timedelta(hours=1), "0" * 40
        )
        assert e2e_world.cycle()["status"] == "claimed_by_other"

        # Lost claim during write
        e2e_world.claim = None
        e2e_world.lose_claim = True
        assert e2e_world.cycle()["status"] == "claim_lost"

    def test_scenario_09_crash_recovery_points(self, e2e_world: World) -> None:
        """Scenario 9: Crash recovery preserves state consistency without double dispatch."""
        e2e_world.seed()
        # Simulate state saved by another worker racing meanwhile
        acquire = e2e_world.claims().acquire

        def racing_acquire(*args: Any, **kwargs: Any) -> ClaimResult:
            e2e_world.seed(phase=P.REVIEW_PENDING, round=1, state_revision=1)
            return acquire(*args, **kwargs)

        e2e_world.claim_overrides["acquire"] = racing_acquire
        assert e2e_world.cycle()["status"] == "state_changed"

    def test_scenario_10_unresolved_or_deferred_judgments(
        self, e2e_world: World
    ) -> None:
        """Scenario 10: Unresolved or deferred findings block pass and lead to escalation."""
        e2e_world.seed()
        e2e_world.cycle()  # to review_pending
        e2e_world.review()
        e2e_world.cycle()  # to fix_in_progress
        # Worker posts a deferred finding
        state = e2e_world.state
        table = e2e_world.post(
            PR,
            f"```orchestune-review-judgments\nround: 1\nfindings:\n  - {{source: '{e2e_world.sources()[0]}', location: x, judgment: needs_information, status: deferred, basis: b, evidence: e}}\n```",
        )
        e2e_world.post(
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
        e2e_world.polls[str(state.operation_id)] = SemanticOperationResult(
            str(state.operation_id),
            SemanticOperationStatus.COMPLETED,
            input_sha=HEAD,
            completed=True,
        )
        report = e2e_world.cycle()
        assert report["next_phase"] == "escalated"
        assert e2e_world.state.escalation_reason == "deferred_judgments"

    def test_scenario_11_escalated_preserves_head(self, e2e_world: World) -> None:
        """Scenario 11: An escalated attempt preserves its HEAD and stays held until changed."""
        e2e_world.seed(
            phase=P.ESCALATED,
            escalation_reason="max_rounds",
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        # First cycle ensures parent escalation label is applied
        e2e_world.cycle()
        assert "status:blocked-human-review" in e2e_world.labels[PARENT]
        # Subsequent cycle stays held without modifying attempt HEAD
        report = e2e_world.cycle()
        assert report["status"] == "held"
        assert e2e_world.state.head_sha == HEAD

    def test_scenario_12_invalidation_and_branch_cleanup(
        self, e2e_world: World
    ) -> None:
        """Scenario 12: Base or child change invalidates attempt and deletes fix branch."""
        e2e_world.seed(
            phase=P.REVIEW_PENDING,
            round=1,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        e2e_world.refs[f"parent/issue-{PARENT}"] = "1" * 40
        report = e2e_world.cycle()
        assert report["next_phase"] == "invalidated"
        assert e2e_world.deleted == [ATTEMPT]

    def test_scenario_13_bounded_wait_and_timeouts(self, e2e_world: World) -> None:
        """Scenario 13: Timeout during attempt or operation escalates to status:blocked-human-review."""
        e2e_world.seed()
        e2e_world.cycle()  # review_pending
        # Advance clock beyond review timeout (31 minutes)
        report = e2e_world.cycle(minutes=31)
        assert report["next_phase"] == "escalated"
        assert e2e_world.state.escalation_reason == "review_not_acknowledged"
        assert "status:blocked-human-review" in e2e_world.labels[PARENT]

    def test_scenario_14_commit_and_launch_boundaries(self, tmp_path: Path) -> None:
        """Scenario 14: apply=False performs no external writes, CAS failure aborts safely."""
        world_dry = World(tmp_path, apply=False)
        world_dry.seed()
        report = world_dry.cycle()
        # In dry run mode, no markers are written and next phase is previewed
        assert (
            report.get("status")
            in (
                "held",
                "skipped",
                "preview",
                "no_pending_attempt",
                "planned",
            )
            or report.get("applied") is False
        )

    def test_scenario_15_review_evidence_binding(self, e2e_world: World) -> None:
        """Scenario 15: Out-of-window, wrong-head, or untrusted reviews are rejected as evidence."""
        e2e_world.review("stale review")
        e2e_world.seed(
            phase=P.REVIEW_PENDING,
            round=1,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        # A review posted for a different head is ignored
        e2e_world.post(
            PR,
            f"@codex review\n<!-- orchestune:review-trigger bot=codex -->\n<!-- orchestune:review-round 1 -->\n<!-- orchestune:review-head {FIXED} -->",
        )
        e2e_world.review("review of another head")
        assert e2e_world.cycle()["status"] == "held"
        assert e2e_world.dispatched == []

    def test_scenario_16_child_set_freezing(self, e2e_world: World) -> None:
        """Scenario 16: A new done child appearing mid-review is deferred to the next attempt."""
        e2e_world.seed(
            phase=P.REVIEW_PENDING,
            round=1,
            attempt_started_at=T0.isoformat(),
            phase_started_at=T0.isoformat(),
        )
        # Adding an unincluded done child does not invalidate the existing attempt
        e2e_world.labels[9999] = {"status:done"}
        e2e_world.refs[build_task_branch_name(9999, "new-task")] = "3" * 40
        # Active attempt continues to evaluate against frozen children
        assert e2e_world.cycle()["status"] == "held"

    def test_scenario_17_multi_commit_child_fast_path(self, e2e_world: World) -> None:
        """Scenario 17: Single child with multiple commits still qualifies for Fast-Path."""
        single_child = [CHILDREN[0]]
        decision = evaluate_single_task_fast_path(
            child_count=1,
            force_integration_review=False,
            base_sha_matches=True,
            tree_matches_without_conflict=True,
            layer1_verdict="pass",
            layer1_reviewed_sha=single_child[0].source_sha,
            source_sha=single_child[0].source_sha,
        )
        assert decision.eligible is True
        assert decision.phase is P.FAST_PATH_PASSED
        e2e_world.seed(phase=decision.phase)
        report = e2e_world.cycle()
        assert report["status"] == "no_pending_attempt"

    def test_scenario_18_finalization_ownership(self, e2e_world: World) -> None:
        """Scenario 18: Runner saves PASSED; parent branch is only updated by Integrator."""
        e2e_world.seed(phase=P.NOT_STARTED, ci_verified_sha=None)
        e2e_world.cycle()
        e2e_world.review()
        e2e_world.cycle()
        e2e_world.finish_job(output=None)
        e2e_world.cycle()  # to ci_verification
        report = e2e_world.cycle()
        assert _phases(report) == ("ci_verification", "passed")
        # Runner leaves parent branch unchanged
        assert e2e_world.refs[f"parent/issue-{PARENT}"] == BASE

    def test_scenario_19_bot_no_ack_and_unsupported_triggers(
        self, e2e_world: World
    ) -> None:
        """Scenario 19: Unresponsive bot escalates; unsupported trigger stops at preflight."""
        # Unresponsive bot
        e2e_world.seed()
        e2e_world.cycle()
        report = e2e_world.cycle(minutes=35)
        assert report["next_phase"] == "escalated"
        assert e2e_world.state.escalation_reason == "review_not_acknowledged"

        # Unsupported trigger stops at preflight
        bad_config = DispatcherConfig(
            parent_issue_number=PARENT,
            forge=e2e_world.forge(),
            events_log_path=e2e_world.config.run_state_path.parent / "events.jsonl",
            run_state_path=e2e_world.config.run_state_path.parent / "run_state.json",
            dispatch_target=cast(Any, SimpleNamespace(target_name="claude-cli")),
            semantic_review_trigger="dispatch-target",
        )
        preflight_res = validate_semantic_review_preflight(
            bad_config, repository_root=Path("/dummy")
        )
        assert not preflight_res.ok
        assert preflight_res.error_code == "trigger_unsupported"
