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

from orchestune.infra.git_cli import WorktreeStatus, inspect_worktree_status, run_git
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
    worktree_clean: bool = True
    tree_sha: str = ""

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
            worktree_clean=bool(data["worktree_clean"]),
            tree_sha=str(data["tree_sha"]),
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
        "worktree_clean",
        "tree_sha",
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


def _query_python_version(cmd: list[str], cwd: Path) -> str | None:
    """Run command to query implementation and version of Python interpreter."""
    try:
        res = subprocess.run(
            [
                *cmd,
                "-c",
                "import platform; print(f'{platform.python_implementation()} {platform.python_version()}')",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _find_configured_python(root: Path) -> Path | None:
    """Find configured Python interpreter using uv python find without mutating filesystem."""
    try:
        res = subprocess.run(
            ["uv", "python", "find"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            candidate = Path(res.stdout.strip())
            if candidate.is_file():
                return candidate
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _resolve_runner_environment(
    worktree_root: Path | str | None = None,
) -> tuple[str, str, str]:
    """Return (os, architecture, python_version) for the expected CI runner."""
    os_name = platform.system()
    arch_name = platform.machine()
    root = (
        Path(worktree_root).resolve()
        if worktree_root is not None
        else Path.cwd().resolve()
    )

    candidate_envs: list[Path] = []
    if "UV_PROJECT_ENVIRONMENT" in os.environ:
        raw_env = os.environ["UV_PROJECT_ENVIRONMENT"]
        p = Path(raw_env)
        candidate_envs.append(p if p.is_absolute() else (root / p).resolve())
    candidate_envs.append(root / ".venv")

    for env_dir in candidate_envs:
        for py_rel in ("bin/python", "Scripts/python.exe"):
            py_bin = env_dir / py_rel
            if py_bin.is_file():
                ver = _query_python_version([str(py_bin)], cwd=root)
                if ver:
                    return os_name, arch_name, ver

    configured_py = _find_configured_python(root)
    if configured_py:
        ver = _query_python_version([str(configured_py)], cwd=root)
        if ver:
            return os_name, arch_name, ver

    py_ver = f"{platform.python_implementation()} {platform.python_version()}"
    return os_name, arch_name, py_ver


def _get_current_environment(
    worktree_root: Path | str | None = None,
) -> tuple[str, str, str]:
    """Compatibility alias for _resolve_runner_environment."""
    return _resolve_runner_environment(worktree_root)


def _resolve_head_sha(root: Path) -> str:
    """Resolve current HEAD commit SHA."""
    res = run_git(["rev-parse", "HEAD"], cwd=root, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        raise CiEvidenceError(f"Failed to resolve HEAD SHA in {root}")
    return res.stdout.strip()


def _resolve_tree_sha(root: Path) -> str:
    """Resolve tree SHA of the current HEAD commit."""
    res = run_git(["rev-parse", "HEAD^{tree}"], cwd=root, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        raise CiEvidenceError(f"Failed to resolve tree SHA in {root}")
    return res.stdout.strip()


def _has_origin_remote(root: Path) -> bool:
    """Check if repository has a configured origin remote."""
    try:
        res = run_git(["remote", "get-url", "origin"], cwd=root, check=False)
        return res.returncode == 0 and bool(res.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _query_remote_ref_tip(root: Path, ref: str) -> str | None:
    """Query authoritative remote origin tip commit SHA for a ref.

    Raises CiEvidenceError if origin is configured but the remote query fails,
    preventing silent fallback to stale cached refs.
    """
    if not _has_origin_remote(root):
        return None

    cleaned = ref.removeprefix("origin/").removeprefix("refs/heads/")
    is_remote_ref = ref.startswith("origin/") or ref.startswith("refs/remotes/origin/")

    for query_ref in (f"refs/heads/{cleaned}", cleaned):
        try:
            res = run_git(
                ["ls-remote", "origin", query_ref],
                cwd=root,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as err:
            raise CiEvidenceError(
                f"Failed to query authoritative remote tip for {ref}: {err}"
            ) from err

        if res.returncode != 0:
            raise CiEvidenceError(
                f"Failed to query authoritative remote tip for {ref} from origin "
                f"(exit code {res.returncode}): {res.stderr.strip()}"
            )

        if res.stdout.strip():
            for line in res.stdout.strip().splitlines():
                parts = line.split()
                if parts and len(parts[0]) == 40:
                    return parts[0]

    if is_remote_ref:
        raise CiEvidenceError(
            f"Remote reference {ref!r} was not found on remote origin"
        )
    return None


def _resolve_ref_tip(root: Path, ref: str) -> str | None:
    """Resolve commit SHA for a ref or origin/ref, querying remote origin first."""
    remote_tip = _query_remote_ref_tip(root, ref)
    if remote_tip:
        return remote_tip

    cleaned = ref.removeprefix("origin/")
    for candidate in (f"origin/{cleaned}", cleaned):
        res = run_git(["rev-parse", f"{candidate}^{{commit}}"], cwd=root, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    return None


def _find_active_worktree_base(
    root: Path,
    request: Any | None = None,
    *,
    state_path: Path | str | None = None,
    issue_number: int | str | None = None,
) -> tuple[str | None, str | None]:
    """Look up (base_ref, base_sha) for root from request, state_path, or run_state.json."""
    candidate_paths: list[Path] = []
    if state_path is not None:
        candidate_paths.append(Path(state_path).resolve())
    env_state = os.environ.get("ORCHESTUNE_STATE_PATH")
    if env_state:
        candidate_paths.append(Path(env_state).resolve())
    if request is not None and getattr(request, "state_path", None):
        candidate_paths.append(Path(request.state_path).resolve())
    for candidate_dir in [root] + list(root.parents)[:5]:
        candidate_paths.append(candidate_dir / "run_state.json")

    req_issue: str | None = None
    if issue_number is not None:
        req_issue = str(issue_number)
    elif os.environ.get("ORCHESTUNE_ISSUE_NUMBER"):
        req_issue = str(os.environ["ORCHESTUNE_ISSUE_NUMBER"])
    elif request is not None and getattr(request, "issue_number", None):
        req_issue = str(request.issue_number)

    for p in candidate_paths:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            active_trees = data.get("active_worktrees", {})
            for issue_str, entry in active_trees.items():
                if req_issue and issue_str == req_issue:
                    return entry.get("base_ref"), entry.get("base_sha")
                wt_path = entry.get("worktree_path")
                if wt_path and Path(wt_path).resolve() == root.resolve():
                    return entry.get("base_ref"), entry.get("base_sha")
        except (OSError, json.JSONDecodeError):
            pass
    return None, None


def _resolve_fallback_base_tip(root: Path) -> str | None:
    """Find fallback base tip from parent/* branches or main."""
    res = run_git(
        [
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/parent/",
            "refs/remotes/origin/parent/",
        ],
        cwd=root,
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
        for parent_ref in res.stdout.strip().splitlines():
            tip = _resolve_ref_tip(root, parent_ref)
            if tip:
                return tip

    for default_ref in ("origin/main", "main"):
        tip = _resolve_ref_tip(root, default_ref)
        if tip:
            return tip
    return None


def _resolve_base_sha(
    root: Path,
    request: Any | None = None,
    *,
    base_sha: str | None = None,
    base_ref: str | None = None,
    state_path: Path | str | None = None,
    issue_number: int | str | None = None,
) -> str:
    """Resolve target base branch tip commit SHA."""
    explicit_sha = base_sha or os.environ.get("ORCHESTUNE_BASE_SHA")
    if explicit_sha:
        return explicit_sha

    if request is not None:
        payload = getattr(request, "payload", None)
        if payload is not None and getattr(payload, "base_sha", None):
            return str(payload.base_sha)

    explicit_ref = base_ref or os.environ.get("ORCHESTUNE_BASE_REF")
    if explicit_ref:
        tip = _resolve_ref_tip(root, explicit_ref)
        if tip:
            return tip

    active_ref, active_sha = _find_active_worktree_base(
        root, request, state_path=state_path, issue_number=issue_number
    )
    if active_ref:
        tip = _resolve_ref_tip(root, active_ref)
        if tip:
            return tip
    if active_sha:
        return active_sha

    fallback_tip = _resolve_fallback_base_tip(root)
    if fallback_tip:
        return fallback_tip

    res = run_git(["rev-parse", "HEAD~1"], cwd=root, check=False)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()

    return _resolve_head_sha(root)


def invalidate_ci_evidence(worktree_root: Path | str | None = None) -> None:
    """Invalidate existing CI evidence and delete leftover temporary files."""
    evidence_path = resolve_evidence_path(worktree_root)
    if evidence_path.is_file():
        try:
            evidence_path.unlink()
        except OSError as err:
            raise CiEvidenceError(
                f"Failed to remove prior CI evidence at {evidence_path}: {err}"
            ) from err

    parent_dir = evidence_path.parent
    if parent_dir.is_dir():
        pattern = f"{CI_EVIDENCE_FILENAME}.tmp.*"
        for tmp_file in parent_dir.glob(pattern):
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass


def _check_run_boundaries(
    expected_head: str | None,
    expected_tree: str | None,
    expected_base: str | None,
    current_head: str,
    tree_sha: str,
    resolved_base: str,
) -> None:
    """Ensure HEAD, tree, and base did not mutate between CI launch and evidence recording."""
    exp_head = expected_head or os.environ.get("ORCHESTUNE_EXPECTED_HEAD")
    if exp_head and exp_head != current_head:
        raise CiEvidenceMismatchError(
            f"HEAD changed during CI run: started at {exp_head}, current is {current_head}"
        )

    exp_tree = expected_tree or os.environ.get("ORCHESTUNE_EXPECTED_TREE")
    if exp_tree and exp_tree != tree_sha:
        raise CiEvidenceMismatchError(
            f"Tree changed during CI run: started at {exp_tree}, current is {tree_sha}"
        )

    exp_base = expected_base or os.environ.get("ORCHESTUNE_EXPECTED_BASE")
    if exp_base and resolved_base and exp_base != resolved_base:
        raise CiEvidenceMismatchError(
            f"Base tip changed during CI run: started at {exp_base}, current is {resolved_base}"
        )


def _build_ci_evidence(
    root: Path,
    current_head: str,
    tree_sha: str,
    is_clean: bool,
    resolved_base: str,
    started_at: str | None,
    completed_at: str | None,
    exit_code: int,
) -> CiEvidence:
    """Construct CiEvidence instance for the validated worktree run."""
    os_name, arch_name, py_ver = _resolve_runner_environment(root)
    now_utc = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return CiEvidence(
        head_sha=current_head,
        base_sha=resolved_base,
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
        worktree_clean=is_clean,
        tree_sha=tree_sha,
    )


def _record_and_save_evidence(
    root: Path,
    current_head: str,
    tree_sha: str,
    resolved_base: str,
    started_at: str | None,
    completed_at: str | None,
    exit_code: int,
) -> CiEvidence:
    """Build and atomically persist CI evidence."""
    status = inspect_worktree_status(root)
    evidence = _build_ci_evidence(
        root=root,
        current_head=current_head,
        tree_sha=tree_sha,
        is_clean=(status == WorktreeStatus.CLEAN),
        resolved_base=resolved_base,
        started_at=started_at,
        completed_at=completed_at,
        exit_code=exit_code,
    )
    _write_evidence_atomic(evidence, resolve_evidence_path(root))
    return evidence


def record_ci_evidence(
    worktree_root: Path | str | None = None,
    *,
    started_at: str | None = None,
    completed_at: str | None = None,
    exit_code: int = 0,
    base_sha: str | None = None,
    base_ref: str | None = None,
    state_path: Path | str | None = None,
    issue_number: int | str | None = None,
    expected_head: str | None = None,
    expected_tree: str | None = None,
    expected_base: str | None = None,
) -> CiEvidence:
    """Atomically record successful CI evidence outside the worktree tracking area."""
    root = (
        Path(worktree_root).resolve()
        if worktree_root is not None
        else Path.cwd().resolve()
    )
    current_head = _resolve_head_sha(root)
    tree_sha = _resolve_tree_sha(root)
    resolved_base = _resolve_base_sha(
        root,
        base_sha=base_sha,
        base_ref=base_ref,
        state_path=state_path,
        issue_number=issue_number,
    )
    _check_run_boundaries(
        expected_head,
        expected_tree,
        expected_base,
        current_head,
        tree_sha,
        resolved_base,
    )
    return _record_and_save_evidence(
        root,
        current_head,
        tree_sha,
        resolved_base,
        started_at,
        completed_at,
        exit_code,
    )


def _write_evidence_atomic(evidence: CiEvidence, evidence_path: Path) -> None:
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
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


def _validate_environment_match(evidence: CiEvidence, root: Path) -> None:
    cur_os, cur_arch, cur_py = _resolve_runner_environment(root)
    if (
        evidence.os != cur_os
        or evidence.architecture != cur_arch
        or evidence.python_version != cur_py
    ):
        raise CiEvidenceMismatchError(
            f"Execution environment mismatch: evidence has ({evidence.os}, {evidence.architecture}, "
            f"{evidence.python_version}), current is ({cur_os}, {cur_arch}, {cur_py})"
        )


def _validate_worktree_cleanliness(evidence: CiEvidence, root: Path) -> None:
    """Validate that evidence and current worktree are both clean."""
    if not evidence.worktree_clean:
        raise CiEvidenceMismatchError(
            "CI evidence was recorded from a dirty worktree; clean worktree verification is required"
        )
    current_status = inspect_worktree_status(root)
    if current_status != WorktreeStatus.CLEAN:
        raise CiEvidenceMismatchError(
            f"Worktree has uncommitted changes (status={current_status.value}); "
            "clean worktree verification is required"
        )


def _validate_tree_and_commit(evidence: CiEvidence, root: Path, request: Any) -> None:
    """Validate HEAD, tree, and base SHAs."""
    current_head = _resolve_head_sha(root)
    if evidence.head_sha != current_head:
        raise CiEvidenceMismatchError(
            f"HEAD SHA mismatch: evidence has {evidence.head_sha}, current worktree is {current_head}"
        )
    current_tree = _resolve_tree_sha(root)
    if evidence.tree_sha and evidence.tree_sha != current_tree:
        raise CiEvidenceMismatchError(
            f"Tree SHA mismatch: evidence has {evidence.tree_sha}, current worktree is {current_tree}"
        )
    expected_base = _resolve_base_sha(root, request)
    if expected_base and evidence.base_sha != expected_base:
        raise CiEvidenceMismatchError(
            f"Base SHA mismatch: evidence has {evidence.base_sha}, expected {expected_base}"
        )


def _validate_digests_and_env(evidence: CiEvidence, root: Path) -> None:
    """Validate CI definition, lockfile digest, and execution environment."""
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
    _validate_environment_match(evidence, root)


def _validate_evidence_context(
    evidence: CiEvidence,
    root: Path,
    request: Any,
) -> None:
    """Validate that evidence matches current HEAD, base, definition, and environment."""
    _validate_worktree_cleanliness(evidence, root)
    _validate_tree_and_commit(evidence, root, request)
    _validate_digests_and_env(evidence, root)


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


def _build_ci_env(
    worktree_path: Path,
    request: Any,
    expected_base_sha: str,
    initial_head: str,
    initial_tree: str,
    initial_base: str,
) -> dict[str, str]:
    """Prepare environment variables for running local CI command."""
    env = os.environ.copy()
    env["ORCHESTUNE_BASE_SHA"] = expected_base_sha
    env["ORCHESTUNE_EXPECTED_HEAD"] = initial_head
    env["ORCHESTUNE_EXPECTED_TREE"] = initial_tree
    if initial_base:
        env["ORCHESTUNE_EXPECTED_BASE"] = initial_base
    if getattr(request, "state_path", None):
        env["ORCHESTUNE_STATE_PATH"] = str(request.state_path)
    if getattr(request, "issue_number", None):
        env["ORCHESTUNE_ISSUE_NUMBER"] = str(request.issue_number)
    return env


def _execute_local_ci(
    worktree_path: Path,
    request: Any,
    ci_command: Sequence[str] | str | None,
    expected_base_sha: str,
    initial_head: str,
    initial_tree: str,
    initial_base: str,
) -> None:
    """Execute local CI runner command with appropriate environment variables."""
    cmd = (
        _resolve_default_ci_command(worktree_path)
        if ci_command is None
        else (
            shlex.split(ci_command) if isinstance(ci_command, str) else list(ci_command)
        )
    )
    env = _build_ci_env(
        worktree_path,
        request,
        expected_base_sha,
        initial_head,
        initial_tree,
        initial_base,
    )
    res = subprocess.run(cmd, cwd=worktree_path, env=env, check=False)
    if res.returncode != 0:
        raise CiExecutionError(
            f"Local CI execution failed with exit code {res.returncode}"
        )


def _verify_ci_run_result(
    request: Any,
    worktree_path: Path,
    initial_head: str,
    initial_tree: str,
    initial_base: str,
) -> CiEvidence:
    """Verify evidence against worktree invariants after local CI finishes."""
    evidence = validate_ci_evidence(request)
    if evidence.head_sha != initial_head or (
        evidence.tree_sha and evidence.tree_sha != initial_tree
    ):
        raise CiEvidenceMismatchError(
            f"HEAD or tree changed during CI run: started at ({initial_head}, {initial_tree}), "
            f"evidence has ({evidence.head_sha}, {evidence.tree_sha})"
        )
    current_base = _resolve_base_sha(worktree_path, request)
    if initial_base and current_base != initial_base:
        raise CiEvidenceMismatchError(
            f"Base tip changed during CI run: started at {initial_base}, current is {current_base}"
        )
    return evidence


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

    if getattr(request, "dry_run", False):
        return validate_ci_evidence(request)

    try:
        return validate_ci_evidence(request)
    except CiEvidenceError:
        pass

    root = getattr(request, "worktree_root", None)
    worktree_path = Path(root).resolve() if root is not None else Path.cwd().resolve()
    initial_base = _resolve_base_sha(worktree_path, request)
    initial_head = _resolve_head_sha(worktree_path)
    initial_tree = _resolve_tree_sha(worktree_path)

    _execute_local_ci(
        worktree_path,
        request,
        ci_command,
        initial_base,
        initial_head,
        initial_tree,
        initial_base,
    )
    return _verify_ci_run_result(
        request, worktree_path, initial_head, initial_tree, initial_base
    )


def _cli_invalidate(args: argparse.Namespace) -> None:
    invalidate_ci_evidence(args.worktree)


def _cli_resolve_base(args: argparse.Namespace) -> None:
    root = Path(args.worktree).resolve() if args.worktree else Path.cwd().resolve()
    base_sha = _resolve_base_sha(
        root,
        base_ref=args.base_ref,
        state_path=args.state_path,
        issue_number=args.issue,
    )
    if base_sha:
        print(base_sha)


def _cli_record(args: argparse.Namespace) -> None:
    record_ci_evidence(
        worktree_root=args.worktree,
        started_at=args.started_at,
        exit_code=args.exit_code,
        base_sha=args.base_sha,
        base_ref=args.base_ref,
        state_path=args.state_path,
        issue_number=args.issue,
        expected_head=args.expected_head,
        expected_tree=args.expected_tree,
        expected_base=args.expected_base,
    )


def _add_resolve_base_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--worktree", type=Path, default=None, help="Path to worktree root"
    )
    parser.add_argument(
        "--base-ref", type=str, default=None, help="Target base branch reference"
    )
    parser.add_argument(
        "--state-path", type=Path, default=None, help="Path to run_state.json"
    )
    parser.add_argument(
        "--issue", type=int, default=None, help="Issue number for worktree"
    )


def _add_record_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--worktree", type=Path, default=None, help="Path to worktree root"
    )
    parser.add_argument(
        "--started-at", type=str, default=None, help="CI start timestamp (ISO 8601)"
    )
    parser.add_argument("--exit-code", type=int, default=0, help="CI runner exit code")
    parser.add_argument(
        "--base-sha", type=str, default=None, help="Authoritative base commit SHA"
    )
    parser.add_argument(
        "--base-ref", type=str, default=None, help="Target base branch reference"
    )
    parser.add_argument(
        "--state-path", type=Path, default=None, help="Path to run_state.json"
    )
    parser.add_argument(
        "--issue", type=int, default=None, help="Issue number for worktree"
    )
    parser.add_argument(
        "--expected-head",
        type=str,
        default=None,
        help="Initial HEAD commit SHA before CI",
    )
    parser.add_argument(
        "--expected-tree",
        type=str,
        default=None,
        help="Initial tree SHA before CI",
    )
    parser.add_argument(
        "--expected-base",
        type=str,
        default=None,
        help="Initial base commit SHA before CI",
    )


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entrypoint for local-CI evidence management."""
    parser = argparse.ArgumentParser(
        description="Orchestune complete local-CI evidence manager"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inv = subparsers.add_parser("invalidate", help="Invalidate existing CI evidence")
    inv.add_argument(
        "--worktree", type=Path, default=None, help="Path to worktree root"
    )

    res = subparsers.add_parser(
        "resolve-base", help="Resolve current base commit SHA for worktree"
    )
    _add_resolve_base_args(res)

    rec = subparsers.add_parser("record", help="Record CI execution evidence")
    _add_record_args(rec)

    args = parser.parse_args(argv)
    if args.command == "invalidate":
        _cli_invalidate(args)
    elif args.command == "resolve-base":
        _cli_resolve_base(args)
    elif args.command == "record":
        _cli_record(args)


if __name__ == "__main__":
    main()
