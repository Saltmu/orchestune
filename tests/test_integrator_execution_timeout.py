"""#820: an Integrator cycle is bounded, stops what it started and keeps a durable budget.

Git and GitHub are doubled by ``integrator_env``; the dependency/CI runner is scripted
so each outcome (success, non-zero exit, timeout, unconfirmed stop, ...) is exact.
Real process behaviour is covered separately in ``test_managed_process*.py`` and by the
real-git test at the end of this module.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from orchestune.infra.execution_deadline import ExecutionScope
from orchestune.infra.managed_process import (
    ManagedProcessResult,
    ManagedProcessRunner,
    ManagedProcessSpec,
    ProcessOutcome,
)
from orchestune.infra.process_utils import FileLockContentionError
from orchestune.integrator import Integrator, IntegratorConfig
from orchestune.integrator.execution import ExecutionState, parent_execution_lock_path
from orchestune.integrator.git_ops import IntegrationMerger
from orchestune.integrator.timeout_policy import ExecutionFailureCause
from orchestune.integrator.timeout_retry import (
    EVENT_FINISHED,
    EVENT_RESERVED,
    EVENT_TERMINAL,
    parse_event,
)
from orchestune.worktree_ops.temp_branches import load_holds
from tests.conftest import IntegratorEnv, make_done_issue

PRE_MERGE_SHA = "c" * 40
BLOCKED = "status:blocked-human-review"


def _result(
    spec: ManagedProcessSpec, outcome: ProcessOutcome, **fields: Any
) -> ManagedProcessResult:
    defaults: dict[str, Any] = {
        "returncode": {ProcessOutcome.SUCCESS: 0, ProcessOutcome.NONZERO_EXIT: 1}.get(
            outcome
        ),
        "stop_confirmed": {
            ProcessOutcome.START_FAILED: None,
            ProcessOutcome.STOP_UNCONFIRMED: False,
        }.get(outcome, True),
        "stdout_tail": "",
        "stderr_tail": "",
        "detail": "",
    }
    defaults.update(fields)
    return ManagedProcessResult(
        outcome=outcome,
        stage=spec.stage,
        elapsed_seconds=spec.timeout_seconds,
        timeout_seconds=spec.timeout_seconds,
        **defaults,
    )


Step = ProcessOutcome | Callable[[ManagedProcessSpec], ManagedProcessResult]


class ScriptedRunner:
    """Answer each managed command from a script; later calls succeed."""

    def __init__(self, *script: Step) -> None:
        self.script = list(script)
        self.calls: list[ManagedProcessSpec] = []

    def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
        self.calls.append(spec)
        step = self.script.pop(0) if self.script else ProcessOutcome.SUCCESS
        if callable(step):
            return step(spec)
        return _result(spec, step)


def _git(env: IntegratorEnv, **overrides: Any) -> None:
    """HEAD is a fixed SHA; ``overrides`` maps a git verb to a handler."""

    def handler(args: list[str]) -> Any:
        for verb, override in overrides.items():
            if verb in args:
                return override(args)
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{PRE_MERGE_SHA}\n")
        return None

    env.stub_git(handler)


def _integrator(
    tmp_path: Path, runner: ScriptedRunner | ManagedProcessRunner, **config: Any
) -> Integrator:
    return Integrator(
        IntegratorConfig(
            parent_issue_number=100,
            apply=True,
            repository_root=tmp_path,
            integration_run_id="run-1",
            process_runner=runner,
            **config,
        )
    )


def _events(forge: MagicMock) -> list[Any]:
    return [
        parse_event(comment["body"])
        for comment in forge.event_comments
        if comment["issue_number"] == 100
    ]


def _kinds(forge: MagicMock) -> list[tuple[str, str | None]]:
    return [(e.event, e.outcome) for e in _events(forge)]


def _pushed(env: IntegratorEnv) -> bool:
    return bool(env.calls_with("push"))


def _worktree_removed(env: IntegratorEnv) -> bool:
    return bool(env.calls_with("worktree", "remove"))


@pytest.fixture
def one_task(integrator_env: IntegratorEnv) -> IntegratorEnv:
    integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
    _git(integrator_env)
    return integrator_env


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A movable cycle clock (monotonic) and wall clock (retry timestamps)."""
    state = [1000.0, 1_700_000_000.0]
    monkeypatch.setattr("orchestune.integrator.execution._monotonic", lambda: state[0])
    monkeypatch.setattr(
        "orchestune.integrator.timeout_retry._wall_clock", lambda: state[1]
    )
    return state


