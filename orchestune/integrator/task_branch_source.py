from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from orchestune.branch_naming import build_task_branch_name
from orchestune.forge import Forge
from orchestune.infra.git_cli import fetch_remote_branch, resolve_commit_sha
from orchestune.models import Task
from orchestune.task_branch_resolution import (
    BranchCapability,
    CanonicalBranchState,
    TaskBranchResolution,
    TaskBranchResolver,
    probe_canonical_state,
)


class TaskBranchSource:
    """Fetches task branches and resolves canonical branch states for integration."""

    def __init__(
        self,
        repository_root: Path,
        forge: Forge,
        branch_resolver: TaskBranchResolver,
    ) -> None:
        self.repository_root = repository_root
        self.forge = forge
        self.branch_resolver = branch_resolver

    def fetch_task_branch(
        self, branch_name: str, base_branch: str
    ) -> tuple[bool, bool, str | None, str]:
        """Fetch remote task branch into remote-tracking ref.

        Returns `(success, already_merged, source_sha, error_message)`.
        """
        # actions/checkout のデフォルト（単一ブランチの浅いclone）では
        # `origin/{branch_name}` のremote-trackingブランチが存在しないため、
        # refspecを明示してfetchしないと後続のmergeが常に
        # 「not something we can merge」で失敗する（内容衝突ではない）。
        try:
            remote_ref = fetch_remote_branch(self.repository_root, branch_name)
            return True, False, resolve_commit_sha(self.repository_root, remote_ref), ""
        except (subprocess.CalledProcessError, ValueError, OSError) as e:
            # GitHub上の現在のbranch tip SHAがbaseに含まれると証明できた
            # 場合だけ統合済みとして扱う。同名branchの過去PRだけでは、
            # branch再利用後の新commitを見落とすため根拠にしない。
            # branch削除/API障害を含む不確実なケースはfail closedにする。
            base_branch_name = base_branch.removeprefix("origin/")
            try:
                source_sha = self.forge.get_current_branch_tip_sha_if_merged_into(
                    branch_name, base_branch_name
                )
            except Exception as lookup_error:
                source_sha = None
                print(
                    "Warning: Failed to verify whether "
                    f"{branch_name} was merged into {base_branch_name}: "
                    f"{lookup_error}",
                    file=sys.stderr,
                )

            if source_sha is not None:
                print(
                    f"[Integrator] Branch {branch_name} could not be "
                    "fetched, but its current remote tip is contained "
                    f"in {base_branch_name}. "
                    "Skipping integration merge."
                )
                return True, True, source_sha, ""

            fetch_error = getattr(e, "stderr", None) or str(e)
            return False, False, None, f"Failed to fetch branch: {fetch_error}"

    def fetch_branch_tip(self, branch_name: str) -> tuple[str | None, str]:
        try:
            remote_ref = fetch_remote_branch(self.repository_root, branch_name)
            return resolve_commit_sha(self.repository_root, remote_ref), ""
        except (subprocess.CalledProcessError, ValueError, OSError) as error:
            detail = getattr(error, "stderr", None) or str(error)
            return None, f"Failed to fetch branch: {detail}"

    def canonical_state(self, canonical_branch: str) -> CanonicalBranchState:
        state = probe_canonical_state(canonical_branch, self.forge.branch_exists)
        if state is CanonicalBranchState.INDETERMINATE:
            print(
                "Warning: canonical branch state is indeterminate for "
                f"{canonical_branch}",
                file=sys.stderr,
            )
        return state

    def already_merged_canonical(
        self, resolution: TaskBranchResolution, base_branch: str
    ) -> str | None:
        try:
            return self.forge.get_current_branch_tip_sha_if_merged_into(
                resolution.canonical_branch, base_branch.removeprefix("origin/")
            )
        except Exception as error:
            print(
                "Warning: failed to verify canonical branch containment for "
                f"{resolution.canonical_branch}: {error}",
                file=sys.stderr,
            )
            return None

    def resolve_and_fetch(
        self, task: Task, base_branch: str
    ) -> tuple[TaskBranchResolution | None, bool, str | None, str]:
        canonical = build_task_branch_name(task.issue_number, task.subtask_id)
        source_sha, fetch_error = self.fetch_branch_tip(canonical)
        if source_sha is not None:
            resolution = self.branch_resolver.resolve(
                task.issue_number, task.subtask_id, CanonicalBranchState.PRESENT
            )
            return resolution, False, source_sha, ""

        state = self.canonical_state(canonical)
        if state is CanonicalBranchState.INDETERMINATE:
            return (
                None,
                False,
                None,
                f"Canonical branch state indeterminate: {fetch_error}",
            )
        resolution = self.branch_resolver.resolve(
            task.issue_number, task.subtask_id, state
        )
        if state is CanonicalBranchState.PRESENT:
            merged_sha = self.already_merged_canonical(resolution, base_branch)
            if merged_sha is not None:
                return resolution, True, merged_sha, ""
            return None, False, None, fetch_error
        if not resolution.allows(BranchCapability.FETCH_MERGE):
            return None, False, None, f"No unique verified fallback PR: {fetch_error}"

        rechecked = self.canonical_state(canonical)
        if rechecked is CanonicalBranchState.PRESENT:
            return None, False, None, "Canonical branch appeared before fallback fetch"
        if rechecked is CanonicalBranchState.INDETERMINATE:
            return None, False, None, "Canonical branch recheck is indeterminate"
        fallback_sha, fallback_error = self.fetch_branch_tip(resolution.branch_name)
        if fallback_sha is None:
            return None, False, None, fallback_error
        return resolution, False, fallback_sha, ""
