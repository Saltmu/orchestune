"""Expand the footprint of a held interactive claim without re-claiming the task."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from orchestune.claim.contracts import (
    ClaimFailure,
    ClaimFailureReason,
    ClaimStage,
    OwnerKind,
    ReservationKind,
)
from orchestune.claim.ownership import (
    evaluate_claim_conflicts,
    held_claim_next_actions,
    owner_token_digest,
)
from orchestune.claim.service import _DefaultConflictView, _validate_issue_for_resume
from orchestune.claim.workspace import (
    check_repository_identity_match,
    resolve_claim_workspace,
)
from orchestune.forge import Forge, GitHubForge
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import FileLockContentionError, run_state_lock
from orchestune.issue_parsing import FOOTPRINT_BLOCK_PATTERN, parse_task_from_issue
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.models import IssueRecord

OwnerTokenReader = Callable[[str], "str | None"]


@dataclass(frozen=True)
class FootprintAmendOutcome:
    """Result of expanding a held reservation footprint."""

    success: bool
    issue_number: int
    claim_id: str | None = None
    worktree_path: Path | None = None
    previous_footprint: tuple[str, ...] = ()
    amended_footprint: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    issue_body_updated: bool = False
    failure: ClaimFailure | None = field(default=None)


class _AmendRejected(Exception):
    def __init__(self, failure: ClaimFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _reject(
    reason: ClaimFailureReason, message: str, *next_actions: str
) -> _AmendRejected:
    return _AmendRejected(ClaimFailure(reason, message, next_actions=next_actions))


def _find_held_reservation(
    run_state: RunState, issue_number: int
) -> tuple[str, ActiveWorktree]:
    for key, active in run_state.active_worktrees.items():
        if active.issue_number == issue_number:
            return key, active
    raise _reject(
        ClaimFailureReason.INVALID_RESUME,
        f"No active claim is held for issue #{issue_number}.",
        f"Run `orchestune claim {issue_number}` to claim the task first.",
    )


def _check_eligibility(active: ActiveWorktree, repository_identity: str) -> None:
    n = active.issue_number
    problems = []
    if active.owner_kind != OwnerKind.INTERACTIVE.value:
        problems.append("it is owned by the dispatcher")
    if active.claim_stage != ClaimStage.COMPLETED.value:
        problems.append(f"its claim stage is {active.claim_stage}")
    if active.reservation_kind != ReservationKind.FOOTPRINT.value:
        problems.append("it already reserves the whole repository")
    if active.completion_id is not None:
        problems.append("completion has already started")
    if not check_repository_identity_match(repository_identity, active.repository_id):
        problems.append("it belongs to a different repository checkout")
    if problems:
        actions = [
            a for a in held_claim_next_actions(active) if "--amend-footprint" not in a
        ]
        raise _reject(
            ClaimFailureReason.INVALID_RESUME,
            f"The claim for issue #{n} cannot be amended: {'; '.join(problems)}.",
            *actions,
        )


def _authenticate(active: ActiveWorktree, read_owner_token: OwnerTokenReader) -> None:
    token = read_owner_token(active.claim_id or "")
    if token is None or owner_token_digest(token) != active.owner_token_digest:
        raise _reject(
            ClaimFailureReason.INVALID_RESUME,
            f"No matching protected owner token exists for claim {active.claim_id}.",
            "Run the command from the workspace that created the claim.",
        )


def _issue_footprint(forge: Forge, active: ActiveWorktree) -> IssueRecord:
    issue = forge.get_issue(active.issue_number)
    if issue is None:
        raise _reject(
            ClaimFailureReason.ISSUE_NOT_FOUND,
            f"Issue #{active.issue_number} was not found.",
        )
    failure = _validate_issue_for_resume(issue)
    if failure is not None:
        raise _AmendRejected(failure)
    if not parse_task_from_issue(issue).footprint:
        raise _reject(
            ClaimFailureReason.INVALID_RESUME,
            f"Issue #{issue.number} declares no footprint (repository reservation).",
            "List every file the task needs under `footprint` in the Issue body and retry.",
            "Switching a held file reservation to a repository reservation is not supported.",
        )
    return issue


def _git_paths(args: list[str], cwd: str) -> list[str]:
    result = run_git(args, cwd=cwd, check=False)
    if result.returncode != 0:
        raise _reject(
            ClaimFailureReason.INVALID_RESUME,
            f"Unable to list changed files in the worktree: {(result.stderr or '').strip()}",
            "Inspect the worktree manually; the reservation was left unchanged.",
        )
    return [path for path in result.stdout.split("\0") if path]


def _changed_files(active: ActiveWorktree) -> list[str]:
    """Files changed since the claim base, including uncommitted and untracked ones."""
    if not active.base_sha or not active.worktree_path:
        raise _reject(
            ClaimFailureReason.INVALID_RESUME,
            f"Claim {active.claim_id} has no recorded base commit or worktree path.",
            "Inspect the worktree manually; the reservation was left unchanged.",
        )
    diff = _git_paths(
        ["diff", "--name-only", "--no-renames", "-z", active.base_sha],
        active.worktree_path,
    )
    untracked = _git_paths(
        ["ls-files", "--others", "--exclude-standard", "-z"], active.worktree_path
    )
    return [*diff, *untracked]


def _check_conflicts(
    key: str, amended: ActiveWorktree, run_state: RunState, forge: Forge
) -> None:
    others = RunState(
        active_worktrees={
            k: v for k, v in run_state.active_worktrees.items() if k != key
        }
    )
    try:
        conflict = evaluate_claim_conflicts(
            amended, others, _DefaultConflictView(forge)
        )
    except Exception as error:
        raise _reject(
            ClaimFailureReason.CLAIM_CONFLICT,
            f"Failed to evaluate footprint conflicts: {error}",
        ) from error
    if conflict is not None:
        other = conflict.active
        raise _AmendRejected(
            ClaimFailure(
                reason=ClaimFailureReason.CLAIM_CONFLICT,
                message=(
                    f"Amended footprint conflicts with issue #{other.issue_number}: "
                    f"{conflict.reason.value}"
                ),
                conflicting_issue_number=other.issue_number,
                conflicting_branch=other.branch or None,
                conflicting_path=Path(other.worktree_path)
                if other.worktree_path
                else None,
                next_actions=(
                    f"Wait for issue #{other.issue_number} to finish, or revert the overlapping changes and retry.",
                ),
            )
        )


def _rewrite_issue_footprint(body: str, footprint: tuple[str, ...]) -> str:
    match = FOOTPRINT_BLOCK_PATTERN.search(body)
    assert match is not None
    data = yaml.safe_load(match.group(1))
    data["footprint"] = list(footprint)
    new_block = yaml.dump(
        data, allow_unicode=True, default_flow_style=False, sort_keys=False
    )
    start, end = match.span(1)
    return body[:start] + new_block + body[end:]


def _publish_issue_footprint(
    forge: Forge, issue: IssueRecord, footprint: tuple[str, ...]
) -> bool:
    if set(footprint) <= set(parse_task_from_issue(issue).footprint):
        return False
    try:
        forge.update_issue_body(
            issue.number, _rewrite_issue_footprint(issue.body, footprint)
        )
    except Exception as error:
        raise _reject(
            ClaimFailureReason.STATE_SAVE_FAILED,
            f"Failed to update the footprint in issue #{issue.number}: {error}",
            "The reservation was left unchanged; retry the command.",
        ) from error
    return True


def _amend_in_lock(
    issue_number: int,
    read_owner_token: OwnerTokenReader,
    *,
    apply: bool,
    forge: Forge,
    run_state_path: Path,
    repository_identity: str,
) -> FootprintAmendOutcome:
    run_state = load_run_state(run_state_path)
    key, active = _find_held_reservation(run_state, issue_number)
    _check_eligibility(active, repository_identity)
    _authenticate(active, read_owner_token)
    issue = _issue_footprint(forge, active)

    previous = tuple(active.declared_footprint)
    candidates = [
        *previous,
        *parse_task_from_issue(issue).footprint,
        *_changed_files(active),
    ]
    amended_footprint = tuple(dict.fromkeys(candidates))
    added = tuple(p for p in amended_footprint if p not in previous)
    amended = dataclasses.replace(active, declared_footprint=amended_footprint)
    _check_conflicts(key, amended, run_state, forge)

    body_updated = False
    if apply:
        body_updated = _publish_issue_footprint(forge, issue, amended_footprint)
        active.declared_footprint = amended_footprint
        try:
            save_run_state(run_state, run_state_path)
        except Exception as error:
            raise _reject(
                ClaimFailureReason.STATE_SAVE_FAILED,
                f"Failed to persist the amended reservation: {error}",
                "The Issue footprint may already be updated; retry the command.",
            ) from error
    return FootprintAmendOutcome(
        success=True,
        issue_number=issue_number,
        claim_id=active.claim_id,
        worktree_path=Path(active.worktree_path),
        previous_footprint=previous,
        amended_footprint=amended_footprint,
        added=added,
        issue_body_updated=body_updated,
    )


def amend_claim_footprint(
    issue_number: int,
    *,
    read_owner_token: OwnerTokenReader,
    apply: bool = True,
    forge: Forge | None = None,
    cwd: str | Path | None = None,
    state_path: str | Path | None = None,
    timeout_seconds: float | None = None,
) -> FootprintAmendOutcome:
    """Widen a held interactive file reservation to cover newly needed files.

    The footprint only grows: it becomes the union of the held footprint, the
    Issue footprint and every file already changed in the worktree, and it is
    rejected when the result overlaps another active reservation.
    """
    workspace = resolve_claim_workspace(cwd, explicit_state_path=state_path)
    timeout = timeout_seconds if timeout_seconds is not None else 0.0
    try:
        with run_state_lock(workspace.lock_path, timeout=timeout):
            return _amend_in_lock(
                issue_number,
                read_owner_token,
                apply=apply,
                forge=forge or GitHubForge(),
                run_state_path=workspace.run_state_path,
                repository_identity=workspace.repository_identity,
            )
    except (FileLockContentionError, RuntimeError) as error:
        failure = ClaimFailure(
            ClaimFailureReason.STATE_LOCK_FAILED,
            f"Could not acquire run_state lock: {error}",
        )
    except _AmendRejected as rejected:
        failure = rejected.failure
    return FootprintAmendOutcome(
        success=False, issue_number=issue_number, failure=failure
    )


__all__ = ["FootprintAmendOutcome", "amend_claim_footprint"]
