"""Fail-closed local-CI evidence recording, validation, and execution (#1000)."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import shlex
import subprocess
import sys
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from orchestune.infra.git_cli import run_git
from orchestune.outcome_record import RESULT_DONE

CI_EVIDENCE_FILENAME = "ci_evidence.json"
CURRENT_SCHEMA_VERSION = 1


class CiEvidenceError(ValueError):
    """Base error for CI evidence validation and execution failures."""


class CiEvidenceMissingError(CiEvidenceError):
    """Raised when CI evidence does not exist."""


class CiEvidenceInvalidError(CiEvidenceError):
    """Raised when CI evidence is corrupt, partial, or has an invalid/unknown schema."""


class CiEvidenceMismatchError(CiEvidenceError):
    """Raised when CI evidence does not match the current HEAD, base, definition, or environment."""


class CiExecutionError(CiEvidenceError):
    """Raised when running local CI fails."""


@dataclass(frozen=True)
class CiEvidence:
    """Versioned evidence bound to a completion request."""

    head_sha: str
    base_sha: str
    succeeded: bool
    schema_version: int = CURRENT_SCHEMA_VERSION
    ci_definition_digest: str = ""
    lockfile_digest: str = ""
    os: str = ""
    architecture: str = ""
    python_version: str = ""
    started_at: str = ""
    completed_at: str = ""
    exit_code: int = 0
    status: str = "passed"

    def to_dict(self) -> dict[str, Any]:
        """Serialize evidence to a dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CiEvidence:
        """Construct and validate CiEvidence from dictionary."""
        if not isinstance(data, dict):
            raise CiEvidenceInvalidError("Evidence payload must be a dictionary")
        _validate_raw_evidence_data(data)
        return cls(
            head_sha=str(data["head_sha"]),
            base_sha=str(data["base_sha"]),
            succeeded=bool(data["succeeded"]),
            schema_version=int(data["schema_version"]),
            ci_definition_digest=str(data["ci_definition_digest"]),
            lockfile_digest=str(data["lockfile_digest"]),
            os=str(data["os"]),
            architecture=str(data["architecture"]),
            python_version=str(data["python_version"]),
            started_at=str(data["started_at"]),
            completed_at=str(data["completed_at"]),
            exit_code=int(data["exit_code"]),
            status=str(data["status"]),
        )


def _validate_raw_evidence_data(data: dict[str, Any]) -> None:
    schema_version = data.get("schema_version")
    if schema_version != CURRENT_SCHEMA_VERSION:
        raise CiEvidenceInvalidError(
            f"Unsupported schema version: {schema_version!r} (expected {CURRENT_SCHEMA_VERSION})"
        )
    required = (
        "head_sha",
        "base_sha",
        "succeeded",
        "ci_definition_digest",
        "lockfile_digest",
        "os",
        "architecture",
        "python_version",
        "started_at",
        "completed_at",
        "exit_code",
        "status",
    )
    for field_name in required:
        if field_name not in data:
            raise CiEvidenceInvalidError(
                f"Missing required evidence field: {field_name!r}"
            )
    if data["exit_code"] != 0:
        raise CiEvidenceInvalidError(
            f"Evidence recorded non-zero exit code: {data['exit_code']}"
        )
    if data["succeeded"] is not True:
        raise CiEvidenceInvalidError("Evidence succeeded flag is not True")
    if data["status"] not in ("passed", "success"):
        raise CiEvidenceInvalidError(f"Invalid evidence status: {data['status']!r}")


def resolve_evidence_dir(worktree_root: Path | str | None = None) -> Path:
    """Resolve the directory outside the worktree's tracked area to store evidence."""
    override = os.environ.get("ORCHESTUNE_CI_EVIDENCE_PATH")
    if override:
        return Path(override).parent.resolve()

    cwd = (
        Path(worktree_root).resolve()
        if worktree_root is not None
        else Path.cwd().resolve()
    )
    try:
        res = run_git(["rev-parse", "--git-dir"], cwd=cwd, check=False)
        if res.returncode == 0 and res.stdout.strip():
            raw = Path(res.stdout.strip())
            return raw if raw.is_absolute() else (cwd / raw).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return (cwd / ".git").resolve()


def resolve_evidence_path(worktree_root: Path | str | None = None) -> Path:
    """Resolve the file path for CI evidence."""
    override = os.environ.get("ORCHESTUNE_CI_EVIDENCE_PATH")
    if override:
        return Path(override).resolve()
    return resolve_evidence_dir(worktree_root) / CI_EVIDENCE_FILENAME


