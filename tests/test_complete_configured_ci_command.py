import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.complete import ci_command, ci_evidence
from orchestune.complete.ci_command import configured_ci_command
from orchestune.complete.ci_evidence import (
    CiEvidenceError,
    CiExecutionError,
    _execute_local_ci,
    resolve_evidence_path,
    run_local_ci_if_needed,
)
from orchestune.complete.contracts import CompleteRequest
from tests import test_complete_ci_evidence as ci_support

git_worktree = ci_support.git_worktree


@pytest.mark.parametrize(
    "filename, content",
    [
        ("orchestune.toml", 'ci-command = "python check.py"'),
        ("orchestune.toml", 'ci_command = ["python", "check.py"]'),
        ("pyproject.toml", '[tool.orchestune]\nci-command = "python check.py"'),
    ],
)
def test_primary_config(tmp_path, filename, content):
    primary, task = tmp_path / "primary", tmp_path / "task"
    primary.mkdir()
    task.mkdir()
    (primary / filename).write_text(content)
    with patch.object(
        ci_command,
        "resolve_checkout_roots",
        return_value=(primary, task),
    ):
        assert configured_ci_command(task) == ["python", "check.py"]


@pytest.mark.parametrize("content", ['ci-command = ""', "ci-command = 1", "[broken"])
def test_bad_config_explains_source(tmp_path, content):
    (tmp_path / "orchestune.toml").write_text(content)
    with pytest.raises(CiExecutionError, match="orchestune.toml"):
        configured_ci_command(tmp_path)


def test_missing_script_never_launches_ci(tmp_path):
    with patch.object(ci_evidence.subprocess, "run") as run:
        with pytest.raises(CiExecutionError, match="ci-command"):
            _execute_local_ci(
                tmp_path, CompleteRequest.done(1, 1), None, "base", "head", "tree"
            )
    run.assert_not_called()


@pytest.mark.parametrize(
    "code, succeeds",
    [
        ("pass", True),
        ("raise SystemExit(7)", False),
        ('open("file.txt", "w").write("dirty")', False),
        (
            'import subprocess; subprocess.run(["git", "commit", "--allow-empty", "-m", "changed"], check=True)',
            False,
        ),
    ],
)
def test_runner_records_only_verified_success(git_worktree: Path, code, succeeds):
    request = CompleteRequest.done(1, 1, worktree_root=git_worktree)
    if succeeds:
        evidence = run_local_ci_if_needed(
            request, ci_command=[sys.executable, "-c", code]
        )
        assert evidence.succeeded
        assert evidence.started_at and evidence.completed_at
    else:
        with pytest.raises(CiEvidenceError):
            run_local_ci_if_needed(request, ci_command=[sys.executable, "-c", code])
        assert not resolve_evidence_path(git_worktree).exists()


@pytest.mark.parametrize(
    "filename, prefix",
    [("orchestune.toml", ""), ("pyproject.toml", "[tool.orchestune]\n")],
)
def test_service_runs_primary_config_in_real_task_worktree(
    git_worktree: Path, tmp_path, filename, prefix
):
    import json

    from orchestune.complete.service import _run_configured_ci

    script = git_worktree / "check.py"
    script.write_text(
        "import pathlib, sys\nassert pathlib.Path.cwd() == pathlib.Path(sys.argv[1])\n"
    )
    ignore = git_worktree / ".gitignore"
    ignore.write_text(ignore.read_text() + "\norchestune.toml\n")
    subprocess.run(["git", "add", "."], cwd=git_worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "task CI"],
        cwd=git_worktree,
        check=True,
        capture_output=True,
    )
    task = tmp_path / "task"
    subprocess.run(
        ["git", "worktree", "add", "-b", "task", str(task)],
        cwd=git_worktree,
        check=True,
        capture_output=True,
    )
    (git_worktree / filename).write_text(
        prefix + "ci_command = " + json.dumps([sys.executable, "check.py", str(task)])
    )
    request = CompleteRequest.done(1, 1, worktree_root=task)
    evidence = _run_configured_ci(request)
    assert evidence.succeeded
    assert resolve_evidence_path(task).is_file()


def test_unset_and_dry_run_keep_single_argument_call(tmp_path):
    from orchestune.complete import service

    request = CompleteRequest.done(1, 1, worktree_root=tmp_path)
    with (
        patch.object(service, "configured_ci_command", return_value=None),
        patch.object(service, "run_local_ci_if_needed") as run,
    ):
        service._run_configured_ci(request)
        run.assert_called_once_with(request)
    request = CompleteRequest.done(1, 1, worktree_root=tmp_path, dry_run=True)
    with (
        patch.object(service, "configured_ci_command") as configured,
        patch.object(service, "run_local_ci_if_needed") as run,
    ):
        service._run_configured_ci(request)
        configured.assert_not_called()
        run.assert_called_once_with(request)
