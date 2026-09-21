"""Process-level acceptance tests for claim/dispatch ownership serialization."""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Sequence
from pathlib import Path
from queue import Empty
from typing import Any, cast
from unittest.mock import patch

import pytest

from orchestune.claim.contracts import ClaimRequest, OwnerKind
from orchestune.dispatch.state import ActiveWorktree, RunState, load_run_state
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import run_state_lock
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord
from tests.claim_helpers import MockForge

pytestmark = pytest.mark.integration
_PROCESS_TIMEOUT = 60


class _NoDependencyView:
    def assess_dependencies(self, _issue_number: int):
        from orchestune.dispatch.dependency_assessment import DependencyAssessment

        return DependencyAssessment()

    def canonical_branch(self, _issue_number: int) -> str | None:
        return None

    def task(self, _issue_number: int):
        return None


def _issue(number: int, footprint: Sequence[str]) -> IssueRecord:
    footprint_yaml = ", ".join(footprint)
    body = (
        "## Footprint\n\n"
        "```yaml\n"
        f"subtask_id: task-{number}\n"
        f"footprint: [{footprint_yaml}]\n"
        "symbols: []\n"
        "depends_on: []\n"
        "```\n"
    )
    return IssueRecord(
        number=number,
        title=f"[TEST] task {number}",
        body=body,
        labels=(StatusLabel.QUEUED,),
        created_at="2026-09-21T00:00:00Z",
        state="OPEN",
    )


def _fake_preparation(branch: str, worktree_root: Path, *_args: Any, **_kwargs: Any):
    from orchestune.dispatch.worktree import WorktreePreparation

    return WorktreePreparation(
        worktree_path=Path(worktree_root) / branch.replace("/", "-"),
        branch=branch,
        accepted=True,
        created=True,
        branch_created=True,
        base_sha="test-base-sha",
    )


def _claim_process(
    repo_root: str,
    state_path: str | None,
    issue_number: int,
    footprint: tuple[str, ...],
    owner_kind: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    """Run a claim or the dispatch launch claim boundary in an isolated process."""
    from orchestune.claim.service import claim_task

    repo = Path(repo_root)
    state = Path(state_path) if state_path is not None else None
    forge = MockForge({issue_number: _issue(issue_number, footprint)})
    ready.put(issue_number)
    if not start.wait(timeout=_PROCESS_TIMEOUT):
        results.put({"issue": issue_number, "error": "start-timeout"})
        return

    with (
        patch("orchestune.claim.service._perform_git_fetch", return_value=None),
        patch(
            "orchestune.claim.service.prepare_task_worktree",
            side_effect=_fake_preparation,
        ),
    ):
        if owner_kind == OwnerKind.DISPATCH.value:
            assert state is not None
            outcome = _run_dispatch_claim(repo, state, forge, issue_number, footprint)
        else:
            outcome = claim_task(
                ClaimRequest(
                    issue_number=issue_number,
                    owner_kind=OwnerKind.INTERACTIVE,
                    state_path=state,
                    timeout_seconds=10,
                ),
                forge=forge,
                cwd=repo,
                default_base="main",
            )

    results.put(
        {
            "issue": issue_number,
            "success": outcome.success,
            "owner_kind": owner_kind,
            "claim_id": outcome.claim_id,
            "failure": outcome.failure.reason.value if outcome.failure else None,
        }
    )


def _run_dispatch_claim(
    repo: Path,
    state: Path,
    forge: MockForge,
    issue_number: int,
    footprint: tuple[str, ...],
):
    """Exercise #943's dispatch request construction before calling claim_task."""
    from orchestune.dispatch.config import DispatcherConfig
    from orchestune.dispatch.cycle_actions import _bind_dispatch_claim_fn
    from orchestune.dispatch.launch import TaskLaunchPlan, _try_planned_launch
    from orchestune.dispatch.scoring import Task
    from orchestune.dispatch.state import load_run_state
    from orchestune.dispatch.worktree import LaunchResult

    task = Task(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        footprint=footprint,
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=(StatusLabel.QUEUED,),
        created_at="2026-09-21T00:00:00Z",
        depends_on=(),
    )
    plan = TaskLaunchPlan(
        task,
        f"claude/issue-{issue_number}-task-{issue_number}",
        "main",
        "main",
    )
    config = DispatcherConfig(
        parent_issue_number=100,
        run_state_path=state,
        worktree_root=repo / "worktrees",
        events_log_path=repo / "events.jsonl",
        forge=forge,
        apply=True,
    )
    dispatch_claim = _bind_dispatch_claim_fn(config, _NoDependencyView())
    captured_outcomes = []

    def observed_dispatch_claim(*args: Any, **kwargs: Any):
        outcome = dispatch_claim(*args, **kwargs)
        captured_outcomes.append(outcome)
        return outcome

    def launched(*_args: Any, **_kwargs: Any) -> LaunchResult:
        return LaunchResult(
            issue_number=issue_number,
            branch=plan.branch_name,
            worktree_path=str(repo / "worktrees" / plan.branch_name.replace("/", "-")),
            pid=os.getpid(),
            launched=True,
        )

    with patch("orchestune.dispatch.launch._launch_on_prepared_worktree", launched):
        os.chdir(repo)
        launch = _try_planned_launch(
            plan,
            cast(Any, object()),
            config,
            load_run_state(state),
            observed_dispatch_claim,
        )
    assert launch is not None
    assert len(captured_outcomes) == 1
    return captured_outcomes[0]


def _run_claimers(
    repo: Path,
    state: Path,
    workers: Sequence[tuple[int, tuple[str, ...], str]],
) -> list[dict[str, Any]]:
    return _run_claimers_from_repos(
        state,
        [(repo, issue, footprint, kind) for issue, footprint, kind in workers],
    )


def _run_claimers_from_repos(
    state: Path | None,
    workers: Sequence[tuple[Path, int, tuple[str, ...], str]],
) -> list[dict[str, Any]]:
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    start = ctx.Event()
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_claim_process,
            args=(
                str(repo),
                str(state) if state is not None else None,
                issue,
                footprint,
                kind,
                ready,
                start,
                results,
            ),
        )
        for repo, issue, footprint, kind in workers
    ]
    for process in processes:
        process.start()
    try:
        for _ in processes:
            ready.get(timeout=_PROCESS_TIMEOUT)
        start.set()
        observed = [results.get(timeout=_PROCESS_TIMEOUT) for _ in processes]
    except Empty as error:
        raise AssertionError("worker process did not report its result") from error
    finally:
        start.set()
        for process in processes:
            process.join(timeout=_PROCESS_TIMEOUT)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
    assert [process.exitcode for process in processes] == [0] * len(processes)
    return observed


