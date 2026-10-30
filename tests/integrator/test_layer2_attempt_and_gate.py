from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from orchestune.forge import Forge
from orchestune.infra.git_cli import GitResult
from orchestune.integrator.attempt import (
    OpenAttempt,
    PublishStatus,
    _semantic_finalization_exempt,
    build_attempt_identity,
    check_fast_forward_identity,
    checkout_verified_attempt_head,
    find_open_attempts,
    handle_non_required_review,
    prepare_attempt_context,
    publish_attempt_branch,
    publish_parent_under_claim,
    restore_attempt,
    select_attempt_children,
    start_required_review,
    validate_finalization_context,
)
from orchestune.integrator.pr import list_open_attempt_prs
from orchestune.models import PrRecord
from orchestune.pr_link_notice_trust import TrustContext
from orchestune.review.semantic_gate import (
    ChildSourceRef,
    SemanticReviewPhase,
    SemanticReviewStateV1,
    format_semantic_review_state_marker,
)

BASE = "a" * 40
HEAD = "b" * 40
CHILD_A = ChildSourceRef(1, "task-a", "c" * 40)
CHILD_B = ChildSourceRef(2, "task-b", "d" * 40)


def _identity(*children: ChildSourceRef):
    return build_attempt_identity(100, BASE, children)


def _state(*children: ChildSourceRef, phase=SemanticReviewPhase.REVIEW_PENDING):
    identity = _identity(*children)
    return SemanticReviewStateV1(
        attempt_id=identity.attempt_id,
        parent_issue_number=100,
        pr_number=7,
        phase=phase,
        round=0,
        head_sha=HEAD,
        base_sha=BASE,
        child_sources=identity.child_sources,
    )


def _attempt(*children: ChildSourceRef, phase=SemanticReviewPhase.REVIEW_PENDING):
    state = _state(*children, phase=phase)
    return OpenAttempt(
        pr_number=7,
        temp_branch=_identity(*children).temp_branch,
        base_branch="parent/issue-100",
        head_sha=HEAD,
        state=state,
    )


def test_list_open_attempt_prs_requires_upstream_head_prefix_and_parent_base():
    forge = MagicMock(spec=Forge)
    forge.list_open_prs.return_value = [
        PrRecord(
            1,
            "integration/temp-parent-issue-100-good",
            (),
            base_ref="parent/issue-100",
            is_cross_repository=False,
        ),
        PrRecord(
            2,
            "integration/temp-parent-issue-100-fork",
            (),
            base_ref="parent/issue-100",
            is_cross_repository=True,
        ),
        PrRecord(
            3,
            "integration/temp-parent-issue-100-wrong-base",
            (),
            base_ref="main",
            is_cross_repository=False,
        ),
    ]

    assert [pr.number for pr in list_open_attempt_prs(forge, 100)] == [1]


def test_find_open_attempts_uses_only_trusted_state_and_human_override():
    state = _state(CHILD_A)
    forge = MagicMock(spec=Forge)
    forge.get_authenticated_user.return_value = "orchestune"
    forge.list_notice_trusted_authors.return_value = frozenset({"runner[bot]"})
    forge.get_actor_permission.side_effect = lambda actor: {
        "alice": "write",
        "runner[bot]": "write",
    }.get(actor, "none")
    forge.list_open_prs.return_value = [
        PrRecord(
            7,
            _identity(CHILD_A).temp_branch,
            (),
            base_ref="parent/issue-100",
            is_cross_repository=False,
            head_sha=HEAD,
        )
    ]
    forge.list_comments.return_value = [
        {"body": format_semantic_review_state_marker(state), "author": "mallory"},
        {"body": format_semantic_review_state_marker(state), "author": "runner[bot]"},
        {
            "body": '<!-- orchestune:semantic-review-override:v1 {"attempt_id":"'
            + state.attempt_id
            + '","head_sha":"'
            + HEAD
            + '","reason":"approved"} -->',
            "author": "runner[bot]",
        },
        {
            "body": '<!-- orchestune:semantic-review-override:v1 {"attempt_id":"'
            + state.attempt_id
            + '","head_sha":"'
            + HEAD
            + '","reason":"approved"} -->',
            "author": "alice",
        },
    ]

    attempts = find_open_attempts(forge, 100)

    assert attempts[0].state == state
    assert attempts[0].override is not None
    assert attempts[0].override.actor == "alice"


