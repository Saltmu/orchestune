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
    build_attempt_identity,
    check_fast_forward_identity,
    checkout_verified_attempt_head,
    find_open_attempts,
    publish_attempt_branch,
    publish_parent_under_claim,
    select_attempt_children,
    validate_finalization_context,
)
from orchestune.integrator.pr import list_open_attempt_prs
from orchestune.models import PrRecord
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