def _save_state(path: Path, state: RunState) -> None:
    from orchestune.dispatch.state import save_run_state

    with run_state_lock(path.with_suffix(".lock"), timeout=10):
        save_run_state(state, path)


def _active(issue_number: int, **overrides: Any) -> ActiveWorktree:
    values: dict[str, Any] = {
        "issue_number": issue_number,
        "branch": f"claude/issue-{issue_number}-active",
        "worktree_path": f"worktrees/issue-{issue_number}",
        "pid": None,
        "started_at": None,
        "declared_footprint": ("shared.py",),
        "owner_kind": "dispatch",
        "claim_id": f"claim-{issue_number}",
        "claim_stage": "completed",
        "reservation_kind": "footprint",
        "forced_serial": False,
    }
    values.update(overrides)
    return ActiveWorktree(**values)


def test_claim_claim_same_issue_has_exactly_one_owner(
    claim_env: dict[str, Path],
) -> None:
    results = _run_claimers(
        claim_env["repo_root"],
        claim_env["state_path"],
        [(101, ("same.py",), "interactive"), (101, ("same.py",), "interactive")],
    )

    assert sum(item["success"] for item in results) == 1
    state = load_run_state(claim_env["state_path"])
    assert list(state.active_worktrees) == ["101"]


def test_claim_dispatch_same_issue_has_exactly_one_owner(
    claim_env: dict[str, Path],
) -> None:
    results = _run_claimers(
        claim_env["repo_root"],
        claim_env["state_path"],
        [(102, ("same.py",), "interactive"), (102, ("same.py",), "dispatch")],
    )

    assert sum(item["success"] for item in results) == 1
    state = load_run_state(claim_env["state_path"])
    assert state.active_worktrees["102"].owner_kind in {"interactive", "dispatch"}


def test_claim_dispatch_overlapping_footprints_have_exactly_one_owner(
    claim_env: dict[str, Path],
) -> None:
    results = _run_claimers(
        claim_env["repo_root"],
        claim_env["state_path"],
        [(103, ("shared.py",), "interactive"), (104, ("shared.py",), "dispatch")],
    )

    assert sum(item["success"] for item in results) == 1
    assert len(load_run_state(claim_env["state_path"]).active_worktrees) == 1


