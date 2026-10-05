"""Bounded dependency preparation and CI execution for the Integrator (#820).

Each stage runs at most once per attempt through the managed process runner with
``min(stage limit, remaining cycle time)`` as its limit; a retry is the next
integration cycle's decision, never an in-stage loop. The result is a typed
``CiStageResult`` so a timeout is not folded into a generic "CI verification failed".
The legacy ``(ok, message)`` text is derived from it for existing callers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from orchestune.infra.execution_deadline import ExecutionScope
from orchestune.infra.managed_process import (
    ManagedProcessResult,
    ManagedProcessSpec,
    ProcessOutcome,
    ProcessRunner,
    get_default_runner,
)
from orchestune.infra.python_env import (
    DEPENDENCY_STAGE,
    describe_sync_failure,
    resolve_virtualenv_path,
    sync_dependencies,
)
from orchestune.integrator.timeout_policy import (
    ExecutionFailureCause,
    IntegrationExecutionPolicy,
)

CI_STAGE = "ci"
ENVIRONMENT_STAGE = "environment"
# #1248: Integrator CI verifies a temporary merge worktree; no Orchestune control or
# completion-context variable of the parent process is a valid input for it.
_ORCHESTUNE_ENV_PREFIX = "ORCHESTUNE_"


@dataclass(frozen=True)
class CiStageResult:
    """The outcome of dependency preparation plus the CI command, or the stage that stopped it."""

    ok: bool
    stage: str
    process: ManagedProcessResult | None = None
    message: str = ""
    configured_limit_seconds: float | None = None
    effective_limit_seconds: float | None = None
    # The cycle deadline had already passed, so the stage was never started.
    deadline_exceeded: bool = False

    @property
    def timed_out(self) -> bool:
        if self.deadline_exceeded:
            return True
        return self.process is not None and self.process.outcome in (
            ProcessOutcome.TIMED_OUT,
        )

    @property
    def stop_unconfirmed(self) -> bool:
        return (
            self.process is not None
            and self.process.outcome is ProcessOutcome.STOP_UNCONFIRMED
        )

    @property
    def cause(self) -> ExecutionFailureCause | None:
        """The execution failure this result represents; ``None`` for ordinary failures."""
        if self.stop_unconfirmed:
            return ExecutionFailureCause.CLEANUP_FAILED
        if not self.timed_out:
            return None
        configured = self.configured_limit_seconds
        effective = self.effective_limit_seconds
        if self.deadline_exceeded or (
            configured is not None and effective is not None and effective < configured
        ):
            return ExecutionFailureCause.CYCLE_DEADLINE_EXCEEDED
        if self.stage == DEPENDENCY_STAGE:
            return ExecutionFailureCause.DEPENDENCY_TIMEOUT
        return ExecutionFailureCause.CI_TIMEOUT

    def legacy(self) -> tuple[bool, str]:
        """The pre-#820 ``(ok, output)`` pair."""
        return self.ok, self.message


def _stage_limit(configured: float, scope: ExecutionScope | None) -> float:
    return configured if scope is None else scope.stage_limit(configured)


def _expired(
    stage: str, configured: float, scope: ExecutionScope | None
) -> CiStageResult | None:
    if scope is None or not scope.expired():
        return None
    return CiStageResult(
        ok=False,
        stage=stage,
        message=f"Integration cycle deadline exceeded before the {stage} stage",
        configured_limit_seconds=configured,
        effective_limit_seconds=0.0,
        deadline_exceeded=True,
    )


def _ci_output(process: ManagedProcessResult) -> str:
    return (
        f"--- stdout ---\n{process.stdout_tail}\n"
        f"--- stderr ---\n{process.stderr_tail}"
    )


def _failure_message(process: ManagedProcessResult, subject: str) -> str:
    if process.outcome is ProcessOutcome.TIMED_OUT:
        return (
            f"{subject} timed out after {process.timeout_seconds:g}s "
            f"(stage={process.stage})\n{_ci_output(process)}"
        )
    if process.outcome is ProcessOutcome.STOP_UNCONFIRMED:
        return (
            f"{subject} could not be confirmed stopped (stage={process.stage}): "
            f"{process.detail}\n{_ci_output(process)}"
        )
    if process.outcome is ProcessOutcome.START_FAILED:
        return f"Failed to start {subject}: {process.detail}"
    return _ci_output(process)


