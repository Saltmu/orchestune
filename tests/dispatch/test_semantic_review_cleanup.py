"""Cleanup of invalidated Layer-2 attempts in the Dispatcher post-cycle (#1440)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch.postcycle_semantic_review import (
    advance_pending_semantic_reviews,
)
from orchestune.dispatch.pre_scheduling_integration import (
    _advance_pending_semantic_reviews,
)
from orchestune.dispatch.result import PhaseStatus
from orchestune.dispatch.semantic_review_adapters import SemanticOperationResult
from orchestune.forge import ForgeAuthError
from orchestune.infra.git_cli import ConditionalBranchDeletionResult as Deletion
from orchestune.integrator.attempt_cleanup import RemoteRefPorts
from orchestune.models import PrRecord
from orchestune.review.semantic_gate import SemanticOperationStatus
from tests.dispatch.test_postcycle_semantic_review import (
    ATTEMPT,
    HEAD,
    IDENTITY,
    PARENT,
    PR,
    T0,
    P,
    World,
)

BRANCH = IDENTITY.temp_branch


class CleanupWorld(World):
    """`World` whose PR can be closed, with a remote that holds the temp branch."""

    def __init__(self, tmp_path: Path, **config: Any) -> None:
        self.pr_state = "OPEN"
        self.remote = {BRANCH: HEAD}
        self.lease_calls: list[tuple[str, str]] = []
        self.fail_listing_after: int | None = None
        super().__init__(tmp_path, **config)

    def record(self) -> PrRecord:
        return PrRecord(
            number=PR,
            head_ref=BRANCH,
            changed_files=(),
            base_ref=f"parent/issue-{PARENT}",
            is_cross_repository=False,
            head_sha=self.pr_head,
            state=self.pr_state,
        )

    def forge(self) -> MagicMock:
        forge = super().forge()

        def list_open(*_: Any, **__: Any) -> list[PrRecord]:
            if self.fail_listing_after is not None:
                if self.fail_listing_after == 0:
                    raise RuntimeError("listing failed")
                self.fail_listing_after -= 1
            return [self.record()] if self.pr_state == "OPEN" else []

        def list_prs(
            state: str = "open", head: str | None = None, **_: Any
        ) -> list[PrRecord]:
            record = self.record()
            wanted = state == "all" or record.state.casefold() == state
            return [record] if wanted and head in {None, BRANCH} else []

        forge.list_open_prs.side_effect = list_open
        forge.list_prs.side_effect = list_prs
        forge.get_pull_request.side_effect = lambda _n: self.record()
        forge.close_pull_request.side_effect = lambda _n: setattr(
            self, "pr_state", "CLOSED"
        )
        return forge

    def refs_port(self) -> RemoteRefPorts:
        def delete(_root: Any, branch: str, expected: str) -> Deletion:
            self.lease_calls.append((branch, expected))
            self.remote.pop(branch, None)
            return Deletion.DELETED

        return RemoteRefPorts(
            list_refs=lambda _r, _p: dict(self.remote),
            parent_tip=lambda _r, _p: "c" * 40,
            delete_ref=delete,
        )

    def advance(self, *, minutes: float = 1) -> dict[str, Any]:
        self.clock += timedelta(minutes=minutes)
        return advance_pending_semantic_reviews(
            self.config,
            now=self.clock,
            repository_root=Path("."),
            worker_id="w1",
            verify_ci_fn=self.verify_ci,
            operations=self.operations(),
            claims=self.claims(),
            remote_head_fn=self.refs.get,
            cleanup_refs=self.refs_port(),
        )


@pytest.fixture
def world(tmp_path: Path) -> CleanupWorld:
    return CleanupWorld(tmp_path)


def _notices(world: CleanupWorld) -> list[str]:
    return [c["body"] for c in world.comments[PR] if "orchestune:notice:" in c["body"]]


class TestCleanupInThePostCycle:
    def test_an_invalidated_attempt_is_closed_and_its_branch_deleted(
        self, world: CleanupWorld
    ) -> None:
        world.seed(phase=P.INVALIDATED)

        result = world.advance()

        assert [a["status"] for a in result["attempts"]] == ["no_pending_attempt"]
        (cleanup,) = result["cleanup"]
        assert cleanup["status"] == "cleaned" and cleanup["pr_number"] == PR
        assert cleanup["attempt_id"] == ATTEMPT
        assert world.pr_state == "CLOSED" and world.remote == {}
        assert world.lease_calls == [(BRANCH, HEAD)]
        assert len(_notices(world)) == 1

    def test_the_second_cycle_has_nothing_to_do(self, world: CleanupWorld) -> None:
        world.seed(phase=P.INVALIDATED)
        world.advance()
        notices = len(world.comments[PR])

        assert world.advance()["cleanup"] == []
        assert len(world.comments[PR]) == notices

    def test_the_cleanup_runs_alongside_a_pending_review_pass(
        self, world: CleanupWorld
    ) -> None:
        world.seed(phase=P.REVIEW_PENDING, round=1, round_started_at=T0.isoformat())

        result = world.advance()

        assert result["cleanup"] == []
        assert world.pr_state == "OPEN" and world.lease_calls == []

    def test_dry_run_only_reports_the_plan(self, tmp_path: Path) -> None:
        world = CleanupWorld(tmp_path, apply=False)
        world.seed(phase=P.INVALIDATED)

        (cleanup,) = world.advance()["cleanup"]

        assert cleanup["status"] == "planned"
        assert cleanup["done"] == ["notify", "close", "delete_ref"]
        assert world.pr_state == "OPEN" and world.lease_calls == []
        assert _notices(world) == []

    def test_a_still_running_old_job_holds_the_cleanup(
        self, world: CleanupWorld
    ) -> None:
        world.seed(
            phase=P.INVALIDATED,
            operation_id="op-1",
            operation_status=SemanticOperationStatus.RUNNING,
        )

        (cleanup,) = world.advance()["cleanup"]

        assert cleanup["status"] == "pending"
        assert world.pr_state == "OPEN" and world.lease_calls == []

    def test_a_stopped_old_job_lets_the_cleanup_proceed(
        self, world: CleanupWorld
    ) -> None:
        world.seed(
            phase=P.INVALIDATED,
            operation_id="op-1",
            operation_status=SemanticOperationStatus.RUNNING,
        )
        world.polls["op-1"] = SemanticOperationResult(
            "op-1", SemanticOperationStatus.FAILED
        )

        (cleanup,) = world.advance()["cleanup"]

        assert cleanup["status"] == "cleaned" and world.pr_state == "CLOSED"

    def test_an_unreadable_poll_is_not_settled(self, world: CleanupWorld) -> None:
        world.seed(
            phase=P.INVALIDATED,
            operation_id="op-1",
            operation_status=SemanticOperationStatus.RUNNING,
        )
        with patch(
            "orchestune.dispatch.semantic_review_cleanup.poll_semantic_operation",
            side_effect=RuntimeError("provider down"),
        ):
            (cleanup,) = world.advance()["cleanup"]

        assert cleanup["status"] == "pending" and world.pr_state == "OPEN"

    def test_a_cleanup_crash_is_reported_without_touching_the_review_result(
        self, world: CleanupWorld
    ) -> None:
        world.seed(phase=P.INVALIDATED)
        world.fail_listing_after = 1  # the review pass lists once, the cleanup next

        result = world.advance()

        assert [a["status"] for a in result["attempts"]] == ["no_pending_attempt"]
        (cleanup,) = result["cleanup"]
        assert cleanup["status"] == "failed" and "listing failed" in cleanup["detail"]

    def test_an_authentication_failure_still_propagates(
        self, world: CleanupWorld
    ) -> None:
        world.seed(phase=P.INVALIDATED)
        cast(Any, world.config.resolved_forge).list_prs.side_effect = ForgeAuthError(
            "bad token"
        )

        with pytest.raises(ForgeAuthError):
            world.advance()


class TestPhaseStatus:
    @pytest.mark.parametrize(
        ("cleanup", "expected"),
        [
            ([], PhaseStatus.SUCCESS),
            ([{"status": "cleaned"}], PhaseStatus.SUCCESS),
            ([{"status": "planned"}], PhaseStatus.SUCCESS),
            ([{"status": "pending"}], PhaseStatus.SUCCESS),
            ([{"status": "blocked"}], PhaseStatus.SUCCESS),
            ([{"status": "failed"}], PhaseStatus.WARNING),
            ([{"status": "cleaned"}, {"status": "failed"}], PhaseStatus.WARNING),
        ],
    )
    def test_only_a_failed_cleanup_warns(
        self, cleanup: list[dict[str, str]], expected: PhaseStatus
    ) -> None:
        report = {"attempts": [{"status": "held"}], "cleanup": cleanup}
        with patch(
            "orchestune.dispatch.pre_scheduling_integration.advance_pending_semantic_reviews",
            return_value=report,
        ):
            assert _advance_pending_semantic_reviews(MagicMock()).status is expected

    def test_a_report_without_cleanup_keeps_working(self) -> None:
        with patch(
            "orchestune.dispatch.pre_scheduling_integration.advance_pending_semantic_reviews",
            return_value={"attempts": [{"status": "held"}]},
        ):
            result = _advance_pending_semantic_reviews(MagicMock())

        assert result.status is PhaseStatus.SUCCESS
