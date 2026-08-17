"""Issue本文からのタスク定義パース。"""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

import yaml

from orchestune.models import IssueRecord, Task

if TYPE_CHECKING:
    from orchestune.forge import IssueForge

BASE_PRIORITY = {"low": 1.0, "medium": 2.0, "high": 3.0}

FOOTPRINT_BLOCK_PATTERN = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)

# Embedded in every parent (EPIC) issue `provisioning.py` creates, and required
# (in addition to an exact title match) before an orphan-recovery lookup is
# allowed to adopt an existing issue as "our" parent: an unrelated issue that
# coincidentally has the same title has no way to also have this exact literal
# string in its body. Lives here (L2) rather than in `provisioning.py` (L4) so
# that `dispatch_cycle.py` (L3) can validate a `--parent-issue` number against
# it without depending on the entrypoint-layer `provisioning` module.
PARENT_MARKER = "<!-- orchestune:decomposition-plan-parent -->"


def _parse_footprint_block(body: str) -> dict | None:
    """Footprint YAMLフェンスをdictとして返す（存在しない/壊れている場合はNone）。"""
    match = FOOTPRINT_BLOCK_PATTERN.search(body)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def parent_issue_number_from_body(body: str) -> int | None:
    """#485: ネイティブSub-issue関係が使えない環境向けに、Footprint YAML
    フェンスへ永続化された`parent_issue_number`を読み取るフォールバック。

    ネイティブの`issue.parent`が利用できる場合はそちらを優先すべきで、
    これは`gh`/GitHub MCPが関係操作を提供しない縮退時にのみ使われる。
    """
    data = _parse_footprint_block(body)
    if not data:
        return None
    value = data.get("parent_issue_number")
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def find_children_by_parent(
    forge: IssueForge, parent_issue_number: int | str
) -> list[IssueRecord]:
    """`parent_issue_number`配下の子Issueを、ネイティブSub-issue関係を起点に、
    本文metadataフォールバックで補完して返す（#485）。

    `forge`が`find_issues_by_parent_metadata`の呼び出しに失敗した場合
    （後方互換のため未実装のforge実装を含む）は、ネイティブの結果だけを
    黙って返す（既存の`gh`ベース運用は完全動作のまま変わらない）。
    """
    native = forge.list_sub_issues(parent_issue_number)
    seen = {issue.number for issue in native}

    try:
        candidates = forge.find_issues_by_parent_metadata(parent_issue_number)
    except Exception as e:
        print(
            f"Warning: parent-metadata fallback search for #{parent_issue_number} "
            f"failed: {e}",
            file=sys.stderr,
        )
        return native

    target_number = int(parent_issue_number)
    extra: list[IssueRecord] = [
        candidate
        for candidate in candidates
        if candidate.number not in seen
        and parent_issue_number_from_body(candidate.body) == target_number
    ]
    return native + extra


def is_epic_issue(issue: IssueRecord) -> bool:
    """`issue`がEPIC（親）Issueと構造的に一致するかを判定する。

    `provisioning.py`の持続済みparent番号検証は、特定のplanの`metadata.title`との
    厳密な一致まで要求するが、dispatch実行時（`--parent-issue`検証）にはどのplanの
    親かという情報がなく番号しか分からないため、「本物のEPICらしい構造を持つか」
    という緩い判定に留める。
    """
    return issue.title.startswith("[EPIC] ") and PARENT_MARKER in issue.body


def parse_task_from_issue(
    issue: IssueRecord,
    issue_to_subtask_id: dict[int, str] | None = None,
) -> Task:
    subtask_id = ""
    footprint: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    yaml_error = False

    match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    if match:
        try:
            data = yaml.safe_load(match.group(1))
            if isinstance(data, dict):
                subtask_id = str(data.get("subtask_id", ""))
                footprint = tuple(str(f) for f in (data.get("footprint") or []))
                symbols = tuple(str(s) for s in (data.get("symbols") or []))
        except yaml.YAMLError as e:
            print(
                f"Warning: Failed to parse YAML from issue #{issue.number}: {e}",
                file=sys.stderr,
            )
            yaml_error = True

    if issue_to_subtask_id is not None and issue.blocked_by:
        depends_on = tuple(
            issue_to_subtask_id[num]
            for num in issue.blocked_by
            if num in issue_to_subtask_id
        )
    else:
        if match and not yaml_error:
            try:
                data = yaml.safe_load(match.group(1))
                if isinstance(data, dict):
                    depends_on = tuple(str(d) for d in (data.get("depends_on") or []))
            except Exception:
                pass

    priority = "medium"
    has_unknown_priority_label = False
    risk = False
    progress_partial = False
    for label in issue.labels:
        if label.startswith("priority:"):
            candidate = label.split(":", 1)[1]
            if candidate in BASE_PRIORITY:
                priority = candidate
            else:
                has_unknown_priority_label = True
                print(
                    f"Warning: Unknown priority label '{label}' on issue "
                    f"#{issue.number}; falling back to 'medium'.",
                    file=sys.stderr,
                )
        elif label == "risk:flagged":
            risk = True
        elif label == "progress:partial":
            progress_partial = True

    if has_unknown_priority_label:
        priority = "medium"

    parent_number = None
    parent_state = None
    if issue.parent:
        parent_number = issue.parent.get("number")
        parent_state = issue.parent.get("state")
    else:
        # #485: ネイティブSub-issue関係が無い（MCP-only縮退環境で作成された）
        # Issueでも、本文metadataから親を復元する。closed判定は分からないため
        # `parent_state`はNoneのままとする(=未closedとして扱う、安全側)。
        parent_number = parent_issue_number_from_body(issue.body)

    return Task(
        issue_number=issue.number,
        subtask_id=subtask_id,
        footprint=footprint,
        symbols=symbols,
        risk=risk,
        priority=priority,
        progress_partial=progress_partial,
        status_labels=tuple(issue.labels),
        created_at=issue.created_at,
        depends_on=depends_on,
        yaml_error=yaml_error,
        parent_number=parent_number,
        issue_state=issue.state,
        parent_state=parent_state,
    )