@pytest.mark.parametrize("kind", ["interactive", "dispatch"])
@pytest.mark.parametrize(
    ("active_overrides", "candidate_footprint"),
    [
        ({"reservation_kind": "repository"}, ("other.py",)),
        ({"forced_serial": True}, ("shared.py",)),
    ],
    ids=["repository-reservation", "forced-serial"],
)
def test_both_entry_paths_reject_repository_and_forced_serial_conflicts(
    claim_env: dict[str, Path],
    kind: str,
    active_overrides: dict[str, Any],
    candidate_footprint: tuple[str, ...],
) -> None:
    state_path = claim_env["state_path"]
    _save_state(
        state_path,
        RunState(active_worktrees={"90": _active(90, **active_overrides)}),
    )

    [result] = _run_claimers(
        claim_env["repo_root"],
        state_path,
        [(105, candidate_footprint, kind)],
    )

    assert result["success"] is False
    assert result["failure"] == "claim_conflict"
    assert set(load_run_state(state_path).active_worktrees) == {"90"}
    if active_overrides.get("forced_serial"):
        from orchestune.claim.ownership import (
            ClaimConflictReason,
            evaluate_claim_conflicts,
        )

        conflict = evaluate_claim_conflicts(
            _active(105, declared_footprint=candidate_footprint),
            load_run_state(state_path),
            _NoDependencyView(),
        )
        assert conflict is not None
        assert conflict.reason is ClaimConflictReason.FORCED_SERIAL


def test_primary_and_linked_worktree_share_one_ledger_without_lost_updates(
    claim_env: dict[str, Path], tmp_path: Path
) -> None:
    primary = claim_env["repo_root"]
    linked = tmp_path / "linked"
    run_git(["worktree", "add", "-b", "linked-test", str(linked), "main"], cwd=primary)

    results = _run_claimers_from_repos(
        None,
        [
            (primary, 106, ("first.py",), "interactive"),
            (linked, 107, ("second.py",), "interactive"),
        ],
    )

    assert all(result["success"] for result in results)
    assert set(load_run_state(claim_env["state_path"]).active_worktrees) == {
        "106",
        "107",
    }


def _gc_save_process(state_path: str, ready: Any, start: Any, results: Any) -> None:
    from orchestune.dispatch.state import (
        load_run_state,
        prune_run_state,
        save_run_state,
    )

    path = Path(state_path)
    ready.put("gc")
    if not start.wait(timeout=_PROCESS_TIMEOUT):
        return
    with run_state_lock(path.with_suffix(".lock"), timeout=10):
        state = load_run_state(path)
        save_run_state(prune_run_state(state, now=1_000_000), path, now=1_000_000)
    results.put("gc-saved")


def test_gc_and_claim_simultaneous_saves_preserve_other_active_issue(
    claim_env: dict[str, Path],
) -> None:
    state_path = claim_env["state_path"]
    _save_state(state_path, RunState(active_worktrees={"700": _active(700)}))
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    start = ctx.Event()
    gc_results = ctx.Queue()
    claim_results = ctx.Queue()
    gc_process = ctx.Process(
        target=_gc_save_process,
        args=(str(state_path), ready, start, gc_results),
    )
    claim_process = ctx.Process(
        target=_claim_process,
        args=(
            str(claim_env["repo_root"]),
            str(state_path),
            701,
            ("independent.py",),
            "interactive",
            ready,
            start,
            claim_results,
        ),
    )
    gc_process.start()
    claim_process.start()
    assert {ready.get(timeout=_PROCESS_TIMEOUT) for _ in range(2)} == {"gc", 701}
    start.set()
    claim_result = claim_results.get(timeout=_PROCESS_TIMEOUT)
    assert gc_results.get(timeout=_PROCESS_TIMEOUT) == "gc-saved"
    for process in (gc_process, claim_process):
        process.join(timeout=_PROCESS_TIMEOUT)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)

    assert gc_process.exitcode == claim_process.exitcode == 0
    assert claim_result["success"] is True
    assert set(load_run_state(state_path).active_worktrees) == {"700", "701"}


def test_interactive_claim_stays_active_after_owning_process_exits(
    claim_env: dict[str, Path],
) -> None:
    [result] = _run_claimers(
        claim_env["repo_root"],
        claim_env["state_path"],
        [(108, ("interactive.py",), "interactive")],
    )
    assert result["success"] is True

    from orchestune.dispatch.gc.zombies import list_unattended_interactive_claims

    state = load_run_state(claim_env["state_path"])
    assert [
        item["issue_number"] for item in list_unattended_interactive_claims(state)
    ] == [108]
    assert state.active_worktrees["108"].owner_kind == "interactive"
