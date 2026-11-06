import sys
from pathlib import Path
from types import SimpleNamespace
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
            _execute_local_ci(tmp_path, SimpleNamespace(), None, "base", "head", "tree")
    run.assert_not_called()


@pytest.mark.parametrize(
    "code, succeeds",
    [
        ("pass", True),
        ("raise SystemExit(7)", False),
        ('open("file.txt", "w").write("dirty")', False),
    ],
)
def test_runner_records_only_verified_success(git_worktree: Path, code, succeeds):
    request = SimpleNamespace(
        result="done", worktree_root=git_worktree, base_ref="main", dry_run=False
    )
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
