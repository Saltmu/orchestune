"""Deterministic integration-attempt discovery and git operations."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from orchestune.forge import Forge
from orchestune.infra.git_cli import run_git
from orchestune.integrator.pr import list_open_attempt_prs
from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.integrator.semantic_claim import (
    ClaimStatus,
    RefUpdate,
    acquire_semantic_claim,
    delete_attempt_fix_branches,
    push_refs_under_claim,
)
from orchestune.integrator.semantic_claim import (
    PushStatus as ClaimPushStatus,
)
from orchestune.pr_link_notice_trust import TrustContext
from orchestune.review.semantic_gate import (
    ACTIVE_PENDING_PHASES,
    TERMINAL_PASSED_PHASES,
    ChildSourceRef,
    IntegrationAttemptIdentity,
    SemanticReviewHumanOverrideV1,
    SemanticReviewPhase,
    SemanticReviewStateV1,
    evaluate_single_task_fast_path,
    format_semantic_review_state_marker,
    normalize_child_sources,
    restore_latest_human_override,
    restore_latest_semantic_review_state,
)

_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})


def _error_detail(error: BaseException) -> str:
    detail = getattr(error, "stderr", None)
    return detail if isinstance(detail, str) and detail else str(error)


def mark_tasks_included(ctx: Any, subtask_ids: set[str] | None = None) -> list[str]:
    """Label only finalized children that are not already marked included."""
    newly_included: list[str] = []
    tasks = {task.subtask_id: task for task in ctx.active_done_tasks if task.subtask_id}
    for subtask_id in ctx.merged_tasks:
        if subtask_ids is not None and subtask_id not in subtask_ids:
            continue
        task = tasks.get(subtask_id)
        if task is None or "integration:included" in task.status_labels:
            continue
        try:
            ctx.forge.add_label(task.issue_number, "integration:included")
            newly_included.append(subtask_id)
        except Exception as error:
            print(
                "Warning: Failed to add integration:included label to "
                f"issue #{task.issue_number}: {error}",
                file=sys.stderr,
            )
    return newly_included


@dataclass(frozen=True)
class OpenAttempt:
    pr_number: int
    temp_branch: str
    base_branch: str
    head_sha: str
    state: SemanticReviewStateV1 | None = None
    override: SemanticReviewHumanOverrideV1 | None = None
    state_error: str | None = None


@dataclass(frozen=True)
class AttemptSelection:
    children: tuple[ChildSourceRef, ...]
    deferred: tuple[ChildSourceRef, ...] = ()
    reusable: OpenAttempt | None = None
    invalidated: tuple[OpenAttempt, ...] = ()
    fail_closed: bool = False


class PublishStatus(StrEnum):
    CREATED = "created"
    ADOPTED = "adopted"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class PublishResult:
    status: PublishStatus
    remote_head_sha: str | None = None
    detail: str = ""

    @property
    def published(self) -> bool:
        return self.status in {PublishStatus.CREATED, PublishStatus.ADOPTED}


@dataclass(frozen=True)
class ParentPublishResult:
    handled: bool
    pushed: bool
    detail: str = ""


def _trusted_state_authors(forge: Forge, trust: TrustContext) -> frozenset[str]:
    trusted: set[str] = set()
    identity = trust.get_self_identity()
    if identity:
        trusted.add(identity.casefold())
    configured = trust.get_trusted_collaborators()
    if configured:
        trusted.update(
            author.casefold()
            for author in configured
            if author.casefold().endswith("[bot]")
        )
    return frozenset(trusted)


def _comment_body(comment: dict[str, object]) -> str | None:
    body = comment.get("body")
    return body if isinstance(body, str) else None


def _state_bodies(
    comments: Sequence[dict[str, object]], trust: TrustContext, trusted: frozenset[str]
) -> list[str]:
    bodies: list[str] = []
    for comment in comments:
        author = trust.extract_author(comment)
        body = _comment_body(comment)
        if author and body and author.casefold() in trusted:
            bodies.append(body)
    return bodies


def _human_override_bodies(
    forge: Forge, comments: Sequence[dict[str, object]], trust: TrustContext
) -> list[tuple[str, str]]:
    bodies: list[tuple[str, str]] = []
    for comment in comments:
        author = trust.extract_author(comment)
        body = _comment_body(comment)
        if not author or not body or author.casefold().endswith("[bot]"):
            continue
        if forge.get_actor_permission(author).casefold() in _WRITE_PERMISSIONS:
            bodies.append((body, author))
    return bodies


def _restore_override(
    forge: Forge,
    comments: Sequence[dict[str, object]],
    trust: TrustContext,
    state: SemanticReviewStateV1,
) -> SemanticReviewHumanOverrideV1 | None:
    candidates = _human_override_bodies(forge, comments, trust)
    for index in range(len(candidates) - 1, -1, -1):
        body, actor = candidates[index]
        override = restore_latest_human_override(
            [body], attempt_id=state.attempt_id, head_sha=state.head_sha
        )
        if override is not None:
            return replace(override, actor=actor)
    return None


def _state_binding_error(
    state: SemanticReviewStateV1, pr_number: int, parent: int, branch: str, head: str
) -> str | None:
    identity = IntegrationAttemptIdentity.create(
        state.parent_issue_number, state.base_sha, state.child_sources
    )
    if state.attempt_id != identity.attempt_id or branch != identity.temp_branch:
        return "semantic state attempt identity does not match the PR branch"
    if state.pr_number != pr_number or state.parent_issue_number != parent:
        return "semantic state PR or parent binding does not match"
    if head and state.head_sha != head:
        return "semantic state head does not match the PR head"
    return None


def _open_attempt(forge: Forge, trust: TrustContext, parent: int, pr) -> OpenAttempt:
    comments = forge.list_comments(pr.number)
    trusted = _trusted_state_authors(forge, trust)
    try:
        state = restore_latest_semantic_review_state(
            _state_bodies(comments, trust, trusted)
        )
        error = "trusted semantic review state is missing" if state is None else None
        if state is not None:
            error = _state_binding_error(
                state, pr.number, parent, pr.head_ref, pr.head_sha
            )
        override = (
            _restore_override(forge, comments, trust, state)
            if state is not None and error is None
            else None
        )
    except (ValueError, OSError) as exc:
        state, override, error = None, None, str(exc)
    return OpenAttempt(
        pr.number,
        pr.head_ref,
        pr.base_ref,
        pr.head_sha,
        state=state if error is None else None,
        override=override,
        state_error=error,
    )


def find_open_attempts(forge: Forge, parent_issue_number: int) -> list[OpenAttempt]:
    """Restore trusted attempts for one parent; broken histories remain visible."""
    trust = TrustContext(forge)
    attempts: list[OpenAttempt] = []
    for pr in list_open_attempt_prs(forge, parent_issue_number):
        try:
            attempts.append(_open_attempt(forge, trust, parent_issue_number, pr))
        except Exception as exc:
            attempts.append(
                OpenAttempt(
                    pr.number,
                    pr.head_ref,
                    pr.base_ref,
                    pr.head_sha,
                    state_error=str(exc),
                )
            )
    return attempts


def _attempt_matches(
    attempt: OpenAttempt, base_sha: str, children: tuple[ChildSourceRef, ...]
) -> bool:
    state = attempt.state
    if state is None or state.phase is SemanticReviewPhase.INVALIDATED:
        return False
    available = {child.key() for child in children}
    return state.base_sha == base_sha and all(
        child.key() in available for child in state.child_sources
    )


def select_attempt_children(
    open_attempts: Sequence[OpenAttempt],
    base_sha: str,
    done_children: Sequence[ChildSourceRef],
) -> AttemptSelection:
    """Freeze the children of one matching attempt and defer later arrivals."""
    children = normalize_child_sources(done_children)
    broken = tuple(attempt for attempt in open_attempts if attempt.state_error)
    if broken:
        return AttemptSelection(children, fail_closed=True)
    matches = tuple(
        attempt
        for attempt in open_attempts
        if _attempt_matches(attempt, base_sha, children)
    )
    if len(matches) > 1:
        return AttemptSelection(children, fail_closed=True)
    invalidated = tuple(
        attempt
        for attempt in open_attempts
        if attempt not in matches
        and attempt.state is not None
        and attempt.state.phase is not SemanticReviewPhase.INVALIDATED
    )
    if not matches:
        return AttemptSelection(children, invalidated=invalidated)
    frozen = matches[0].state.child_sources  # type: ignore[union-attr]
    frozen_keys = {child.key() for child in frozen}
    deferred = tuple(child for child in children if child.key() not in frozen_keys)
    return AttemptSelection(frozen, deferred, matches[0], invalidated)


def build_attempt_identity(
    parent_issue_number: int,
    base_sha: str,
    child_sources: Sequence[ChildSourceRef],
) -> IntegrationAttemptIdentity:
    return IntegrationAttemptIdentity.create(
        parent_issue_number, base_sha, child_sources
    )


def _base_sha(ctx: Any) -> str:
    return run_git(
        ["rev-parse", "--verify", ctx.base_branch], cwd=ctx.repository_root
    ).stdout.strip()


def _discover_child_sources(
    ctx: Any, merger: Any
) -> tuple[list[ChildSourceRef], dict[int, TaskIntegrationProof]] | None:
    children: list[ChildSourceRef] = []
    proofs: dict[int, TaskIntegrationProof] = {}
    for task in ctx.active_done_tasks:
        resolution, _, source_sha, _ = merger._resolve_and_fetch_task_branch(  # noqa: SLF001
            task, ctx.base_branch
        )
        if resolution is None or source_sha is None:
            return None
        children.append(ChildSourceRef(task.issue_number, task.subtask_id, source_sha))
        proofs[task.issue_number] = TaskIntegrationProof(
            task.issue_number,
            task.subtask_id,
            resolution.branch_name,
            source_sha,
            resolution.source,
            resolution,
        )
    return children, proofs


def _restore_attempt_context(
    ctx: Any, selection: AttemptSelection, proofs: dict
) -> str:
    attempt = selection.reusable
    assert attempt is not None and attempt.state is not None
    state = attempt.state
    ctx.attempt_identity = build_attempt_identity(
        state.parent_issue_number, state.base_sha, state.child_sources
    )
    ctx.attempt_base_sha = state.base_sha
    ctx.temp_branch = ctx.config.temp_branch = attempt.temp_branch
    ctx.integration_pr_number = attempt.pr_number
    ctx.semantic_review_state = state
    ctx.semantic_review_override = attempt.override
    ctx.reuse_existing_temp_branch = True
    ctx.deferred_tasks = [child.subtask_id for child in selection.deferred]
    selected = {child.subtask_id for child in state.child_sources}
    ctx.active_done_tasks = [
        task for task in ctx.active_done_tasks if task.subtask_id in selected
    ]
    ctx.merged_tasks = [child.subtask_id for child in state.child_sources]
    ctx.merged_task_proofs.update(
        {
            number: proof
            for number, proof in proofs.items()
            if proof.subtask_id in selected
        }
    )
    ctx.task_merge_receipts.update(
        {
            number: proof.merge_receipt
            for number, proof in ctx.merged_task_proofs.items()
        }
    )
    if state.phase in ACTIVE_PENDING_PHASES:
        return "semantic_review_pending"
    if state.phase not in TERMINAL_PASSED_PHASES:
        return "review_gate_blocked"
    verified = checkout_verified_attempt_head(
        ctx.repository_root,
        attempt.temp_branch,
        state.head_sha,
        state.base_sha,
        state.child_sources,
    )
    return "success" if verified else "review_gate_blocked"


def prepare_attempt_context(ctx: Any, merger: Any) -> tuple[str | None, str | None]:
    """Restore a reusable attempt before merge, or permit a fresh merge."""
    if not ctx.config.apply:
        return None, None
    attempts = find_open_attempts(ctx.forge, ctx.config.parent_issue_number)
    if not attempts:
        return None, None
    discovered = _discover_child_sources(ctx, merger)
    if discovered is None:
        return None, None
    children, proofs = discovered
    selection = select_attempt_children(attempts, _base_sha(ctx), children)
    if selection.fail_closed or selection.invalidated:
        # #1362 owns the durable INVALIDATED transition. Until that trusted
        # marker exists, publishing a replacement attempt would race its runner.
        return "review_gate_blocked", "existing attempt needs trusted invalidation"
    if selection.reusable is None:
        return None, None
    status = _restore_attempt_context(ctx, selection, proofs)
    error = None if status == "success" else "integration attempt is not finalizable"
    return status, error


def set_new_attempt_identity(ctx: Any) -> None:
    children = [
        ChildSourceRef(proof.issue_number, proof.subtask_id, proof.source_sha)
        for proof in ctx.merged_task_proofs.values()
        if proof.subtask_id in ctx.merged_tasks
    ]
    identity = build_attempt_identity(
        ctx.config.parent_issue_number, ctx.attempt_base_sha, children
    )
    ctx.attempt_identity = identity
    ctx.temp_branch = ctx.config.temp_branch = identity.temp_branch


def handle_non_required_review(ctx: Any) -> tuple[bool, bool]:
    """Handle off/advisory modes and return ``(handled, dispatched)``."""
    mode = ctx.config.semantic_review_mode
    if (
        mode == "off"
        or not ctx.config.enable_semantic_review
        or (mode == "required" and ctx.config.child_review_gate == "off")
    ):
        print(
            "Warning: semantic_review_mode is off; skipping layer-2 review",
            file=sys.stderr,
        )
        return True, False
    if mode != "advisory":
        return False, False
    coordinator = ctx.config.coordinator
    if coordinator is None:
        return True, False
    try:
        coordinator.dispatch_review(
            temp_branch=ctx.temp_branch,
            base_branch=ctx.base_branch,
            pr_number=ctx.integration_pr_number,
            parent_issue_number=ctx.config.parent_issue_number,
            merged_subtask_ids=ctx.merged_tasks,
        )
        ctx.semantic_review_dispatched = True
        return True, True
    except Exception as error:
        print(f"Warning: Failed to dispatch semantic review: {error}", file=sys.stderr)
        return True, False


def start_required_review(ctx: Any, fetch_outcome: Callable[[Forge, int], Any]) -> str:
    identity = ctx.attempt_identity
    assert identity is not None and ctx.integration_pr_number is not None
    head = (
        run_git(["rev-parse", "HEAD"], cwd=ctx.repository_root).stdout.strip() or "HEAD"
    )
    phase = SemanticReviewPhase.NOT_STARTED
    if len(identity.child_sources) == 1:
        try:
            decision = _single_fast_path(ctx, identity, fetch_outcome)
            phase = decision.phase
        except Exception as error:
            print(
                f"Warning: failed to evaluate the single-task Fast-Path: {error}",
                file=sys.stderr,
            )
    state = SemanticReviewStateV1(
        identity.attempt_id,
        identity.parent_issue_number,
        ctx.integration_pr_number,
        phase,
        0,
        head,
        identity.base_sha,
        identity.child_sources,
        ci_verified_sha=head,
    )
    ctx.forge.add_comment(
        ctx.integration_pr_number, format_semantic_review_state_marker(state)
    )
    ctx.semantic_review_state = state
    return (
        "success"
        if phase is SemanticReviewPhase.FAST_PATH_PASSED
        else "semantic_review_pending"
    )


def _single_fast_path(
    ctx: Any,
    identity: IntegrationAttemptIdentity,
    fetch_outcome: Callable[[Forge, int], Any],
):
    child = identity.child_sources[0]
    record = fetch_outcome(ctx.forge, child.issue_number).record
    fast_forward = check_fast_forward_identity(
        ctx.repository_root, identity.base_sha, child.source_sha
    )
    return evaluate_single_task_fast_path(
        child_count=1,
        force_integration_review=ctx.config.force_integration_review,
        base_sha_matches=fast_forward,
        tree_matches_without_conflict=fast_forward,
        layer1_verdict=None if record is None else record.review.verdict,
        layer1_reviewed_sha=None if record is None else record.review.reviewed_head_sha,
        source_sha=child.source_sha,
    )


def validate_finalization_context(ctx: Any) -> str | None:
    """Revalidate durable state, checkout and live PR before finalization."""
    identity = ctx.attempt_identity
    state = ctx.semantic_review_state
    if identity is None:
        exempt = _semantic_finalization_exempt(ctx)
        return None if exempt else "integration attempt identity is missing"
    if state is None:
        exempt = _semantic_finalization_exempt(ctx)
        return None if exempt else "semantic review state is missing"
    if state.phase not in TERMINAL_PASSED_PHASES:
        return f"semantic review phase is not passed: {state.phase.value}"
    if (
        not identity.matches_state(state)
        or identity.child_sources != state.child_sources
    ):
        return "semantic review state does not match the integration identity"
    if state.pr_number != ctx.integration_pr_number:
        return "semantic review state does not match the integration PR"
    if state.ci_verified_sha != state.head_sha:
        return "semantic review head lacks matching CI evidence"
    current = run_git(["rev-parse", "HEAD"], cwd=ctx.repository_root).stdout.strip()
    if current and current != state.head_sha:
        return "checked out integration head differs from semantic review state"
    matching = [
        pr
        for pr in list_open_attempt_prs(ctx.forge, identity.parent_issue_number)
        if pr.number == state.pr_number
    ]
    if len(matching) != 1 or matching[0].head_sha != state.head_sha:
        return "live integration PR does not match the passed semantic review head"
    if state.phase is SemanticReviewPhase.SKIPPED_BY_HUMAN:
        override = ctx.semantic_review_override
        if override is None or override.attempt_id != state.attempt_id:
            return "human override evidence is missing"
        if override.head_sha != state.head_sha:
            return "human override does not match the passed head"
    return None


def _semantic_finalization_exempt(ctx: Any) -> bool:
    return (
        ctx.config.semantic_review_mode in {"off", "advisory"}
        or not ctx.config.enable_semantic_review
        or ctx.config.child_review_gate == "off"
    )


def publish_parent_under_claim(ctx: Any) -> ParentPublishResult:
    """Atomically advance the parent ref and the semantic claim."""
    state, identity = ctx.semantic_review_state, ctx.attempt_identity
    if state is None or identity is None:
        return ParentPublishResult(False, False)
    holder = ctx.forge.get_authenticated_user() or "orchestune-integrator"
    now = datetime.now(UTC)
    acquired = acquire_semantic_claim(
        ctx.repository_root,
        parent_issue_number=identity.parent_issue_number,
        attempt_id=identity.attempt_id,
        holder=holder,
        now=now,
        ttl_seconds=300,
    )
    if acquired.status is not ClaimStatus.ACQUIRED or acquired.claim is None:
        return ParentPublishResult(
            True, False, acquired.detail or acquired.status.value
        )
    parent_ref = f"refs/heads/parent/issue-{identity.parent_issue_number}"
    result = push_refs_under_claim(
        ctx.repository_root,
        acquired.claim,
        [RefUpdate(parent_ref, state.head_sha, state.base_sha)],
        now=now,
    )
    return ParentPublishResult(
        True,
        result.status is ClaimPushStatus.PUSHED,
        result.detail or result.status.value,
    )


def cleanup_attempt_fix_outputs(ctx: Any) -> None:
    if ctx.attempt_identity is not None:
        delete_attempt_fix_branches(
            ctx.repository_root, ctx.attempt_identity.attempt_id
        )


def check_fast_forward_identity(
    repository_root: Path, base_sha: str, child_source_sha: str
) -> bool:
    result = run_git(
        ["merge-base", "--is-ancestor", base_sha, child_source_sha],
        cwd=repository_root,
        check=False,
    )
    return result.returncode == 0


def _remote_head(repository_root: Path, branch: str) -> str | None:
    result = run_git(
        ["ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
        cwd=repository_root,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0].lower()


def _tree(repository_root: Path, ref: str) -> str:
    return run_git(["rev-parse", f"{ref}^{{tree}}"], cwd=repository_root).stdout.strip()


def _adoptable_existing_ref(
    repository_root: Path,
    identity: IntegrationAttemptIdentity,
    local_head_sha: str,
    remote_head: str,
) -> bool:
    run_git(
        [
            "fetch",
            "origin",
            f"{identity.temp_branch}:refs/remotes/origin/{identity.temp_branch}",
        ],
        cwd=repository_root,
    )
    remote_ref = f"origin/{identity.temp_branch}"
    if _tree(repository_root, remote_ref) != _tree(repository_root, local_head_sha):
        return False
    ancestors = (
        identity.base_sha,
        *(child.source_sha for child in identity.child_sources),
    )
    return all(
        run_git(
            ["merge-base", "--is-ancestor", ancestor, remote_head],
            cwd=repository_root,
            check=False,
        ).returncode
        == 0
        for ancestor in ancestors
    )


def publish_attempt_branch(
    repository_root: Path,
    identity: IntegrationAttemptIdentity,
    local_head_sha: str,
) -> PublishResult:
    """Create the deterministic remote ref without overwriting an existing tip."""
    ref = f"refs/heads/{identity.temp_branch}"
    try:
        run_git(
            [
                "push",
                "--porcelain",
                f"--force-with-lease={ref}:",
                "origin",
                f"{local_head_sha}:{ref}",
            ],
            cwd=repository_root,
        )
        return PublishResult(PublishStatus.CREATED, local_head_sha)
    except (subprocess.CalledProcessError, OSError) as exc:
        try:
            remote_head = _remote_head(repository_root, identity.temp_branch)
        except (subprocess.CalledProcessError, OSError):
            return PublishResult(PublishStatus.FAILED, detail=_error_detail(exc))
        if remote_head is None:
            return PublishResult(PublishStatus.FAILED, detail=_error_detail(exc))
        try:
            if _adoptable_existing_ref(
                repository_root, identity, local_head_sha, remote_head
            ):
                return PublishResult(PublishStatus.ADOPTED, remote_head)
        except (subprocess.CalledProcessError, OSError, ValueError) as verify_error:
            return PublishResult(PublishStatus.BLOCKED, remote_head, str(verify_error))
        return PublishResult(PublishStatus.BLOCKED, remote_head, "existing ref differs")


def checkout_verified_attempt_head(
    repository_root: Path,
    temp_branch: str,
    expected_head_sha: str,
    base_sha: str,
    child_sources: Sequence[ChildSourceRef],
) -> bool:
    """Fetch and detach at a passed attempt only after all identity checks hold."""
    try:
        remote = f"origin/{temp_branch}"
        run_git(
            ["fetch", "origin", f"{temp_branch}:refs/remotes/{remote}"],
            cwd=repository_root,
        )
        observed = run_git(["rev-parse", remote], cwd=repository_root).stdout.strip()
        if observed != expected_head_sha:
            return False
        for ancestor in (base_sha, *(child.source_sha for child in child_sources)):
            result = run_git(
                ["merge-base", "--is-ancestor", ancestor, expected_head_sha],
                cwd=repository_root,
                check=False,
            )
            if result.returncode != 0:
                return False
        run_git(
            ["checkout", "--detach", expected_head_sha],
            cwd=repository_root,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, OSError, ValueError):
        return False


__all__ = [
    "AttemptSelection",
    "OpenAttempt",
    "ParentPublishResult",
    "PublishResult",
    "PublishStatus",
    "build_attempt_identity",
    "check_fast_forward_identity",
    "checkout_verified_attempt_head",
    "cleanup_attempt_fix_outputs",
    "find_open_attempts",
    "handle_non_required_review",
    "publish_attempt_branch",
    "prepare_attempt_context",
    "publish_parent_under_claim",
    "mark_tasks_included",
    "select_attempt_children",
    "set_new_attempt_identity",
    "start_required_review",
    "validate_finalization_context",
]
