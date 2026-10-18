"""Layer-2 semantic review claim and claim-guarded ref updates (#1370).

A GitHub comment is append-only and cannot be compared-and-swapped, so the
claim lives in a Git ref: ``refs/orchestune/semantic-claims/parent-issue-{N}``.
Its value is an empty-tree commit whose message is the claim JSON (followed
by a per-commit nonce) and whose parent is the previous claim commit. Every write is a ``git push`` with an
expected old value (``--force-with-lease``), which makes the remote the only
arbiter of ownership.

Protected refs (the integration temp branch, the parent branch) move only in
the same ``git push --atomic`` that advances the claim ref, so a holder whose
claim was taken over cannot write anything. Fix agents push to their own
``integration/semantic-fix/<operation>`` branch; ``promote_fix_output`` moves
that result onto the temp branch under the claim.

Failures are classified like ``infra.git_cli.delete_remote_branch_if_matches``:
a lease refusal, a remote policy refusal and an unknown transport failure are
kept apart, and an unknown outcome is settled by reading the claim ref back.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from orchestune.infra.git_cli import (
    GitResult,
    fetch_remote_branch,
    get_clean_git_env,
    is_ancestor_commit,
    resolve_commit_sha,
    run_git,
)

CLAIM_REF_PREFIX = "refs/orchestune/semantic-claims/"
FIX_BRANCH_PREFIX = "integration/semantic-fix/"
CLAIM_BACKEND_UNSUPPORTED = "claim_backend_unsupported"
CLAIM_VERSION = 1

# ``PushResult.detail`` values decided locally, before or instead of a push.
DETAIL_CLAIM_EXPIRED = "claim_expired"
DETAIL_CLAIM_LOST = "claim_lost"
DETAIL_FIX_NOT_DESCENDANT = "fix_not_descendant"
DETAIL_FIX_FETCH_FAILED = "fix_fetch_failed"

_CLAIM_KEYS = frozenset(
    {
        "version",
        "parent_issue_number",
        "attempt_id",
        "holder",
        "generation",
        "expires_at",
    }
)
_OID_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")
_ATTEMPT_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_IDENTITY = {
    "GIT_AUTHOR_NAME": "Orchestune",
    "GIT_AUTHOR_EMAIL": "orchestune@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "Orchestune",
    "GIT_COMMITTER_EMAIL": "orchestune@users.noreply.github.com",
}
# A lease refusal (client side ``stale info`` or a non-fast-forward
# ``[rejected]``); ``[remote rejected]`` does not contain ``[rejected]``.
_LEASE_MARKERS = ("stale info", "[rejected]")
_REMOTE_REJECTED = "[remote rejected]"
_ATOMIC_UNSUPPORTED = "does not support --atomic"
_NONCE_TRAILER = "Orchestune-Claim-Nonce"
# ``(ref, new_oid, expected_old_oid)`` for one ref of an atomic push.
_Change = tuple[str, str | None, str | None]


class SemanticClaimError(RuntimeError):
    """The claim could not be read; never to be taken as "no claim"."""


class SemanticClaimCorruptError(SemanticClaimError):
    """The claim ref exists but does not hold a valid claim commit."""


@dataclass(frozen=True)
class SemanticClaim:
    parent_issue_number: int
    attempt_id: str
    holder: str
    generation: int
    expires_at: datetime
    ref_oid: str

    def is_expired(self, now: datetime) -> bool:
        return _utc(now) >= self.expires_at


class ClaimStatus(StrEnum):
    ACQUIRED = "acquired"
    HELD_BY_OTHER = "held_by_other"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClaimResult:
    """``claim`` is the held claim when acquired, the other holder's claim
    when held by another, and ``None`` when the outcome is unknown."""

    status: ClaimStatus
    claim: SemanticClaim | None = None
    detail: str = ""


@dataclass(frozen=True)
class RefUpdate:
    """``new_oid=None`` deletes ``ref``; ``expected_old_oid=None`` requires
    that ``ref`` does not exist yet.

    A ref that already holds ``new_oid`` is left as is and counts as applied:
    Git does not check the lease of an up-to-date ref. This keeps a retry
    after an unknown outcome idempotent.
    """

    ref: str
    new_oid: str | None
    expected_old_oid: str | None


class PushStatus(StrEnum):
    PUSHED = "pushed"
    # A lease did not hold (the claim or a target ref moved): nothing applied.
    REJECTED = "rejected"
    # The remote refused by policy (hook, protection, ``denyDeletes``).
    DENIED = "denied"
    # Confirmed not applied (the claim ref is unchanged) for another reason.
    FAILED = "failed"
    # Not applied or applied; reading the claim ref back did not tell.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PushResult:
    """``claim`` is the claim to continue with: the advanced claim after a
    push, the unchanged claim when the claim ref is confirmed untouched, and
    ``None`` when the claim was lost or its state is unknown."""

    status: PushStatus
    claim: SemanticClaim | None = None
    detail: str = ""
    ref_updates: tuple[RefUpdate, ...] = ()


def semantic_claim_ref(parent_issue_number: int) -> str:
    return f"{CLAIM_REF_PREFIX}parent-issue-{_positive_int(parent_issue_number)}"


def read_semantic_claim(
    repository_root: str | Path, parent_issue_number: int
) -> SemanticClaim | None:
    """Fetch and parse the claim; ``None`` only when the remote has no ref."""
    ref = semantic_claim_ref(parent_issue_number)
    fetched = _run(
        repository_root,
        ["fetch", "--no-tags", "--no-write-fetch-head", "origin", f"+{ref}:{ref}"],
    )
    if fetched.returncode != 0:
        if _remote_ref_oid(repository_root, ref) is not None:
            raise SemanticClaimError(f"failed to fetch {ref}: {fetched.stderr}")
        _run(repository_root, ["update-ref", "-d", ref])
        return None
    resolved = _run(repository_root, ["rev-parse", "--verify", ref])
    if resolved.returncode != 0:
        raise SemanticClaimError(f"failed to resolve {ref}: {resolved.stderr}")
    oid = resolved.stdout.strip()
    return _parse_claim_commit(repository_root, oid, parent_issue_number)


def acquire_semantic_claim(
    repository_root: str | Path,
    *,
    parent_issue_number: int,
    attempt_id: str,
    holder: str,
    now: datetime,
    ttl_seconds: float,
) -> ClaimResult:
    """Take the claim when it is absent, expired, or already ours.

    Taking a claim that another holder or attempt owned increments the
    generation; renewing our own keeps it.
    """
    now = _utc(now)
    expires_at = now + _ttl(ttl_seconds)
    _text(attempt_id, "attempt_id")
    _text(holder, "holder")
    current = read_semantic_claim(repository_root, parent_issue_number)
    if current is None:
        generation = 1
    elif _owned_by(current, attempt_id, holder):
        generation = current.generation
    elif current.is_expired(now):
        generation = current.generation + 1
    else:
        return ClaimResult(ClaimStatus.HELD_BY_OTHER, current)
    wanted = SemanticClaim(
        parent_issue_number, attempt_id, holder, generation, expires_at, ref_oid=""
    )
    expected_old = current.ref_oid if current else None
    oid = _create_claim_commit(repository_root, wanted, parent=expected_old, now=now)
    wanted = replace(wanted, ref_oid=oid)
    ref = semantic_claim_ref(parent_issue_number)
    ok, detail = _push(repository_root, [(ref, oid, expected_old)])
    if ok:
        return ClaimResult(ClaimStatus.ACQUIRED, wanted)
    return _reconcile_acquire(repository_root, wanted, expected_old, detail)


def renew_semantic_claim(
    repository_root: str | Path,
    claim: SemanticClaim,
    *,
    now: datetime,
    ttl_seconds: float,
) -> PushResult:
    now = _utc(now)
    return _advance_claim(
        repository_root, claim, (), expires_at=now + _ttl(ttl_seconds), now=now
    )


def release_semantic_claim(
    repository_root: str | Path, claim: SemanticClaim, *, now: datetime
) -> PushResult:
    """Expire the claim in place so the next holder takes generation + 1.

    The ref is kept rather than deleted: deleting it would restart the
    generation at 1 for the next acquirer.
    """
    now = _utc(now)
    return _advance_claim(repository_root, claim, (), expires_at=now, now=now)


def push_refs_under_claim(
    repository_root: str | Path,
    claim: SemanticClaim,
    updates: Sequence[RefUpdate],
    *,
    now: datetime,
) -> PushResult:
    """Apply ``updates`` and advance the claim ref in one atomic push."""
    now = _utc(now)
    checked = _validated_updates(updates)
    if claim.is_expired(now):
        return PushResult(
            PushStatus.REJECTED, claim, DETAIL_CLAIM_EXPIRED, ref_updates=checked
        )
    return _advance_claim(
        repository_root, claim, checked, expires_at=claim.expires_at, now=now
    )


def promote_fix_output(
    repository_root: str | Path,
    claim: SemanticClaim,
    *,
    temp_branch: str,
    expected_head_sha: str,
    fix_branch: str,
    now: datetime,
) -> PushResult:
    """Move ``temp_branch`` from ``expected_head_sha`` to the fix branch tip."""
    now = _utc(now)
    temp_ref = f"refs/heads/{_branch(temp_branch)}"
    expected = _oid(expected_head_sha)
    try:
        fix_sha = resolve_commit_sha(
            repository_root, fetch_remote_branch(repository_root, _branch(fix_branch))
        )
        descends = is_ancestor_commit(repository_root, expected, fix_sha)
    except (subprocess.CalledProcessError, OSError, ValueError) as error:
        return PushResult(
            PushStatus.FAILED, claim, f"{DETAIL_FIX_FETCH_FAILED}: {_detail(error)}"
        )
    update = RefUpdate(temp_ref, fix_sha, expected)
    if not descends:
        return PushResult(
            PushStatus.REJECTED,
            claim,
            DETAIL_FIX_NOT_DESCENDANT,
            ref_updates=(update,),
        )
    return push_refs_under_claim(repository_root, claim, [update], now=now)


def delete_attempt_fix_branches(repository_root: str | Path, attempt_id: str) -> None:
    """Delete ``integration/semantic-fix/{attempt_id[:12]}-*`` on the remote.

    Each branch is deleted with a lease on the tip just listed. Any failure
    raises ``SemanticClaimError``; a partial deletion is safe to repeat.
    """
    prefix = _text(attempt_id, "attempt_id")[:12]
    if not _ATTEMPT_PREFIX_PATTERN.fullmatch(prefix):
        raise ValueError(f"attempt_id is unsafe for a branch name: {attempt_id!r}")
    wanted = f"refs/heads/{FIX_BRANCH_PREFIX}{prefix}-"
    listed = _run(repository_root, ["ls-remote", "--heads", "origin"])
    if listed.returncode != 0:
        raise SemanticClaimError(f"failed to list remote branches: {listed.stderr}")
    targets = [
        (ref, oid)
        for oid, ref in _ls_remote_lines(listed.stdout)
        if ref.startswith(wanted)
    ]
    if not targets:
        return
    leases = [f"--force-with-lease={ref}:{oid}" for ref, oid in targets]
    refspecs = [f":{ref}" for ref, _ in targets]
    deleted = _run(
        repository_root, ["push", "--porcelain", *leases, "origin", *refspecs]
    )
    if deleted.returncode != 0:
        raise SemanticClaimError(
            f"failed to delete fix branches: {deleted.stdout} {deleted.stderr}"
        )


def probe_claim_backend(repository_root: str | Path) -> str | None:
    """Create and delete a probe claim ref with ``--atomic``.

    Returns ``CLAIM_BACKEND_UNSUPPORTED`` when the remote refuses the
    namespace, atomic pushes or ref deletion, and ``None`` when it accepts
    both. A transport failure raises ``SemanticClaimError``: it says nothing
    about the backend's capability.
    """
    ref = f"{CLAIM_REF_PREFIX}probe-{uuid.uuid4().hex}"
    oid = _commit_tree(
        repository_root,
        "orchestune semantic claim backend probe",
        parent=None,
        now=datetime.now(UTC),
    )
    for new, old in ((oid, None), (None, oid)):
        ok, detail = _push(repository_root, [(ref, new, old)])
        if ok:
            continue
        markers = (*_LEASE_MARKERS, _REMOTE_REJECTED, _ATOMIC_UNSUPPORTED)
        if any(marker in detail for marker in markers):
            return CLAIM_BACKEND_UNSUPPORTED
        raise SemanticClaimError(f"claim backend probe failed: {detail}")
    return None


def _advance_claim(
    repository_root: str | Path,
    claim: SemanticClaim,
    updates: tuple[RefUpdate, ...],
    *,
    expires_at: datetime,
    now: datetime,
) -> PushResult:
    _oid(claim.ref_oid)
    advanced = replace(claim, expires_at=expires_at)
    oid = _create_claim_commit(repository_root, advanced, parent=claim.ref_oid, now=now)
    advanced = replace(advanced, ref_oid=oid)
    ref = semantic_claim_ref(claim.parent_issue_number)
    changes: list[_Change] = [(ref, oid, claim.ref_oid)]
    changes += [(u.ref, u.new_oid, u.expected_old_oid) for u in updates]
    ok, detail = _push(repository_root, changes)
    if ok:
        return PushResult(PushStatus.PUSHED, advanced, ref_updates=updates)
    return _classify_failed_push(repository_root, claim, advanced, updates, detail)


def _classify_failed_push(
    repository_root: str | Path,
    claim: SemanticClaim,
    advanced: SemanticClaim,
    updates: tuple[RefUpdate, ...],
    detail: str,
) -> PushResult:
    lease_refused = any(marker in detail for marker in _LEASE_MARKERS)
    try:
        observed = _remote_ref_oid(
            repository_root, semantic_claim_ref(claim.parent_issue_number)
        )
    except SemanticClaimError:
        status = PushStatus.REJECTED if lease_refused else PushStatus.UNKNOWN
        return PushResult(status, None, detail, updates)
    if observed == advanced.ref_oid:
        return PushResult(PushStatus.PUSHED, advanced, ref_updates=updates)
    if observed != claim.ref_oid:
        return PushResult(
            PushStatus.REJECTED, None, f"{DETAIL_CLAIM_LOST}: {detail}", updates
        )
    if lease_refused:
        return PushResult(PushStatus.REJECTED, claim, detail, updates)
    if _REMOTE_REJECTED in detail:
        return PushResult(PushStatus.DENIED, claim, detail, updates)
    return PushResult(PushStatus.FAILED, claim, detail, updates)


def _reconcile_acquire(
    repository_root: str | Path,
    wanted: SemanticClaim,
    expected_old: str | None,
    detail: str,
) -> ClaimResult:
    try:
        observed = read_semantic_claim(repository_root, wanted.parent_issue_number)
    except SemanticClaimError as error:
        return ClaimResult(ClaimStatus.UNKNOWN, None, f"{detail}; {error}")
    if observed is not None and observed.ref_oid == wanted.ref_oid:
        return ClaimResult(ClaimStatus.ACQUIRED, observed)
    if (
        observed is not None
        and observed.ref_oid != expected_old
        and not _owned_by(observed, wanted.attempt_id, wanted.holder)
    ):
        return ClaimResult(ClaimStatus.HELD_BY_OTHER, observed, detail)
    return ClaimResult(ClaimStatus.UNKNOWN, None, detail)


def _push(repository_root: str | Path, changes: Sequence[_Change]) -> tuple[bool, str]:
    """One atomic push of ``(ref, new_oid, expected_old_oid)`` changes."""
    leases = [f"--force-with-lease={ref}:{old or ''}" for ref, _, old in changes]
    refspecs = [f"{new or ''}:{ref}" for ref, new, _ in changes]
    result = _run(
        repository_root,
        ["push", "--atomic", "--porcelain", *leases, "origin", *refspecs],
    )
    return result.returncode == 0, f"{result.stdout}\n{result.stderr}".strip()


def _remote_ref_oid(repository_root: str | Path, ref: str) -> str | None:
    result = _run(repository_root, ["ls-remote", "--exit-code", "origin", ref])
    if result.returncode == 2:
        return None
    if result.returncode != 0:
        raise SemanticClaimError(f"failed to read {ref}: {result.stderr}")
    for oid, name in _ls_remote_lines(result.stdout):
        if name == ref:
            return oid
    return None


def _ls_remote_lines(output: str) -> list[tuple[str, str]]:
    lines = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and _OID_PATTERN.fullmatch(parts[0].lower()):
            lines.append((parts[0].lower(), parts[1]))
    return lines


def _create_claim_commit(
    repository_root: str | Path,
    claim: SemanticClaim,
    *,
    parent: str | None,
    now: datetime,
) -> str:
    message = json.dumps(
        {
            "version": CLAIM_VERSION,
            "parent_issue_number": claim.parent_issue_number,
            "attempt_id": claim.attempt_id,
            "holder": claim.holder,
            "generation": claim.generation,
            "expires_at": claim.expires_at.isoformat(),
        }
    )
    # Two advancements of one claim with equal content and second-precision
    # dates would otherwise produce the same commit: Git treats the second
    # push of that OID as up to date and skips its lease, so a reused claim
    # could write twice. The nonce keeps every claim commit unique.
    message += f"\n\n{_NONCE_TRAILER}: {uuid.uuid4().hex}"
    return _commit_tree(repository_root, message, parent=parent, now=now)


def _commit_tree(
    repository_root: str | Path, message: str, *, parent: str | None, now: datetime
) -> str:
    with tempfile.TemporaryDirectory() as directory:
        empty = Path(directory) / "empty"
        empty.write_bytes(b"")
        tree = run_git(
            ["hash-object", "-w", "-t", "tree", str(empty)], cwd=repository_root
        ).stdout.strip()
    date = f"{int(now.timestamp())} +0000"
    env = get_clean_git_env(
        {**_IDENTITY, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    )
    parents = ["-p", parent] if parent else []
    result = run_git(
        ["commit-tree", "--no-gpg-sign", *parents, "-m", message, tree],
        cwd=repository_root,
        env=env,
    )
    return result.stdout.strip()


def _parse_claim_commit(
    repository_root: str | Path, oid: str, parent_issue_number: int
) -> SemanticClaim:
    raw = _run(repository_root, ["cat-file", "commit", oid])
    if raw.returncode != 0 or "\n\n" not in raw.stdout:
        raise SemanticClaimCorruptError(f"claim ref {oid} is not a claim commit")
    try:
        message = raw.stdout.split("\n\n", 1)[1]
        data = json.loads(message.split("\n\n", 1)[0])
        claim = _claim_from_json(data, oid)
    except (ValueError, TypeError) as error:
        raise SemanticClaimCorruptError(f"claim {oid} is corrupt: {error}") from error
    if claim.parent_issue_number != parent_issue_number:
        raise SemanticClaimCorruptError(
            f"claim {oid} names parent issue {claim.parent_issue_number}"
        )
    return claim


def _claim_from_json(data: object, oid: str) -> SemanticClaim:
    if not isinstance(data, dict) or set(data) != _CLAIM_KEYS:
        raise ValueError("unexpected claim fields")
    if data["version"] != CLAIM_VERSION or isinstance(data["version"], bool):
        raise ValueError(f"unsupported claim version {data['version']!r}")
    expires_at = data["expires_at"]
    if not isinstance(expires_at, str):
        raise ValueError("expires_at must be a string")
    return SemanticClaim(
        parent_issue_number=_positive_int(data["parent_issue_number"]),
        attempt_id=_text(data["attempt_id"], "attempt_id"),
        holder=_text(data["holder"], "holder"),
        generation=_positive_int(data["generation"]),
        expires_at=_utc(datetime.fromisoformat(expires_at)),
        ref_oid=oid,
    )


def _validated_updates(updates: Sequence[RefUpdate]) -> tuple[RefUpdate, ...]:
    checked = []
    for update in updates:
        if update.new_oid is None and update.expected_old_oid is None:
            raise ValueError(f"deleting {update.ref!r} needs an expected old oid")
        checked.append(
            RefUpdate(
                _target_ref(update.ref),
                None if update.new_oid is None else _oid(update.new_oid),
                None
                if update.expected_old_oid is None
                else _oid(update.expected_old_oid),
            )
        )
    refs = [update.ref for update in checked]
    if len(refs) != len(set(refs)):
        raise ValueError(f"a ref is updated twice: {refs}")
    return tuple(checked)


def _target_ref(ref: str) -> str:
    if (
        not ref.startswith("refs/")
        or not _BRANCH_PATTERN.fullmatch(ref.removeprefix("refs/"))
        or ".." in ref
        or "//" in ref
        or ref.endswith(("/", ".lock"))
        or ref.startswith(CLAIM_REF_PREFIX)
    ):
        raise ValueError(f"invalid ref for a claim-guarded update: {ref!r}")
    return ref


def _branch(branch: str) -> str:
    if not _BRANCH_PATTERN.fullmatch(branch) or ".." in branch:
        raise ValueError(f"invalid branch name: {branch!r}")
    return branch


def _oid(value: str) -> str:
    oid = value.lower() if isinstance(value, str) else ""
    if not _OID_PATTERN.fullmatch(oid):
        raise ValueError(f"invalid commit sha: {value!r}")
    return oid


def _owned_by(claim: SemanticClaim, attempt_id: str, holder: str) -> bool:
    return claim.attempt_id == attempt_id and claim.holder == holder


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"an aware UTC datetime is required: {value!r}")
    return value.astimezone(UTC)


def _ttl(ttl_seconds: float) -> timedelta:
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int | float):
        raise ValueError(f"ttl_seconds must be a number: {ttl_seconds!r}")
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be positive: {ttl_seconds!r}")
    return timedelta(seconds=ttl_seconds)


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"a positive integer is required: {value!r}")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _run(repository_root: str | Path, args: list[str]) -> GitResult:
    """Run git without raising; a failure to start becomes a non-zero result."""
    try:
        return run_git(args, cwd=repository_root, check=False)
    except OSError as error:
        return GitResult(-1, "", str(error))


def _detail(error: subprocess.CalledProcessError | OSError | ValueError) -> str:
    if not isinstance(error, subprocess.CalledProcessError):
        return str(error)
    return f"{error.stdout or ''} {error.stderr or ''}".strip()