def test_find_open_attempts_marks_trusted_corrupt_latest_state_fail_closed():
    forge = MagicMock(spec=Forge)
    forge.get_authenticated_user.return_value = "orchestune"
    forge.list_notice_trusted_authors.return_value = frozenset()
    forge.list_open_prs.return_value = [
        PrRecord(
            7,
            _identity(CHILD_A).temp_branch,
            (),
            base_ref="parent/issue-100",
            is_cross_repository=False,
            head_sha=HEAD,
        )
    ]
    forge.list_comments.return_value = [
        {
            "body": "<!-- orchestune:semantic-review-state:v1 {broken} -->",
            "author": "orchestune",
        }
    ]

    attempt = find_open_attempts(forge, 100)[0]

    assert attempt.state is None
    assert attempt.state_error is not None


def test_find_open_attempts_does_not_trust_human_collaborator_state():
    state = _state(CHILD_A)
    forge = MagicMock(spec=Forge)
    forge.get_authenticated_user.return_value = "orchestune"
    forge.list_notice_trusted_authors.return_value = frozenset({"alice"})
    forge.list_open_prs.return_value = [
        PrRecord(
            7,
            _identity(CHILD_A).temp_branch,
            (),
            base_ref="parent/issue-100",
            is_cross_repository=False,
            head_sha=HEAD,
        )
    ]
    forge.list_comments.return_value = [
        {"body": format_semantic_review_state_marker(state), "author": "alice"}
    ]

    attempt = find_open_attempts(forge, 100)[0]

    assert attempt.state is None
    assert attempt.state_error == "trusted semantic review state is missing"


def test_select_attempt_children_freezes_existing_set_and_defers_new_child():
    selection = select_attempt_children([_attempt(CHILD_A)], BASE, [CHILD_A, CHILD_B])

    assert selection.reusable is not None
    assert selection.children == (CHILD_A,)
    assert selection.deferred == (CHILD_B,)
    assert selection.invalidated == ()
    assert selection.fail_closed is False


@pytest.mark.parametrize(
    ("base_sha", "children"),
    [("e" * 40, [CHILD_A]), (BASE, [ChildSourceRef(1, "task-a", "f" * 40)])],
)
def test_select_attempt_children_invalidates_only_changed_inputs(base_sha, children):
    existing = _attempt(CHILD_A)

    selection = select_attempt_children([existing], base_sha, children)

    assert selection.reusable is None
    assert selection.children == tuple(children)
    assert selection.invalidated == (existing,)


def test_select_attempt_children_fails_closed_for_corrupt_attempt():
    corrupt = OpenAttempt(
        7,
        _identity(CHILD_A).temp_branch,
        "parent/issue-100",
        HEAD,
        state_error="corrupt state",
    )

    selection = select_attempt_children([corrupt], BASE, [CHILD_A])

    assert selection.fail_closed is True


def test_changed_attempt_stays_blocked_until_runner_persists_invalidation():
    changed = ChildSourceRef(1, "task-a", "f" * 40)
    ctx = SimpleNamespace(
        config=SimpleNamespace(apply=True, parent_issue_number=100),
        forge=MagicMock(spec=Forge),
        repository_root=Path("."),
    )
    with (
        patch(
            "orchestune.integrator.attempt.find_open_attempts",
            return_value=[_attempt(CHILD_A)],
        ),
        patch(
            "orchestune.integrator.attempt._discover_child_sources",
            return_value=([changed], {}),
        ),
        patch("orchestune.integrator.attempt._base_sha", return_value=BASE),
    ):
        status, error = prepare_attempt_context(ctx, object())

    assert status == "review_gate_blocked"
    assert error == "existing attempt needs trusted invalidation"


