from __future__ import annotations

import json
import re
import subprocess
import sys
from urllib.parse import quote

# #284(git-adapter): git実行専任のorchestune.git_cliへ移設済み。呼び出し側
# （9参照）の付け替えは後続Issueに分離するため、同一オブジェクトへの
# 再エクスポートとして維持する。
from orchestune.git_cli import (
    branch_changed_files,  # noqa: F401
    ensure_parent_branch,  # noqa: F401
    fetch_remote_branch,  # noqa: F401
    list_remote_branches,  # noqa: F401
    resolve_local_or_remote_branch,  # noqa: F401
)

from orchestune.models import IssueRecord, PrRecord
from orchestune.validation import (
    validate_issue_number as _validate_issue_number,
    validate_label as _validate_label,
    validate_ref_name as _validate_ref_name,
    validate_username as _validate_username,
)


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


def list_issues_by_label(
    label: str, state: str = "open", limit: int = 1000
) -> list[IssueRecord]:
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
    `"none"`を返す。#208: ユーザー名が空（actor解決不能）の場合も同様に`"none"`を返す。
    """
    if not username:
        return "none"
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


def is_branch_merged_into(head: str, base: str) -> bool:
    """Return whether GitHub records a merged PR for the exact head/base pair.

    This lookup remains valid after GitHub deletes the merged PR's head branch,
    which is required by the parent-completion lifecycle.
    """
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


def get_merged_pr_timestamp(head: str, base: str) -> str | None:
    """#255/#276: 指定head/baseの直近のmerged PRの`mergedAt`（ISO8601）を返す。
    該当PRが無ければNoneを返す。`is_branch_merged_into`同様、branch削除後も
    historical記録として有効。呼び出し側（parent-completionの再open判定）が
    「このマージは直近の再openより前だったか」を時刻比較で判定するために使う。

    #276レビュー対応: `parent_branch`はbranch削除後の再作成により、同じ
    head/baseで複数回mergeされたPR記録が積み重なりうる。`gh pr list`は
    `mergedAt`順を保証しないため、`--limit 1`の先頭を無条件に信頼せず、
    取得した全件から最大の`mergedAt`（＝最も新しいmerge）を明示的に選ぶ。
    """
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
            "mergedAt",
            "--limit",
            "1000",
        ]
    )
    results = json.loads(stdout)
    timestamps = [r.get("mergedAt") for r in results if r.get("mergedAt")]
    if not timestamps:
        return None
    return str(max(timestamps))


def get_issue_last_reopened_at(issue_number: int | str) -> str | None:
    """#276: 指定Issueの直近の`reopened`イベント時刻（ISO8601）を返す。
    一度もreopenされていなければNoneを返す。`get_label_actor`と同じ
    events API（ページネーション込み）を使う。"""
    number = _validate_issue_number(issue_number)
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
    reopened_at = [
        event.get("created_at") for event in events if event.get("event") == "reopened"
    ]
    return str(reopened_at[-1]) if reopened_at else None


def branch_exists(branch: str) -> bool:
    """#276: 指定branchが現在GitHub上に存在するかどうかを返す。

    404（存在しない）と判定できた場合のみFalseを返す。それ以外の失敗
    （権限・レート制限等）はそのまま例外として送出し、呼び出し側が
    「真に存在しないと確認できた場合」だけを安全なフォールバック判定に
    使い、それ以外の不確実な失敗はfail closedに扱えるようにする。"""
    _validate_ref_name(branch)
    encoded_branch = quote(branch, safe="")
    try:
        _run(["gh", "api", f"repos/{{owner}}/{{repo}}/branches/{encoded_branch}"])
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").lower()
        if "404" in detail or "not found" in detail:
            return False
        raise
    return True


def is_current_branch_tip_merged_into(head: str, base: str) -> bool:
    """Return whether the current remote ``head`` tip is contained in ``base``.

    Looking up historical pull requests by branch name is insufficient because a
    branch can be recreated with new commits. Resolve its current GitHub SHA and
    compare that immutable commit to the base so callers fail closed when the
    current tip cannot be fetched or proven merged. This stricter check is for
    the integrator's fetch-failure path, where branch names may be reused.
    """
    _validate_ref_name(head)
    _validate_ref_name(base)
    encoded_head = quote(head, safe="")
    tip_sha = _run(
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

    status = _run(
        [
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/compare/{tip_sha}...{base}",
            "--jq",
            ".status",
        ]
    ).strip()
    return status in {"ahead", "identical"}


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


def _fetch_all_pr_files(pr_number: int) -> tuple[tuple[str, ...], bool]:
    """#250: gh pr list --json files は first: 100 固定でページングされないため、
    100件以上のファイル変更があるPRについては GraphQL API で完全取得する。
    取得に失敗した場合は (既知のファイルパス..., True) を返し、呼び出し側に
    is_files_truncated=True を通知する。
    """
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
            stdout = _run(args)
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
    state: str = "open", limit: int = 1000, paginate_files: bool = False
) -> list[PrRecord]:
    """#239: ブランチ名がAIセッションの指示通りにならない場合でも自己PRと
    判定できるよう、`closingIssuesReferences`（`Closes #N`等から解決される
    GitHub側の正規のIssue参照一覧）も併せて取得する。
    パフォーマンス向上のため、一括で取得する。
    #210: 完了シグナルがPRのclose/mergeで消えないよう、呼び出し側が
    `open`/`closed`/`merged`/`all`を指定できる。
    #250: 外部ロック判定等のため全件のchanged filesが必要な呼び出し元(paginate_files=True)
    に対してのみ、100件以上のPRでGraphQL APIで全件取得し、完全取得できない場合は
    is_files_truncated=Trueを設定する。
    """
    if state not in {"open", "closed", "merged", "all"}:
        raise ValueError(f"Unsupported PR state: {state}")
    stdout = _run(
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
        # #250: GraphQL ページネーションは外部ロック判定など changed files 全件を必要とする
        # 明示的呼び出し (paginate_files=True かつ state=="open") のみに限定する。
        # 完了検知やリカバリ等の他呼び出し元での不要な GraphQL API 呼び出し・レイテンシ増大を防ぐ。
        if paginate_files and state == "open" and len(files) >= 100:
            all_files, is_truncated = _fetch_all_pr_files(number)
            if not is_truncated:
                changed_files = all_files
            else:
                changed_files = (
                    all_files if all_files else tuple(f["path"] for f in files)
                )
        else:
            changed_files = tuple(f["path"] for f in files)

        rollup = _status_check_contexts(raw.get("statusCheckRollup"))
        is_ci_passing = bool(rollup) and all(
            _is_check_passing(check) for check in rollup
        )
        # #243: boolとして取得できない場合は「不明」(None)のまま保持し、
        # 統合PR再利用側でfail closedに判定させる。
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


def list_open_prs(limit: int = 1000, paginate_files: bool = False) -> list[PrRecord]:
    """Return open PRs, preserving the existing compatibility API."""
    return list_prs(state="open", limit=limit, paginate_files=paginate_files)


def _is_check_passing(check: dict[str, object]) -> bool:
    """#256: `statusCheckRollup`はCheckRunとlegacy StatusContextのunionだが、
    各要素はそれぞれ異なるschemaを持つ（CheckRun: `status`/`conclusion`、
    StatusContext: `state`）。両方を区別せずCheckRun用のfieldだけを見ると、
    StatusContextは常に`status`が存在せず`COMPLETED`と一致しないため、
    成功していてもCI失敗扱いになってしまう。

    `__typename`があればそれを優先し、無い場合はfield shape
    （`state`のみ持ち`status`/`conclusion`を持たない）で判定する
    （gh CLIバージョン差異への耐性）。"""
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


def _status_check_contexts(rollup: object) -> list[dict[str, object]]:
    if isinstance(rollup, list):
        return [check for check in rollup if isinstance(check, dict)]
    if not isinstance(rollup, dict):
        return []
    contexts = rollup.get("contexts")
    if isinstance(contexts, list):
        return [check for check in contexts if isinstance(check, dict)]
    return []


def normalize_remote_branch_name(branch: str) -> str:
    """`origin/` 接頭辞の有無を吸収し、リモートのブランチ名を返す。"""
    if branch.startswith("origin/"):
        branch = branch.removeprefix("origin/")
    return _validate_ref_name(branch)
