from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:.-]*$")
_REF_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\[\]-]*$")


def _validate_issue_number(value: int | str) -> int:
    text = str(value)
    if not re.fullmatch(r"[0-9]+", text) or int(text) <= 0:
        raise ValueError(f"issue番号が不正です: {value!r}")
    return int(text)


def _validate_label(label: str) -> str:
    if not label or not _LABEL_PATTERN.match(label):
        raise ValueError(f"ラベル名が不正です: {label!r}")
    return label


def _validate_ref_name(ref: str) -> str:
    if (
        not ref
        or not _REF_NAME_PATTERN.match(ref)
        or ref.startswith("-")
        or ".." in ref
    ):
        raise ValueError(f"ブランチ名が不正です: {ref!r}")
    return ref


def _validate_username(username: str) -> str:
    if not username or not _USERNAME_PATTERN.match(username):
        raise ValueError(f"ユーザー名が不正です: {username!r}")
    return username


@dataclass(frozen=True)
class IssueRecord:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    created_at: str
    state: str = "OPEN"
    parent: dict | None = None
    blocked_by: tuple[int, ...] = ()


@dataclass(frozen=True)
class PrRecord:
    number: int
    head_ref: str
    changed_files: tuple[str, ...]
    closes_issue_numbers: tuple[int, ...] = ()
    review_decision: str = ""
    is_ci_passing: bool = True


def _run(args: list[str], input_text: str | None = None) -> str:
    result = subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


_VALID_ISSUE_STATES = frozenset({"open", "closed", "all"})