def test_pr_without_trusted_state_is_a_fail_closed_crash_window():
    missing = OpenAttempt(
        7,
        _identity(CHILD_A).temp_branch,
        "parent/issue-100",
        HEAD,
        state_error="trusted semantic review state is missing",
    )

    selection = select_attempt_children([missing], BASE, [CHILD_A])

    assert selection.fail_closed is True
    assert selection.reusable is None


def test_existing_attempt_blocks_when_child_sources_cannot_be_resolved():
    ctx = SimpleNamespace(
        config=SimpleNamespace(apply=True, parent_issue_number=100),
        forge=MagicMock(spec=Forge),
    )
    with (
        patch(
            "orchestune.integrator.attempt.find_open_attempts",
            return_value=[_attempt(CHILD_A)],
        ),
        patch(
            "orchestune.integrator.attempt._discover_child_sources", return_value=None
        ),
    ):
        status, error = prepare_attempt_context(ctx, object())
    assert status == "review_gate_blocked"
    assert error == "existing attempt child sources could not be verified"
    ctx.forge.add_comment.assert_not_called()


def test_required_coordinator_waits_for_runner_to_dispatch_review():
    ctx = _finalization_context()
    ctx.semantic_review_state = None
    ctx.config.coordinator = MagicMock()
    with (
        patch(
            "orchestune.integrator.attempt.run_git", return_value=GitResult(0, HEAD, "")
        ),
        patch(
            "orchestune.integrator.attempt._single_fast_path",
            return_value=SimpleNamespace(phase=SemanticReviewPhase.NOT_STARTED),
        ),
    ):
        assert start_required_review(ctx, MagicMock()) == "semantic_review_pending"
    assert ctx.semantic_review_state.phase is SemanticReviewPhase.NOT_STARTED
    assert ctx.semantic_review_state.ci_verified_sha == HEAD
    ctx.forge.add_comment.assert_called_once()
    ctx.config.coordinator.dispatch_review.assert_not_called()


def test_build_attempt_identity_is_order_independent_and_deterministic():
    first = build_attempt_identity(100, BASE, [CHILD_B, CHILD_A])
    second = build_attempt_identity(100, BASE, [CHILD_A, CHILD_B])

    assert first == second
    assert first.temp_branch.endswith(first.attempt_id[:12])


def test_fast_forward_identity_accepts_multi_commit_descendant():
    with patch("orchestune.integrator.attempt.run_git") as run:
        run.return_value = GitResult(0, "", "")
        assert check_fast_forward_identity(Path("."), BASE, CHILD_A.source_sha)
        run.assert_called_once_with(
            ["merge-base", "--is-ancestor", BASE, CHILD_A.source_sha],
            cwd=Path("."),
            check=False,
        )


def test_fast_path_lookup_failure_posts_not_started_state_when_review_is_managed():
    identity = _identity(CHILD_A)
    forge = MagicMock(spec=Forge)
    ctx = SimpleNamespace(
        attempt_identity=identity,
        integration_pr_number=7,
        repository_root=Path("."),
        forge=forge,
        config=SimpleNamespace(
            force_integration_review=False,
            coordinator=object(),
        ),
        semantic_review_state=None,
    )
    with patch("orchestune.integrator.attempt.run_git") as run:
        run.return_value = GitResult(0, HEAD + "\n", "")
        status = start_required_review(
            ctx, lambda _forge, _issue: (_ for _ in ()).throw(OSError("offline"))
        )

    assert status == "semantic_review_pending"
    assert ctx.semantic_review_state.phase is SemanticReviewPhase.NOT_STARTED
    forge.add_comment.assert_called_once()


