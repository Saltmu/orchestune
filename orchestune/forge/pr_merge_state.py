"""GitHub PR and branch merge status inspection mixed into :class:`GitHubForge`."""

from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote

from orchestune.validation import validate_ref_name

from .graphql_fallback import GhRunner


class GitHubPullRequestMergeStateMixin:
    """PR and branch merge state inspection mixed into :class:`GitHubForge`."""

    _run: GhRunner

    def is_branch_merged_into(self, head: str, base: str) -> bool:
        validate_ref_name(head)
        validate_ref_name(base)
        stdout = self._run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--head",
                head,
                "--base",
                base,
                "--json",
                "number",
                "--limit",
                "1",
            ]
        )
        return bool(json.loads(stdout))

    def get_merged_pr_timestamp(self, head: str, base: str) -> str | None:
        validate_ref_name(head)
        validate_ref_name(base)
        stdout = self._run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--head",
                head,
                "--base",
                base,
                "--json",
                "mergedAt",
                "--limit",
                "1000",
            ]
        )
        timestamps = [
            result.get("mergedAt")
            for result in json.loads(stdout)
            if result.get("mergedAt")
        ]
        return str(max(timestamps)) if timestamps else None

    def branch_exists(self, branch: str) -> bool:
        validate_ref_name(branch)
        encoded_branch = quote(branch, safe="")
        try:
            self._run(
                ["gh", "api", f"repos/{{owner}}/{{repo}}/branches/{encoded_branch}"]
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").lower()
            if "404" in detail or "not found" in detail:
                return False
            raise
        return True

    def is_current_branch_tip_merged_into(self, head: str, base: str) -> bool:
        return self.get_current_branch_tip_sha_if_merged_into(head, base) is not None

    def get_current_branch_tip_sha_if_merged_into(
        self, head: str, base: str
    ) -> str | None:
        """Return the observed tip only when that exact commit is in ``base``."""
        validate_ref_name(head)
        validate_ref_name(base)
        encoded_head = quote(head, safe="")
        tip_sha = self._run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/branches/{encoded_head}",
                "--jq",
                ".commit.sha",
            ]
        ).strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40}", tip_sha):
            raise ValueError(f"GitHub returned an invalid branch tip SHA: {tip_sha!r}")
        status = self._run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/compare/{tip_sha}...{base}",
                "--jq",
                ".status",
            ]
        ).strip()
        return tip_sha if status in {"ahead", "identical"} else None

    def is_merge_commit_reachable_from(self, commit_oid: str, base: str) -> bool:
        """Whether a historical PR merge commit remains in the current base tip."""
        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit_oid):
            raise ValueError(f"invalid merge commit SHA: {commit_oid!r}")
        validate_ref_name(base)
        status = self._run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/compare/{commit_oid}...{base}",
                "--jq",
                ".status",
            ]
        ).strip()
        return status in {"ahead", "identical"}
