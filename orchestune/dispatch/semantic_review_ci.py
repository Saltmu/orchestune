"""Remote-head and CI observations of a Layer-2 integration attempt (#1362).

Split from :mod:`orchestune.dispatch.postcycle_semantic_review` (#1436) to keep
that runner within the file-size limit; it re-exports both names.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from orchestune.dispatch.config import DispatcherConfig
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import default_ci_command
from orchestune.integrator.ci_execution import run_ci_stages
from orchestune.review.semantic_gate import SemanticCiConclusion, SemanticCiObservation

RemoteHeadFn = Callable[[str], str | None]


def remote_branch_sha(repository_root: str | Path, branch: str) -> str | None:
    """The remote tip of ``branch``; ``None`` only when the branch is absent."""
    result = run_git(
        ["ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
        cwd=repository_root,
        check=False,
    )
    if result.returncode == 2:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"failed to read origin/{branch}: {result.stderr}")
    return result.stdout.split()[0].lower()


def verify_semantic_ci(
    temp_branch: str,
    expected_head_sha: str,
    *,
    config: DispatcherConfig,
    repository_root: Path,
    remote_head_fn: RemoteHeadFn | None = None,
    runner: Any = None,
) -> SemanticCiObservation | None:
    """Run ``ci_command`` on a detached checkout of exactly ``expected_head_sha``.

    Integration PRs target ``parent/issue-{N}``, where GitHub CI does not run.
    The result is discarded (``None``) when the remote head is not the expected
    one before or after the run.
    """
    remote = remote_head_fn or (lambda name: remote_branch_sha(repository_root, name))
    if remote(temp_branch) != expected_head_sha:
        return None
    policy = replace(
        config.integration_execution_policy,
        integration_ci_timeout_seconds=config.semantic_ci_timeout_seconds,
    )
    command = config.ci_command or default_ci_command()
    run_git(
        ["fetch", "--no-tags", "origin", f"+refs/heads/{temp_branch}"],
        cwd=repository_root,
    )
    with tempfile.TemporaryDirectory(prefix="orchestune-semantic-ci-") as directory:
        checkout = Path(directory) / "head"
        run_git(
            ["worktree", "add", "--detach", str(checkout), expected_head_sha],
            cwd=repository_root,
        )
        try:
            result = run_ci_stages(
                command, checkout, repository_root, policy=policy, runner=runner
            )
        finally:
            run_git(
                ["worktree", "remove", "--force", str(checkout)],
                cwd=repository_root,
                check=False,
            )
    if remote(temp_branch) != expected_head_sha:
        return None
    conclusion = (
        SemanticCiConclusion.SUCCESS if result.ok else SemanticCiConclusion.FAILURE
    )
    return SemanticCiObservation(expected_head_sha, conclusion)