@pytest.mark.parametrize("lookup_error", [False, True])
def test_required_single_without_coordinator_persists_pending(lookup_error):
    ctx = _finalization_context()
    ctx.config.coordinator = None
    ctx.semantic_review_state = None
    outcome = MagicMock()
    outcome.record = None
    fetch = MagicMock(return_value=outcome)
    if lookup_error:
        fetch.side_effect = OSError("offline")
    with (
        patch(
            "orchestune.integrator.attempt.run_git", return_value=GitResult(0, HEAD, "")
        ),
        patch(
            "orchestune.integrator.attempt.check_fast_forward_identity",
            return_value=False,
        ),
    ):
        status = start_required_review(ctx, fetch)
    assert status == "semantic_review_pending"
    assert ctx.semantic_review_state.phase is SemanticReviewPhase.NOT_STARTED
    ctx.forge.add_comment.assert_called_once()


def test_required_single_without_state_cannot_finalize_without_coordinator():
    ctx = _finalization_context()
    ctx.config.coordinator = None
    ctx.semantic_review_state = None
    assert validate_finalization_context(ctx) == "semantic review state is missing"


def test_runner_invalidated_attempt_allows_fresh_preparation():
    ctx = SimpleNamespace(
        config=SimpleNamespace(apply=True, parent_issue_number=100),
        forge=MagicMock(spec=Forge),
        repository_root=Path("."),
    )
    with (
        patch(
            "orchestune.integrator.attempt.find_open_attempts",
            return_value=[_attempt(CHILD_A, phase=SemanticReviewPhase.INVALIDATED)],
        ),
        patch(
            "orchestune.integrator.attempt._discover_child_sources",
            return_value=([CHILD_B], {}),
        ),
        patch("orchestune.integrator.attempt._base_sha", return_value=BASE),
    ):
        assert prepare_attempt_context(ctx, object()) == (None, None)
    ctx.forge.add_comment.assert_not_called()


def test_a_leftover_invalidated_attempt_with_the_same_children_does_not_block_a_new_one():
    """#1440: a failed branch deletion must not keep new attempts from starting."""
    ctx = SimpleNamespace(
        config=SimpleNamespace(apply=True, parent_issue_number=100),
        forge=MagicMock(spec=Forge),
        repository_root=Path("."),
    )
    leftover = _attempt(CHILD_A, phase=SemanticReviewPhase.INVALIDATED)
    with (
        patch(
            "orchestune.integrator.attempt.find_open_attempts",
            return_value=[leftover],
        ),
        patch(
            "orchestune.integrator.attempt._discover_child_sources",
            return_value=([CHILD_A], {}),
        ),
        patch("orchestune.integrator.attempt._base_sha", return_value=BASE),
    ):
        assert prepare_attempt_context(ctx, object()) == (None, None)
    selection = select_attempt_children([leftover], BASE, [CHILD_A])
    assert selection.reusable is None and not selection.fail_closed


def test_restore_attempt_reads_a_closed_pr_with_the_same_trust_rules():
    state = _state(CHILD_A, phase=SemanticReviewPhase.INVALIDATED)
    forge = MagicMock(spec=Forge)
    forge.get_authenticated_user.return_value = "orchestune"
    forge.list_notice_trusted_authors.return_value = frozenset()
    forge.list_comments.return_value = [
        {"body": format_semantic_review_state_marker(state), "author": "mallory"},
        {"body": format_semantic_review_state_marker(state), "author": "orchestune"},
    ]
    closed = PrRecord(
        7,
        _identity(CHILD_A).temp_branch,
        (),
        base_ref="parent/issue-100",
        is_cross_repository=False,
        head_sha=HEAD,
        state="CLOSED",
    )

    attempt = restore_attempt(forge, TrustContext(forge), 100, closed)

    assert attempt.state == state and attempt.state_error is None
    moved = restore_attempt(
        forge, TrustContext(forge), 100, replace(closed, head_sha="e" * 40)
    )
    assert moved.state is None and "head" in str(moved.state_error)
    forged = restore_attempt(
        forge, TrustContext(forge), 100, replace(closed, head_ref="integration/temp-x")
    )
    assert forged.state is None


