"""Issue本文からのタスク定義パース。"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from orchestune.forge import MetadataSearchUnavailableError
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


_INTEGER_STRING_PATTERN = re.compile(r"-?\d+")


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
    # #485 review round 8 (P2): `int(100.9)` truncates instead of rejecting
    # a malformed non-integral value, and `int(True)` == 1 would silently
    # accept a YAML boolean as a valid issue number. Only accept an actual
    # `int` (excluding `bool`, a subclass of `int`) or a digit-only string
    # (rejecting a numeric-looking-but-fractional string like `"100.9"`).
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER_STRING_PATTERN.fullmatch(value.strip()):
        return int(value)
    return None


def effective_parent_number(issue: IssueRecord) -> int | None:
    """ネイティブSub-issue関係(`issue.parent`)を優先し、無い場合のみ本文
    metadataにフォールバックする（#485）。

    ネイティブ関係がある場合はそれを唯一の正とみなし、本文metadataは一切
    参照しない: Issueがネイティブに再親化された後も、本文の
    `parent_issue_number`が古いまま更新されていないケースがありうるため
    （#485 review P2）、本文metadataを「ネイティブが無い場合のみの
    フォールバック」以上の意味で使うと、旧親の完了判定やprovisioningの
    再利用インデックスが誤って古い親の下にこのIssueを含めてしまう。
    """
    if issue.parent and issue.parent.get("number") is not None:
        return issue.parent.get("number")
    return parent_issue_number_from_body(issue.body)


def backfill_parent_issue_number(body: str, parent_issue_number: int) -> str | None:
    """#485 review (P2): a reused Issue can predate this field (created by
    an older template, or by a prior run before `parent_issue_number` was
    added to `.github/issue_template.md`). Returns `body` with the
    Footprint YAML fence's `parent_issue_number` set/corrected, or `None`
    if it's already correct (nothing to write) or the fence can't be
    parsed at all (nothing safe to patch).

    Only the Footprint fence is touched — the rest of the body (which may
    carry human edits) is left byte-for-byte identical, unlike re-rendering
    the whole body from the template would.
    """
    match = FOOTPRINT_BLOCK_PATTERN.search(body)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("parent_issue_number") == parent_issue_number:
        return None
    data["parent_issue_number"] = parent_issue_number
    new_block = yaml.dump(data, allow_unicode=True, default_flow_style=False)
    start, end = match.span(1)
    return body[:start] + new_block + body[end:]


@dataclass(frozen=True)
class ChildDiscoveryResult:
    issues: list[IssueRecord]
    # #485 review round 7 (P2): whether `find_issues_by_parent_metadata`
    # actually works on this `forge` (as opposed to raising a
    # capability-absence signal). A body correctly carrying
    # `parent_issue_number` is worthless for discovery if nothing can ever
    # search for it — callers must not treat `has_parent_metadata` as
    # sufficient proof of discoverability unless this is also `True`.
    metadata_search_supported: bool


def find_children_by_parent(
    forge: IssueForge, parent_issue_number: int | str
) -> ChildDiscoveryResult:
    """`parent_issue_number`配下の子Issueを、ネイティブSub-issue関係を起点に、
    本文metadataフォールバックで補完して返す（#485）。

    `forge`が構造的にこの検索をサポートしない場合（`MetadataSearchUnavailableError`、
    または後方互換のため未実装のforge実装が送出する`AttributeError`/
    `NotImplementedError`）は、ネイティブの結果だけを黙って返す（既存の
    `gh`ベース運用は完全動作のまま変わらない）。それ以外の失敗（`gh`認証切れ・
    レート制限・ネットワーク瞬断などの一時的なもの）は伝播させ、呼び出し元の
    再試行に委ねる — 黙って握りつぶすと、metadataでしか発見できないIssueが
    一時的に消え、`provisioning.py`のdedup fallbackが誤って重複作成しうる。
    """
    native = forge.list_sub_issues(parent_issue_number)
    seen = {issue.number for issue in native}

    try:
        candidates = forge.find_issues_by_parent_metadata(parent_issue_number)
    except (MetadataSearchUnavailableError, AttributeError, NotImplementedError) as e:
        print(
            f"Warning: this forge does not support parent-metadata search; "
            f"falling back to native sub-issue relationships only for "
            f"#{parent_issue_number}: {e}",
            file=sys.stderr,
        )
        return ChildDiscoveryResult(issues=native, metadata_search_supported=False)

    # #485 review (P2): re-verify with `effective_parent_number`, not a raw
    # body-metadata read — a candidate returned by `gh search`'s substring
    # match could have since been natively reparented elsewhere while its
    # body still carries the old `parent_issue_number` (never rewritten).
    # `effective_parent_number` treats a present native `parent` as
    # authoritative, so such a stale-body candidate is correctly rejected
    # here instead of indefinitely blocking the old parent's completion.
    target_number = int(parent_issue_number)
    extra: list[IssueRecord] = [
        candidate
        for candidate in candidates
        if candidate.number not in seen
        and effective_parent_number(candidate) == target_number
    ]
    return ChildDiscoveryResult(issues=native + extra, metadata_search_supported=True)


def is_epic_issue(issue: IssueRecord) -> bool:
    """`issue`がEPIC（親）Issueと構造的に一致するかを判定する。

    `provisioning.py`の持続済みparent番号検証は、特定のplanの`metadata.title`との
    厳密な一致まで要求するが、dispatch実行時（`--parent-issue`検証）にはどのplanの
    親かという情報がなく番号しか分からないため、「本物のEPICらしい構造を持つか」
    という緩い判定に留める。
    """
    return issue.title.startswith("[EPIC] ") and PARENT_MARKER in issue.body


def _native_depends_on(
    issue: IssueRecord, issue_to_subtask_id: dict[int, str] | None
) -> tuple[str, ...]:
    if issue_to_subtask_id is None or not issue.blocked_by:
        return ()
    return tuple(
        issue_to_subtask_id[num]
        for num in issue.blocked_by
        if num in issue_to_subtask_id
    )


def _body_depends_on(match: re.Match | None, yaml_error: bool) -> tuple[str, ...]:
    if not match or yaml_error:
        return ()
    try:
        data = yaml.safe_load(match.group(1))
        if isinstance(data, dict):
            return tuple(str(d) for d in (data.get("depends_on") or []))
    except Exception:
        pass
    return ()


def _resolve_depends_on(
    issue: IssueRecord,
    issue_to_subtask_id: dict[int, str] | None,
    match: re.Match | None,
    yaml_error: bool,
) -> tuple[str, ...]:
    """#485 review (P1): a task can have some dependencies linked via native
    `blocked_by` and others not (e.g. one `set_blocked_by` call raised
    `RelationshipUnavailableError` after an earlier one succeeded for the
    same issue). Treating a non-empty native list as fully authoritative
    then silently drops the unlinked dependency, letting the task be
    promoted the moment only the linked one finishes. The body's
    `depends_on` is always the complete, authoritative list (rendered
    directly from `subtask.depends_on` at creation time), so union it in
    rather than discarding it whenever any native entry exists.
    """
    depends_on = _native_depends_on(issue, issue_to_subtask_id)
    for dep in _body_depends_on(match, yaml_error):
        if dep not in depends_on:
            depends_on += (dep,)
    return depends_on


def parse_task_from_issue(
    issue: IssueRecord,
    issue_to_subtask_id: dict[int, str] | None = None,
) -> Task:
    subtask_id = ""
    footprint: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
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

    depends_on = _resolve_depends_on(issue, issue_to_subtask_id, match, yaml_error)

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
