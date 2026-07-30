from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from orchestune.models import IssueRecord, PrRecord
from orchestune.validation import (
    validate_issue_number,
    validate_label,
    validate_ref_name,
    validate_username,
)


class ForgeError(RuntimeError):
    """フォージ操作(gh CLI呼び出し等)が失敗した場合に送出する。"""


class ForgeAuthError(ForgeError):
    """フォージCLI(gh等)が未インストール、または未認証の場合に送出する。"""


# gh label listの1回のAPI呼び出しで取得する上限件数。実在するrepositoryの
# label数を十分に上回る値とし、これに達した場合は取得が打ち切られた
# (truncateされた)可能性があるとみなして明示的に失敗させる。
_LABEL_LIST_LIMIT = 1000
_VALID_ISSUE_STATES = frozenset({"open", "closed", "all"})
_VALID_CLOSE_REASONS = frozenset({"completed", "not planned"})

_SUB_ISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          number
          title
          body
          state
          createdAt
          labels(first: 50) { nodes { name } }
          parent { number }
          blockedBy(first: 50) { nodes { number } }
        }
      }
    }
  }
}
"""

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


@dataclass(frozen=True)
class LabelSpec:
    name: str
    color: str
    description: str


@dataclass(frozen=True)
class BootstrapResult:
    created_labels: tuple[str, ...]
    existing_labels: tuple[str, ...]


@runtime_checkable
class IssueForge(Protocol):
    def list_issues_by_label(
        self, label: str, state: str = "open", limit: int = 1000
    ) -> list[IssueRecord]: ...

    def list_sub_issues(self, parent_issue_number: int | str) -> list[IssueRecord]: ...

    def add_label(self, issue_number: int | str, label: str) -> None: ...

    def remove_label(self, issue_number: int | str, label: str) -> None: ...

    def close_issue(
        self, issue_number: int | str, reason: str, comment: str | None = None
    ) -> None: ...

    def add_comment(self, issue_number: int | str, body: str) -> None: ...

    def get_issue_state(self, issue_number: int | str) -> str: ...

    def get_issue_labels(self, issue_number: int | str) -> tuple[str, ...]: ...

    def get_label_actor(self, issue_number: int | str, label: str) -> str: ...

    def get_actor_permission(self, username: str) -> str: ...

    def get_issue_last_reopened_at(self, issue_number: int | str) -> str | None: ...

    def create_issue(
        self, title: str, body: str, labels: Sequence[str] = ()
    ) -> int: ...

    def add_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None: ...

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None: ...

    def find_open_issue_by_exact_title(self, title: str) -> int | None: ...


@runtime_checkable
class PullRequestForge(Protocol):
    def merge_pull_request(self, pr_number: int | str) -> None: ...

    def create_pull_request(
        self, head: str, base: str, title: str, body: str
    ) -> int: ...

    def is_branch_merged_into(self, head: str, base: str) -> bool: ...

    def get_merged_pr_timestamp(self, head: str, base: str) -> str | None: ...

    def branch_exists(self, branch: str) -> bool: ...

    def is_current_branch_tip_merged_into(self, head: str, base: str) -> bool: ...

    def list_prs(
        self, state: str = "open", limit: int = 1000, paginate_files: bool = False
    ) -> list[PrRecord]: ...

    def list_open_prs(
        self, limit: int = 1000, paginate_files: bool = False
    ) -> list[PrRecord]: ...


@runtime_checkable
class RepoAdminForge(Protocol):
    def check_auth(self) -> None:
        """認証が利用できない場合はForgeAuthErrorを送出する。"""

    def ensure_labels(self, labels: tuple[LabelSpec, ...]) -> BootstrapResult:
        """未作成のラベルのみ作成する（既存ラベルは変更しない）。"""


@runtime_checkable
class Forge(IssueForge, PullRequestForge, RepoAdminForge, Protocol):
    """Orchestune が利用する分割済み Forge 契約の合成型。"""


class GitHubForge:
    def _run(self, args: list[str], input_text: str | None = None) -> str:
        result = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def list_issues_by_label(
        self, label: str, state: str = "open", limit: int = 1000
    ) -> list[IssueRecord]:
        """ラベルと状態でIssueを取得する。"""
        validate_label(label)
        if state not in _VALID_ISSUE_STATES:
            raise ValueError(f"stateが不正です: {state!r}")
        stdout = self._run(
            [
                "gh",
                "issue",
                "list",
                "--label",
                label,
                "--state",
                state,
                "--limit",
                str(limit),
                "--json",
                "number,title,body,labels,createdAt,parent,blockedBy,state",
            ]
        )
        raw_issues = json.loads(stdout)
        return [
            IssueRecord(
                number=raw["number"],
                title=raw["title"],
                body=raw["body"],
                labels=tuple(entry["name"] for entry in raw.get("labels", [])),
                created_at=raw["createdAt"],
                state=raw.get("state", "OPEN"),
                parent=raw.get("parent"),
                blocked_by=tuple(
                    node["number"] for node in raw.get("blockedBy", {}).get("nodes", [])
                ),
            )
            for raw in raw_issues
        ]

    def list_sub_issues(self, parent_issue_number: int | str) -> list[IssueRecord]:
        """親Issue配下のサブタスクIssueをGraphQLで取得する。"""
        number = validate_issue_number(parent_issue_number)

        records: list[IssueRecord] = []
        after: str | None = None
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
                f"number={number}",
                "-f",
                f"query={_SUB_ISSUES_QUERY}",
            ]
            if after is not None:
                args += ["-F", f"after={after}"]
            stdout = self._run(args)
            sub_issues = json.loads(stdout)["data"]["repository"]["issue"]["subIssues"]

            for node in sub_issues["nodes"]:
                records.append(
                    IssueRecord(
                        number=node["number"],
                        title=node["title"],
                        body=node["body"],
                        labels=tuple(
                            entry["name"]
                            for entry in node.get("labels", {}).get("nodes", [])
                        ),
                        created_at=node["createdAt"],
                        state=node.get("state", "OPEN"),
                        parent=node.get("parent"),
                        blocked_by=tuple(
                            blocked["number"]
                            for blocked in node.get("blockedBy", {}).get("nodes", [])
                        ),
                    )
                )

            page_info = sub_issues["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            after = page_info["endCursor"]

        return records

    def add_label(self, issue_number: int | str, label: str) -> None:
        number = validate_issue_number(issue_number)
        validate_label(label)
        self._run(["gh", "issue", "edit", str(number), "--add-label", label])

    def remove_label(self, issue_number: int | str, label: str) -> None:
        number = validate_issue_number(issue_number)
        validate_label(label)
        self._run(["gh", "issue", "edit", str(number), "--remove-label", label])

    def close_issue(
        self, issue_number: int | str, reason: str, comment: str | None = None
    ) -> None:
        """Issueを指定理由で決定論的にクローズする。"""
        number = validate_issue_number(issue_number)
        if reason not in _VALID_CLOSE_REASONS:
            raise ValueError(f"reasonが不正です: {reason!r}")
        args = ["gh", "issue", "close", str(number), "--reason", reason]
        if comment is not None:
            args.extend(["--comment", comment])
        self._run(args)

    def add_comment(self, issue_number: int | str, body: str) -> None:
        number = validate_issue_number(issue_number)
        self._run(
            ["gh", "issue", "comment", str(number), "--body-file", "-"],
            input_text=body,
        )

    def get_issue_state(self, issue_number: int | str) -> str:
        number = validate_issue_number(issue_number)
        stdout = self._run(["gh", "issue", "view", str(number), "--json", "state"])
        return str(json.loads(stdout).get("state", "OPEN"))

    def get_issue_labels(self, issue_number: int | str) -> tuple[str, ...]:
        number = validate_issue_number(issue_number)
        stdout = self._run(["gh", "issue", "view", str(number), "--json", "labels"])
        raw = json.loads(stdout)
        return tuple(entry["name"] for entry in raw.get("labels", []))

    def get_label_actor(self, issue_number: int | str, label: str) -> str:
        """指定ラベルを最後に付与したユーザーのログイン名を返す。"""
        number = validate_issue_number(issue_number)
        validate_label(label)
        stdout = self._run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/issues/{number}/events",
                "--paginate",
                "--slurp",
            ]
        )
        pages = json.loads(stdout)
        events = [event for page in pages for event in page]
        labeled_actors = [
            event["actor"]["login"]
            for event in events
            if event.get("event") == "labeled"
            and event.get("label", {}).get("name") == label
        ]
        if labeled_actors:
            return str(labeled_actors[-1])

        stdout = self._run(["gh", "issue", "view", str(number), "--json", "author"])
        author = json.loads(stdout).get("author") or {}
        return str(author.get("login", ""))

    def get_actor_permission(self, username: str) -> str:
        """指定ユーザーのrepository権限を返す。"""
        if not username:
            return "none"
        login = validate_username(username)
        try:
            stdout = self._run(
                [
                    "gh",
                    "api",
                    f"repos/{{owner}}/{{repo}}/collaborators/{login}/permission",
                ]
            )
        except subprocess.CalledProcessError:
            return "none"
        return str(json.loads(stdout).get("permission", "none"))

    def get_issue_last_reopened_at(self, issue_number: int | str) -> str | None:
        """指定Issueの直近のreopenedイベント時刻を返す。"""
        number = validate_issue_number(issue_number)
        stdout = self._run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/issues/{number}/events",
                "--paginate",
                "--slurp",
            ]
        )
        pages = json.loads(stdout)
        events = [event for page in pages for event in page]
        reopened_at = [
            event.get("created_at")
            for event in events
            if event.get("event") == "reopened"
        ]
        return str(reopened_at[-1]) if reopened_at else None

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> int:
        """Issueを新規作成し、番号を返す。"""
        args = ["gh", "issue", "create", "--title", title, "--body-file", "-"]
        for label in labels:
            validate_label(label)
            args.extend(["--label", label])
        stdout = self._run(args, input_text=body)
        url = stdout.strip().splitlines()[-1]
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def add_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None:
        """childをparent配下のsub-issueとして紐付ける。"""
        parent = validate_issue_number(parent_issue_number)
        child = validate_issue_number(child_issue_number)
        self._run(["gh", "issue", "edit", str(child), "--set-parent", str(parent)])

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None:
        """issue_numberをblocking_issue_numberでブロック済みとしてマークする。"""
        number = validate_issue_number(issue_number)
        blocker = validate_issue_number(blocking_issue_number)
        self._run(
            ["gh", "issue", "edit", str(number), "--add-blocked-by", str(blocker)]
        )

    def find_open_issue_by_exact_title(self, title: str) -> int | None:
        """タイトル完全一致のopen Issueを検索する（親Issueの重複作成防止用）。"""
        stdout = self._run(
            [
                "gh",
                "issue",
                "list",
                "--search",
                f'in:title "{title}"',
                "--state",
                "open",
                "--json",
                "number,title",
                "--limit",
                "100",
            ]
        )
        for entry in json.loads(stdout):
            if entry.get("title") == title:
                return int(entry["number"])
        return None

    def merge_pull_request(self, pr_number: int | str) -> None:
        """PRをmerge commit方式でマージする。"""
        number = validate_issue_number(pr_number)
        self._run(["gh", "pr", "merge", str(number), "--merge"])

    def create_pull_request(self, head: str, base: str, title: str, body: str) -> int:
        """統合ブランチからbaseへのPRを作成し、PR番号を返す。"""
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

    def is_branch_merged_into(self, head: str, base: str) -> bool:
        """head/baseが一致するmerged PRの存在を返す。"""
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
        """head/baseが一致する最新merged PRの時刻を返す。"""
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
        results = json.loads(stdout)
        timestamps = [
            result.get("mergedAt") for result in results if result.get("mergedAt")
        ]
        if not timestamps:
            return None
        return str(max(timestamps))

    def branch_exists(self, branch: str) -> bool:
        """GitHub上にbranchが存在すると確認できた場合だけTrueを返す。"""
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
        """現在のremote head tipがbaseへ含まれるかを返す。"""
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
        """PRのchanged filesをGraphQLで完全取得する。"""
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

    def list_prs(
        self, state: str = "open", limit: int = 1000, paginate_files: bool = False
    ) -> list[PrRecord]:
        """指定状態のPRと統合判定に必要なmetadataを取得する。"""
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
        raw_prs = json.loads(stdout)
        prs: list[PrRecord] = []
        for raw in raw_prs:
            number = raw["number"]
            files = raw.get("files", [])
            closing_refs = raw.get("closingIssuesReferences", [])

            is_truncated = False
            if paginate_files and state == "open" and len(files) >= 100:
                all_files, is_truncated = self._fetch_all_pr_files(number)
                if not is_truncated:
                    changed_files = all_files
                else:
                    changed_files = (
                        all_files if all_files else tuple(f["path"] for f in files)
                    )
            else:
                changed_files = tuple(f["path"] for f in files)

            rollup = self._status_check_contexts(raw.get("statusCheckRollup"))
            is_ci_passing = bool(rollup) and all(
                self._is_check_passing(check) for check in rollup
            )
            raw_is_cross = raw.get("isCrossRepository")
            prs.append(
                PrRecord(
                    number=number,
                    head_ref=raw["headRefName"],
                    changed_files=changed_files,
                    created_at=raw.get("createdAt") or "",
                    closed_at=raw.get("closedAt") or "",
                    closes_issue_numbers=tuple(
                        sorted(ref["number"] for ref in closing_refs)
                    ),
                    review_decision=raw.get("reviewDecision") or "",
                    is_ci_passing=is_ci_passing,
                    state=(raw.get("state") or "OPEN").upper(),
                    base_ref=raw.get("baseRefName") or "",
                    is_cross_repository=(
                        raw_is_cross if isinstance(raw_is_cross, bool) else None
                    ),
                    is_files_truncated=is_truncated,
                )
            )
        return prs

    def list_open_prs(
        self, limit: int = 1000, paginate_files: bool = False
    ) -> list[PrRecord]:
        """open PR一覧を返す互換API。"""
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

    def check_auth(self) -> None:
        if shutil.which("gh") is None:
            raise ForgeAuthError(
                "gh CLIが見つかりません。https://cli.github.com/ からインストールしてください。"
            )

        # `gh auth status`の非0終了は「未認証」という想定内の結果であり、
        # `_run`(check=True)を使う通常のgh操作とは失敗の扱いを分ける。
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise ForgeAuthError(
                f"gh認証が未設定です。`gh auth login`を実行してください: {result.stderr.strip()}"
            )

    def ensure_labels(self, labels: tuple[LabelSpec, ...]) -> BootstrapResult:
        for label in labels:
            validate_label(label.name)

        existing_names = self._list_existing_label_names()

        created: list[str] = []
        existing: list[str] = []
        for label in labels:
            if label.name in existing_names:
                existing.append(label.name)
                continue
            subprocess.run(
                [
                    "gh",
                    "label",
                    "create",
                    label.name,
                    "--color",
                    label.color,
                    "--description",
                    label.description,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            created.append(label.name)

        return BootstrapResult(
            created_labels=tuple(created), existing_labels=tuple(existing)
        )

    def _list_existing_label_names(self) -> set[str]:
        result = subprocess.run(
            [
                "gh",
                "label",
                "list",
                "--json",
                "name",
                "--limit",
                str(_LABEL_LIST_LIMIT),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        raw = json.loads(result.stdout)
        if len(raw) >= _LABEL_LIST_LIMIT:
            raise ForgeError(
                f"既存ラベル一覧の取得件数が上限({_LABEL_LIST_LIMIT}件)に達しました。"
                "取得が打ち切られた(truncateされた)可能性があるため、"
                "誤って重複ラベルを作成しないようbootstrapを中断します。"
            )
        return {entry["name"] for entry in raw}


REQUIRED_LABELS: tuple[LabelSpec, ...] = (
    LabelSpec(
        "status:queued", "0E8A16", "Issue is ready to be picked up by the dispatcher"
    ),
    LabelSpec(
        "status:blocked", "B60205", "Issue is blocked on unresolved dependencies"
    ),
    LabelSpec(
        "status:blocked-recompute", "B60205", "Blocked pending DAG recomputation"
    ),
    LabelSpec("status:blocked-human-review", "B60205", "Blocked pending human review"),
    LabelSpec("status:done", "0E8A16", "Subtask work is complete"),
    LabelSpec(
        "status:external-lock",
        "5319E7",
        "Blocked by an externally-held footprint lock",
    ),
    LabelSpec(
        "status:force-serial",
        "5319E7",
        "Forced to run serially after recompute retries exhausted",
    ),
    LabelSpec(
        "status:in-progress", "1D76DB", "Currently being worked by a dispatched agent"
    ),
    LabelSpec(
        "status:manual-merge-required", "FBCA04", "Needs a human to manually merge"
    ),
    LabelSpec("status:not-needed", "CCCCCC", "Subtask determined to be unnecessary"),
    LabelSpec("priority:high", "D93F0B", "High priority subtask"),
    LabelSpec("priority:medium", "FBCA04", "Medium priority subtask"),
    LabelSpec("priority:low", "C2E0C6", "Low priority subtask"),
    LabelSpec("risk:flagged", "E11D21", "Flagged as risky by the decomposition step"),
    LabelSpec(
        "progress:partial", "BFD4F2", "Partial progress recorded on this subtask"
    ),
    LabelSpec(
        "not-needed-review:passed",
        "0E8A16",
        "Not-needed determination verified as correct",
    ),
    LabelSpec(
        "not-needed-review:failed",
        "B60205",
        "Not-needed determination verified as incorrect",
    ),
    LabelSpec(
        "integration:included",
        "BFD4F2",
        "Already merged into an integration branch/PR by the Integrator",
    ),
)
