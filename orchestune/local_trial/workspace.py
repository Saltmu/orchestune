"""Git worktree and storage management for local trial (#1734, #1736)."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from orchestune.infra.git_cli import GitResult, get_git_repository_paths
from orchestune.infra.git_cli import run_git as default_git_runner
from orchestune.local_trial.contracts import (
    TrialError,
    TrialWorkspace,
    canonical_digest,
)
from orchestune.local_trial.models import (
    Change,
    GitIntent,
    GitOperation,
    RepositoryIdentity,
    TrialState,
    WorkspaceIdentity,
    WorkspaceSnapshot,
)

git_runner: Callable[..., GitResult] = default_git_runner
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class LocalTrialWorkspace(TrialWorkspace):
    """Git worktree, discovery, staging, and review operations for local trials."""

    def __init__(self, *, runner: Callable[..., GitResult] | None = None) -> None:
        self._runner = runner

    def _run(
        self, cmd: list[str], cwd: str | Path | None = None, check: bool = True
    ) -> GitResult:
        active_runner = self._runner or git_runner
        try:
            res = active_runner(cmd, cwd=str(cwd) if cwd else None, check=check)
        except Exception as exc:
            raise TrialError(
                "repository_invalid",
                f"git command failed: git {' '.join(cmd)}\nerror: {exc}",
            ) from exc
        return res

    def storage_roots(self, repo: str) -> tuple[str, str]:
        try:
            toplevel, common_dir = get_git_repository_paths(repo)
        except Exception as exc:
            raise TrialError(
                "repository_invalid", f"Not a valid git repository: {repo}"
            ) from exc
        worktree_res = self._run(
            ["worktree", "list", "--porcelain", "-z"], cwd=toplevel
        )
        entries = worktree_res.stdout.split("\0")
        primary_str = str(toplevel.resolve())
        for entry in entries:
            if entry.startswith("worktree "):
                primary_str = str(Path(entry.removeprefix("worktree ")).resolve())
                break
        return primary_str, str(common_dir.resolve())

    def discover(self, repo: str) -> RepositoryIdentity:
        primary_root, common_dir = self.storage_roots(repo)
        try:
            source_root_path, _ = get_git_repository_paths(repo)
        except Exception as exc:
            raise TrialError(
                "repository_invalid", f"Failed to get source paths: {repo}"
            ) from exc
        source_root = str(source_root_path.resolve())

        bare_res = self._run(["rev-parse", "--is-bare-repository"], cwd=source_root)
        if bare_res.stdout.strip() == "true":
            raise TrialError("repository_invalid", "Bare repository is not supported")

        branch_res = self._run(
            ["symbolic-ref", "-q", "HEAD"], cwd=source_root, check=False
        )
        if branch_res.returncode != 0 or not branch_res.stdout.strip():
            raise TrialError(
                "repository_invalid", "Source HEAD is detached or invalid branch"
            )
        base_ref = branch_res.stdout.strip()

        sha_res = self._run(
            ["rev-parse", "--verify", "HEAD^{commit}"], cwd=source_root, check=False
        )
        if sha_res.returncode != 0 or not sha_res.stdout.strip():
            raise TrialError(
                "repository_invalid", "Source repository has no initial commit"
            )
        base_sha = sha_res.stdout.strip().lower()

        self._check_clean_repo(source_root, "Source")
        if primary_root != source_root:
            self._check_clean_repo(primary_root, "Primary")

        return RepositoryIdentity(
            source_root=source_root,
            primary_root=primary_root,
            common_dir=common_dir,
            base_ref=base_ref,
            base_sha=base_sha,
        )

    def _check_clean_repo(self, repo_dir: str, label: str) -> None:
        status_res = self._run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=repo_dir,
        )
        if status_res.stdout:
            raise TrialError(
                "source_dirty", f"{label} repository at {repo_dir} is dirty"
            )

    def paths(self, repo: RepositoryIdentity, trial_id: str) -> WorkspaceIdentity:
        if not isinstance(trial_id, str) or not _ID_RE.fullmatch(trial_id):
            raise TrialError(
                "invalid_input", f"trial_id must be 32 hex digits, got {trial_id!r}"
            )
        primary = Path(repo.primary_root).resolve()
        worktrees_dir = (primary / ".orchestune" / "worktrees").resolve()
        impl_path = (worktrees_dir / f"trial-{trial_id}").resolve()
        review_path = (worktrees_dir / f"trial-review-{trial_id}").resolve()
        try:
            impl_path.relative_to(primary)
            review_path.relative_to(primary)
        except ValueError:
            raise TrialError(
                "invalid_input", "Workspace path escapes primary repository root"
            ) from None
        return WorkspaceIdentity(
            implementation_path=str(impl_path),
            review_path=str(review_path),
            branch=f"trial/{trial_id}",
        )

    def ensure_storage_ignored(self, repo: RepositoryIdentity) -> None:
        exclude_file = Path(repo.common_dir) / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
        )
        lines = [line.strip() for line in existing.splitlines()]
        if "/.orchestune/" not in lines and "/.orchestune" not in lines:
            new_content = existing
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            new_content += "/.orchestune/\n"
            try:
                with tempfile.NamedTemporaryFile(
                    dir=exclude_file.parent,
                    mode="w",
                    encoding="utf-8",
                    delete=False,
                ) as tmp:
                    tmp.write(new_content)
                    tmp_name = tmp.name
                os.replace(tmp_name, exclude_file)
            except Exception as exc:
                raise TrialError(
                    "exclude_unwritable", f"Failed to write exclude file: {exc}"
                ) from exc

        check_s = self._run(
            ["check-ignore", "-q", "--", ".orchestune/trials/probe"],
            cwd=repo.source_root,
            check=False,
        )
        if check_s.returncode != 0:
            raise TrialError(
                "exclude_unwritable",
                "Source repository failed check-ignore for .orchestune",
            )
        if repo.primary_root != repo.source_root:
            check_p = self._run(
                ["check-ignore", "-q", "--", ".orchestune/trials/probe"],
                cwd=repo.primary_root,
                check=False,
            )
            if check_p.returncode != 0:
                raise TrialError(
                    "exclude_unwritable",
                    "Primary repository failed check-ignore for .orchestune",
                )

    def prepare(self, repo: RepositoryIdentity, workspace: WorkspaceIdentity) -> None:
        impl_path = Path(workspace.implementation_path)
        if not impl_path.exists():
            self._create_worktree(impl_path, repo, workspace)
        else:
            self._verify_existing_worktree(impl_path, repo, workspace)

    def _create_worktree(
        self,
        impl_path: Path,
        repo: RepositoryIdentity,
        workspace: WorkspaceIdentity,
    ) -> None:
        branch_check = self._run(
            ["rev-parse", "--verify", f"refs/heads/{workspace.branch}"],
            cwd=repo.primary_root,
            check=False,
        )
        impl_path.parent.mkdir(parents=True, exist_ok=True)
        if branch_check.returncode == 0:
            cur_tip = branch_check.stdout.strip().lower()
            if cur_tip != repo.base_sha.lower():
                raise TrialError(
                    "identity_mismatch",
                    f"Branch {workspace.branch} points to {cur_tip}",
                )
            self._run(
                ["worktree", "add", str(impl_path), workspace.branch],
                cwd=repo.primary_root,
            )
        else:
            self._run(
                [
                    "worktree",
                    "add",
                    "-b",
                    workspace.branch,
                    str(impl_path),
                    repo.base_sha,
                ],
                cwd=repo.primary_root,
            )

    def _verify_existing_worktree(
        self,
        impl_path: Path,
        repo: RepositoryIdentity,
        workspace: WorkspaceIdentity,
    ) -> None:
        try:
            toplevel, common_dir = get_git_repository_paths(impl_path)
        except Exception as exc:
            raise TrialError(
                "identity_mismatch", f"Existing path is not a git worktree: {impl_path}"
            ) from exc
        if (
            toplevel.resolve() != impl_path.resolve()
            or common_dir.resolve() != Path(repo.common_dir).resolve()
        ):
            raise TrialError(
                "identity_mismatch", "Existing worktree toplevel/common_dir mismatch"
            )
        branch_res = self._run(
            ["symbolic-ref", "-q", "HEAD"], cwd=str(impl_path), check=False
        )
        if (
            branch_res.returncode != 0
            or branch_res.stdout.strip() != f"refs/heads/{workspace.branch}"
        ):
            raise TrialError(
                "identity_mismatch",
                f"Existing worktree not on branch {workspace.branch}",
            )
        st = self._run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=str(impl_path),
        )
        if st.stdout:
            raise TrialError(
                "worktree_dirty", f"Existing worktree {impl_path} is dirty"
            )

    def inspect(
        self, repo: RepositoryIdentity, workspace: WorkspaceIdentity
    ) -> WorkspaceSnapshot:
        return self._inspect_directory(
            workspace.implementation_path, repo.base_ref, repo.base_sha
        )

    def inspect_review(self, state: TrialState) -> WorkspaceSnapshot:
        return self._inspect_directory(
            state.workspace.review_path,
            state.repository.base_ref,
            state.repository.base_sha,
        )

    def _inspect_directory(
        self, dir_path: str, base_ref: str, fallback_base_sha: str
    ) -> WorkspaceSnapshot:
        head_res = self._run(["rev-parse", "HEAD"], cwd=dir_path)
        head_sha = head_res.stdout.strip().lower()

        base_tip_res = self._run(["rev-parse", base_ref], cwd=dir_path, check=False)
        base_tip_sha = (
            base_tip_res.stdout.strip().lower()
            if base_tip_res.returncode == 0
            else fallback_base_sha.lower()
        )

        branch_res = self._run(
            ["symbolic-ref", "-q", "HEAD"], cwd=dir_path, check=False
        )
        branch = (
            branch_res.stdout.strip().removeprefix("refs/heads/")
            if branch_res.returncode == 0
            else None
        )

        ls_res = self._run(["ls-files", "--stage", "-z"], cwd=dir_path)
        index_digest = hashlib.sha256(ls_res.stdout.encode("utf-8")).hexdigest()

        status_res = self._run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=dir_path,
        )
        changes, untracked_paths = self._parse_porcelain_changes(status_res.stdout)
        content_digest = self._compute_content_digest(dir_path, untracked_paths)

        return WorkspaceSnapshot(
            head_sha=head_sha,
            base_tip_sha=base_tip_sha,
            index_digest=index_digest,
            content_digest=content_digest,
            branch=branch,
            changes=tuple(changes),
        )

    def _parse_porcelain_changes(
        self, raw_status: str
    ) -> tuple[list[Change], list[str]]:
        changes: list[Change] = []
        untracked: list[str] = []
        tokens = raw_status.split("\0")
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if not token:
                i += 1
                continue
            status_code = token[:2]
            path = token[3:]
            old_path: str | None = None
            if ("R" in status_code or "C" in status_code) and i + 1 < len(tokens):
                old_path = tokens[i + 1]
                i += 1
            norm_status = status_code.strip()
            if norm_status == "??":
                untracked.append(path)
            changes.append(Change(status=norm_status, path=path, old_path=old_path))
            i += 1
        return changes, untracked

    def _compute_content_digest(self, dir_path: str, untracked_paths: list[str]) -> str:
        diff_res = self._run(
            ["diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
            cwd=dir_path,
            check=False,
        )
        tracked_diff_hash = hashlib.sha256(diff_res.stdout.encode("utf-8")).hexdigest()
        untracked_records: list[dict[str, str]] = []
        for p in sorted(untracked_paths):
            target = Path(dir_path) / p
            if target.is_symlink():
                untracked_records.append(
                    {"path": p, "kind": "symlink", "target": os.readlink(target)}
                )
            elif target.is_file():
                h = hashlib.sha256(target.read_bytes()).hexdigest()
                untracked_records.append({"path": p, "kind": "file", "digest": h})
        return canonical_digest(
            {"tracked_diff": tracked_diff_hash, "untracked": untracked_records}
        )

    def stage_commit(self, state: TrialState) -> GitIntent:
        impl = state.workspace.implementation_path
        if not state.plan or not state.plan.write_paths:
            raise TrialError("out_of_scope", "Plan write_paths must be configured")
        allowed_paths = self._validate_write_paths(state.plan.write_paths)

        cur_head = self._run(["rev-parse", "HEAD"], cwd=impl).stdout.strip().lower()
        if cur_head != state.head_sha.lower():
            raise TrialError(
                "head_changed",
                f"HEAD changed: expected {state.head_sha}, got {cur_head}",
            )

        self._check_index_staged(impl, state)
        snap = self.inspect(state.repository, state.workspace)
        if not snap.changes:
            raise TrialError("no_changes", "No changes to commit")
        target_paths = self._collect_commit_targets(snap, allowed_paths)
        self._verify_git_user(impl)

        self._run(["add", "--"] + sorted(target_paths), cwd=impl)
        tree_sha = self._run(["write-tree"], cwd=impl).stdout.strip().lower()

        return GitIntent(
            operation=GitOperation.COMMIT,
            pre_head_sha=state.head_sha.lower(),
            expected_tree_sha=tree_sha,
            target_sha=tree_sha,
            allowed_paths=tuple(sorted(target_paths)),
        )

    def _check_index_staged(self, impl: str, state: TrialState) -> None:
        ls_res = self._run(["ls-files", "--stage", "-z"], cwd=impl)
        current_index_digest = hashlib.sha256(ls_res.stdout.encode("utf-8")).hexdigest()
        diff_cached = self._run(
            ["diff-index", "--cached", "--name-status", "HEAD", "--"], cwd=impl
        ).stdout.strip()
        if diff_cached:
            if not (
                state.approved_index_digest
                and state.approved_index_digest == current_index_digest
                and state.approved_worktree_digest
            ):
                raise TrialError("worktree_dirty", "Unapproved staged changes in index")

    def _collect_commit_targets(
        self, snap: WorkspaceSnapshot, allowed_paths: set[str]
    ) -> set[str]:
        target_paths: set[str] = set()
        for c in snap.changes:
            if c.old_path:
                if c.old_path not in allowed_paths:
                    raise TrialError(
                        "out_of_scope",
                        f"Rename source {c.old_path} not in approved write_paths",
                    )
                target_paths.add(c.old_path)
            if c.path not in allowed_paths:
                raise TrialError(
                    "out_of_scope", f"Path {c.path} not in approved write_paths"
                )
            target_paths.add(c.path)
        return target_paths

    def _verify_git_user(self, impl: str) -> None:
        name = self._run(["config", "user.name"], cwd=impl, check=False).stdout.strip()
        email = self._run(
            ["config", "user.email"], cwd=impl, check=False
        ).stdout.strip()
        if not name or not email:
            raise TrialError(
                "commit_identity_missing",
                "git user.name and user.email must be configured",
            )

    def _validate_write_paths(self, write_paths: tuple[str, ...]) -> set[str]:
        cleaned: set[str] = set()
        for p in write_paths:
            if not isinstance(p, str) or not p.strip():
                raise TrialError("out_of_scope", "Write path must be nonempty")
            if (
                p.startswith(("/", "\\"))
                or ".." in p
                or "*" in p
                or "?" in p
                or "[" in p
            ):
                raise TrialError("out_of_scope", f"Write path invalid or glob: {p}")
            if p == ".git" or p.startswith(
                (".git/", ".git\\", ".orchestune/", ".orchestune\\")
            ):
                raise TrialError(
                    "out_of_scope", f"Write path touches reserved directory: {p}"
                )
            cleaned.add(p)
        return cleaned

    def finish_commit(self, state: TrialState, intent: GitIntent) -> str:
        impl = state.workspace.implementation_path
        cur_head = self._run(["rev-parse", "HEAD"], cwd=impl).stdout.strip().lower()
        if cur_head != intent.pre_head_sha.lower():
            raise TrialError(
                "head_changed",
                f"HEAD changed: expected {intent.pre_head_sha}, got {cur_head}",
            )
        cur_tree = self._run(["write-tree"], cwd=impl).stdout.strip().lower()
        if intent.expected_tree_sha and cur_tree != intent.expected_tree_sha.lower():
            raise TrialError(
                "head_changed",
                f"Index tree {cur_tree} != expected {intent.expected_tree_sha}",
            )

        goal = state.plan.goal if state.plan and state.plan.goal else "implement"
        commit_msg = f"trial({state.trial_id[:8]}): {goal}\n\nOrchestune-Trial: {state.trial_id}\n"
        self._run(["commit", "-m", commit_msg], cwd=impl)
        return self._run(["rev-parse", "HEAD"], cwd=impl).stdout.strip().lower()

    def reconcile_commit(self, state: TrialState, intent: GitIntent) -> str | None:
        impl = state.workspace.implementation_path
        cur_head = self._run(["rev-parse", "HEAD"], cwd=impl).stdout.strip().lower()
        if cur_head == intent.pre_head_sha.lower():
            return None

        parent = (
            self._run(["rev-parse", "HEAD^1"], cwd=impl, check=False)
            .stdout.strip()
            .lower()
        )
        if parent != intent.pre_head_sha.lower():
            raise TrialError(
                "head_changed",
                f"Commit parent {parent} != pre_head {intent.pre_head_sha}",
            )

        commit_tree = (
            self._run(["rev-parse", "HEAD^{tree}"], cwd=impl).stdout.strip().lower()
        )
        if intent.expected_tree_sha and commit_tree != intent.expected_tree_sha.lower():
            raise TrialError(
                "head_changed",
                f"Commit tree {commit_tree} != expected {intent.expected_tree_sha}",
            )

        msg = self._run(["log", "-1", "--format=%B"], cwd=impl).stdout
        if f"Orchestune-Trial: {state.trial_id}" not in msg:
            raise TrialError(
                "head_changed", "Commit message missing Orchestune-Trial trailer"
            )

        diff_tree = self._run(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], cwd=impl
        ).stdout.splitlines()
        allowed = set(intent.allowed_paths)
        for p in diff_tree:
            if p and p not in allowed:
                raise TrialError(
                    "head_changed", f"Commit diff path {p} not in allowed_paths"
                )

        st = self._run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=impl
        ).stdout
        if st:
            raise TrialError("head_changed", "Worktree is dirty after commit")
        return cur_head

    def prepare_review(self, state: TrialState) -> None:
        review_path = Path(state.workspace.review_path)
        if not review_path.exists():
            review_path.parent.mkdir(parents=True, exist_ok=True)
            self._run(
                ["worktree", "add", "--detach", str(review_path), state.head_sha],
                cwd=state.repository.primary_root,
            )
            return

        try:
            toplevel, common_dir = get_git_repository_paths(review_path)
        except Exception as exc:
            raise TrialError(
                "identity_mismatch", "Review path is not a git worktree"
            ) from exc
        if (
            toplevel.resolve() != review_path.resolve()
            or common_dir.resolve() != Path(state.repository.common_dir).resolve()
        ):
            raise TrialError("identity_mismatch", "Review worktree identity mismatch")

        st = self._run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=str(review_path),
        ).stdout
        if st:
            raise TrialError("worktree_dirty", "Review worktree is dirty")
        self._run(["switch", "--detach", state.head_sha], cwd=str(review_path))

    def diff(self, state: TrialState) -> str:
        res = self._run(
            [
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                state.repository.base_sha,
                state.head_sha,
                "--",
            ],
            cwd=state.workspace.implementation_path,
        )
        return res.stdout

    def working_diff(self, state: TrialState) -> str:
        impl = state.workspace.implementation_path
        diff_res = self._run(
            ["diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
            cwd=impl,
            check=False,
        )
        status_res = self._run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=impl,
        )
        _, untracked = self._parse_porcelain_changes(status_res.stdout)
        lines = [diff_res.stdout]
        if untracked:
            lines.append("\n--- Untracked files ---")
            for p in sorted(untracked):
                full = Path(impl) / p
                size = full.stat().st_size if full.exists() else 0
                lines.append(f"Untracked: {p} ({size} bytes)")
        return "\n".join(lines)

    def includes_head(self, state: TrialState) -> bool:
        res = self._run(
            ["merge-base", "--is-ancestor", state.head_sha, state.repository.base_ref],
            cwd=state.repository.primary_root,
            check=False,
        )
        if res.returncode == 0:
            return True
        if res.returncode == 1:
            return False
        raise TrialError(
            "repository_invalid",
            f"merge-base check failed with returncode {res.returncode}",
        )