class TestConfirmedTimeout:
    def test_a_ci_timeout_is_its_own_status_and_never_requeues_the_worker(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        runner = ScriptedRunner(
            lambda spec: _result(
                spec,
                ProcessOutcome.TIMED_OUT,
                stdout_tail="running tests...",
                stderr_tail="Terminated",
            )
        )

        res = _integrator(tmp_path, runner).run()

        assert res["status"] == "execution_timed_out"
        assert "failed" not in res
        failure = res["execution_failures"][0]
        assert failure["cause"] == "ci_timeout"
        assert failure["stage"] == "ci"
        assert (failure["issue_number"], failure["subtask_id"]) == (1, "task-1")
        assert failure["configured_limit_seconds"] == 1800.0
        assert failure["effective_limit_seconds"] == 1800.0
        assert failure["stop_confirmed"] is True
        assert failure["rollback_confirmed"] is True
        assert failure["side_effect_state"] == "none"
        assert failure["attempt"] == 1 and failure["max_attempts"] == 3
        assert failure["next_retry_at"]
        assert "Terminated" in failure["output_tail"]
        # Not an ordinary CI failure: the worker is not re-queued or told "CI failed".
        one_task.add_label.assert_not_called()
        one_task.remove_label.assert_not_called()
        one_task.add_comment.assert_not_called()

    def test_the_merge_is_rolled_back_to_the_saved_sha_after_the_stop(
        self, one_task: IntegratorEnv, tmp_path: Path
    ) -> None:
        runner = ScriptedRunner(ProcessOutcome.TIMED_OUT)

        _integrator(tmp_path, runner).run()

        reset = one_task.calls_with("reset", "--hard")
        assert [call.args[0] for call in reset] == [
            ["git", "reset", "--hard", PRE_MERGE_SHA]
        ]
        assert one_task.call_index(one_task.calls_with("merge", "--no-ff")[0]) < (
            one_task.call_index(reset[0])
        )

    def test_nothing_unreflected_is_pushed_included_closed_or_deleted(
        self, integrator_env: IntegratorEnv, tmp_path: Path
    ) -> None:
        integrator_env.set_done_issues(
            make_done_issue(1, subtask_id="task-1"),
            make_done_issue(2, subtask_id="task-2"),
        )
        _git(integrator_env)
        # task-1 passes CI, task-2 times out: the whole cycle is abandoned.
        runner = ScriptedRunner(ProcessOutcome.SUCCESS, ProcessOutcome.TIMED_OUT)

        res = _integrator(tmp_path, runner).run()

        assert res["status"] == "execution_timed_out"
        assert res["merged"] == []
        assert not _pushed(integrator_env)
        integrator_env.close_issue.assert_not_called()
        integrator_env.add_label.assert_not_called()
        integrator_env.delete_branch.assert_not_called()
        integrator_env.create_pull_request.assert_not_called()
        integrator_env.merge_pull_request.assert_not_called()
        assert res["execution_failures"][0]["subtask_id"] == "task-2"

    def test_the_worktree_is_removed_after_a_confirmed_timeout(
        self, one_task: IntegratorEnv, tmp_path: Path
    ) -> None:
        _integrator(tmp_path, ScriptedRunner(ProcessOutcome.TIMED_OUT)).run()

        assert _worktree_removed(one_task)
        assert load_holds(tmp_path) == []

    def test_the_worktree_is_removed_only_after_the_result_is_saved(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        saved_before_removal: list[bool] = []
        real = one_task.run.side_effect

        def watch(args: list[str], **kwargs: Any) -> Any:
            if args[:3] == ["git", "worktree", "remove"]:
                saved_before_removal.append(
                    (EVENT_FINISHED, "ci_timeout") in _kinds(fake_forge)
                )
            return real(args, **kwargs)

        one_task.run.side_effect = watch

        _integrator(tmp_path, ScriptedRunner(ProcessOutcome.TIMED_OUT)).run()

        assert saved_before_removal == [True]

    def test_the_attempt_is_reserved_before_ci_and_finished_after(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        seen_before_ci: list[list[tuple[str, str | None]]] = []

        def ci(spec: ManagedProcessSpec) -> ManagedProcessResult:
            seen_before_ci.append(_kinds(fake_forge))
            return _result(spec, ProcessOutcome.TIMED_OUT)

        _integrator(tmp_path, ScriptedRunner(ci)).run()

        assert seen_before_ci == [[(EVENT_RESERVED, None)]]
        assert _kinds(fake_forge) == [
            (EVENT_RESERVED, None),
            (EVENT_FINISHED, "ci_timeout"),
        ]
        finished = _events(fake_forge)[-1]
        assert finished.stop_confirmed is True and finished.rollback_confirmed is True
        assert finished.next_retry_at is not None
        assert finished.targets[0].subtask_id == "task-1"

    def test_dry_run_starts_nothing_and_writes_nothing(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        runner = ScriptedRunner(ProcessOutcome.TIMED_OUT)

        res = Integrator(
            IntegratorConfig(
                parent_issue_number=100,
                apply=False,
                repository_root=tmp_path,
                process_runner=runner,
            )
        ).run()

        assert res["status"] == "success"
        assert runner.calls == []
        fake_forge.create_issue_comment.assert_not_called()
        one_task.add_label.assert_not_called()

    def test_dry_run_still_validates_the_policy(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="integration_ci_timeout_seconds"):
            IntegratorConfig(
                parent_issue_number=100,
                apply=False,
                repository_root=tmp_path,
                integration_ci_timeout_seconds=0,
            )


class TestNormalOutcomesAreUnchanged:
    def test_a_nonzero_ci_exit_still_requeues_and_is_not_counted(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        runner = ScriptedRunner(
            lambda spec: _result(
                spec, ProcessOutcome.NONZERO_EXIT, stderr_tail="1 failed"
            )
        )

        res = _integrator(tmp_path, runner).run()

        assert res["status"] == "failure"
        assert res["failed"] == ["task-1"]
        one_task.add_label.assert_called_with(1, "status:queued")
        assert "1 failed" in one_task.add_comment.call_args[0][1]
        assert "execution_failures" not in res
        assert _kinds(fake_forge) == [
            (EVENT_RESERVED, None),
            (EVENT_FINISHED, "failed"),
        ]

    def test_a_ci_start_failure_is_an_ordinary_failure_with_its_reason(
        self, one_task: IntegratorEnv, tmp_path: Path
    ) -> None:
        runner = ScriptedRunner(
            lambda spec: _result(
                spec, ProcessOutcome.START_FAILED, detail="FileNotFoundError: ci"
            )
        )

        res = _integrator(tmp_path, runner).run()

        assert res["status"] == "failure"
        assert "FileNotFoundError: ci" in one_task.add_comment.call_args[0][1]

    def test_success_closes_the_generation_so_old_timeouts_stop_counting(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path, clock
    ) -> None:
        _integrator(tmp_path, ScriptedRunner(ProcessOutcome.TIMED_OUT)).run()
        clock[1] += 3600
        res = _integrator(tmp_path, ScriptedRunner()).run()

        assert res["status"] == "success"
        outcomes = [e.outcome for e in _events(fake_forge) if e.event == EVENT_FINISHED]
        assert outcomes == ["ci_timeout", "success"]

    def test_dependency_preparation_is_skipped_without_a_pyproject(
        self, integrator_env: IntegratorEnv, tmp_path: Path
    ) -> None:
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        _git(integrator_env)
        # The (mocked) worktree has no pyproject.toml, so only the CI stage runs.
        runner = ScriptedRunner()

        res = _integrator(tmp_path, runner).run()

        assert res["status"] == "success"
        assert [spec.stage for spec in runner.calls] == ["ci"]


class TestBounds:
    def test_the_stage_limit_is_capped_by_the_remaining_cycle_time(
        self, one_task: IntegratorEnv, tmp_path: Path
    ) -> None:
        runner = ScriptedRunner()

        _integrator(
            tmp_path,
            runner,
            integration_cycle_timeout_seconds=100,
            integration_ci_timeout_seconds=1800,
        ).run()

        assert runner.calls[0].timeout_seconds <= 100

    def test_a_short_ci_limit_wins_over_a_long_cycle(
        self, one_task: IntegratorEnv, tmp_path: Path
    ) -> None:
        runner = ScriptedRunner()

        _integrator(
            tmp_path,
            runner,
            integration_cycle_timeout_seconds=3600,
            integration_ci_timeout_seconds=45,
        ).run()

        assert runner.calls[0].timeout_seconds == 45

    def test_the_deadline_is_not_renewed_per_task_or_stage(
        self, integrator_env: IntegratorEnv, tmp_path: Path, clock: list[float]
    ) -> None:
        integrator_env.set_done_issues(
            make_done_issue(1, subtask_id="task-1"),
            make_done_issue(2, subtask_id="task-2"),
            make_done_issue(3, subtask_id="task-3"),
        )
        _git(integrator_env)
        limits: list[float] = []

        def slow_ci(spec: ManagedProcessSpec) -> ManagedProcessResult:
            limits.append(spec.timeout_seconds)
            clock[0] += 40
            return _result(spec, ProcessOutcome.SUCCESS)

        runner = ScriptedRunner(slow_ci, slow_ci, slow_ci)

        res = _integrator(
            tmp_path,
            runner,
            integration_cycle_timeout_seconds=100,
            integration_ci_timeout_seconds=1800,
        ).run()

        # 100 s for the whole parent cycle: 100 -> 60 -> 20; task-3 gets what is left
        # and the third stage then runs out of cycle time at the push guard.
        assert limits == [100.0, 60.0, 20.0]
        assert res["status"] == "execution_timed_out"
        assert res["execution_failures"][0]["cause"] == "cycle_deadline_exceeded"

    def test_no_stage_starts_after_the_cycle_deadline(
        self, integrator_env: IntegratorEnv, tmp_path: Path, clock: list[float]
    ) -> None:
        integrator_env.set_done_issues(
            make_done_issue(1, subtask_id="task-1"),
            make_done_issue(2, subtask_id="task-2"),
        )
        _git(integrator_env)

        def consume_cycle(spec: ManagedProcessSpec) -> ManagedProcessResult:
            clock[0] += 500
            return _result(spec, ProcessOutcome.SUCCESS)

        runner = ScriptedRunner(consume_cycle)

        res = _integrator(tmp_path, runner, integration_cycle_timeout_seconds=100).run()

        assert len(runner.calls) == 1  # the second task's CI never starts
        assert res["status"] == "execution_timed_out"
        failure = res["execution_failures"][0]
        assert failure["cause"] == "cycle_deadline_exceeded"
        assert failure["rollback_confirmed"] is True
        assert not _pushed(integrator_env)

    def test_the_deadline_before_push_stops_the_cycle_without_a_write(
        self, one_task: IntegratorEnv, tmp_path: Path, clock: list[float]
    ) -> None:
        def pass_ci_then_run_out(spec: ManagedProcessSpec) -> ManagedProcessResult:
            clock[0] += 200
            return _result(spec, ProcessOutcome.SUCCESS)

        res = _integrator(
            tmp_path,
            ScriptedRunner(pass_ci_then_run_out),
            integration_cycle_timeout_seconds=100,
        ).run()

        assert res["status"] == "execution_timed_out"
        failure = res["execution_failures"][0]
        assert failure["stage"] == "PushTempBranchStep"
        assert failure["side_effect_state"] == "none"
        assert not _pushed(one_task)
        one_task.close_issue.assert_not_called()

    def test_cleanup_gets_its_own_budget_after_the_deadline(
        self, one_task: IntegratorEnv, tmp_path: Path, clock: list[float]
    ) -> None:
        def hang_past_the_deadline(spec: ManagedProcessSpec) -> ManagedProcessResult:
            clock[0] += 500
            return _result(spec, ProcessOutcome.TIMED_OUT)

        res = _integrator(
            tmp_path,
            ScriptedRunner(hang_past_the_deadline),
            integration_cycle_timeout_seconds=100,
        ).run()

        # Rollback and the result record still ran although the cycle had expired.
        assert res["status"] == "execution_timed_out"
        assert one_task.calls_with("reset", "--hard")
        assert _worktree_removed(one_task)

    def test_git_calls_are_bounded_by_the_scope(
        self, one_task: IntegratorEnv, tmp_path: Path
    ) -> None:
        _integrator(
            tmp_path, ScriptedRunner(), integration_command_timeout_seconds=7
        ).run()

        timeouts = {
            call.kwargs.get("timeout")
            for call in one_task.run.call_args_list
            if call.args[0][:2] == ["git", "merge"]
        }
        assert timeouts == {7}


class TestFailureRecordingWrites:
    """A timeout while *reporting* an ordinary failure leaves that write unknown."""

    def _ci_fails(self) -> ScriptedRunner:
        return ScriptedRunner(lambda spec: _result(spec, ProcessOutcome.NONZERO_EXIT))

    @pytest.mark.parametrize("write", ["add_label", "remove_label", "add_comment"])
    def test_a_write_timeout_while_reporting_ci_failure_is_indeterminate(
        self, one_task: IntegratorEnv, write: str, tmp_path: Path
    ) -> None:
        from orchestune.infra.execution_deadline import ExecutionCommandTimeout

        getattr(one_task, write).side_effect = ExecutionCommandTimeout(
            f"gh {write}", 60, "normal"
        )

        res = _integrator(tmp_path, self._ci_fails()).run()

        assert res["status"] == "execution_indeterminate"
        failure = res["execution_failures"][0]
        assert failure["cause"] == "side_effect_indeterminate"
        assert failure["stage"] == "record-task-failure"
        assert failure["side_effect_state"] == "unknown"
        assert (failure["issue_number"], failure["subtask_id"]) == (1, "task-1")
        assert load_holds(tmp_path)
        assert not _worktree_removed(one_task)

    def test_the_unknown_write_blocks_the_next_cycle_and_is_not_a_counted_timeout(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path, clock
    ) -> None:
        from orchestune.infra.execution_deadline import ExecutionCommandTimeout

        one_task.add_comment.side_effect = ExecutionCommandTimeout("gh", 60, "normal")
        _integrator(tmp_path, self._ci_fails()).run()
        second = ScriptedRunner()

        res = _integrator(tmp_path, second).run()

        assert second.calls == []
        assert res["status"] == "execution_indeterminate"
        outcomes = [e.outcome for e in _events(fake_forge) if e.event == EVENT_FINISHED]
        assert outcomes == ["side_effect_indeterminate"]


class TestMergeTimeout:
    """A timed-out ``git merge`` never returns its saved SHA; it must still be used."""

    def _merge_times_out(self, args: list[str]) -> Any:
        raise subprocess.TimeoutExpired(args, 60)

    def test_a_merge_timeout_resets_to_the_sha_saved_before_it(
        self, integrator_env: IntegratorEnv, tmp_path: Path
    ) -> None:
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        _git(integrator_env, merge=self._merge_times_out)

        res = _integrator(tmp_path, ScriptedRunner()).run()

        assert res["status"] == "execution_timed_out"
        resets = integrator_env.calls_with("reset", "--hard")
        assert [call.args[0] for call in resets] == [
            ["git", "reset", "--hard", PRE_MERGE_SHA]
        ]
        failure = res["execution_failures"][0]
        assert failure["stage"] == "merge"
        assert failure["rollback_confirmed"] is True
        assert "failed" not in res  # not requeued as a merge conflict

    def test_a_failed_reset_after_a_merge_timeout_holds_the_worktree(
        self, integrator_env: IntegratorEnv, tmp_path: Path
    ) -> None:
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        def reset_fails(args: list[str]) -> Any:
            raise subprocess.CalledProcessError(1, args, stderr=b"locked")

        _git(integrator_env, merge=self._merge_times_out, reset=reset_fails)

        res = _integrator(tmp_path, ScriptedRunner()).run()

        assert res["status"] == "execution_cleanup_failed"
        assert res["execution_failures"][0]["rollback_confirmed"] is False
        assert load_holds(tmp_path)
        assert not _worktree_removed(integrator_env)

    def test_a_timeout_before_any_merge_needs_no_rollback(
        self, integrator_env: IntegratorEnv, tmp_path: Path
    ) -> None:
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        def fetch_times_out(args: list[str]) -> Any:
            if "fetch" in args and any("refs/heads/claude" in a for a in args):
                raise subprocess.TimeoutExpired(args, 60)
            return None

        integrator_env.stub_git(fetch_times_out)

        res = _integrator(tmp_path, ScriptedRunner()).run()

        assert res["status"] == "execution_timed_out"
        assert not integrator_env.calls_with("reset", "--hard")
        assert res["execution_failures"][0]["rollback_confirmed"] is True


class TestUnconfirmedStopOrRollback:
    def _assert_held(
        self, env: IntegratorEnv, forge: MagicMock, tmp_path: Path, res: Any
    ) -> None:
        assert res["status"] == "execution_cleanup_failed"
        assert not _worktree_removed(env)
        holds = load_holds(tmp_path)
        assert holds and holds[0]["temp_branch"].endswith("run-1")
        assert "stop" in holds[0]["reason"] or "rollback" in holds[0]["reason"].lower()
        env.add_label.assert_any_call(100, BLOCKED)
        assert not _pushed(env)

    def test_an_unconfirmed_stop_holds_the_worktree_and_never_rolls_back(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        runner = ScriptedRunner(
            lambda spec: _result(
                spec, ProcessOutcome.STOP_UNCONFIRMED, detail="group still alive"
            )
        )

        res = _integrator(tmp_path, runner).run()

        self._assert_held(one_task, fake_forge, tmp_path, res)
        assert not one_task.calls_with("reset", "--hard")
        failure = res["execution_failures"][0]
        assert failure["cause"] == "cleanup_failed"
        assert failure["stop_confirmed"] is False
        assert failure["rollback_confirmed"] is False
        # The task is neither requeued nor marked done again.
        assert not any(
            call.args == (1, "status:queued")
            for call in one_task.add_label.call_args_list
        )

    def test_a_failed_rollback_holds_the_worktree(
        self, integrator_env: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        def reset_fails(args: list[str]) -> Any:
            raise subprocess.CalledProcessError(1, args, stderr=b"reset failed")

        _git(integrator_env, reset=reset_fails)

        res = _integrator(tmp_path, ScriptedRunner(ProcessOutcome.TIMED_OUT)).run()

        self._assert_held(integrator_env, fake_forge, tmp_path, res)
        failure = res["execution_failures"][0]
        assert failure["stop_confirmed"] is True
        assert failure["rollback_confirmed"] is False

    def test_a_head_mismatch_after_rollback_holds_the_worktree(
        self, integrator_env: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        heads = iter([PRE_MERGE_SHA, "d" * 40])

        def head(args: list[str]) -> Any:
            if args[:3] == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout=f"{next(heads, 'd' * 40)}\n"
                )
            return None

        integrator_env.stub_git(head)

        res = _integrator(tmp_path, ScriptedRunner(ProcessOutcome.TIMED_OUT)).run()

        assert res["status"] == "execution_cleanup_failed"
        assert "does not match" in res["execution_failures"][0]["detail"]
        assert load_holds(tmp_path)

    def test_the_next_cycle_starts_nothing_while_the_hold_is_unresolved(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        first = ScriptedRunner(ProcessOutcome.STOP_UNCONFIRMED)
        _integrator(tmp_path, first).run()
        second = ScriptedRunner()

        res = _integrator(tmp_path, second).run()

        assert second.calls == []
        assert res["status"] == "execution_cleanup_failed"
        assert "unconfirmed" in res["execution_failures"][0]["detail"]

    def test_a_failed_escalation_label_does_not_start_new_ci(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        one_task.add_label.side_effect = RuntimeError("label API down")
        _integrator(tmp_path, ScriptedRunner(ProcessOutcome.STOP_UNCONFIRMED)).run()
        second = ScriptedRunner()

        _integrator(tmp_path, second).run()

        assert second.calls == []


class TestRetryBudget:
    def _timeout_cycle(self, tmp_path: Path, clock: list[float], **config: Any) -> Any:
        res = _integrator(
            tmp_path, ScriptedRunner(ProcessOutcome.TIMED_OUT), **config
        ).run()
        return res

    def test_a_retry_inside_the_backoff_starts_nothing(
        self, one_task: IntegratorEnv, tmp_path: Path, clock: list[float]
    ) -> None:
        self._timeout_cycle(tmp_path, clock)
        second = ScriptedRunner()

        clock[1] += 30  # inside the 60 s back-off
        res = _integrator(tmp_path, second).run()

        assert second.calls == []
        assert res["status"] == "execution_timed_out"
        assert "backing off" in res["execution_failures"][0]["detail"]
        assert not one_task.calls_with("merge", "--no-ff")[1:]  # no second merge

    def test_the_retry_runs_once_the_backoff_has_elapsed(
        self, one_task: IntegratorEnv, tmp_path: Path, clock: list[float]
    ) -> None:
        self._timeout_cycle(tmp_path, clock)
        second = ScriptedRunner()

        clock[1] += 61
        res = _integrator(tmp_path, second).run()

        assert len(second.calls) == 1
        assert res["status"] == "success"

    def test_backoff_doubles_for_the_second_retry(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path, clock
    ) -> None:
        self._timeout_cycle(tmp_path, clock)
        first_at = _events(fake_forge)[-1].next_retry_at
        clock[1] += 61
        self._timeout_cycle(tmp_path, clock)
        second_at = _events(fake_forge)[-1].next_retry_at

        from orchestune.integrator.timeout_retry import _iso

        assert first_at == _iso(1_700_000_000.0 + 60)
        assert second_at == _iso(1_700_000_000.0 + 61 + 120)

    def test_the_third_timeout_is_terminal_and_a_fourth_never_runs(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path, clock
    ) -> None:
        results = []
        for _ in range(3):
            results.append(self._timeout_cycle(tmp_path, clock))
            clock[1] += 3600
        fourth = ScriptedRunner()

        res = _integrator(tmp_path, fourth).run()

        assert [r["status"] for r in results] == [
            "execution_timed_out",
            "execution_timed_out",
            "execution_retry_exhausted",
        ]
        assert fourth.calls == []
        assert res["status"] == "execution_retry_exhausted"
        assert (EVENT_TERMINAL, "retry_budget_exhausted") in _kinds(fake_forge)
        one_task.add_label.assert_any_call(100, BLOCKED)

    def test_a_label_failure_on_the_terminal_cycle_still_blocks_the_next_ci(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path, clock
    ) -> None:
        one_task.add_label.side_effect = RuntimeError("label API down")
        for _ in range(3):
            self._timeout_cycle(tmp_path, clock)
            clock[1] += 3600
        runner = ScriptedRunner()

        res = _integrator(tmp_path, runner).run()

        assert runner.calls == []
        assert res["status"] == "execution_retry_exhausted"

    def test_the_label_and_notice_are_retried_without_running_ci(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path, clock
    ) -> None:
        one_task.add_label.side_effect = RuntimeError("label API down")
        for _ in range(3):
            self._timeout_cycle(tmp_path, clock)
            clock[1] += 3600
        one_task.add_label.side_effect = None
        one_task.add_label.reset_mock()

        _integrator(tmp_path, ScriptedRunner()).run()

        one_task.add_label.assert_any_call(100, BLOCKED)

    def test_zero_retries_makes_the_first_timeout_terminal(
        self, one_task: IntegratorEnv, tmp_path: Path, clock: list[float]
    ) -> None:
        res = self._timeout_cycle(tmp_path, clock, max_integration_timeout_retries=0)

        assert res["status"] == "execution_retry_exhausted"
        one_task.add_label.assert_any_call(100, BLOCKED)

    def test_a_new_context_and_run_id_cannot_reset_the_count(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path, clock
    ) -> None:
        for run_id in ("a", "b", "c"):
            config = IntegratorConfig(
                parent_issue_number=100,
                apply=True,
                repository_root=tmp_path,
                integration_run_id=run_id,
                process_runner=ScriptedRunner(ProcessOutcome.TIMED_OUT),
            )
            res = Integrator(config).run()
            clock[1] += 3600

        assert res["status"] == "execution_retry_exhausted"

    def test_a_changed_policy_does_not_clear_recorded_timeouts(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path, clock
    ) -> None:
        for _ in range(2):
            self._timeout_cycle(tmp_path, clock, max_integration_timeout_retries=5)
            clock[1] += 3600
        runner = ScriptedRunner()

        res = _integrator(tmp_path, runner, max_integration_timeout_retries=1).run()

        assert runner.calls == []
        assert res["status"] == "execution_retry_exhausted"

    def test_a_different_parent_has_its_own_budget(
        self,
        integrator_env: IntegratorEnv,
        fake_forge: MagicMock,
        tmp_path: Path,
        clock,
    ) -> None:
        integrator_env.set_done_issues(
            make_done_issue(1, subtask_id="task-1"),
            make_done_issue(
                2, subtask_id="task-2", parent={"number": 200, "state": "OPEN"}
            ),
        )
        _git(integrator_env)
        for _ in range(3):
            self._timeout_cycle(tmp_path, clock)
            clock[1] += 3600
        other = ScriptedRunner()

        res = Integrator(
            IntegratorConfig(
                parent_issue_number=200,
                apply=True,
                repository_root=tmp_path,
                integration_run_id="other",
                process_runner=other,
            )
        ).run()

        assert len(other.calls) == 1
        assert res["status"] == "success"


class TestHistoryFailuresStartNothing:
    def _assert_not_started(
        self, env: IntegratorEnv, runner: ScriptedRunner, res: Any
    ) -> None:
        assert runner.calls == []
        assert not env.calls_with("merge", "--no-ff")
        assert not _pushed(env)
        assert not any(
            call.args == (1, "status:queued") for call in env.add_label.call_args_list
        )

    def test_an_unreadable_history_starts_nothing(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        fake_forge.list_all_issue_comments.side_effect = RuntimeError("API down")
        runner = ScriptedRunner()

        res = _integrator(tmp_path, runner).run()

        assert res["status"] == "execution_indeterminate"
        self._assert_not_started(one_task, runner, res)

    def test_an_unconfirmed_reservation_starts_nothing(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        fake_forge.create_issue_comment.side_effect = lambda *_: {"id": 1}  # dropped
        runner = ScriptedRunner()

        res = _integrator(tmp_path, runner).run()

        assert res["status"] == "execution_indeterminate"
        assert "not confirmed" in res["execution_failures"][0]["detail"]
        self._assert_not_started(one_task, runner, res)

    def test_a_reservation_without_a_result_is_never_rerun_automatically(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        # The first run crashed after reserving: nothing proves its processes stopped.
        reserved_only = [True]
        real = fake_forge.create_issue_comment.side_effect

        def only_reservation(issue: int, body: str) -> dict[str, Any]:
            if reserved_only and EVENT_FINISHED in body.split('"event": "')[1][:10]:
                return {"id": 0}
            created: dict[str, Any] = real(issue, body)
            return created

        fake_forge.create_issue_comment.side_effect = only_reservation
        _integrator(tmp_path, ScriptedRunner()).run()
        fake_forge.create_issue_comment.side_effect = real
        runner = ScriptedRunner()

        res = _integrator(tmp_path, runner).run()

        assert runner.calls == []
        assert res["status"] == "execution_indeterminate"
        one_task.add_label.assert_any_call(100, BLOCKED)

    def test_an_unsaved_result_is_indeterminate_and_holds_the_worktree(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        real = fake_forge.create_issue_comment.side_effect

        def drop_results(issue: int, body: str) -> dict[str, Any]:
            if '"event": "finished"' in body:
                return {"id": 0}
            created: dict[str, Any] = real(issue, body)
            return created

        fake_forge.create_issue_comment.side_effect = drop_results

        res = _integrator(tmp_path, ScriptedRunner(ProcessOutcome.TIMED_OUT)).run()

        assert res["status"] == "execution_indeterminate"
        assert res["execution_failures"][-1]["stage"] == "record-result"
        assert load_holds(tmp_path)
        # The worktree is removed only after the result is saved, so it is still there.
        assert not _worktree_removed(one_task)
        one_task.add_label.assert_any_call(100, BLOCKED)


class TestLocalHold:
    """A local hold blocks new CI for its parent until a reset opens a newer generation."""

    def _hold(self, tmp_path: Path, parent: int = 100, generation: int = 1) -> None:
        from orchestune.integrator.worktree import IntegrationWorktree

        IntegrationWorktree(tmp_path, "integration/temp-old").write_hold(
            parent_issue_number=parent,
            attempt_id=None,
            reason="push write unknown",
            generation=generation,
        )

    def test_a_local_hold_alone_blocks_new_ci(
        self, one_task: IntegratorEnv, tmp_path: Path
    ) -> None:
        self._hold(tmp_path)
        runner = ScriptedRunner()

        res = _integrator(tmp_path, runner).run()

        assert runner.calls == []
        assert res["status"] == "execution_cleanup_failed"
        assert "held integration worktree" in res["execution_failures"][0]["detail"]
        assert not one_task.calls_with("merge", "--no-ff")

    def test_another_parents_hold_does_not_block(
        self, one_task: IntegratorEnv, tmp_path: Path
    ) -> None:
        self._hold(tmp_path, parent=200)
        runner = ScriptedRunner()

        res = _integrator(tmp_path, runner).run()

        assert res["status"] == "success"
        assert len(runner.calls) == 1

    def test_unreadable_hold_records_start_nothing(
        self, one_task: IntegratorEnv, tmp_path: Path
    ) -> None:
        holds = tmp_path / "worktrees" / ".holds"
        holds.mkdir(parents=True)
        (holds / "broken.json").write_text("{nope")
        runner = ScriptedRunner()

        res = _integrator(tmp_path, runner).run()

        assert runner.calls == []
        assert res["status"] == "execution_indeterminate"

    def test_a_reset_that_names_the_hold_releases_it(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        from orchestune.integrator.timeout_retry import (
            LOCAL_HOLD_REFERENCE_PREFIX,
            reset_event,
        )

        self._hold(tmp_path)
        fake_forge.event_comments.append(
            {
                "id": 1,
                "issue_number": 100,
                "body": reset_event(
                    100,
                    next_generation=2,
                    references=f"{LOCAL_HOLD_REFERENCE_PREFIX}integration-temp-old",
                    reason="worktree and refs verified by an operator",
                    executed_at="2026-01-01T00:00:00Z",
                    attempt_id="reset-1",
                ).render(),
                "user": {"login": "bot"},
            }
        )
        runner = ScriptedRunner()

        res = _integrator(tmp_path, runner).run()

        assert res["status"] == "success"
        assert len(runner.calls) == 1


class TestWriteTimeouts:
    def test_a_push_timeout_leaves_an_unknown_write_and_stops_everything(
        self, integrator_env: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        def push_times_out(args: list[str]) -> Any:
            raise subprocess.TimeoutExpired(args, 60)

        _git(integrator_env, push=push_times_out)

        res = _integrator(tmp_path, ScriptedRunner()).run()

        assert res["status"] == "execution_indeterminate"
        failure = res["execution_failures"][0]
        assert failure["cause"] == "side_effect_indeterminate"
        assert failure["side_effect_state"] == "unknown"
        assert failure["stage"] == "PushTempBranchStep"
        assert len(integrator_env.calls_with("push")) == 1  # never retried
        integrator_env.create_pull_request.assert_not_called()
        integrator_env.merge_pull_request.assert_not_called()
        integrator_env.close_issue.assert_not_called()
        # No guess, no remote rollback, worktree held for reconciliation.
        assert not integrator_env.calls_with("push", "--delete")
        assert not _worktree_removed(integrator_env)
        assert load_holds(tmp_path)
        integrator_env.add_label.assert_any_call(100, BLOCKED)

    def test_an_unknown_write_blocks_the_next_cycle_until_a_reset(
        self, integrator_env: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        def push_times_out(args: list[str]) -> Any:
            raise subprocess.TimeoutExpired(args, 60)

        _git(integrator_env, push=push_times_out)
        _integrator(tmp_path, ScriptedRunner()).run()
        second = ScriptedRunner()

        res = _integrator(tmp_path, second).run()

        assert second.calls == []
        assert res["status"] == "execution_indeterminate"

    def test_a_timeout_in_a_best_effort_gh_call_is_not_swallowed(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        from orchestune.infra.execution_deadline import ExecutionCommandTimeout

        fake_forge.create_pull_request.side_effect = ExecutionCommandTimeout(
            "gh pr", 60, "normal"
        )

        res = _integrator(tmp_path, ScriptedRunner()).run()

        assert res["status"] == "execution_indeterminate"
        assert res["execution_failures"][0]["stage"] == "EnsureIntegrationPrStep"
        one_task.close_issue.assert_not_called()


class TestPreconditions:
    def test_an_unbounded_forge_is_refused_before_anything_starts(
        self, one_task: IntegratorEnv, fake_forge: MagicMock, tmp_path: Path
    ) -> None:
        fake_forge.supports_bounded_execution = False
        runner = ScriptedRunner()

        res = _integrator(tmp_path, runner).run()

        assert res["status"] == "failure"
        assert "bounded execution" in res["error"]
        assert runner.calls == []
        assert not one_task.calls_with("merge", "--no-ff")

    def test_a_held_parent_lock_starts_nothing(
        self,
        one_task: IntegratorEnv,
        fake_forge: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def contended(*_args: Any, **_kwargs: Any) -> Any:
            raise FileLockContentionError("Another instance is already running")

        monkeypatch.setattr("orchestune.integrator.execution.file_lock", contended)
        runner = ScriptedRunner()

        res = _integrator(tmp_path, runner).run()

        assert res["status"] == "integration_branch_locked"
        assert runner.calls == []
        fake_forge.create_issue_comment.assert_not_called()

    def test_the_parent_lock_has_no_run_id_and_differs_per_parent(
        self, tmp_path: Path
    ) -> None:
        first = parent_execution_lock_path(tmp_path, 100)

        assert first.name == "integration-parent-issue-100-execution.lock"
        assert first != parent_execution_lock_path(tmp_path, 200)
        assert first.parent == tmp_path / "worktrees" / ".locks"

    def test_the_lock_is_held_from_reservation_to_the_recorded_result(
        self,
        one_task: IntegratorEnv,
        fake_forge: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        trace: list[str] = []

        class Recording:
            def __init__(self, *_a: Any, **_k: Any) -> None: ...

            def __enter__(self) -> None:
                trace.append("acquire")

            def __exit__(self, *_exc: Any) -> None:
                trace.append("release")

        monkeypatch.setattr("orchestune.integrator.execution.file_lock", Recording)
        real = fake_forge.create_issue_comment.side_effect

        def recording(issue: int, body: str) -> dict[str, Any]:
            trace.append("event")
            created: dict[str, Any] = real(issue, body)
            return created

        fake_forge.create_issue_comment.side_effect = recording

        _integrator(tmp_path, ScriptedRunner()).run()

        assert trace == ["acquire", "event", "event", "release"]


class TestDependencyStage:
    def _merger(self, tmp_path: Path, runner: ScriptedRunner, **kwargs: Any):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        return IntegrationMerger(
            tmp_path, tmp_path, ["ci-command"], process_runner=runner, **kwargs
        )

    def test_a_dependency_timeout_never_starts_ci(self, tmp_path: Path) -> None:
        runner = ScriptedRunner(ProcessOutcome.TIMED_OUT)

        result = self._merger(tmp_path, runner).run_ci_stages()

        assert result.cause is ExecutionFailureCause.DEPENDENCY_TIMEOUT
        assert [spec.stage for spec in runner.calls] == ["dependency"]
        assert runner.calls[0].args == ["uv", "sync"]
        assert runner.calls[0].timeout_seconds == 600

    def test_each_stage_runs_at_most_once(self, tmp_path: Path) -> None:
        runner = ScriptedRunner(ProcessOutcome.SUCCESS, ProcessOutcome.NONZERO_EXIT)

        result = self._merger(tmp_path, runner).run_ci_stages()

        assert result.ok is False and result.cause is None
        assert [spec.stage for spec in runner.calls] == ["dependency", "ci"]

    def test_an_unconfirmed_dependency_stop_is_a_cleanup_failure(
        self, tmp_path: Path
    ) -> None:
        runner = ScriptedRunner(ProcessOutcome.STOP_UNCONFIRMED)

        result = self._merger(tmp_path, runner).run_ci_stages()

        assert result.cause is ExecutionFailureCause.CLEANUP_FAILED
        assert [spec.stage for spec in runner.calls] == ["dependency"]

    def test_a_cycle_shorter_than_the_stage_limit_is_a_cycle_deadline_timeout(
        self, tmp_path: Path
    ) -> None:
        scope = ExecutionScope(cycle_seconds=20, cleanup_seconds=5, command_seconds=5)
        state = MagicMock(spec=ExecutionState)
        state.scope = scope
        state.policy = MagicMock(
            integration_dependency_timeout_seconds=600,
            integration_ci_timeout_seconds=1800,
        )
        runner = ScriptedRunner(ProcessOutcome.TIMED_OUT)
        merger = self._merger(tmp_path, runner, execution=state)
        merger.policy = state.policy

        result = merger.run_ci_stages()

        assert runner.calls[0].timeout_seconds <= 20
        assert result.cause is ExecutionFailureCause.CYCLE_DEADLINE_EXCEEDED
