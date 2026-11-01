"""Local CLI target execution and worktree lifecycle for semantic fix agents."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import is_process_alive
from orchestune.targets.contracts import DispatchHandle


def build_local_semantic_fix_cmd(
    target_name: str,
    prompt: str,
    custom_local_cmd: str | None = None,
) -> list[str] | None:
    """Build the CLI execution command for a semantic fix agent.

    Built-in CLI templates (claude, codex, agy) substitute the prompt directly while
    reusing permission and format flags. Custom user local_cmd is rejected because
    its command structure cannot safely substitute the prompt.
    """
    if custom_local_cmd is not None:
        return None

    normalized = target_name.removesuffix("-cli").strip().lower()
    if normalized == "claude":
        return [
            "claude",
            "-p",
            prompt,
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
    if normalized == "codex":
        return [
            "codex",
            "exec",
            prompt,
            "--dangerously-bypass-approvals-and-sandbox",
        ]
    if normalized == "agy":
        return [
            "agy",
            "-p",
            prompt,
            "--add-dir",
            ".",
            "--print-timeout",
            "60m",
            "--dangerously-skip-permissions",
        ]
    return None


def create_semantic_fix_worktree(
    repo_root: Path,
    worktree_root: Path,
    operation_id: str,
    input_sha: str,
) -> Path:
    """Create a detached worktree for the semantic fix agent."""
    wt_path = worktree_root / f"semantic-fix-{operation_id}"
    worktree_root.mkdir(parents=True, exist_ok=True)
    run_git(
        [
            "worktree",
            "add",
            "--detach",
            str(wt_path),
            input_sha,
        ],
        cwd=repo_root,
        check=True,
    )
    return wt_path


def cleanup_semantic_fix_worktree(
    worktree_path: Path,
    repo_root: Path | None = None,
    *,
    evaluate_fn: Callable[..., Any] | None = None,
    pid: int | None = None,
) -> bool:
    """Clean up a semantic fix worktree after terminal operation state.

    Only deletes the worktree if evaluate_fn confirms it is safe
    (registered, non-primary, not dirty) and any associated agent pid is dead.
    """
    if pid is not None and is_process_alive(pid):
        return False
    if evaluate_fn is not None:
        evaluation = evaluate_fn(worktree_path=worktree_path, repo_root=repo_root)
        if not getattr(evaluation, "can_remove", False):
            return False
    res = run_git(
        ["worktree", "remove", str(worktree_path)], cwd=repo_root, check=False
    )
    return res.returncode == 0


def launch_local_semantic_fix(
    target: Any,
    operation_id: str,
    prompt: str,
    expected_head_sha: str | None = None,
    *,
    repo_root: Path | None = None,
    worktree_root: Path | None = None,
) -> DispatchHandle | None:
    """Launch a semantic fix agent on a local CLI target."""
    if getattr(target, "_local_cmd", None) is not None:
        return None

    target_name = getattr(target, "target_name", None) or ""
    cmd = build_local_semantic_fix_cmd(target_name, prompt=prompt)
    if cmd is None:
        return None

    resolved_repo_root = repo_root or Path.cwd()
    resolved_worktree_root = worktree_root or (resolved_repo_root / "worktrees")
    input_sha = expected_head_sha or "HEAD"

    wt_path = create_semantic_fix_worktree(
        repo_root=resolved_repo_root,
        worktree_root=resolved_worktree_root,
        operation_id=operation_id,
        input_sha=input_sha,
    )

    log_dir = getattr(target, "_log_dir", Path("logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    slug = f"semantic-fix-{operation_id}"
    log_path = log_dir / f"{slug}.log"
    try:
        with open(log_path, "ab") as log_fh:
            log_offset = log_fh.tell()
            process = subprocess.Popen(
                cmd,
                cwd=str(wt_path),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except Exception:
        cleanup_semantic_fix_worktree(wt_path, repo_root=resolved_repo_root)
        raise

    return DispatchHandle(
        pid=process.pid,
        branch_name=f"semantic-fix-{operation_id}",
        target_name=target.target_name,
        log_path=str(log_path),
        log_offset=log_offset,
    )
