"""GitHub pull-request operations used by the composed Forge facade."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any, Protocol
from urllib.parse import quote

from orchestune.models import PrRecord
from orchestune.validation import validate_issue_number, validate_ref_name

_PR_FILES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      files(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { path }
      }
    }
  }
}
"""


class _Runner(Protocol):
    def __call__(self, args: list[str], input_text: str | None = None) -> str: ...


class GitHubPullRequestMixin:
    """Pull-request-specific implementation mixed into :class:`GitHubForge`."""

    _run: _Runner

    def delete_branch(self, branch: str) -> None:
        validate_ref_name(branch)
        encoded_branch = quote(branch, safe="")
        try:
            self._run(
                [
                    "gh",
                    "api",
                    "--method",
                    "DELETE",
                    f"repos/{{owner}}/{{repo}}/git/refs/heads/{encoded_branch}",
                ]
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").lower()
            if "404" in detail or "not found" in detail:
                return
            raise

    def merge_pull_request(self, pr_number: int | str) -> None:
        number = validate_issue_number(pr_number)
        self._run(["gh", "pr", "merge", str(number), "--merge"])

    def create_pull_request(self, head: str, base: str, title: str, body: str) -> int:
        validate_ref_name(head)
        validate_ref_name(base)
        stdout = self._run(
            [
                "gh",
                "pr",
                "create",
                "--head",
                head,
                "--base",
                base,
                "--title",
                title,
                "--body-file",
                "-",
            ],
            input_text=body,
        )
        url = stdout.strip().splitlines()[-1]
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def update_pull_request(self, pr_number: int | str, title: str, body: str) -> None:
        number = validate_issue_number(pr_number)
        self._run(
            [
                "gh",
                "pr",
                "edit",
                str(number),
                "--title",
                title,
                "--body-file",
                "-",
            ],
            input_text=body,
        )

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
        return status in {"ahead", "identical"}

    def _fetch_all_pr_files(self, pr_number: int) -> tuple[tuple[str, ...], bool]:
        paths: list[str] = []
        after: str | None = None
        try:
            while True:
                args = [
                    "gh",
                    "api",
                    "graphql",
                    "-F",
                    "owner={owner}",
                    "-F",
                    "name={repo}",
                    "-F",
                    f"number={pr_number}",
                    "-f",
                    f"query={_PR_FILES_QUERY}",
                ]
                if after is not None:
                    args.extend(["-F", f"after={after}"])
                stdout = self._run(args)
                data = json.loads(stdout)["data"]["repository"]["pullRequest"]["files"]
                for node in data.get("nodes", []):
                    paths.append(node["path"])
                page_info = data.get("pageInfo", {})
                if not page_info.get("hasNextPage"):
                    break
                after = page_info.get("endCursor")
        except Exception as exc:
            print(
                f"Warning: failed to paginate changed files for PR #{pr_number}: {exc}",
                file=sys.stderr,
            )
            return tuple(paths), True
        return tuple(paths), False

    def _parse_pr_record(
        self, raw: dict[str, Any], state: str, paginate_files: bool
    ) -> PrRecord:
        number = raw["number"]
        files = raw.get("files", [])
        closing_refs = raw.get("closingIssuesReferences", [])
        is_truncated = False
        if paginate_files and state == "open" and len(files) >= 100:
            all_files, is_truncated = self._fetch_all_pr_files(number)
            changed_files = (
                all_files
                if not is_truncated or all_files
                else tuple(file["path"] for file in files)
            )
        else:
            changed_files = tuple(file["path"] for file in files)
        rollup = self._status_check_contexts(raw.get("statusCheckRollup"))
        is_ci_passing = bool(rollup) and all(
            self._is_check_passing(check) for check in rollup
        )
        raw_is_cross = raw.get("isCrossRepository")
        return PrRecord(
            number=number,
            head_ref=raw["headRefName"],
            changed_files=changed_files,
            created_at=raw.get("createdAt") or "",
            closed_at=raw.get("closedAt") or "",
            closes_issue_numbers=tuple(sorted(ref["number"] for ref in closing_refs)),
            review_decision=raw.get("reviewDecision") or "",
            is_ci_passing=is_ci_passing,
            state=(raw.get("state") or "OPEN").upper(),
            base_ref=raw.get("baseRefName") or "",
            is_cross_repository=(
                raw_is_cross if isinstance(raw_is_cross, bool) else None
            ),
            is_files_truncated=is_truncated,
        )

    def list_prs(
        self, state: str = "open", limit: int = 1000, paginate_files: bool = False
    ) -> list[PrRecord]:
        if state not in {"open", "closed", "merged", "all"}:
            raise ValueError(f"Unsupported PR state: {state}")
        stdout = self._run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                state,
                "--limit",
                str(limit),
                "--json",
                "number,headRefName,baseRefName,isCrossRepository,state,createdAt,closedAt,reviewDecision,statusCheckRollup,files,closingIssuesReferences",
            ]
        )
        return [
            self._parse_pr_record(raw, state, paginate_files)
            for raw in json.loads(stdout)
        ]

    def list_open_prs(
        self, limit: int = 1000, paginate_files: bool = False
    ) -> list[PrRecord]:
        return self.list_prs(state="open", limit=limit, paginate_files=paginate_files)

    @staticmethod
    def _is_check_passing(check: dict[str, object]) -> bool:
        typename = check.get("__typename")
        is_status_context = typename == "StatusContext" or (
            typename is None
            and "state" in check
            and "status" not in check
            and "conclusion" not in check
        )
        if is_status_context:
            return check.get("state") == "SUCCESS"
        return check.get("status") == "COMPLETED" and check.get("conclusion") in (
            "SUCCESS",
            "NEUTRAL",
            "SKIPPED",
        )

    @staticmethod
    def _status_check_contexts(rollup: object) -> list[dict[str, object]]:
        if isinstance(rollup, list):
            return [check for check in rollup if isinstance(check, dict)]
        if not isinstance(rollup, dict):
            return []
        contexts = rollup.get("contexts")
        if isinstance(contexts, list):
            return [check for check in contexts if isinstance(check, dict)]
        return []