def test_reuse_restores_frozen_children_without_duplicates():
    from orchestune.integrator.attempt import _restore_attempt_context

    selection = select_attempt_children([_attempt(CHILD_A)], BASE, [CHILD_A, CHILD_B])
    ctx = SimpleNamespace(
        config=SimpleNamespace(),
        merged_tasks=["task-a", "task-b"],
        active_done_tasks=[
            SimpleNamespace(subtask_id="task-a"),
            SimpleNamespace(subtask_id="task-b"),
        ],
        merged_task_proofs={},
        task_merge_receipts={},
    )
    assert _restore_attempt_context(ctx, selection, {}) == "semantic_review_pending"
    assert ctx.merged_tasks == ["task-a"]
    assert ctx.deferred_tasks == ["task-b"]


def test_publish_attempt_branch_is_create_only():
    identity = _identity(CHILD_A)
    with patch("orchestune.integrator.attempt.run_git") as run:
        result = publish_attempt_branch(Path("."), identity, HEAD)

    assert result.status is PublishStatus.CREATED
    assert run.call_args.args[0] == [
        "push",
        "--porcelain",
        f"--force-with-lease=refs/heads/{identity.temp_branch}:",
        "origin",
        f"{HEAD}:refs/heads/{identity.temp_branch}",
    ]


def test_publish_attempt_branch_adopts_only_equivalent_existing_ref():
    identity = _identity(CHILD_A)
    rejected = subprocess.CalledProcessError(1, ["git", "push"], stderr="stale info")
    with patch("orchestune.integrator.attempt.run_git") as run:
        run.side_effect = [
            rejected,
            GitResult(0, HEAD + "\n", ""),
            GitResult(0, "", ""),
            GitResult(0, "tree\n", ""),
            GitResult(0, "tree\n", ""),
            GitResult(0, "", ""),
            GitResult(0, "", ""),
        ]
        result = publish_attempt_branch(Path("."), identity, HEAD)

    assert result.status is PublishStatus.ADOPTED


def test_checkout_verified_attempt_head_verifies_remote_and_all_ancestors():
    identity = _identity(CHILD_A, CHILD_B)
    with patch("orchestune.integrator.attempt.run_git") as run:
        run.side_effect = [
            GitResult(0, "", ""),
            GitResult(0, HEAD + "\n", ""),
            GitResult(0, "", ""),
            GitResult(0, "", ""),
            GitResult(0, "", ""),
            GitResult(0, "", ""),
        ]

        assert checkout_verified_attempt_head(
            Path("."), identity.temp_branch, HEAD, BASE, identity.child_sources
        )

    assert run.call_args_list[-1] == call(
        ["checkout", "--detach", HEAD], cwd=Path("."), check=True
    )


def _finalization_context():
    identity = _identity(CHILD_A)
    state = _state(CHILD_A, phase=SemanticReviewPhase.PASSED)
    state = replace(state, ci_verified_sha=HEAD)
    forge = MagicMock(spec=Forge)
    forge.get_authenticated_user.return_value = "orchestune"
    forge.list_open_prs.return_value = [
        PrRecord(
            7,
            identity.temp_branch,
            (),
            base_ref="parent/issue-100",
            is_cross_repository=False,
            head_sha=HEAD,
        )
    ]
    config = SimpleNamespace(
        parent_issue_number=100,
        semantic_review_mode="required",
        enable_semantic_review=True,
        child_review_gate="required",
        coordinator=object(),
        force_integration_review=False,
    )
    return SimpleNamespace(
        attempt_identity=identity,
        semantic_review_state=state,
        semantic_review_override=None,
        integration_pr_number=7,
        repository_root=Path("."),
        forge=forge,
        config=config,
    )


