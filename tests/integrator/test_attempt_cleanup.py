"""Close and clean up invalidated Layer-2 integration attempts (#1440)."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from orchestune.infra.git_cli import ConditionalBranchDeletionResult as Deletion
from orchestune.integrator.attempt_cleanup import (
    AttemptCleaner,
    AttemptCleanupReport,
    CleanupClaims,
    RemoteRefPorts,
    list_remote_attempt_refs,
)
from orchestune.integrator.semantic_claim import (
    ClaimResult,
    ClaimStatus,
    PushResult,
    PushStatus,
    SemanticClaim,
)
from orchestune.issue_notice import latest_notice_body
from orchestune.models import PrRecord
from orchestune.review.semantic_gate import (
    ChildSourceRef,
    IntegrationAttemptIdentity,
    SemanticOperationStatus,
    SemanticReviewPhase,
    SemanticReviewStateV1,
    format_semantic_review_state_marker,
)

PARENT = 1368
PR = 1430
BASE = "b" * 40
NEW_BASE = "c" * 40
HEAD = "1" * 40
CHILDREN = (ChildSourceRef(1101, "task-a", "a" * 40),)
IDENTITY = IntegrationAttemptIdentity.create(PARENT, BASE, CHILDREN)
BRANCH = IDENTITY.temp_branch
ATTEMPT = IDENTITY.attempt_id
PARENT_BRANCH = f"parent/issue-{PARENT}"
SELF = "orchestune[bot]"
NOW = datetime(2026, 10, 26, tzinfo=UTC)

SUCCESSOR_CHILDREN = (*CHILDREN, ChildSourceRef(1102, "task-b", "d" * 40))
SUCCESSOR = IntegrationAttemptIdentity.create(PARENT, NEW_BASE, SUCCESSOR_CHILDREN)
SUCCESSOR_PR = 1435


def _state(
    identity: IntegrationAttemptIdentity,
    pr: int,
    phase: SemanticReviewPhase,
    **changes: Any,
) -> SemanticReviewStateV1:
    return SemanticReviewStateV1(
        identity.attempt_id,
        PARENT,
        pr,
        phase,
        1,
        HEAD,
        identity.base_sha,
        identity.child_sources,
        **changes,
    )


class Repo:
    """GitHub, the remote refs and the claim store of one parent."""

    def __init__(self) -> None:
        self.prs: dict[int, PrRecord] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.refs: dict[str, str] = {}
        self.events: list[str] = []
        self.deleted_with: list[tuple[str, str]] = []
        self.delete_result = Deletion.DELETED
        self.close_fails = False
        self.close_noop = False
        self.post_fails_after_landing = False
        self.post_fails = False
        self.claim_status = ClaimStatus.ACQUIRED
        self.lose_claim = False
        self.settled = True
        self.refs_available = True
        self.ahead_of_state = False
        self.holder_claims = 0
        self.released = 0
        self.settled_asked: list[int] = []

    # -- seeding -----------------------------------------------------------------
    def attempt(
        self,
        identity: IntegrationAttemptIdentity = IDENTITY,
        number: int = PR,
        *,
        phase: SemanticReviewPhase = SemanticReviewPhase.INVALIDATED,
        pr_state: str = "OPEN",
        ref: bool = True,
        author: str = SELF,
        cross: bool | None = False,
        **changes: Any,
    ) -> SemanticReviewStateV1:
        branch = identity.temp_branch
        self.prs[number] = PrRecord(
            number=number,
            head_ref=branch,
            changed_files=(),
            base_ref=PARENT_BRANCH,
            is_cross_repository=cross,
            head_sha=HEAD,
            state=pr_state,
            merged_at="2026-10-26T00:00:00Z" if pr_state == "MERGED" else "",
        )
        state = _state(identity, number, phase, **changes)
        self.post(number, format_semantic_review_state_marker(state), author)
        if ref:
            self.refs[branch] = HEAD
        return state

    def post(self, number: int, body: str, author: str = SELF) -> dict[str, Any]:
        comment = {
            "id": 100 + sum(len(c) for c in self.comments.values()),
            "body": body,
            "user": {"login": author},
        }
        self.comments.setdefault(number, []).append(comment)
        return comment

    def notices(self, number: int, kind_prefix: str) -> list[str]:
        marker = f"<!-- orchestune:notice:{kind_prefix}"
        return [c["body"] for c in self.comments.get(number, []) if marker in c["body"]]

    # -- forge -------------------------------------------------------------------
    def list_open_prs(self, *_: Any, **__: Any) -> list[PrRecord]:
        return [p for p in self.prs.values() if p.state == "OPEN"]

    def list_prs(
        self, state: str = "open", head: str | None = None, **_: Any
    ) -> list[PrRecord]:
        return [
            p
            for p in self.prs.values()
            if (state == "all" or p.state.casefold() == state)
            and (head is None or p.head_ref == head)
        ]

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        return list(self.comments.get(int(number), []))

    list_all_issue_comments = list_comments

    def add_comment(self, number: int, body: str) -> None:
        if self.post_fails:
            raise RuntimeError("API down")
        self.events.append("notice")
        self.post(int(number), body)
        if self.post_fails_after_landing:
            raise RuntimeError("answer lost")

    def get_pull_request(self, number: int) -> PrRecord:
        return self.prs[int(number)]

    def close_pull_request(self, number: int) -> None:
        if self.close_fails:
            raise RuntimeError("close failed")
        self.events.append("close")
        if self.close_noop:
            return
        pr = self.prs[int(number)]
        if pr.state == "OPEN":
            self.prs[int(number)] = replace(pr, state="CLOSED")

    def get_authenticated_user(self) -> str:
        return SELF

    def list_notice_trusted_authors(self) -> frozenset[str]:
        return frozenset({SELF})

    def get_actor_permission(self, _user: str) -> str:
        return "read"

    # -- ports -------------------------------------------------------------------
    def ports(self) -> RemoteRefPorts:
        def delete(_root: Any, branch: str, expected: str) -> Deletion:
            self.events.append("delete")
            self.deleted_with.append((branch, expected))
            if self.delete_result in {Deletion.DELETED, Deletion.ALREADY_ABSENT}:
                self.refs.pop(branch, None)
            return self.delete_result

        return RemoteRefPorts(
            list_refs=lambda _r, _p: dict(self.refs) if self.refs_available else None,
            parent_tip=lambda _r, _p: NEW_BASE,
            delete_ref=delete,
        )

    def claims(self) -> CleanupClaims:
        def acquire(
            _root: Any, *, attempt_id: str, holder: str, now: datetime, **_: Any
        ) -> ClaimResult:
            if self.claim_status is not ClaimStatus.ACQUIRED:
                return ClaimResult(self.claim_status, None, "busy")
            self.holder_claims += 1
            claim = SemanticClaim(
                PARENT, attempt_id, holder, 1, now + timedelta(minutes=5), "0" * 40
            )
            return ClaimResult(ClaimStatus.ACQUIRED, claim)

        def renew(
            _root: Any, claim: SemanticClaim, *, now: datetime, **_: Any
        ) -> PushResult:
            if self.lose_claim:
                return PushResult(PushStatus.REJECTED, None, "claim_lost")
            return PushResult(PushStatus.PUSHED, claim)

        def release(_root: Any, claim: SemanticClaim, *, now: datetime) -> PushResult:
            self.released += 1
            return PushResult(PushStatus.PUSHED, claim)

        return CleanupClaims(acquire=acquire, renew=renew, release=release)

    def cleaner(self, *, apply: bool = True) -> AttemptCleaner:
        def settled(_state: SemanticReviewStateV1, pr_number: int) -> bool:
            self.settled_asked.append(pr_number)
            return self.settled

        return AttemptCleaner(
            self,  # type: ignore[arg-type]
            Path("."),
            PARENT,
            apply=apply,
            holder="w1",
            clock=lambda: NOW,
            refs=self.ports(),
            claims=self.claims(),
            operation_settled=settled,
        )

    def run(self, *, apply: bool = True) -> list[AttemptCleanupReport]:
        return self.cleaner(apply=apply).run()


@pytest.fixture
def repo() -> Repo:
    return Repo()


def _by_pr(reports: list[AttemptCleanupReport], number: int) -> AttemptCleanupReport:
    (report,) = (r for r in reports if r.pr_number == number)
    return report


class TestHappyPath:
    def test_notice_close_and_lease_delete_in_order(self, repo: Repo) -> None:
        repo.attempt()

        (report,) = repo.run()

        assert repo.events == ["notice", "close", "delete"]
        assert report.status == "cleaned"
        assert report.done == ("notify", "close", "delete_ref")
        assert repo.prs[PR].state == "CLOSED"
        assert repo.deleted_with == [(BRANCH, HEAD)]
        assert repo.refs == {}

    def test_notice_states_reason_old_and_current_base_and_head(
        self, repo: Repo
    ) -> None:
        repo.attempt()

        repo.run()

        (body,) = repo.notices(PR, f"semantic-review-invalidated:{ATTEMPT}")
        text = latest_notice_body(
            [{"body": body}], f"semantic-review-invalidated:{ATTEMPT}"
        )
        assert text is not None
        for fact in (ATTEMPT, BASE, NEW_BASE, HEAD, "not created yet"):
            assert fact in text
        assert "parent branch" in text and "closed" in text

    def test_a_second_run_changes_nothing(self, repo: Repo) -> None:
        repo.attempt()
        repo.run()
        repo.events.clear()
        posted = len(repo.comments[PR])

        assert repo.run() == []

        assert repo.events == [] and len(repo.comments[PR]) == posted

    def test_the_claim_is_held_around_the_writes_and_released(self, repo: Repo) -> None:
        repo.attempt()

        repo.run()

        assert repo.holder_claims == 1 and repo.released == 1

    def test_dry_run_reports_the_plan_without_any_side_effect(self, repo: Repo) -> None:
        repo.attempt()

        (report,) = repo.run(apply=False)

        assert report.status == "planned"
        assert report.done == ("notify", "close", "delete_ref")
        assert repo.events == [] and repo.holder_claims == 0
        assert repo.prs[PR].state == "OPEN" and BRANCH in repo.refs


class TestResume:
    def test_close_failure_keeps_the_notice_and_resumes_at_close(
        self, repo: Repo
    ) -> None:
        repo.attempt()
        repo.close_fails = True

        (failed,) = repo.run()

        assert failed.status == "failed" and "close" in failed.detail
        assert repo.events == ["notice"] and BRANCH in repo.refs
        repo.close_fails = False
        repo.events.clear()

        (done,) = repo.run()

        assert repo.events == ["close", "delete"] and done.status == "cleaned"
        assert len(repo.notices(PR, "semantic-review-invalidated")) == 1

    def test_a_failed_deletion_never_reopens_and_only_retries_the_deletion(
        self, repo: Repo
    ) -> None:
        repo.attempt()
        repo.delete_result = Deletion.FAILED

        (failed,) = repo.run()

        assert failed.status == "failed" and repo.prs[PR].state == "CLOSED"
        repo.delete_result = Deletion.DELETED
        repo.events.clear()

        (done,) = repo.run()

        assert repo.events == ["delete"] and done.status == "cleaned"

    def test_an_already_closed_pr_with_a_leftover_ref_is_collected(
        self, repo: Repo
    ) -> None:
        repo.attempt(pr_state="CLOSED")

        (report,) = repo.run()

        assert repo.events == ["delete"]  # no notice or close on a closed PR
        assert report.status == "cleaned" and repo.refs == {}

    def test_an_uncertain_notice_that_landed_is_not_sent_again(
        self, repo: Repo
    ) -> None:
        repo.attempt()
        repo.post_fails_after_landing = True

        (report,) = repo.run()

        assert repo.events.count("notice") == 1
        assert report.status == "cleaned"

    def test_a_notice_that_did_not_land_stops_before_the_close(
        self, repo: Repo
    ) -> None:
        repo.attempt()
        repo.post_fails = True

        (report,) = repo.run()

        assert report.status == "failed" and repo.events == []
        assert repo.prs[PR].state == "OPEN" and BRANCH in repo.refs

    def test_close_not_confirmed_by_the_read_back_keeps_the_ref(
        self, repo: Repo
    ) -> None:
        repo.attempt()
        repo.close_noop = True

        (report,) = repo.run()

        assert report.status == "pending" and "close" in report.detail
        assert "delete" not in repo.events and BRANCH in repo.refs


class TestSafety:
    def test_a_merged_pr_is_reported_and_nothing_destructive_happens(
        self, repo: Repo
    ) -> None:
        repo.attempt(pr_state="MERGED")

        (report,) = repo.run()

        assert report.status == "blocked" and "merged" in report.detail
        assert repo.events == [] and BRANCH in repo.refs

    def test_a_ref_whose_tip_moved_is_kept(self, repo: Repo) -> None:
        repo.attempt(pr_state="CLOSED")
        repo.refs[BRANCH] = "9" * 40

        (report,) = repo.run()

        assert report.status == "blocked" and "tip" in report.detail
        assert repo.events == [] and repo.refs[BRANCH] == "9" * 40

    def test_a_lease_rejection_is_blocked_and_visible(self, repo: Repo) -> None:
        repo.attempt(pr_state="CLOSED")
        repo.delete_result = Deletion.TIP_MISMATCH

        (report,) = repo.run()

        assert report.status == "blocked" and BRANCH in repo.refs

    def test_a_denied_deletion_is_a_failure(self, repo: Repo) -> None:
        repo.attempt(pr_state="CLOSED")
        repo.delete_result = Deletion.DENIED

        (report,) = repo.run()

        assert report.status == "failed" and "denied" in report.detail

    @pytest.mark.parametrize("cross", [True, None])
    def test_a_fork_or_unknown_origin_is_never_touched(
        self, repo: Repo, cross: bool | None
    ) -> None:
        repo.attempt(cross=cross, pr_state="CLOSED")

        assert repo.run() == []
        assert repo.events == [] and BRANCH in repo.refs

    def test_state_posted_by_an_untrusted_author_is_ignored(self, repo: Repo) -> None:
        repo.attempt(author="mallory")

        assert repo.run() == []
        assert repo.events == [] and BRANCH in repo.refs

    def test_an_active_attempt_is_left_alone(self, repo: Repo) -> None:
        repo.attempt(phase=SemanticReviewPhase.REVIEW_PENDING)

        assert repo.run() == []
        assert repo.events == [] and repo.prs[PR].state == "OPEN"

    def test_a_passed_attempt_is_left_alone(self, repo: Repo) -> None:
        repo.attempt(phase=SemanticReviewPhase.PASSED, pr_state="CLOSED")

        assert repo.run() == []
        assert repo.events == [] and BRANCH in repo.refs

    def test_a_ref_without_a_managed_pr_is_ignored(self, repo: Repo) -> None:
        repo.refs[BRANCH] = HEAD

        assert repo.run() == []
        assert repo.events == []

    def test_a_ref_whose_name_is_not_the_attempts_branch_is_ignored(
        self, repo: Repo
    ) -> None:
        other = IntegrationAttemptIdentity.create(PARENT, NEW_BASE, CHILDREN)
        repo.attempt(other, number=7, pr_state="CLOSED", ref=False)
        repo.prs[7] = replace(repo.prs[7], head_ref=BRANCH)  # forged binding
        repo.refs[BRANCH] = HEAD

        assert repo.run() == []
        assert repo.events == []

    def test_another_open_pr_on_the_same_branch_blocks_the_deletion(
        self, repo: Repo
    ) -> None:
        repo.attempt(pr_state="CLOSED")
        repo.prs[99] = PrRecord(
            number=99,
            head_ref=BRANCH,
            changed_files=(),
            base_ref="main",
            is_cross_repository=False,
        )

        (report,) = repo.run()

        assert report.status == "blocked" and "open PR" in report.detail
        assert repo.events == [] and BRANCH in repo.refs

    def test_an_unsettled_old_operation_holds_everything(self, repo: Repo) -> None:
        repo.attempt(
            operation_id="op-1",
            operation_status=SemanticOperationStatus.RUNNING,
        )
        repo.settled = False

        (report,) = repo.run()

        assert report.status == "pending" and "operation" in report.detail
        assert repo.events == [] and repo.settled_asked == [PR]
        assert repo.holder_claims == 0

    def test_a_claim_held_by_another_worker_holds_the_attempt(self, repo: Repo) -> None:
        repo.attempt()
        repo.claim_status = ClaimStatus.HELD_BY_OTHER

        (report,) = repo.run()

        assert report.status == "pending" and "claim" in report.detail
        assert repo.events == []

    def test_a_lost_claim_stops_before_the_next_write(self, repo: Repo) -> None:
        repo.attempt()
        repo.lose_claim = True

        (report,) = repo.run()

        assert report.status == "pending" and "claim" in report.detail
        assert repo.events == [] and repo.released == 0

    def test_without_the_remote_refs_the_deletion_waits(self, repo: Repo) -> None:
        repo.attempt()
        repo.refs_available = False

        (report,) = repo.run()

        assert repo.events == ["notice", "close"]
        assert report.status == "pending" and "refs" in report.detail


class TestSuccessor:
    def _successor(self, repo: Repo, **changes: Any) -> None:
        repo.attempt(
            SUCCESSOR,
            SUCCESSOR_PR,
            phase=changes.pop("phase", SemanticReviewPhase.REVIEW_PENDING),
            ref=False,
            **changes,
        )

    def test_an_existing_successor_is_announced_once_in_its_own_comment(
        self, repo: Repo
    ) -> None:
        repo.attempt()
        self._successor(repo)

        repo.run()

        (announce,) = repo.notices(
            PR, f"semantic-review-superseded:{ATTEMPT}:{SUCCESSOR_PR}"
        )
        assert f"#{SUCCESSOR_PR}" in announce and SUCCESSOR.attempt_id in announce
        (notice,) = repo.notices(PR, f"semantic-review-invalidated:{ATTEMPT}")
        assert "not created yet" not in notice
        assert repo.run() == []
        assert len(repo.notices(PR, "semantic-review-superseded")) == 1

    def test_a_successor_created_after_the_cleanup_is_still_announced(
        self, repo: Repo
    ) -> None:
        repo.attempt()
        repo.run()  # successor not created yet
        assert repo.notices(PR, "semantic-review-superseded") == []
        self._successor(repo)
        repo.events.clear()

        (report,) = repo.run()

        assert report.pr_number == PR and report.done == ("announce",)
        assert len(repo.notices(PR, "semantic-review-superseded")) == 1
        assert repo.events == ["notice"]
        assert repo.run() == []

    def test_two_possible_successors_are_not_guessed(self, repo: Repo) -> None:
        repo.attempt()
        self._successor(repo)
        third = IntegrationAttemptIdentity.create(
            PARENT, "e" * 40, (ChildSourceRef(1103, "task-c", "f" * 40),)
        )
        repo.attempt(third, 1436, phase=SemanticReviewPhase.REVIEW_PENDING, ref=False)

        repo.run()

        assert repo.notices(PR, "semantic-review-superseded") == []
        assert repo.prs[PR].state == "CLOSED"

    def test_an_invalidated_attempt_is_not_its_own_successor(self, repo: Repo) -> None:
        repo.attempt()
        self._successor(repo, phase=SemanticReviewPhase.INVALIDATED)

        repo.run()

        assert repo.notices(PR, "semantic-review-superseded") == []

    def test_a_forged_announcement_does_not_suppress_the_real_one(
        self, repo: Repo
    ) -> None:
        repo.attempt()
        repo.run()
        self._successor(repo)
        kind = f"semantic-review-superseded:{ATTEMPT}:{SUCCESSOR_PR}"
        repo.post(PR, f"<!-- orchestune:notice:{kind} -->\nfake", "mallory")

        repo.run()

        real = [b for b in repo.notices(PR, kind) if not b.endswith("fake")]
        assert len(real) == 1


class TestRemoteRefs:
    def _git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_lists_only_this_parents_attempt_branches(self, tmp_path: Path) -> None:
        remote = tmp_path / "remote.git"
        work = tmp_path / "work"
        self._git(tmp_path, "init", "--bare", str(remote))
        self._git(tmp_path, "init", str(work))
        self._git(work, "config", "user.email", "t@example.com")
        self._git(work, "config", "user.name", "t")
        (work / "f").write_text("x")
        self._git(work, "add", "f")
        self._git(work, "commit", "-m", "x")
        self._git(work, "remote", "add", "origin", str(remote))
        sha = self._git(work, "rev-parse", "HEAD")
        for name in (
            BRANCH,
            f"integration/temp-parent-issue-{PARENT + 1}-{'a' * 12}",
            f"integration/temp-parent-issue-{PARENT}-not-hex",
            f"integration/temp-parent-issue-{PARENT}-{'b' * 12}/extra",
            "feature/other",
        ):
            self._git(work, "push", "origin", f"HEAD:refs/heads/{name}")

        assert list_remote_attempt_refs(work, PARENT) == {BRANCH: sha}

    def test_an_unreachable_remote_is_unknown_not_empty(self, tmp_path: Path) -> None:
        work = tmp_path / "work"
        self._git(tmp_path, "init", str(work))
        self._git(work, "remote", "add", "origin", str(tmp_path / "missing.git"))

        assert list_remote_attempt_refs(work, PARENT) is None