def compute_ci_definition_digest(worktree_root: Path | str | None = None) -> str:
    """Compute deterministic SHA256 digest of CI runner scripts and configurations."""
    root = (
        Path(worktree_root).resolve()
        if worktree_root is not None
        else Path.cwd().resolve()
    )
    h = hashlib.sha256()

    definition_files = [
        "pyproject.toml",
        "scripts/local-ci.ps1",
        "scripts/local-ci.sh",
    ]
    for rel_path in sorted(definition_files):
        p = root / rel_path
        h.update(rel_path.encode("utf-8"))
        if p.is_file():
            h.update(p.read_bytes())
        else:
            h.update(b"__absent__")
    return h.hexdigest()


def compute_lockfile_digest(worktree_root: Path | str | None = None) -> str:
    """Compute SHA256 digest of the dependency lockfile."""
    root = (
        Path(worktree_root).resolve()
        if worktree_root is not None
        else Path.cwd().resolve()
    )
    lockfile = root / "uv.lock"
    if lockfile.is_file():
        return hashlib.sha256(lockfile.read_bytes()).hexdigest()
    return hashlib.sha256(b"__absent__").hexdigest()


def _get_current_environment() -> tuple[str, str, str]:
    """Return (os, architecture, python_version) for the current runtime."""
    os_name = platform.system()
    arch_name = platform.machine()
    py_ver = f"{platform.python_implementation()} {platform.python_version()}"
    return os_name, arch_name, py_ver


def _resolve_head_sha(root: Path) -> str:
    """Resolve current HEAD commit SHA."""
    res = run_git(["rev-parse", "HEAD"], cwd=root, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        raise CiEvidenceError(f"Failed to resolve HEAD SHA in {root}")
    return res.stdout.strip()


def _resolve_base_sha(root: Path, request: Any | None = None) -> str:
    """Resolve base commit SHA from request, active state, upstream, or fallback."""
    if request is not None:
        payload = getattr(request, "payload", None)
        if payload is not None and getattr(payload, "base_sha", None):
            return str(payload.base_sha)

    # Try merge-base with upstream
    res = run_git(["merge-base", "HEAD", "@{upstream}"], cwd=root, check=False)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()

    # Try merge-base with origin/main or main
    for base_ref in ("origin/main", "main"):
        res = run_git(["merge-base", "HEAD", base_ref], cwd=root, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()

    # Fallback to HEAD~1 or HEAD
    res = run_git(["rev-parse", "HEAD~1"], cwd=root, check=False)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()

    return _resolve_head_sha(root)


def invalidate_ci_evidence(worktree_root: Path | str | None = None) -> None:
    """Invalidate existing CI evidence and delete leftover temporary files."""
    evidence_path = resolve_evidence_path(worktree_root)
    if evidence_path.is_file():
        evidence_path.unlink(missing_ok=True)

    parent_dir = evidence_path.parent
    if parent_dir.is_dir():
        pattern = f"{CI_EVIDENCE_FILENAME}.tmp.*"
        for tmp_file in parent_dir.glob(pattern):
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass


def record_ci_evidence(
    worktree_root: Path | str | None = None,
    *,
    started_at: str | None = None,
    completed_at: str | None = None,
    exit_code: int = 0,
    base_sha: str | None = None,
) -> CiEvidence:
    """Atomically record successful CI evidence outside the worktree tracking area."""
    root = (
        Path(worktree_root).resolve()
        if worktree_root is not None
        else Path.cwd().resolve()
    )
    evidence_path = resolve_evidence_path(root)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)

    os_name, arch_name, py_ver = _get_current_environment()

    now_utc = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    evidence = CiEvidence(
        head_sha=_resolve_head_sha(root),
        base_sha=base_sha or _resolve_base_sha(root),
        succeeded=(exit_code == 0),
        schema_version=CURRENT_SCHEMA_VERSION,
        ci_definition_digest=compute_ci_definition_digest(root),
        lockfile_digest=compute_lockfile_digest(root),
        os=os_name,
        architecture=arch_name,
        python_version=py_ver,
        started_at=started_at or now_utc,
        completed_at=completed_at or now_utc,
        exit_code=exit_code,
        status="passed" if exit_code == 0 else "failed",
    )
    _write_evidence_atomic(evidence, evidence_path)
    return evidence


def _write_evidence_atomic(evidence: CiEvidence, evidence_path: Path) -> None:
    tmp_path = evidence_path.with_name(
        f"{CI_EVIDENCE_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    )
    payload_json = json.dumps(evidence.to_dict(), indent=2, sort_keys=True)
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload_json)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, evidence_path)