def test_finalization_context_requires_live_pr_head_and_matching_ci():
    ctx = _finalization_context()
    with patch("orchestune.integrator.attempt.run_git") as run:
        run.return_value = GitResult(0, HEAD + "\n", "")
        assert validate_finalization_context(ctx) is None

    ctx.forge.list_open_prs.return_value[0] = PrRecord(
        7,
        ctx.attempt_identity.temp_branch,
        (),
        base_ref="parent/issue-100",
        is_cross_repository=False,
        head_sha="e" * 40,
    )
    with patch("orchestune.integrator.attempt.run_git") as run:
        run.return_value = GitResult(0, HEAD + "\n", "")
        assert "live integration PR" in validate_finalization_context(ctx)


def test_parent_update_is_atomic_under_the_attempt_claim():
    from orchestune.integrator.semantic_claim import (
        ClaimResult,
        ClaimStatus,
        PushResult,
        PushStatus,
        SemanticClaim,
    )

    ctx = _finalization_context()
    claim = SemanticClaim(
        100,
        ctx.attempt_identity.attempt_id,
        "orchestune",
        1,
        datetime.now(UTC) + timedelta(minutes=5),
        "f" * 40,
    )
    with (
        patch("orchestune.integrator.attempt.acquire_semantic_claim") as acquire,
        patch("orchestune.integrator.attempt.push_refs_under_claim") as push,
    ):
        acquire.return_value = ClaimResult(ClaimStatus.ACQUIRED, claim)
        push.return_value = PushResult(PushStatus.PUSHED, claim)

        result = publish_parent_under_claim(ctx)

    assert result.handled and result.pushed
    update = push.call_args.args[2][0]
    assert update.ref == "refs/heads/parent/issue-100"
    assert update.new_oid == HEAD
    assert update.expected_old_oid == BASE


def test_handle_non_required_review_prioritizes_required_mode():
    """#1384: required mode is never bypassed by enable=False or child_review_gate=off."""
    ctx = _finalization_context()
    ctx.config.semantic_review_mode = "required"
    ctx.config.enable_semantic_review = False
    ctx.config.child_review_gate = "off"

    assert handle_non_required_review(ctx) == (False, False)


def test_handle_non_required_review_off_and_advisory():
    """#1384: off skips with warning; advisory dispatches or skips based on coordinator."""
    ctx = _finalization_context()
    ctx.config.semantic_review_mode = "off"
    assert handle_non_required_review(ctx) == (True, False)

    ctx.config.semantic_review_mode = "advisory"
    ctx.config.enable_semantic_review = False
    assert handle_non_required_review(ctx) == (True, False)

    ctx.config.enable_semantic_review = True
    coordinator = MagicMock()
    ctx.config.coordinator = coordinator
    ctx.temp_branch = "temp"
    ctx.base_branch = "base"
    ctx.integration_pr_number = 7
    ctx.merged_tasks = ["task-a"]
    assert handle_non_required_review(ctx) == (True, True)
    coordinator.dispatch_review.assert_called_once_with(
        temp_branch="temp",
        base_branch="base",
        pr_number=7,
        parent_issue_number=100,
        merged_subtask_ids=["task-a"],
    )


def test_semantic_finalization_exempt():
    """#1384: _semantic_finalization_exempt only exempts off and advisory, never required."""
    ctx = _finalization_context()
    ctx.config.semantic_review_mode = "required"
    ctx.config.enable_semantic_review = False
    ctx.config.child_review_gate = "off"
    assert _semantic_finalization_exempt(ctx) is False

    ctx.config.semantic_review_mode = "advisory"
    assert _semantic_finalization_exempt(ctx) is True

    ctx.config.semantic_review_mode = "off"
    assert _semantic_finalization_exempt(ctx) is True


def test_validate_finalization_context_off_mode_skipped_by_human_without_override():
    """#1384: off mode accepts trusted SKIPPED_BY_HUMAN without human override."""
    ctx = _finalization_context()
    ctx.config.semantic_review_mode = "off"
    ctx.semantic_review_state = replace(
        ctx.semantic_review_state, phase=SemanticReviewPhase.SKIPPED_BY_HUMAN
    )
    ctx.semantic_review_override = None

    with patch("orchestune.integrator.attempt.run_git") as run:
        run.return_value = GitResult(0, HEAD + "\n", "")
        assert validate_finalization_context(ctx) is None


