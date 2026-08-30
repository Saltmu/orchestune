from unittest.mock import patch

from orchestune.consistency.models import RepairStatus
from orchestune.consistency.repairs.execution import COMMAND_BOOKKEEPING
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import _run_recovery_bookkeeping_boundary
from orchestune.dispatch.state import RunState, load_run_state, save_run_state
from tests.conftest import make_issue, make_pr


def _recovery_config(tmp_path, forge) -> DispatcherConfig:
    return DispatcherConfig(
        apply=True,
        max_concurrent=0,
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=forge,
    )


def _seed_resumable_task(forge) -> None:
    forge.seed_issue(
        make_issue(
            744,
            labels=("status:in-progress",),
            subtask_id="recovery-cutover",
            parent=None,
        )
    )
    forge.seed_pr(
        make_pr(
            755,
            head_ref="codex/issue-744-recovery-cutover",
            closes_issue_numbers=(744,),
        )
    )


def _bookkeeping_result(report):
    return next(
        result
        for repair_pass in report.repair_passes
        for result in repair_pass.results
        if result.command.code == COMMAND_BOOKKEEPING
    )


def test_supervisor_recovery_is_a_noop_without_in_progress_tasks(
    tmp_path, in_memory_forge
) -> None:
    report = _run_recovery_bookkeeping_boundary(
        RunState(), _recovery_config(tmp_path, in_memory_forge), now=1_000.0
    )

    assert report.repair_passes == ()
    assert [scan.boundary for scan in report.scans] == ["recovery-bookkeeping"]


def test_restart_retries_when_crash_happens_before_bookkeeping_persistence(
    tmp_path, in_memory_forge
) -> None:
    _seed_resumable_task(in_memory_forge)
    config = _recovery_config(tmp_path, in_memory_forge)
    interrupted = RunState()

    with patch(
        "orchestune.dispatch.recovery.save_run_state",
        side_effect=RuntimeError("crash before persistence"),
    ):
        failed = _run_recovery_bookkeeping_boundary(interrupted, config, now=1_000.0)

    assert _bookkeeping_result(failed).status is RepairStatus.FAILED
    assert interrupted.active_worktrees == {}
    assert not config.run_state_path.exists()

    recovered = RunState()
    retried = _run_recovery_bookkeeping_boundary(recovered, config, now=1_001.0)

    assert _bookkeeping_result(retried).status is RepairStatus.APPLIED
    assert load_run_state(config.run_state_path).active_worktrees == (
        recovered.active_worktrees
    )


def test_restart_does_not_duplicate_when_crash_happens_after_persistence(
    tmp_path, in_memory_forge
) -> None:
    _seed_resumable_task(in_memory_forge)
    config = _recovery_config(tmp_path, in_memory_forge)
    interrupted = RunState()

    def persist_then_crash(*args, **kwargs):
        save_run_state(*args, **kwargs)
        raise RuntimeError("crash after persistence")

    with patch(
        "orchestune.dispatch.recovery.save_run_state",
        side_effect=persist_then_crash,
    ):
        failed = _run_recovery_bookkeeping_boundary(interrupted, config, now=1_000.0)

    assert _bookkeeping_result(failed).status is RepairStatus.FAILED
    assert interrupted.active_worktrees == {}
    restarted = load_run_state(config.run_state_path)
    assert list(restarted.active_worktrees) == ["744"]

    resumed = _run_recovery_bookkeeping_boundary(restarted, config, now=1_001.0)

    assert resumed.repair_passes == ()
    assert list(restarted.active_worktrees) == ["744"]
