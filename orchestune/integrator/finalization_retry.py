"""Durable, bounded accounting of remote-denied child-branch deletions (#827).

When a repository rule permanently refuses to delete a finalized child branch, the
safe behaviour (#819) is to keep the branch and leave the child Issue open. Repeating
that attempt silently every cycle never reaches a state a human can act on. Each
refused deletion is therefore recorded as a comment on the *child Issue*, so the count
survives a new runner, a new run id and a re-created context, and the budget is rebuilt
from those comments on every read:

``denied``    the remote refused the deletion in one integration run
``terminal``  the limit was reached; the child is labeled for a human

A count is bound to the child branch, its proven tip SHA and the base branch. Runs are
counted once however often they retry, and a moved tip starts a new count. Reading and
writing fail closed: an unreadable history or a failed write never advances the count,
so an escalation can be late but never early. ``status:*`` labels are left alone
because a ``status:done`` task with a second lifecycle label is reported as a conflict
by the consistency kernel.

GitHub comments have no atomic compare-and-swap, so concurrent integrators against one
parent from different hosts are unsupported, as for the other comment-backed records.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from orchestune.forge import REQUIRED_LABELS, Forge
from orchestune.integrator.proofs import TaskIntegrationProof

MARKER = "<!-- orchestune:child-branch-finalization:v1 -->"
PARENT_MARKER = "<!-- orchestune:child-branch-finalization-escalation:v1"
BLOCKED_LABEL = "integration:finalization-blocked"
# Denied integration runs, since the last terminal record, that trigger escalation.
CHILD_BRANCH_DELETION_DENIAL_LIMIT = 3

EVENT_DENIED = "denied"
EVENT_TERMINAL = "terminal"
_EVENTS = frozenset({EVENT_DENIED, EVENT_TERMINAL})
_KEY_FIELDS = ("issue_number", "subtask_id", "branch_name", "source_sha", "base_branch")


class DenialVerdict(StrEnum):
    RECORDED = "recorded"
    LIMIT_REACHED = "limit_reached"
    ESCALATED = "escalated"
    UNKNOWN = "unknown"


class BlockedOutcome(StrEnum):
    HOLD = "hold"
    FINALIZE = "finalize"
    REINTEGRATE = "reintegrate"


@dataclass
class _History:
    denied_runs: set[str] = field(default_factory=set)
    terminals: int = 0
    terminal_is_latest: bool = False

    @property
    def limit_reached(self) -> bool:
        return len(self.denied_runs) >= CHILD_BRANCH_DELETION_DENIAL_LIMIT


def _key(proof: TaskIntegrationProof, base_branch: str) -> dict[str, Any]:
    return {
        "issue_number": proof.issue_number,
        "subtask_id": proof.subtask_id,
        "branch_name": proof.branch_name,
        "source_sha": proof.source_sha.lower(),
        "base_branch": base_branch.removeprefix("origin/"),
    }


def _summary(payload: dict[str, Any]) -> str:
    branch = payload["branch_name"]
    if payload["event"] == EVENT_DENIED:
        return (
            f"子ブランチ `{branch}` の削除がリモートで拒否されました"
            f"（{payload['attempt']}/{payload['limit']}）。"
            "安全のためブランチを残し、このIssueはクローズしません。"
        )
    return (
        f"子ブランチ `{branch}`（`{payload['source_sha']}`）の削除が"
        f"{payload['attempts']}回連続で拒否されたため、"
        f"`{BLOCKED_LABEL}` を付与して人間の確認待ちにしました。"
        "次のいずれかで解消できます。\n\n"
        "1. 子ブランチを手動で削除する（次サイクルで自動的に確定します）。\n"
        f"2. ルールセット等を緩和して `{BLOCKED_LABEL}` を外す"
        "（次サイクルから削除を再試行し、回数は0から数え直します）。"
    )


def _render(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True)
    return f"{MARKER}\n{_summary(payload)}\n\n```json\n{body}\n```"


def _parse(comment: dict[str, Any], trusted_author: str) -> dict[str, Any] | None:
    """Return a canonical event payload, or ``None`` for anything else."""
    body = comment.get("body")
    if comment.get("author") != trusted_author or not isinstance(body, str):
        return None
    if MARKER not in body:
        return None
    try:
        raw = body.split("```json\n", 1)[1].split("\n```", 1)[0]
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or payload.get("event") not in _EVENTS
            or body != _render(payload)
        ):
            return None
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    return payload


def _read_history(
    forge: Forge, proof: TaskIntegrationProof, base_branch: str
) -> _History | None:
    try:
        trusted_author = forge.get_authenticated_user()
        comments = forge.list_comments(proof.issue_number)
    except Exception as error:
        print(
            "Warning: Failed to read child-branch finalization history for "
            f"#{proof.issue_number}: {error}",
            file=sys.stderr,
        )
        return None
    key = _key(proof, base_branch)
    history = _History()
    for comment in comments:
        payload = _parse(comment, trusted_author)
        if payload is None or any(
            payload.get(name) != key[name] for name in _KEY_FIELDS
        ):
            continue
        if payload["event"] == EVENT_TERMINAL:
            history.denied_runs.clear()
            history.terminals += 1
            history.terminal_is_latest = True
        else:
            history.denied_runs.add(str(payload.get("run_id")))
            history.terminal_is_latest = False
    return history


def record_denial(
    forge: Forge, proof: TaskIntegrationProof, base_branch: str, run_id: str
) -> DenialVerdict:
    """Count one refused deletion for ``run_id``; never counts a run twice."""
    history = _read_history(forge, proof, base_branch)
    if history is None:
        return DenialVerdict.UNKNOWN
    if history.limit_reached:
        return DenialVerdict.LIMIT_REACHED
    if run_id not in history.denied_runs:
        payload = {
            **_key(proof, base_branch),
            "event": EVENT_DENIED,
            "attempt": len(history.denied_runs) + 1,
            "limit": CHILD_BRANCH_DELETION_DENIAL_LIMIT,
            "run_id": run_id,
        }
        try:
            forge.add_comment(proof.issue_number, _render(payload))
        except Exception as error:
            print(
                "Warning: Failed to record a refused child-branch deletion for "
                f"#{proof.issue_number}: {error}",
                file=sys.stderr,
            )
            return DenialVerdict.UNKNOWN
        history.denied_runs.add(run_id)
    if history.limit_reached:
        return DenialVerdict.LIMIT_REACHED
    return DenialVerdict.RECORDED


def _parent_marker(key: dict[str, Any], generation: int) -> str:
    return (
        f"{PARENT_MARKER} issue={key['issue_number']} "
        f"sha={key['source_sha']} gen={generation} -->"
    )


def _post_parent_comment_once(
    forge: Forge,
    key: dict[str, Any],
    generation: int,
    attempts: int,
    parent_issue_number: int,
) -> bool:
    marker = _parent_marker(key, generation)
    try:
        trusted_author = forge.get_authenticated_user()
        existing = forge.list_comments(parent_issue_number)
        if any(
            comment.get("author") == trusted_author
            and marker in str(comment.get("body", ""))
            for comment in existing
        ):
            return True
        forge.add_comment(
            parent_issue_number,
            f"{marker}\n"
            f"⚠️ 子Issue #{key['issue_number']} の統合は完了していますが、"
            f"子ブランチ `{key['branch_name']}` の削除がリモートで{attempts}回"
            "拒否されました。子Issueはクローズされず、親Issueの完了もこの子で"
            f"保留されます。子Issueに `{BLOCKED_LABEL}` を付与しました。"
            "子ブランチを手動で削除するか、削除を拒否しているルールセット等を"
            "緩和してください。",
        )
    except Exception as error:
        print(
            "Warning: Failed to comment on parent issue "
            f"#{parent_issue_number} about child #{key['issue_number']}: {error}",
            file=sys.stderr,
        )
        return False
    return True


def escalate_terminal(
    forge: Forge,
    proof: TaskIntegrationProof,
    base_branch: str,
    parent_issue_number: int,
) -> bool:
    """Label the child, tell the parent, then record the terminal event.

    The terminal record is written last so that its presence proves every earlier
    step happened; a failure at any step is retried by the next cycle. Returns
    whether the escalation is complete.
    """
    history = _read_history(forge, proof, base_branch)
    if history is None:
        return False
    if history.terminal_is_latest:
        return True
    key = _key(proof, base_branch)
    attempts = len(history.denied_runs)
    spec = next(label for label in REQUIRED_LABELS if label.name == BLOCKED_LABEL)
    try:
        forge.ensure_labels((spec,))
    except Exception as error:
        print(
            f"Warning: Failed to ensure the {BLOCKED_LABEL} label exists: {error}",
            file=sys.stderr,
        )
    try:
        forge.add_label(proof.issue_number, BLOCKED_LABEL)
    except Exception as error:
        print(
            f"Warning: Failed to label #{proof.issue_number} {BLOCKED_LABEL}: {error}",
            file=sys.stderr,
        )
        return False
    if not _post_parent_comment_once(
        forge, key, history.terminals, attempts, parent_issue_number
    ):
        return False
    try:
        forge.add_comment(
            proof.issue_number,
            _render({**key, "event": EVENT_TERMINAL, "attempts": attempts}),
        )
    except Exception as error:
        print(
            "Warning: Failed to record the terminal child-branch finalization "
            f"state for #{proof.issue_number}: {error}",
            file=sys.stderr,
        )
        return False
    return True


def handle_denied_deletion(
    forge: Forge,
    proof: TaskIntegrationProof,
    base_branch: str,
    run_id: str,
    parent_issue_number: int,
) -> DenialVerdict:
    """Record a refused deletion and escalate once the limit is reached."""
    verdict = record_denial(forge, proof, base_branch, run_id)
    if verdict is DenialVerdict.LIMIT_REACHED and escalate_terminal(
        forge, proof, base_branch, parent_issue_number
    ):
        return DenialVerdict.ESCALATED
    return verdict


def _remove_blocked_label(forge: Forge, issue_number: int) -> bool:
    try:
        forge.remove_label(issue_number, BLOCKED_LABEL)
    except Exception as error:
        print(
            f"Warning: Failed to remove {BLOCKED_LABEL} from #{issue_number}: {error}",
            file=sys.stderr,
        )
        return False
    return True


def settle_blocked_child(
    forge: Forge,
    proof: TaskIntegrationProof,
    base_branch: str,
    parent_issue_number: int,
    read_tip: Callable[[], str | None],
) -> BlockedOutcome:
    """Watch a labeled child read-only; never attempt another deletion here.

    ``read_tip`` returns the remote branch tip, ``None`` once it is gone, and raises
    when it cannot tell. A vanished branch finalizes the child (the proof already
    reached the parent), a moved tip hands it back to integration, and anything
    else, including an unreadable remote, keeps holding.
    """
    try:
        tip = read_tip()
    except Exception as error:
        print(
            "Warning: Failed to read the remote branch of blocked child "
            f"#{proof.issue_number}: {error}",
            file=sys.stderr,
        )
        return BlockedOutcome.HOLD
    if tip is None:
        _remove_blocked_label(forge, proof.issue_number)
        return BlockedOutcome.FINALIZE
    if tip.lower() != proof.source_sha.lower():
        if _remove_blocked_label(forge, proof.issue_number):
            return BlockedOutcome.REINTEGRATE
        return BlockedOutcome.HOLD
    history = _read_history(forge, proof, base_branch)
    if history is not None and history.limit_reached and not history.terminal_is_latest:
        escalate_terminal(forge, proof, base_branch, parent_issue_number)
    return BlockedOutcome.HOLD
