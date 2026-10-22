"""Layer-2 semantic-review I/O adapters.

The pure state machine lives in :mod:`orchestune.review.semantic_gate`.  This
module binds it to configured targets, GitHub comments, remote refs and the
shared human-escalation terminal without owning orchestration state.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.reviewer import resolve_reviewer_bot
from orchestune.forge import Forge
from orchestune.infra.git_cli import (
    fetch_remote_branch,
    is_ancestor_commit,
    resolve_commit_sha,
)
from orchestune.integrator.semantic_claim import probe_claim_backend
from orchestune.issue_notice import post_notice_if_changed
from orchestune.ledger.escalation import apply_human_review_escalation
from orchestune.pr_link_notice_trust import NoticePresence, TrustContext
from orchestune.review.markers import (
    build_trigger_body,
    review_head_marker,
    review_round_marker,
    review_trigger_marker,
)
from orchestune.review.rounds import effective_lines
from orchestune.review.semantic_gate import (
    SemanticOperationStatus,
    restore_latest_fix_result,
    semantic_fix_output_branch,
)
from orchestune.targets.cloud_routine import ROUTINE_TOKEN_ENV_VAR

_LOCAL_FIX_BINARIES = {
    "claude-cli": "claude",
    "codex-cli": "codex",
    "agy-cli": "agy",
    "codex-cloud": "codex",
}
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization)\s*[:=]\s*(?:(?:bearer|basic)\s+)?\S+|\b(token|secret|password)\s*[:=]\s*\S+"
)


@dataclass(frozen=True)
class SemanticReviewPreflightResult:
    ok: bool
    resolved_reviewer: str | None
    resolved_trigger: str
    error_code: str | None = None
    error_message: str | None = None
    notice_key: str | None = None


@dataclass(frozen=True)
class SemanticOperationResult:
    operation_id: str
    status: SemanticOperationStatus
    provider_handle: str | None = None
    trigger_comment_id: int | None = None
    input_sha: str | None = None
    output_sha: str | None = None
    completed: bool = False
    error_code: str | None = None


def _redact(value: object) -> str:
    text = _SECRET_ASSIGNMENT.sub(
        lambda m: f"{m.group(1) or m.group(2)}=<redacted>", str(value)
    )
    for key, secret in os.environ.items():
        if len(secret) >= 8 and any(
            word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD")
        ):
            text = text.replace(secret, "<redacted>")
    return text


def _preflight_error(
    code: str, reviewer: str | None, trigger: str, message: object
) -> SemanticReviewPreflightResult:
    notice = hashlib.sha256(f"{code}:{reviewer}:{trigger}".encode()).hexdigest()[:16]
    return SemanticReviewPreflightResult(
        False, reviewer, trigger, code, _redact(message), notice
    )


def _target_name(config: DispatcherConfig) -> str:
    name = getattr(config.dispatch_target, "target_name", None)
    return name if isinstance(name, str) else "local"


def _fix_provider_error(
    config: DispatcherConfig,
    provider: str,
    which_fn: Callable[[str], str | None],
) -> str | None:
    binary = _LOCAL_FIX_BINARIES.get(provider)
    if binary is not None and which_fn(binary) is None:
        return f"semantic fix provider {provider!r} requires executable {binary!r}"
    if provider == "cloud-routine":
        token = os.environ.get(ROUTINE_TOKEN_ENV_VAR) or getattr(
            config.dispatch_target, "_routine_token", None
        )
        if not token:
            return f"semantic fix provider 'cloud-routine' requires {ROUTINE_TOKEN_ENV_VAR}=<redacted>"
    if provider == "codex-cloud":
        environment = os.environ.get("ORCHESTUNE_CODEX_CLOUD_ENV") or getattr(
            config.dispatch_target, "_environment_id", None
        )
        if not environment:
            return (
                "semantic fix provider 'codex-cloud' requires a configured environment"
            )
    if provider == "local" and config.dispatch_target is None:
        return "semantic fix provider 'local' requires a configured dispatch target"
    return None


def validate_semantic_review_preflight(
    config: DispatcherConfig,
    *,
    repository_root: Path,
    which_fn: Callable[[str], str | None] = shutil.which,
) -> SemanticReviewPreflightResult:
    """Validate providers, trigger support and the atomic claim backend."""
    trigger = config.semantic_review_trigger
    if config.semantic_review_mode not in {"required", "advisory"}:
        return SemanticReviewPreflightResult(True, None, trigger)
    target_name = _target_name(config)
    provider = config.semantic_review_provider
    if provider not in {"auto", "claude", "codex"}:
        return _preflight_error(
            "reviewer_unresolved", None, trigger, "reviewer could not be resolved"
        )
    reviewer = resolve_reviewer_bot(
        cast(Literal["auto", "claude", "codex"], provider), target_name
    )
    if reviewer is None:
        return _preflight_error(
            "reviewer_unresolved", reviewer, trigger, "reviewer could not be resolved"
        )
    if trigger == "dispatch-target":
        return _preflight_error(
            "trigger_unsupported",
            reviewer,
            trigger,
            "dispatch-target cannot correlate a launch by operation id",
        )
    fix_provider = (
        target_name
        if config.semantic_fix_provider == "auto"
        else config.semantic_fix_provider
    )
    provider_error = _fix_provider_error(config, fix_provider, which_fn)
    if provider_error is not None:
        return _preflight_error(
            "fix_provider_unavailable", reviewer, trigger, provider_error
        )
    try:
        claim_error = probe_claim_backend(repository_root)
    except Exception as error:  # transport failure is not capability evidence
        return _preflight_error("claim_backend_probe_failed", reviewer, trigger, error)
    if claim_error is not None:
        return _preflight_error(
            "claim_backend_unsupported", reviewer, trigger, claim_error
        )
    return SemanticReviewPreflightResult(True, reviewer, trigger)


def _comment_id(comment: dict[str, Any]) -> int | None:
    value = comment.get("id")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _matching_trigger_comments(
    comments: Sequence[dict[str, Any]], reviewer: str, round_number: int, head_sha: str
) -> list[dict[str, Any]]:
    required = {
        review_trigger_marker(reviewer),
        review_round_marker(round_number),
        review_head_marker(head_sha),
    }
    return [
        comment
        for comment in comments
        if isinstance(comment.get("body"), str)
        and required <= set(effective_lines(str(comment["body"])))
    ]


def trigger_semantic_review_round(
    forge: Forge,
    *,
    pr_number: int,
    attempt_id: str,
    round_number: int,
    head_sha: str,
    reviewer: str,
    reply_body: str,
) -> SemanticOperationResult:
    operation_id = f"{attempt_id[:12]}-review-{round_number}"
    matches = _matching_trigger_comments(
        forge.list_comments(pr_number), reviewer, round_number, head_sha
    )
    trust = TrustContext(forge)
    has_unknown = False
    for comment in reversed(matches):
        presence, _ = trust.evaluate_candidate_markers([comment])
        if presence is NoticePresence.PRESENT:
            return SemanticOperationResult(
                operation_id,
                SemanticOperationStatus.DISPATCHING,
                trigger_comment_id=_comment_id(comment),
                input_sha=head_sha,
            )
        if presence is NoticePresence.UNKNOWN:
            has_unknown = True
    if has_unknown:
        return SemanticOperationResult(
            operation_id,
            SemanticOperationStatus.INDETERMINATE,
            input_sha=head_sha,
            error_code="trigger_author_unknown",
        )
    created = forge.create_issue_comment(
        pr_number, build_trigger_body(reply_body, reviewer, round_number, head_sha)
    )
    return SemanticOperationResult(
        operation_id,
        SemanticOperationStatus.DISPATCHING,
        trigger_comment_id=_comment_id(created),
        input_sha=head_sha,
    )


def _fix_prompt(
    *,
    pr_number: int,
    attempt_id: str,
    operation_id: str,
    temp_branch: str,
    expected_head_sha: str,
    round_number: int,
) -> str:
    output_branch = semantic_fix_output_branch(operation_id)
    return (
        f"Handle semantic review round {round_number} for integration PR #{pr_number}.\n"
        f"Operation: {operation_id}; attempt: {attempt_id}; input SHA: {expected_head_sha}.\n"
        "Read every current-round finding and post one complete "
        "`orchestune-review-judgments` table to the integration PR, even with zero findings.\n"
        f"If code changes are required, push them to `{output_branch}`. "
        f"Do not push to `{temp_branch}`; Orchestune alone promotes the result.\n"
        "Finally post the SemanticFixResultV1 marker with operation_id, input_sha, "
        "output_sha (null when unchanged), judgment_comment_id, and status."
    )


def _provider_handle(handle: object) -> str | None:
    for name in ("external_id", "external_url", "pid"):
        value = getattr(handle, name, None)
        if isinstance(value, str | int) and not isinstance(value, bool):
            return str(value)
    return None


def _launch_fix(
    target: Any, prompt: str, operation_id: str, branch: str, pr: int
) -> object:
    if callable(getattr(target, "fire_text_once", None)):
        return target.fire_text_once(prompt)
    if callable(getattr(target, "fire_text", None)):
        return target.fire_text(prompt)
    if callable(getattr(target, "launch_semantic_fix", None)):
        return target.launch_semantic_fix(operation_id=operation_id, prompt=prompt)
    task = SimpleNamespace(
        issue_number=pr,
        subtask_id=prompt,
        footprint=(),
        symbols=(),
        risk=True,
        priority="high",
        progress_partial=False,
        status_labels=(),
        created_at="",
        execution_profile=None,
        model_tier=None,
    )
    return target.launch_attempt(operation_id, task, branch, Path.cwd())


def dispatch_semantic_fix_agent(
    target: Any,
    *,
    pr_number: int,
    attempt_id: str,
    operation_id: str,
    temp_branch: str,
    expected_head_sha: str,
    round_number: int,
) -> SemanticOperationResult:
    output_branch = semantic_fix_output_branch(operation_id)
    prompt = _fix_prompt(
        pr_number=pr_number,
        attempt_id=attempt_id,
        operation_id=operation_id,
        temp_branch=temp_branch,
        expected_head_sha=expected_head_sha,
        round_number=round_number,
    )
    try:
        handle = _launch_fix(target, prompt, operation_id, output_branch, pr_number)
    except Exception:  # an unknown launch response must never cause a second launch
        return SemanticOperationResult(
            operation_id,
            SemanticOperationStatus.INDETERMINATE,
            input_sha=expected_head_sha,
            error_code="launch_indeterminate",
        )
    return SemanticOperationResult(
        operation_id,
        SemanticOperationStatus.DISPATCHING,
        provider_handle=_provider_handle(handle),
        input_sha=expected_head_sha,
    )


def _trusted_comment_bodies(
    forge: Forge, comments: Sequence[dict[str, Any]]
) -> list[str]:
    trust = TrustContext(forge)
    bodies: list[str] = []
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        presence, _ = trust.evaluate_candidate_markers([comment])
        if presence is NoticePresence.PRESENT:
            bodies.append(body)
    return bodies


def _verify_remote_fix_artifact(
    repository_root: str | Path,
    operation_id: str,
    result: Any,
) -> SemanticOperationResult:
    branch = semantic_fix_output_branch(operation_id)
    try:
        ref = fetch_remote_branch(repository_root, branch)
        tip = resolve_commit_sha(repository_root, ref)
        descends = is_ancestor_commit(repository_root, result.input_sha, ref)
    except Exception:
        return SemanticOperationResult(
            operation_id,
            SemanticOperationStatus.RUNNING,
            input_sha=result.input_sha,
            output_sha=result.output_sha,
            error_code="artifact_pending",
        )
    if tip != result.output_sha or not descends:
        return SemanticOperationResult(
            operation_id,
            SemanticOperationStatus.INDETERMINATE,
            input_sha=result.input_sha,
            output_sha=result.output_sha,
            error_code="artifact_mismatch",
        )
    return SemanticOperationResult(
        operation_id,
        SemanticOperationStatus.COMPLETED,
        input_sha=result.input_sha,
        output_sha=result.output_sha,
        completed=True,
    )


def poll_semantic_operation(
    forge: Forge,
    repository_root: str | Path,
    *,
    pr_number: int,
    attempt_id: str,
    operation_id: str,
    started_at: datetime | None = None,
    timeout_seconds: int | None = None,
    now: datetime | None = None,
) -> SemanticOperationResult:
    if started_at is not None and timeout_seconds is not None:
        current_time = now if now is not None else datetime.now(UTC)
        if current_time >= started_at + timedelta(seconds=timeout_seconds):
            return SemanticOperationResult(
                operation_id,
                SemanticOperationStatus.INDETERMINATE,
                error_code="operation_timed_out",
            )
    try:
        bodies = _trusted_comment_bodies(forge, forge.list_comments(pr_number))
        result = restore_latest_fix_result(
            bodies, attempt_id=attempt_id, operation_id=operation_id
        )
    except ValueError:
        return SemanticOperationResult(
            operation_id,
            SemanticOperationStatus.INDETERMINATE,
            error_code="result_marker_invalid",
        )
    if result is None:
        return SemanticOperationResult(operation_id, SemanticOperationStatus.RUNNING)
    if result.status == "failed":
        return SemanticOperationResult(
            operation_id,
            SemanticOperationStatus.FAILED,
            input_sha=result.input_sha,
            error_code="fix_failed",
        )
    if result.output_sha is None:
        return SemanticOperationResult(
            operation_id,
            SemanticOperationStatus.COMPLETED,
            input_sha=result.input_sha,
            completed=True,
        )
    return _verify_remote_fix_artifact(repository_root, operation_id, result)


def escalate_semantic_review_to_human(
    forge: Forge,
    *,
    parent_issue_number: int,
    pr_number: int | None,
    attempt_id: str,
    reason: str,
    details: str,
) -> None:
    safe_reason = _redact(reason)
    safe_details = _redact(details)
    key = hashlib.sha256(f"{attempt_id}:{safe_reason}".encode()).hexdigest()[:16]
    kind = f"semantic-review:{attempt_id}:{key}"
    summary = (
        "## Layer-2 semantic review escalation\n\n"
        f"- Attempt: `{attempt_id}`\n"
        f"- Reason: `{safe_reason}`\n"
        f"- Details: {safe_details}"
    )
    if pr_number is not None:
        post_notice_if_changed(forge, pr_number, kind, summary)
    post_notice_if_changed(forge, parent_issue_number, kind, summary)
    labels = tuple(forge.get_issue_labels(parent_issue_number))
    apply_human_review_escalation(parent_issue_number, labels, summary, forge=forge)
