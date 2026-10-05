"""#820: the OS-independent contract of the managed process runner."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from orchestune.infra import managed_process
from orchestune.infra.execution_deadline import CleanupBudget, ExecutionScope
from orchestune.infra.managed_process import (
    ManagedProcessRunner,
    ManagedProcessSpec,
    ProcessOutcome,
    run_managed,
)

PY = sys.executable


def _spec(code: str, *, timeout: float = 10.0, **kwargs: object) -> ManagedProcessSpec:
    return ManagedProcessSpec(
        args=[PY, "-c", code],
        stage="ci",
        timeout_seconds=timeout,
        term_grace_seconds=0.3,
        **kwargs,  # type: ignore[arg-type]
    )


class TestOutcomes:
    def test_success_reports_stdout_tail_and_confirmed_stop(self) -> None:
        result = run_managed(_spec("print('hello')"))

        assert result.outcome is ProcessOutcome.SUCCESS
        assert result.ok
        assert result.returncode == 0
        assert result.stage == "ci"
        assert "hello" in result.stdout_tail
        assert result.stop_confirmed is True

    def test_nonzero_exit_is_distinct_from_timeout(self) -> None:
        result = run_managed(_spec("import sys; sys.stderr.write('boom'); sys.exit(3)"))

        assert result.outcome is ProcessOutcome.NONZERO_EXIT
        assert not result.ok
        assert result.returncode == 3
        assert "boom" in result.stderr_tail

    def test_start_failure_never_claims_a_process_group(self, tmp_path: Path) -> None:
        spec = ManagedProcessSpec(
            args=[str(tmp_path / "does-not-exist")],
            stage="dependency",
            timeout_seconds=5,
        )

        result = ManagedProcessRunner().run(spec)

        assert result.outcome is ProcessOutcome.START_FAILED
        assert result.returncode is None
        assert result.stop_confirmed is None
        assert result.stage == "dependency"
        assert result.detail

    def test_timeout_stops_the_process_and_returns_promptly(self) -> None:
        started = time.monotonic()

        result = run_managed(_spec("import time; time.sleep(60)", timeout=0.4))

        assert result.outcome is ProcessOutcome.TIMED_OUT
        assert result.stop_confirmed is True
        assert result.timeout_seconds == 0.4
        assert time.monotonic() - started < 10

    def test_expired_deadline_does_not_start_the_command(self, tmp_path: Path) -> None:
        marker = tmp_path / "ran"
        code = f"open({str(marker)!r}, 'w').close()"

        result = run_managed(_spec(code, timeout=0))

        assert result.outcome is ProcessOutcome.TIMED_OUT
        assert result.stop_confirmed is None
        assert not marker.exists()


class TestOutputCollection:
    def test_tail_is_bounded_and_keeps_the_last_bytes(self) -> None:
        code = "import sys; sys.stdout.write('x' * 2_000_000 + 'END')"

        result = run_managed(_spec(code, tail_bytes=1024))

        assert result.outcome is ProcessOutcome.SUCCESS
        assert len(result.stdout_tail) <= 1024
        assert result.stdout_tail.endswith("END")

    def test_stdout_and_stderr_are_read_concurrently(self) -> None:
        code = (
            "import sys\n"
            "for _ in range(200):\n"
            "    sys.stdout.write('o' * 4096); sys.stderr.write('e' * 4096)\n"
        )

        result = run_managed(_spec(code, timeout=30))

        assert result.outcome is ProcessOutcome.SUCCESS
        assert result.stdout_tail and result.stderr_tail


class _StuckGroup:
    """A group whose members never disappear (an unkillable process)."""

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def alive(self) -> bool:
        return True

    def close(self) -> None:
        return None


class TestStopUnconfirmed:
    def test_unconfirmed_stop_is_reported_instead_of_timed_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_start = managed_process._start
        groups: list[_StuckGroup] = []

        def start(spec: ManagedProcessSpec):  # type: ignore[no-untyped-def]
            popen, group = real_start(spec)
            stuck = _StuckGroup()
            groups.append(stuck)
            # The real group is torn down so the test leaves no process behind.
            popen.kill()
            group.terminate()
            group.kill()
            return popen, stuck

        monkeypatch.setattr(managed_process, "_start", start)
        budget = CleanupBudget(0.5)

        result = run_managed(
            _spec("import time; time.sleep(60)", timeout=0.2, cleanup=budget)
        )

        assert result.outcome is ProcessOutcome.STOP_UNCONFIRMED
        assert result.stop_confirmed is False
        assert groups[0].terminated and groups[0].killed
        assert budget.exhausted()

    def test_clean_exit_does_not_arm_the_shared_cleanup_budget(self) -> None:
        budget = CleanupBudget(30)

        result = run_managed(_spec("print('ok')", cleanup=budget))

        assert result.ok
        assert not budget.started


class TestExecutionScope:
    """The monotonic deadline and the independent cleanup budget (#820)."""

    def _scope(self, now: list[float], **kwargs: float) -> ExecutionScope:
        values: dict[str, float] = {
            "cycle_seconds": 100,
            "cleanup_seconds": 30,
            "command_seconds": 10,
        }
        values.update(kwargs)
        return ExecutionScope(
            values["cycle_seconds"],
            values["cleanup_seconds"],
            values["command_seconds"],
            clock=lambda: now[0],
        )

    def test_the_deadline_is_one_reading_and_never_refreshed(self) -> None:
        now = [50.0]
        scope = self._scope(now)

        now[0] += 60
        assert scope.remaining() == 40
        now[0] += 60
        assert scope.expired() and scope.remaining() == 0

    def test_stage_limit_is_the_smaller_of_stage_and_remaining(self) -> None:
        now = [0.0]
        scope = self._scope(now)

        assert scope.stage_limit(30) == 30
        now[0] = 90
        assert scope.stage_limit(30) == 10

    def test_fractional_clock_does_not_inflate_remaining_budget(self) -> None:
        now = [412.007]
        scope = self._scope(now, command_seconds=1800)

        assert scope.remaining() == 100
        assert scope.stage_limit(1800) == 100
        assert scope.command_timeout() == 100
        now[0] += 25
        assert scope.remaining() == 75
        now[0] += 75
        assert scope.remaining() == 0
        assert scope.expired()

    def test_ordinary_work_is_refused_after_the_deadline(self) -> None:
        from orchestune.infra.execution_deadline import ExecutionDeadlineExceeded

        now = [0.0]
        scope = self._scope(now)
        now[0] = 200

        with pytest.raises(ExecutionDeadlineExceeded):
            scope.check("git push")

    def test_cleanup_has_one_total_budget_that_starts_on_first_use(self) -> None:
        now = [0.0]
        scope = self._scope(now)
        now[0] = 500  # long after the deadline

        assert not scope.cleanup.started
        with scope.cleanup_phase():
            assert scope.command_timeout("rollback") == 30
            now[0] += 12
            assert scope.command_timeout("record") == 18
        with scope.cleanup_phase():
            # a second cleanup phase continues the same budget; it is not renewed
            assert scope.command_timeout("again") == 18

    def test_an_exhausted_cleanup_budget_refuses_further_work(self) -> None:
        from orchestune.infra.execution_deadline import ExecutionDeadlineExceeded

        now = [0.0]
        scope = self._scope(now)
        with scope.cleanup_phase():
            now[0] += 31
            with pytest.raises(ExecutionDeadlineExceeded):
                scope.command_timeout("late")

    def test_the_command_limit_applies_before_the_deadline(self) -> None:
        now = [0.0]
        scope = self._scope(now)

        assert scope.command_timeout("git fetch") == 10
        now[0] = 95
        assert scope.command_timeout("git fetch") == 5

    @pytest.mark.parametrize(
        "name", ["cycle_seconds", "cleanup_seconds", "command_seconds"]
    )
    def test_a_zero_bound_is_rejected(self, name: str) -> None:
        values: dict[str, float] = {
            "cycle_seconds": 1,
            "cleanup_seconds": 1,
            "command_seconds": 1,
        }
        values[name] = 0

        with pytest.raises(ValueError, match=name):
            ExecutionScope(
                values["cycle_seconds"],
                values["cleanup_seconds"],
                values["command_seconds"],
            )

    def test_execution_signals_are_not_caught_by_except_exception(self) -> None:
        from orchestune.infra.execution_deadline import (
            ExecutionCommandTimeout,
            ExecutionDeadlineExceeded,
            ExecutionInterrupt,
        )
        from orchestune.integrator.execution import IntegrationExecutionAbort

        for signal in (
            ExecutionDeadlineExceeded("x"),
            ExecutionCommandTimeout("git push", 5, "normal"),
        ):
            with pytest.raises(ExecutionInterrupt):
                try:
                    raise signal
                except Exception:  # noqa: BLE001
                    pytest.fail("a best-effort handler absorbed an execution bound")
        assert issubclass(IntegrationExecutionAbort, ExecutionInterrupt)
        assert not issubclass(IntegrationExecutionAbort, Exception)
