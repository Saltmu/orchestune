"""Autouse isolation for the bounded Integrator execution (#820).

* ``LegacySubprocessRunner`` keeps the pre-#820 seam (``patch("subprocess.run")``)
  authoritative for unit tests, so they never start the real dependency-sync or CI
  command. Tests of the real runner call ``run_managed`` directly or inject
  ``ManagedProcessRunner``.
* The per-parent execution lock is stubbed like the other file locks unless a test
  asks for ``uses_real_file_lock``.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from orchestune.infra.managed_process import (
    ManagedProcessResult,
    ManagedProcessSpec,
    ProcessOutcome,
    set_default_runner,
)


class LegacySubprocessRunner:
    """Route managed commands through ``subprocess.run`` so existing doubles still apply.

    Unit tests must never start the real dependency-sync / CI command. This keeps the
    pre-#820 seam (``patch("subprocess.run")``) authoritative for them, with the same
    ``check=True, capture_output=True`` call shape and exception semantics. Tests of
    the real runner call ``run_managed`` directly or inject ``ManagedProcessRunner``.
    """

    def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
        def result(
            outcome: ProcessOutcome,
            *,
            returncode: int | None = None,
            stdout: object = b"",
            stderr: object = b"",
            detail: str = "",
        ) -> ManagedProcessResult:
            def text(value: object) -> str:
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                return "" if value is None else str(value)

            return ManagedProcessResult(
                outcome=outcome,
                stage=spec.stage,
                returncode=returncode,
                elapsed_seconds=0.0,
                timeout_seconds=spec.timeout_seconds,
                stdout_tail=text(stdout),
                stderr_tail=text(stderr),
                stop_confirmed=None if outcome is ProcessOutcome.START_FAILED else True,
                detail=detail,
            )

        try:
            subprocess.run(
                list(spec.args),
                cwd=None if spec.cwd is None else str(spec.cwd),
                check=True,
                capture_output=True,
                env=None if spec.env is None else dict(spec.env),
            )
        except subprocess.CalledProcessError as error:
            return result(
                ProcessOutcome.NONZERO_EXIT,
                returncode=error.returncode,
                stdout=error.stdout,
                stderr=error.stderr,
            )
        except subprocess.TimeoutExpired as error:
            return result(
                ProcessOutcome.TIMED_OUT,
                stdout=error.stdout,
                stderr=error.stderr,
                detail="command exceeded its time limit",
            )
        except OSError as error:
            return result(ProcessOutcome.START_FAILED, detail=str(error))
        return result(ProcessOutcome.SUCCESS, returncode=0)


@pytest.fixture(autouse=True)
def legacy_subprocess_process_runner() -> Iterator[None]:
    previous = set_default_runner(LegacySubprocessRunner())
    try:
        yield
    finally:
        set_default_runner(previous)


@pytest.fixture(autouse=True)
def stub_parent_execution_lock(request: pytest.FixtureRequest) -> Iterator[None]:
    """Avoid real file locks for the per-parent execution lock, like the other locks."""
    if request.node.get_closest_marker("uses_real_file_lock") is not None:
        yield
        return
    with patch(
        "orchestune.integrator.execution.file_lock",
        lambda _lock_path, **_kwargs: contextlib.nullcontext(),
    ):
        yield