def test_validate_finalization_context_off_mode_still_checks_invariants():
    """#1384: off mode still verifies HEAD, CI, PR, and terminal phase."""
    ctx = _finalization_context()
    ctx.config.semantic_review_mode = "off"
    ctx.semantic_review_state = replace(
        ctx.semantic_review_state, phase=SemanticReviewPhase.SKIPPED_BY_HUMAN
    )

    # 1. Stale / mismatching checkout HEAD
    with patch("orchestune.integrator.attempt.run_git") as run:
        run.return_value = GitResult(0, "x" * 40 + "\n", "")
        assert "checked out integration head differs" in validate_finalization_context(
            ctx
        )

    # 2. CI mismatch
    ctx.semantic_review_state = replace(
        ctx.semantic_review_state, ci_verified_sha="x" * 40
    )
    with patch("orchestune.integrator.attempt.run_git") as run:
        run.return_value = GitResult(0, HEAD + "\n", "")
        assert "lacks matching CI evidence" in validate_finalization_context(ctx)
    ctx.semantic_review_state = replace(ctx.semantic_review_state, ci_verified_sha=HEAD)

    # 3. Live PR mismatch
    ctx.forge.list_open_prs.return_value[0] = replace(
        ctx.forge.list_open_prs.return_value[0], head_sha="x" * 40
    )
    with patch("orchestune.integrator.attempt.run_git") as run:
        run.return_value = GitResult(0, HEAD + "\n", "")
        assert "live integration PR" in validate_finalization_context(ctx)
    ctx.forge.list_open_prs.return_value[0] = replace(
        ctx.forge.list_open_prs.return_value[0], head_sha=HEAD
    )

    # 4. Non-terminal phase (e.g. NOT_STARTED)
    ctx.semantic_review_state = replace(
        ctx.semantic_review_state, phase=SemanticReviewPhase.NOT_STARTED
    )
    with patch("orchestune.integrator.attempt.run_git") as run:
        run.return_value = GitResult(0, HEAD + "\n", "")
        assert "phase is not passed: not_started" in validate_finalization_context(ctx)


def test_validate_finalization_context_requires_override_when_mode_is_not_off():
    """#1384: required and advisory require human override for SKIPPED_BY_HUMAN."""
    from orchestune.review.semantic_gate import SemanticReviewHumanOverrideV1

    ctx = _finalization_context()
    ctx.semantic_review_state = replace(
        ctx.semantic_review_state, phase=SemanticReviewPhase.SKIPPED_BY_HUMAN
    )

    with patch("orchestune.integrator.attempt.run_git") as run:
        run.return_value = GitResult(0, HEAD + "\n", "")

        # In required mode without override
        ctx.config.semantic_review_mode = "required"
        ctx.semantic_review_override = None
        assert (
            validate_finalization_context(ctx) == "human override evidence is missing"
        )

        # In advisory mode without override
        ctx.config.semantic_review_mode = "advisory"
        ctx.semantic_review_override = None
        assert (
            validate_finalization_context(ctx) == "human override evidence is missing"
        )

        # With override matching attempt_id and head_sha
        ctx.config.semantic_review_mode = "required"
        ctx.semantic_review_override = SemanticReviewHumanOverrideV1(
            attempt_id=ctx.semantic_review_state.attempt_id,
            head_sha=HEAD,
            reason="human approved",
        )
        assert validate_finalization_context(ctx) is None

        # With override having mismatched head_sha
        ctx.semantic_review_override = SemanticReviewHumanOverrideV1(
            attempt_id=ctx.semantic_review_state.attempt_id,
            head_sha="x" * 40,
            reason="human approved",
        )
        assert (
            validate_finalization_context(ctx)
            == "human override does not match the passed head"
        )
