"""Python dependency installation and virtualenv resolution at the L1 boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchestune.infra.execution_deadline import CleanupBudget, active_scope
from orchestune.infra.managed_process import (
    ManagedProcessResult,
    ManagedProcessSpec,
    ProcessOutcome,
    ProcessRunner,
    get_default_runner,
)

DEPENDENCY_STAGE = "dependency"
# Fallback for callers that run outside an Integrator execution scope and name no
# limit. The Integrator passes ``integration-dependency-timeout-seconds`` explicitly.
FALLBACK_SYNC_TIMEOUT_SECONDS = 600


def sync_dependencies(
    repository_root: Path,
    env: dict[str, str],
    *,
    timeout_seconds: float | None = None,
    cleanup: CleanupBudget | None = None,
    runner: ProcessRunner | None = None,
) -> ManagedProcessResult | None:
    """Run ``uv sync`` once under a deadline and return the typed result.

    ``None`` means there is nothing to sync (no ``pyproject.toml``). The command is
    never retried here; a retry is the next integration cycle's decision.
    """
    if not (repository_root / "pyproject.toml").exists():
        return None
    limit = (
        FALLBACK_SYNC_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    scope = active_scope()
    if scope is not None:
        limit = scope.stage_limit(limit)
    spec = ManagedProcessSpec(
        args=["uv", "sync"],
        stage=DEPENDENCY_STAGE,
        timeout_seconds=limit,
        cwd=repository_root,
        env=env,
        cleanup=cleanup,
    )
    return (runner or get_default_runner()).run(spec)


def describe_sync_failure(result: ManagedProcessResult) -> str:
    """The legacy ``Failed to sync uv dependencies: ...`` text for a failed sync."""
    if result.outcome is ProcessOutcome.NONZERO_EXIT:
        reason = str(
            subprocess.CalledProcessError(result.returncode or 1, ["uv", "sync"])
        )
    elif result.outcome is ProcessOutcome.TIMED_OUT:
        reason = f"uv sync timed out after {result.timeout_seconds:g}s"
    elif result.outcome is ProcessOutcome.STOP_UNCONFIRMED:
        reason = f"uv sync could not be confirmed stopped: {result.detail}"
    else:
        reason = result.detail
    return f"Failed to sync uv dependencies: {reason}"


def install_dependencies(
    repository_root: Path,
    env: dict[str, str],
    *,
    timeout_seconds: float | None = None,
    runner: ProcessRunner | None = None,
) -> str | None:
    """Synchronize a repository's Python dependencies with uv.

    Compatibility wrapper over :func:`sync_dependencies`: returns an error string or
    ``None``. Integrator control flow uses the typed result instead.
    """
    result = sync_dependencies(
        repository_root, env, timeout_seconds=timeout_seconds, runner=runner
    )
    if result is None or result.ok:
        return None
    return describe_sync_failure(result)


def resolve_virtualenv_path(
    repository_root: Path, original_root: Path, env: dict[str, str]
) -> Path | None:
    """Resolve the repository-local environment created by ``uv sync``."""
    return _fallback_venv_path(repository_root, original_root)


def _fallback_venv_path(repository_root: Path, original_root: Path) -> Path | None:
    for path in (repository_root / ".venv", original_root / ".venv"):
        if path.exists():
            return path
    return _find_ancestor_venv(original_root)


def _find_ancestor_venv(start: Path) -> Path | None:
    for ancestor in start.parents:
        candidate = ancestor / ".venv"
        if candidate.exists():
            return candidate
    return None