def _validate_evidence_context(
    evidence: CiEvidence,
    root: Path,
    request: Any,
) -> None:
    """Validate that evidence matches current HEAD, base, definition, and environment."""
    current_head = _resolve_head_sha(root)
    if evidence.head_sha != current_head:
        raise CiEvidenceMismatchError(
            f"HEAD SHA mismatch: evidence has {evidence.head_sha}, current worktree is {current_head}"
        )

    expected_base = _resolve_base_sha(root, request)
    if expected_base and evidence.base_sha != expected_base:
        raise CiEvidenceMismatchError(
            f"Base SHA mismatch: evidence has {evidence.base_sha}, expected {expected_base}"
        )

    current_ci_digest = compute_ci_definition_digest(root)
    if evidence.ci_definition_digest != current_ci_digest:
        raise CiEvidenceMismatchError(
            f"CI definition digest mismatch: evidence has {evidence.ci_definition_digest}, "
            f"current definition is {current_ci_digest}"
        )

    current_lock_digest = compute_lockfile_digest(root)
    if evidence.lockfile_digest != current_lock_digest:
        raise CiEvidenceMismatchError(
            f"Lockfile digest mismatch: evidence has {evidence.lockfile_digest}, "
            f"current lockfile is {current_lock_digest}"
        )

    cur_os, cur_arch, cur_py = _get_current_environment()
    if (
        evidence.os != cur_os
        or evidence.architecture != cur_arch
        or evidence.python_version != cur_py
    ):
        raise CiEvidenceMismatchError(
            f"Execution environment mismatch: evidence has ({evidence.os}, {evidence.architecture}, "
            f"{evidence.python_version}), current is ({cur_os}, {cur_arch}, {cur_py})"
        )


def validate_ci_evidence(request: Any) -> CiEvidence:
    """Validate saved local-CI evidence for a completion request."""
    root = getattr(request, "worktree_root", None)
    worktree_path = Path(root).resolve() if root is not None else Path.cwd().resolve()
    evidence_path = resolve_evidence_path(worktree_path)

    if not evidence_path.is_file():
        raise CiEvidenceMissingError(f"No CI evidence found at {evidence_path}")

    try:
        raw_text = evidence_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as e:
        raise CiEvidenceInvalidError(f"Corrupt or unreadable CI evidence: {e}") from e

    evidence = CiEvidence.from_dict(data)
    _validate_evidence_context(evidence, worktree_path, request)
    return evidence


def _resolve_default_ci_command(root: Path) -> list[str]:
    """Resolve OS-specific CI script command."""
    if sys.platform == "win32":
        ps1_script = root / "scripts" / "local-ci.ps1"
        return ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ps1_script)]
    sh_script = root / "scripts" / "local-ci.sh"
    return [str(sh_script)]


def run_local_ci_if_needed(
    request: Any,
    *,
    ci_command: Sequence[str] | str | None = None,
) -> CiEvidence:
    """Run local CI when valid evidence is unavailable."""
    req_result = getattr(request, "result", None)
    if req_result != RESULT_DONE:
        raise ValueError(
            f"CI execution is not applicable for non-done requests (got {req_result!r})"
        )

    is_dry_run = getattr(request, "dry_run", False)
    if is_dry_run:
        return validate_ci_evidence(request)

    try:
        return validate_ci_evidence(request)
    except CiEvidenceError:
        pass

    root = getattr(request, "worktree_root", None)
    worktree_path = Path(root).resolve() if root is not None else Path.cwd().resolve()

    cmd: Sequence[str]
    if ci_command is None:
        cmd = _resolve_default_ci_command(worktree_path)
    elif isinstance(ci_command, str):
        cmd = shlex.split(ci_command)
    else:
        cmd = list(ci_command)

    res = subprocess.run(cmd, cwd=worktree_path, check=False)
    if res.returncode != 0:
        raise CiExecutionError(
            f"Local CI execution failed with exit code {res.returncode}"
        )

    return validate_ci_evidence(request)


def _cli_invalidate(args: argparse.Namespace) -> None:
    invalidate_ci_evidence(args.worktree)


def _cli_record(args: argparse.Namespace) -> None:
    record_ci_evidence(
        worktree_root=args.worktree,
        started_at=args.started_at,
        exit_code=args.exit_code,
    )


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entrypoint for local-CI evidence management."""
    parser = argparse.ArgumentParser(
        description="Orchestune complete local-CI evidence manager"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inv_parser = subparsers.add_parser(
        "invalidate", help="Invalidate existing CI evidence"
    )
    inv_parser.add_argument(
        "--worktree", type=Path, default=None, help="Path to worktree root"
    )

    rec_parser = subparsers.add_parser("record", help="Record CI execution evidence")
    rec_parser.add_argument(
        "--worktree", type=Path, default=None, help="Path to worktree root"
    )
    rec_parser.add_argument(
        "--started-at", type=str, default=None, help="CI start timestamp (ISO 8601)"
    )
    rec_parser.add_argument(
        "--exit-code", type=int, default=0, help="CI runner exit code"
    )

    args = parser.parse_args(argv)
    if args.command == "invalidate":
        _cli_invalidate(args)
    elif args.command == "record":
        _cli_record(args)


if __name__ == "__main__":
    main()