def _prepare_environment_vars(
    repository_root: Path, original_root: Path
) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_ORCHESTUNE_ENV_PREFIX)
    }
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    return env


def configure_virtualenv(
    env: dict[str, str], repository_root: Path, original_root: Path
) -> None:
    venv_path = resolve_virtualenv_path(repository_root, original_root, env)
    if venv_path and venv_path.exists():
        env["VIRTUAL_ENV"] = str(venv_path.resolve())
        bin_path = venv_path / "bin"
        if bin_path.exists():
            env["PATH"] = f"{bin_path.resolve()}{os.pathsep}{env.get('PATH', '')}"


def prepare_environment(
    repository_root: Path,
    original_root: Path,
    *,
    policy: IntegrationExecutionPolicy,
    scope: ExecutionScope | None = None,
    runner: ProcessRunner | None = None,
) -> tuple[dict[str, str], CiStageResult | None]:
    """Sync dependencies (bounded) and resolve the virtualenv; ``None`` result means ready."""
    env = _prepare_environment_vars(repository_root, original_root)
    configured = float(policy.integration_dependency_timeout_seconds)
    expired = _expired(DEPENDENCY_STAGE, configured, scope)
    if expired is not None:
        return env, expired
    effective = _stage_limit(configured, scope)
    process = sync_dependencies(
        repository_root,
        env,
        timeout_seconds=effective,
        cleanup=None if scope is None else scope.cleanup,
        runner=runner or get_default_runner(),
    )
    if process is not None and not process.ok:
        return env, CiStageResult(
            ok=False,
            stage=DEPENDENCY_STAGE,
            process=process,
            message=describe_sync_failure(process),
            configured_limit_seconds=configured,
            effective_limit_seconds=effective,
        )
    configure_virtualenv(env, repository_root, original_root)
    return env, None


def run_ci_command(
    ci_command: list[str],
    repository_root: Path,
    env: dict[str, str],
    *,
    policy: IntegrationExecutionPolicy,
    scope: ExecutionScope | None = None,
    runner: ProcessRunner | None = None,
) -> CiStageResult:
    """Run the configured CI command exactly once under the CI deadline."""
    configured = float(policy.integration_ci_timeout_seconds)
    expired = _expired(CI_STAGE, configured, scope)
    if expired is not None:
        return expired
    effective = _stage_limit(configured, scope)
    process = (runner or get_default_runner()).run(
        ManagedProcessSpec(
            args=ci_command,
            stage=CI_STAGE,
            timeout_seconds=effective,
            cwd=repository_root,
            env=env,
            cleanup=None if scope is None else scope.cleanup,
        )
    )
    if process.ok:
        return CiStageResult(
            ok=True,
            stage=CI_STAGE,
            process=process,
            configured_limit_seconds=configured,
            effective_limit_seconds=effective,
        )
    return CiStageResult(
        ok=False,
        stage=CI_STAGE,
        process=process,
        message=_failure_message(process, "CI command"),
        configured_limit_seconds=configured,
        effective_limit_seconds=effective,
    )


def run_ci_stages(
    ci_command: list[str],
    repository_root: Path,
    original_root: Path,
    *,
    policy: IntegrationExecutionPolicy,
    scope: ExecutionScope | None = None,
    runner: ProcessRunner | None = None,
) -> CiStageResult:
    """Prepare the environment, then run CI; each stage at most once."""
    env, failure = prepare_environment(
        repository_root, original_root, policy=policy, scope=scope, runner=runner
    )
    if failure is not None:
        return failure
    return run_ci_command(
        ci_command, repository_root, env, policy=policy, scope=scope, runner=runner
    )


__all__ = [
    "CI_STAGE",
    "ENVIRONMENT_STAGE",
    "CiStageResult",
    "configure_virtualenv",
    "prepare_environment",
    "run_ci_command",
    "run_ci_stages",
]