def list_issues_by_label(label: str, state: str = "open") -> list[IssueRecord]:
    """#236: `state`を明示指定できるようにする。既定は従来通り`open`のみ。

    `status:done`昇格判定は、人間が完了Issueを通常のGitHub運用でClose
    した場合でも依存解決できるよう、呼び出し側から`state="all"`を
    渡してclosedなIssueも含めて検索できる。
    """
    _validate_label(label)
    if state not in _VALID_ISSUE_STATES:
        raise ValueError(f"stateが不正です: {state!r}")
    stdout = _run(
        [
            "gh",
            "issue",
            "list",
            "--label",
            label,
            "--state",
            state,
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


def list_sub_issues(parent_issue_number: int | str) -> list[IssueRecord]:
    """#156: 親Issue配下のサブタスクIssueを、GraphQLの`subIssues`フィールド経由で
    まとめて取得する。

    `list_issues_by_label`はステータスラベル別にリポジトリ全体を検索するため、
    `parent_issue_number`が判明している単一big-rockディスパッチのケースでも、
    無関係な親配下のIssueまで取得してから`IssuesByStatus.filtered_by_parent`で
    破棄する、という無駄が生じる。親Issueが分かっている場合はこちらを使うことで、
    GitHub API呼び出しを（ページネーション分を除き）1回にまとめられる。

    `gh issue view --json subIssues`（CLIラッパー）は`number`/`title`/`state`
    程度しか返さず`IssueRecord`の構築に不足するため、`gh api graphql`で必要な
    フィールドを直接指定している。
    """
    number = _validate_issue_number(parent_issue_number)

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
        stdout = _run(args)
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
                        b["number"] for b in node.get("blockedBy", {}).get("nodes", [])
                    ),
                )
            )

        page_info = sub_issues["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]

    return records


def add_label(issue_number: int | str, label: str) -> None:
    number = _validate_issue_number(issue_number)
    _validate_label(label)
    _run(["gh", "issue", "edit", str(number), "--add-label", label])


def remove_label(issue_number: int | str, label: str) -> None:
    number = _validate_issue_number(issue_number)
    _validate_label(label)
    _run(["gh", "issue", "edit", str(number), "--remove-label", label])


_VALID_CLOSE_REASONS = frozenset({"completed", "not planned"})


def close_issue(
    issue_number: int | str, reason: str, comment: str | None = None
) -> None:
    """#280: `status:not-needed`スキップ機構用に、Issueを決定論的にクローズする。"""
    number = _validate_issue_number(issue_number)
    if reason not in _VALID_CLOSE_REASONS:
        raise ValueError(f"reasonが不正です: {reason!r}")
    args = ["gh", "issue", "close", str(number), "--reason", reason]
    if comment is not None:
        args.extend(["--comment", comment])
    _run(args)


def add_comment(issue_number: int | str, body: str) -> None:
    number = _validate_issue_number(issue_number)
    _run(["gh", "issue", "comment", str(number), "--body-file", "-"], input_text=body)


def merge_pull_request(pr_number: int | str) -> None:
    """#170: 子Issue→親ブランチの統合PRを、人間の確認を待たずに自動マージする。

    マージ不可（コンフリクト等）の場合は`subprocess.CalledProcessError`をそのまま
    伝播させる。呼び出し側でbest-effort処理し、PRはオープンのまま残す。
    """
    number = _validate_issue_number(pr_number)
    _run(["gh", "pr", "merge", str(number), "--merge"])


def get_issue_state(issue_number: int | str) -> str:
    """#170: 親Issueの二重クローズを避けるため、現在のIssue状態を取得する。"""
    number = _validate_issue_number(issue_number)
    stdout = _run(["gh", "issue", "view", str(number), "--json", "state"])
    return str(json.loads(stdout).get("state", "OPEN"))


def get_issue_labels(issue_number: int | str) -> tuple[str, ...]:
    """#186: 統合コーディネーターの意味的レビュー結果（合否ラベル）をポーリングするために使う。"""
    number = _validate_issue_number(issue_number)
    stdout = _run(["gh", "issue", "view", str(number), "--json", "labels"])
    raw = json.loads(stdout)
    return tuple(entry["name"] for entry in raw.get("labels", []))


def get_label_actor(issue_number: int | str, label: str) -> str:
    """#119: 指定ラベルを最後に付与したユーザーのログイン名を返す。

    `gh issue create --label`によりIssue作成時に付与されたラベルは
    GitHubのタイムラインに`labeled`イベントを残さないため、該当イベントが
    1件も見つからない場合はIssue作成者(author)を実質的な付与者とみなす。
    """
    number = _validate_issue_number(issue_number)
    _validate_label(label)
    stdout = _run(
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

    stdout = _run(["gh", "issue", "view", str(number), "--json", "author"])
    author = json.loads(stdout).get("author") or {}
    return str(author.get("login", ""))


def get_actor_permission(username: str) -> str:
    """#119: 指定ユーザーのこのリポジトリに対する権限を返す。

    コラボレーターでない場合や取得に失敗した場合は、安全側のデフォルトとして
    `"none"`を返す。
    """
    login = _validate_username(username)
    try:
        stdout = _run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/collaborators/{login}/permission"]
        )
    except subprocess.CalledProcessError:
        return "none"
    return str(json.loads(stdout).get("permission", "none"))


def create_pull_request(head: str, base: str, title: str, body: str) -> int:
    """統合ブランチ（`head`）から`base`へのPRを作成し、PR番号を返す。

    最終マージは常に人間が行う運用のため、ここではPRを作成するのみで
    マージは一切行わない。
    """
    _validate_ref_name(head)
    _validate_ref_name(base)
    stdout = _run(
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


def list_remote_branches() -> list[str]:
    stdout = _run(["git", "branch", "-r", "--format=%(refname:short)"])
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def is_branch_merged_into(head: str, base: str) -> bool:
    """Return whether GitHub records a merged PR for the exact head/base pair."""
    _validate_ref_name(head)
    _validate_ref_name(base)
    stdout = _run(
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


def list_open_prs() -> list[PrRecord]:
    """#239: ブランチ名がAIセッションの指示通りにならない場合でも自己PRと
    判定できるよう、`closingIssuesReferences`（`Closes #N`等から解決される
    GitHub側の正規のIssue参照一覧）も併せて取得する。
    パフォーマンス向上のため、一括で取得する。"""
    stdout = _run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,headRefName,reviewDecision,statusCheckRollup,files,closingIssuesReferences",
        ]
    )
    raw_prs = json.loads(stdout)
    prs: list[PrRecord] = []
    for raw in raw_prs:
        number = raw["number"]
        files = raw.get("files", [])
        closing_refs = raw.get("closingIssuesReferences", [])

        rollup = _status_check_contexts(raw.get("statusCheckRollup"))
        is_ci_passing = True
        for check in rollup:
            status = check.get("status")
            conclusion = check.get("conclusion")
            if status != "COMPLETED" or conclusion not in (
                "SUCCESS",
                "NEUTRAL",
                "SKIPPED",
            ):
                is_ci_passing = False
                break
        prs.append(
            PrRecord(
                number=number,
                head_ref=raw["headRefName"],
                changed_files=tuple(f["path"] for f in files),
                closes_issue_numbers=tuple(
                    sorted(ref["number"] for ref in closing_refs)
                ),
                review_decision=raw.get("reviewDecision") or "",
                is_ci_passing=is_ci_passing,
            )
        )
    return prs


def _status_check_contexts(rollup: object) -> list[dict[str, object]]:
    if isinstance(rollup, list):
        return [check for check in rollup if isinstance(check, dict)]
    if not isinstance(rollup, dict):
        return []
    contexts = rollup.get("contexts")
    if isinstance(contexts, list):
        return [check for check in contexts if isinstance(check, dict)]
    return []


def branch_changed_files(branch: str, base: str = "origin/main") -> list[str]:
    """#232: `base`と共通の祖先を持たない(orphanな)ブランチとの3点diffは
    `fatal: no merge base`でexit 128になる。dispatch-cycle全体をクラッシュ
    させないよう、footprint差分なし（ロック対象外）として扱う。"""
    _validate_ref_name(branch)
    _validate_ref_name(base)
    try:
        stdout = _run(["git", "diff", "--name-only", f"{base}...{branch}"])
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)
        print(
            f"Warning: failed to diff changed files for {branch!r} against "
            f"{base!r}: {detail}",
            file=sys.stderr,
        )
        return []
    except OSError as exc:
        print(
            f"Warning: unable to inspect changed files for {branch!r} against "
            f"{base!r}: {exc}",
            file=sys.stderr,
        )
        return []
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def ensure_parent_branch(parent_issue_number: int) -> None:
    parent_branch = f"parent/issue-{parent_issue_number}"
    try:
        stdout = _run(["git", "ls-remote", "origin", f"refs/heads/{parent_branch}"])
        remote_exists = bool(stdout.strip())
    except Exception:
        remote_exists = False

    if remote_exists:
        try:
            # すでにリモートに親ブランチが存在する場合、そのリモート追跡ブランチ参照を確実にローカルにフェッチする
            _run(
                [
                    "git",
                    "fetch",
                    "origin",
                    f"+refs/heads/{parent_branch}:refs/remotes/origin/{parent_branch}",
                ]
            )
        except Exception as e:
            print(
                f"Warning: Failed to fetch existing parent branch '{parent_branch}': {e}",
                file=sys.stderr,
            )
        return

    print(f"Creating parent branch '{parent_branch}' from main...", file=sys.stderr)
    try:
        # 競合やローカル変更を避けるため、checkoutを行わずにリモートmainの最新状態をfetchし、
        # FETCH_HEADを指定して直接リモートに親ブランチをプッシュして作成する。
        _run(["git", "fetch", "origin", "main"])
        _run(
            [
                "git",
                "push",
                "origin",
                f"FETCH_HEAD:refs/heads/{parent_branch}",
            ]
        )
        # プッシュ完了後、リモート追跡ブランチ参照を確実にローカルにフェッチする
        _run(
            [
                "git",
                "fetch",
                "origin",
                f"+refs/heads/{parent_branch}:refs/remotes/origin/{parent_branch}",
            ]
        )
    except Exception as e:
        print(
            f"Warning: Failed to auto-create parent branch '{parent_branch}': {e}",
            file=sys.stderr,
        )


def resolve_local_or_remote_branch(
    worktree_path: str | Path, branch: str, *, prefer_remote: bool = False
) -> str:
    """指定されたブランチ名を解決して返す。
    prefer_remote=True の場合はリモート追跡ブランチを最優先し、
    False の場合はローカルブランチを最優先する。"""
    _validate_ref_name(branch)
    worktree_path = Path(worktree_path)

    def check_remote() -> str | None:
        res_remote = subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "show-ref",
                "--verify",
                f"refs/remotes/origin/{branch}",
            ],
            capture_output=True,
        )
        if res_remote.returncode == 0:
            return f"origin/{branch}"
        return None

    def check_local() -> str | None:
        res_local = subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "show-ref",
                "--verify",
                f"refs/heads/{branch}",
            ],
            capture_output=True,
        )
        if res_local.returncode == 0:
            return branch
        return None

    if prefer_remote:
        resolved = check_remote() or check_local()
    else:
        resolved = check_local() or check_remote()

    return resolved or branch
