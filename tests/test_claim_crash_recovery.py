"""Hard-process crash and idempotent claim recovery acceptance tests."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
from pathlib import Path
from queue import Empty
from typing import Any, cast
from unittest.mock import patch

import pytest

from orchestune.dispatch.state import load_run_state
from orchestune.infra.git_cli import run_git
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord
from tests.claim_helpers import MockForge

pytestmark = pytest.mark.integration

_CRASH_EXIT = 97
_OWNER_TOKEN = "test-owner-token-for-crash-resume"
_PROCESS_TIMEOUT = 60


def _issue(number: int) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=f"[TEST] crash task {number}",
        body=(
            "## Footprint\n\n"
            "```yaml\n"
            f"subtask_id: task-{number}\n"
            f"footprint: [crash-{number}.py]\n"
            "symbols: []\n"
            "depends_on: []\n"
            "```\n"
        ),
        labels=(StatusLabel.QUEUED,),
        created_at="2026-09-21T00:00:00Z",
        state="OPEN",
    )


def _crash_claim_process(
    repo_root: str,
    state_path: str,
    issue_number: int,
    boundary: str,
) -> None:
    """Exit without unwinding immediately after the selected durable boundary."""
    from orchestune.claim import service
    from orchestune.claim.contracts import ClaimRequest, ClaimStage
    from orchestune.claim.service import claim_task

    repo = Path(repo_root)
    state = Path(state_path)
    forge = MockForge({issue_number: _issue(issue_number)})
    original_save = service.save_run_state
    original_label = service._apply_status_label
    crashed = False

    def save_then_crash(run_state: Any, path: Any, **kwargs: Any) -> None:
        nonlocal crashed
        original_save(run_state, path, **kwargs)
        active = run_state.active_worktrees.get(str(issue_number))
        expected = {
            "reservation": ClaimStage.RESERVED.value,
            "worktree": ClaimStage.ACTIVE_SAVED.value,
        }.get(boundary)
        if not crashed and active is not None and active.claim_stage == expected:
            crashed = True
            os._exit(_CRASH_EXIT)

    def label_then_crash(*args: Any, **kwargs: Any):
        failure = original_label(*args, **kwargs)
        if failure is None:
            os._exit(_CRASH_EXIT)
        return failure

    label_effect = label_then_crash if boundary == "label" else original_label
    with (
        patch("orchestune.claim.service._perform_git_fetch", return_value=None),
        patch("orchestune.claim.service.save_run_state", side_effect=save_then_crash),
        patch("orchestune.claim.service._apply_status_label", side_effect=label_effect),
    ):
        claim_task(
            ClaimRequest(
                issue_number=issue_number,
                owner_token=_OWNER_TOKEN,
                state_path=state,
                worktree_root=repo / "worktrees",
                timeout_seconds=10,
            ),
            forge=forge,
            cwd=repo,
            default_base="main",
        )
    os._exit(98)


def _resume_process(
    repo_root: str,
    state_path: str,
    issue_number: int,
    claim_id: str,
    results: Any,
) -> None:
    from orchestune.claim.service import resume_claim

    repo = Path(repo_root)
    forge = MockForge({issue_number: _issue(issue_number)})
    with patch("orchestune.claim.service._perform_git_fetch", return_value=None):
        outcome = resume_claim(
            claim_id,
            _OWNER_TOKEN,
            forge=forge,
            cwd=repo,
            state_path=Path(state_path),
            default_base="main",
            timeout_seconds=10,
        )
    results.put(
        {
            "success": outcome.success,
            "claim_id": outcome.claim_id,
            "stage": outcome.stage.value if outcome.stage else None,
            "failure": outcome.failure.reason.value if outcome.failure else None,
        }
    )


def _normal_claim_process(
    repo_root: str,
    state_path: str,
    issue_number: int,
    results: Any,
) -> None:
    from orchestune.claim.contracts import ClaimRequest
    from orchestune.claim.service import claim_task

    repo = Path(repo_root)
    forge = MockForge({issue_number: _issue(issue_number)})
    with patch("orchestune.claim.service._perform_git_fetch", return_value=None):
        outcome = claim_task(
            ClaimRequest(
                issue_number=issue_number,
                owner_token=_OWNER_TOKEN,
                state_path=Path(state_path),
                worktree_root=repo / "worktrees",
                timeout_seconds=10,
            ),
            forge=forge,
            cwd=repo,
            default_base="main",
        )
    results.put(
        {
            "success": outcome.success,
            "failure": outcome.failure.reason.value if outcome.failure else None,
        }
    )


def _spawn_and_join(
    target: Any, args: tuple[Any, ...], timeout: int = _PROCESS_TIMEOUT
) -> Any:
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=target, args=args)
    process.start()
    process.join(timeout=timeout)
    if process.is_alive():
        process.terminate()
        process.join(timeout=10)
        raise AssertionError("child process timed out")
    return process


def _one_result(results: Any) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], results.get(timeout=_PROCESS_TIMEOUT))
    except Empty as error:
        raise AssertionError("child process did not publish a result") from error


def _worktree_paths(repo_root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        Path(line.removeprefix("worktree "))
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    ]


@pytest.mark.parametrize("boundary", ["reservation", "worktree", "label"])
def test_resume_after_hard_crash_does_not_duplicate_ownership_or_worktree(
    claim_env: dict[str, Path], boundary: str
) -> None:
    repo = claim_env["repo_root"]
    state_path = claim_env["state_path"]
    issue_number = {"reservation": 201, "worktree": 202, "label": 203}[boundary]
    crashed = _spawn_and_join(
        _crash_claim_process,
        (str(repo), str(state_path), issue_number, boundary),
    )
    assert crashed.exitcode == _CRASH_EXIT

    interrupted = load_run_state(state_path)
    assert list(interrupted.active_worktrees) == [str(issue_number)]
    active = interrupted.active_worktrees[str(issue_number)]
    assert active.claim_id is not None
    original_claim_id = active.claim_id

    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    resumed = _spawn_and_join(
        _resume_process,
        (str(repo), str(state_path), issue_number, original_claim_id, results),
    )
    assert resumed.exitcode == 0
    result = _one_result(results)
    assert result == {
        "success": True,
        "claim_id": original_claim_id,
        "stage": "completed",
        "failure": None,
    }

    recovered = load_run_state(state_path)
    assert list(recovered.active_worktrees) == [str(issue_number)]
    final = recovered.active_worktrees[str(issue_number)]
    assert final.claim_id == original_claim_id
    assert final.claim_stage == "completed"
    assert recovered.launch_history == []
    paths = _worktree_paths(repo)
    assert len(paths) == 2
    assert paths.count(Path(final.worktree_path)) == 1


@pytest.mark.parametrize("dirty", [False, True], ids=["clean", "dirty"])
def test_existing_unowned_worktree_is_not_deleted(
    claim_env: dict[str, Path], dirty: bool
) -> None:
    from orchestune.branch_naming import build_task_branch_name

    repo = claim_env["repo_root"]
    state_path = claim_env["state_path"]
    issue_number = 210 if dirty else 211
    branch = build_task_branch_name(issue_number, f"task-{issue_number}")
    worktree = repo / "worktrees" / branch.replace("/", "-")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run_git(["worktree", "add", "-b", branch, str(worktree), "main"], cwd=repo)
    sentinel = worktree / "sentinel.txt"
    sentinel.write_text("must survive\n", encoding="utf-8")
    if not dirty:
        run_git(["add", "sentinel.txt"], cwd=worktree)
        run_git(["commit", "-m", "owned outside claim"], cwd=worktree)

    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    process = _spawn_and_join(
        _normal_claim_process,
        (str(repo), str(state_path), issue_number, results),
    )
    assert process.exitcode == 0
    result = _one_result(results)

    assert result == {"success": False, "failure": "worktree_creation_failed"}
    assert worktree.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "must survive\n"
    assert worktree in _worktree_paths(repo)
