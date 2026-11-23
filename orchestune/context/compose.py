"""Assemble a bounded launch context from Forge text and the local index.

Tier 1 stays whole. Tier 2 drops from the tail. Forge and index failures
become an unavailable context instead of an exception.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import yaml

from orchestune.context.budget import (
    DEFAULT_CLOUD_PROMPT_MAX_BYTES,
    DEFAULT_LOCAL_CONTEXT_MAX_BYTES,
    DEFAULT_TIER2_LIMIT,
    DEFAULT_TIER2_MIN_SCORE,
    DEFAULT_TIER2_MIN_SCORE_RATIO,
    fit_context_budget,
)
from orchestune.context.contracts import (
    CONTEXT_OVER_BUDGET,
    CONTEXT_READY,
    CONTEXT_UNAVAILABLE,
    LAUNCH_CONTEXT_SCHEMA,
    REDUCTION_TIER1_EXCEEDS_BUDGET,
    REDUCTION_TIER2_TAIL_DROPPED,
    STATEMENTS_MARKER,
    TIER2_EMPTY,
    TIER2_READY,
    TIER2_REDUCED,
    TIER2_UNAVAILABLE,
    DependencyContext,
    LaunchContext,
    Tier1Context,
    Tier2Hit,
)
from orchestune.context.search import search_artifacts
from orchestune.context.storage import open_context_index
from orchestune.dependencies.resolution import resolve_task_dependencies
from orchestune.issue_parsing import (
    FOOTPRINT_BLOCK_PATTERN,
    decomposition_plan_from_parent_body,
    parse_task_from_issue,
)
from orchestune.models import IssueRecord, Task
from orchestune.outcome_record import (
    RESULT_DONE,
    RESULT_NOT_NEEDED,
    OutcomeRecord,
    find_child_outcome_record,
    parse_from_comments,
)

logger = logging.getLogger(__name__)

DEFAULT_INDEX_PATH = Path(".orchestune/context-index.sqlite3")
_CONFIRMED = frozenset({RESULT_DONE, RESULT_NOT_NEEDED})
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_PURPOSE = frozenset({"概要", "目的", "overview", "purpose"})
_ACCEPTANCE = frozenset(
    {
        "受け入れ",
        "受け入れ条件",
        "受け入れ基準",
        "受入条件",
        "受入基準",
        "acceptance",
        "acceptance criteria",
    }
)
_ACCEPTANCE_FALLBACK_HEADINGS = frozenset(
    {
        "期待される挙動",
        "期待される動作",
        "expected behavior",
        "完了条件",
        "definition of done",
        "設計案・実装アプローチ",
        "実装アプローチ",
        "変更内容",
        "proposed changes",
    }
)
_FOOTPRINT_HEADING = frozenset({"footprint"})
_FENCE = re.compile(r"(```|~~~).*?\1", re.DOTALL)
_CHECKLIST_LINE = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+.+$", re.MULTILINE)


class _Unavailable(Exception):
    """Tier 1 could not be read. Callers turn this into a status."""

    def __init__(self, reason: str = "unavailable") -> None:
        super().__init__(reason)
        self.reason = reason


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Cut ``text`` to a UTF-8 byte limit without splitting a character."""
    if max_bytes <= 0:
        return ""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def compose_launch_context(
    forge: object,
    issue_number: int,
    *,
    parent_issue_number: int | None = None,
    index_path: str | Path | None = None,
    tier2_limit: int = DEFAULT_TIER2_LIMIT,
    max_bytes: int = DEFAULT_LOCAL_CONTEXT_MAX_BYTES,
    repository: str | None = None,
    intro_bytes: int = 0,
    tier2_min_score: float = DEFAULT_TIER2_MIN_SCORE,
) -> LaunchContext:
    """Return Tier 1 plus as many Tier 2 hits as ``max_bytes`` allows.

    A Forge miss, a non-record payload, or an unexpected error yields
    ``context_status='unavailable'`` and does not raise.
    """
    try:
        return _compose_checked(
            forge,
            issue_number,
            parent_issue_number=parent_issue_number,
            index_path=index_path,
            tier2_limit=tier2_limit,
            max_bytes=max_bytes,
            repository=repository,
            intro_bytes=intro_bytes,
            tier2_min_score=tier2_min_score,
        )
    except _Unavailable as exc:
        return _unavailable_context(max_bytes, exc.reason)
    except Exception as exc:  # noqa: BLE001 - boundary: launch context must not escape
        logger.warning("launch context degraded to unavailable: %s", type(exc).__name__)
        return _unavailable_context(
            max_bytes, f"unexpected error: {type(exc).__name__}"
        )


def compose_for_dispatch(
    forge: object,
    issue_number: int,
    *,
    parent_issue_number: int | None = None,
    index_path: str | Path | None = None,
    tier2_limit: int = DEFAULT_TIER2_LIMIT,
    max_bytes: int = DEFAULT_LOCAL_CONTEXT_MAX_BYTES,
    repository: str | None = None,
    tier2_min_score: float = DEFAULT_TIER2_MIN_SCORE,
) -> LaunchContext | None:
    """Return a usable context, or None when Tier 1 could not be fetched."""
    composed = compose_launch_context(
        forge,
        issue_number,
        parent_issue_number=parent_issue_number,
        index_path=index_path,
        tier2_limit=tier2_limit,
        max_bytes=max_bytes,
        repository=repository,
        tier2_min_score=tier2_min_score,
    )
    if composed.context_status == CONTEXT_UNAVAILABLE:
        return None
    return composed


def format_cloud_context(
    launch_context: LaunchContext,
    *,
    max_bytes: int = DEFAULT_CLOUD_PROMPT_MAX_BYTES,
    base_prompt: str = "",
) -> str:
    """Format Tier 1 plus as many whole Tier 2 hits with provenance as fit."""
    if (
        launch_context.context_status == CONTEXT_UNAVAILABLE
        or launch_context.tier1 is None
    ):
        return ""
    prefix_bytes = _cloud_prefix_bytes(base_prompt)
    tier1_text = _cloud_tier1(launch_context.tier1)
    kept = list(launch_context.tier2)
    while True:
        body = _render_cloud_body(
            tier1_text,
            kept,
            over_budget=False,
        )
        if fit_context_budget(body, max_bytes=max_bytes - prefix_bytes).fits:
            return body
        if not kept:
            return _render_cloud_body(tier1_text, (), over_budget=True)
        kept.pop()


def append_cloud_context(
    prompt: str,
    launch_context: object | None,
    *,
    max_bytes: int = DEFAULT_CLOUD_PROMPT_MAX_BYTES,
) -> str:
    """Append bounded Tier 1/2/3 context after the existing cloud prompt."""
    if not isinstance(launch_context, LaunchContext) or launch_context.tier1 is None:
        return prompt
    if launch_context.context_status == CONTEXT_UNAVAILABLE:
        return prompt
    extra = format_cloud_context(
        launch_context, max_bytes=max_bytes, base_prompt=prompt
    )
    if not extra:
        return prompt
    if prompt.endswith("\n"):
        return f"{prompt}{extra}"
    return f"{prompt}\n{extra}"


def local_prompt_reference(relative_path: str) -> str:
    """Short prompt note that points at a worktree ``context.json``."""
    return f" 起動コンテキストは `{relative_path}` を参照してください。"


def fit_local_context(
    launch_context: LaunchContext,
    *,
    intro_bytes: int = 0,
) -> LaunchContext:
    """Re-fit an existing ``LaunchContext`` against actual local file + intro bytes."""
    if (
        launch_context.context_status == CONTEXT_UNAVAILABLE
        or launch_context.tier1 is None
    ):
        return launch_context
    search_state = (
        TIER2_UNAVAILABLE
        if launch_context.tier2_status == TIER2_UNAVAILABLE
        else TIER2_READY
    )
    prior_dropped = (
        launch_context.tier2_status == TIER2_REDUCED
        or launch_context.reduction_reason == REDUCTION_TIER2_TAIL_DROPPED
    )
    return _fit(
        launch_context.tier1,
        search_state,
        launch_context.tier2,
        launch_context.max_bytes,
        intro_bytes=intro_bytes,
        prior_dropped=prior_dropped,
    )


def local_context_document(launch_context: LaunchContext) -> dict[str, Any]:
    """JSON object written to ``context.json``, including the measured size."""
    document = _document_for(
        launch_context.context_status,
        launch_context.tier2_status,
        launch_context.reduction_reason,
        launch_context.tier1,
        launch_context.tier2,
    )
    stored = dict(document)
    stored["size_bytes"] = launch_context.size_bytes
    stored["max_bytes"] = launch_context.max_bytes
    if launch_context.unavailable_reason is not None:
        stored["unavailable_reason"] = launch_context.unavailable_reason
    return stored


def _compose_checked(
    forge: object,
    issue_number: int,
    *,
    parent_issue_number: int | None,
    index_path: str | Path | None,
    tier2_limit: int,
    max_bytes: int,
    repository: str | None,
    intro_bytes: int,
    tier2_min_score: float,
) -> LaunchContext:
    forge_repo = _forge_repository(forge)
    if repository is not None and forge_repo is not None and forge_repo != repository:
        raise _Unavailable(f"repository mismatch: {forge_repo} != {repository}")
    issue = _require_issue(forge, issue_number)
    if not isinstance(issue.body, str) or not issue.body.strip():
        raise _Unavailable(f"issue #{issue.number}: body missing")
    parent_number = _parent_number(issue, parent_issue_number)
    parent = _parent_issue(forge, parent_number)
    if parent is not None and (
        not isinstance(parent.body, str) or not parent.body.strip()
    ):
        raise _Unavailable(f"parent #{parent.number}: body missing")
    tier1 = _tier1_from(forge, issue, parent, parent_number)
    repo = repository or forge_repo
    search_state, hits = _search_tier2(
        issue,
        tier1,
        index_path,
        tier2_limit,
        repository=repo,
        min_score=tier2_min_score,
    )
    return _fit(tier1, search_state, hits, max_bytes, intro_bytes=intro_bytes)


def _require_issue(forge: object, issue_number: int) -> IssueRecord:
    if isinstance(issue_number, bool) or not isinstance(issue_number, int):
        raise _Unavailable("invalid issue number")
    loaded = _invoke(forge, "get_issue", issue_number)
    if not isinstance(loaded, IssueRecord):
        raise _Unavailable(f"issue #{issue_number}: not found")
    return loaded


def _parent_number(issue: IssueRecord, requested: int | None) -> int | None:
    if isinstance(requested, int) and not isinstance(requested, bool):
        return requested
    parent = issue.parent
    if not isinstance(parent, dict):
        return None
    number = parent.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    return number


def _parent_issue(forge: object, number: int | None) -> IssueRecord | None:
    if number is None:
        return None
    loaded = _invoke(forge, "get_issue", number)
    if not isinstance(loaded, IssueRecord):
        raise _Unavailable(f"parent #{number}: not found")
    return loaded


def _invoke(forge: object, name: str, number: int) -> object:
    method = getattr(forge, name, None)
    if not callable(method):
        raise _Unavailable(f"forge.{name} unavailable")
    try:
        return cast(object, method(number))
    except Exception as exc:  # noqa: BLE001 - boundary: forge reads degrade together
        raise _Unavailable(f"forge.{name}({number}) failed") from exc


def _forge_repository(forge: object) -> str | None:
    getter = getattr(forge, "get_repository_slug", None)
    if not callable(getter):
        return None
    try:
        slug = getter()
    except Exception:  # noqa: BLE001 - boundary: repo lookup must not escape
        return None
    if isinstance(slug, str) and "/" in slug:
        return slug
    return None


def _tier1_from(
    forge: object,
    issue: IssueRecord,
    parent: IssueRecord | None,
    parent_number: int | None,
) -> Tier1Context:
    _validate_raw_footprint_depends_on(issue)
    task = parse_task_from_issue(issue)
    if task.yaml_error:
        raise _Unavailable(f"issue #{issue.number}: invalid footprint yaml")
    _check_parent_plan_dependencies(issue, task, parent)
    parent_body = parent.body if parent is not None else ""
    return Tier1Context(
        purpose=_heading_section(issue.body, _PURPOSE),
        acceptance=_acceptance_section(issue.body),
        footprint=task.footprint,
        symbols=task.symbols,
        parent_purpose=_heading_section(parent_body, _PURPOSE),
        parent_acceptance=_acceptance_section(parent_body),
        dependencies=_resolve_dependencies(forge, issue, task, parent_number),
    )


def _validate_raw_footprint_depends_on(issue: IssueRecord) -> None:
    match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    if match is None:
        return
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise _Unavailable(f"issue #{issue.number}: invalid footprint yaml") from exc
    if isinstance(data, dict) and "depends_on" in data:
        _ref_tuple(data.get("depends_on"))


def _check_parent_plan_dependencies(
    issue: IssueRecord, task: Task, parent: IssueRecord | None
) -> None:
    if parent is None:
        return
    plan = decomposition_plan_from_parent_body(parent.body)
    if not isinstance(plan, dict):
        return
    subtasks = plan.get("subtasks")
    if not isinstance(subtasks, list):
        return
    for entry in subtasks:
        if not isinstance(entry, dict):
            continue
        if entry.get("issue_number") == issue.number or (
            task.subtask_id and entry.get("id") == task.subtask_id
        ):
            plan_deps = _ref_tuple(entry.get("depends_on"))
            if set(plan_deps) != set(task.depends_on):
                raise _Unavailable(
                    f"issue #{issue.number}: depends_on mismatch with parent plan"
                )
            return


def _ref_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _Unavailable("depends_on must be a list")
    refs: list[str] = []
    for item in value:
        if isinstance(item, bool):
            raise _Unavailable("depends_on contains bool")
        if isinstance(item, int):
            refs.append(str(item))
        elif isinstance(item, str) and item.strip():
            refs.append(item.strip())
        else:
            raise _Unavailable("depends_on contains invalid item")
    return tuple(refs)


def _acceptance_section(body: str) -> str:
    if not isinstance(body, str) or not body:
        return ""
    primary = _heading_section(body, _ACCEPTANCE)
    if primary:
        return primary
    fallback = _heading_section(body, _ACCEPTANCE_FALLBACK_HEADINGS)
    if fallback:
        return fallback
    return _checklist_lines(_body_without_fences_and_footprint(body))


def _body_without_fences_and_footprint(body: str) -> str:
    return _drop_heading_section(_FENCE.sub("", body), _FOOTPRINT_HEADING)


def _drop_heading_section(body: str, names: frozenset[str]) -> str:
    matches = list(_HEADING.finditer(body))
    for index, match in enumerate(matches):
        if not _heading_matches(_heading_keys(match.group(2)), names):
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[: match.start()] + body[end:]
    return body


def _checklist_lines(body: str) -> str:
    lines = (match.group(0).strip() for match in _CHECKLIST_LINE.finditer(body))
    return "\n".join(lines)


def _heading_section(body: str, names: frozenset[str]) -> str:
    if not isinstance(body, str):
        return ""
    matches = list(_HEADING.finditer(body))
    for index, match in enumerate(matches):
        if not _heading_matches(_heading_keys(match.group(2)), names):
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[start:end].strip()
    return ""


def _heading_matches(keys: frozenset[str], names: frozenset[str]) -> bool:
    if not keys.isdisjoint(names):
        return True
    return any(key.endswith(name) for key in keys for name in names)


def _heading_keys(title: str) -> frozenset[str]:
    folded = title.strip().casefold().rstrip(":：")
    keys = {folded}
    if folded.endswith(")") and " (" in folded:
        left, right = folded.rsplit(" (", 1)
        keys.add(left.strip())
        keys.add(right[:-1].strip())
    return frozenset(keys)


def _with_numeric_body_dependencies(task: Task) -> Task:
    numeric = tuple(int(ref) for ref in task.depends_on if ref.isdigit())
    if not numeric:
        return task
    remaining = tuple(ref for ref in task.depends_on if not ref.isdigit())
    merged_native = tuple(dict.fromkeys((*task.native_depends_on, *numeric)))
    return replace(task, depends_on=remaining, native_depends_on=merged_native)


def _resolve_dependencies(
    forge: object,
    issue: IssueRecord,
    task: Task,
    parent_number: int | None,
) -> tuple[DependencyContext, ...]:
    if not task.depends_on and not task.native_depends_on:
        return ()
    scoped_task = (
        replace(task, parent_number=parent_number)
        if parent_number is not None and task.parent_number is None
        else task
    )
    scoped_task = _with_numeric_body_dependencies(scoped_task)
    tasks_by_issue: dict[int, Task] = {issue.number: scoped_task}
    for child in _children(forge, parent_number, scoped_task):
        child_task = parse_task_from_issue(child)
        if parent_number is not None and child_task.parent_number is None:
            child_task = replace(child_task, parent_number=parent_number)
        tasks_by_issue[child.number] = child_task
    _load_numeric_dependencies(forge, scoped_task, tasks_by_issue)
    resolved = resolve_task_dependencies(scoped_task, tasks_by_issue)
    if resolved.unresolved:
        first = resolved.unresolved[0]
        raise _Unavailable(f"dependency {first.raw}: {first.reason}")
    return tuple(
        _dependency_context(forge, dep_num, tasks_by_issue[dep_num])
        for dep_num in resolved.resolved
    )


def _children(
    forge: object, parent_number: int | None, task: Task
) -> tuple[IssueRecord, ...]:
    if parent_number is None:
        return ()
    if not any(not ref.isdigit() for ref in task.depends_on):
        return ()
    loaded = _invoke(forge, "list_sub_issues", parent_number)
    if not isinstance(loaded, list) or any(
        not isinstance(item, IssueRecord) for item in loaded
    ):
        raise _Unavailable(f"parent #{parent_number}: list_sub_issues failed")
    return tuple(loaded)


def _load_numeric_dependencies(
    forge: object, task: Task, tasks_by_issue: dict[int, Task]
) -> None:
    needed: list[int] = list(task.native_depends_on)
    for ref in task.depends_on:
        if ref.isdigit():
            needed.append(int(ref))
    for number in needed:
        if number in tasks_by_issue:
            continue
        loaded = _invoke(forge, "get_issue", number)
        if not isinstance(loaded, IssueRecord):
            raise _Unavailable(f"dependency #{number}: issue unavailable")
        tasks_by_issue[number] = parse_task_from_issue(loaded)


def _dependency_context(
    forge: object, dep_number: int, dep_task: Task
) -> DependencyContext:
    comments = _comments(forge, dep_number)
    record = find_child_outcome_record(comments, dep_number)
    if record is None:
        raise _Unavailable(f"dependency #{dep_number}: confirmed outcome absent")
    if record.result not in _CONFIRMED:
        raise _Unavailable(
            f"dependency #{dep_number}: outcome {record.result} not confirmed"
        )
    summary = _summary_for_record(comments, dep_number, record)
    subtask_id = dep_task.subtask_id or None
    return DependencyContext(
        issue_number=dep_number,
        subtask_id=subtask_id,
        result=record.result,
        summary=summary,
        pr=record.pr,
        reason=record.reason,
        base_sha=record.base_sha,
        head_sha=record.head_sha,
        claim_id=record.claim_id,
        completion_id=record.completion_id,
        attempt=record.attempt,
        ci=record.ci,
        review_verdict=record.review.verdict,
    )


def _comments(forge: object, number: int) -> list[Mapping[str, Any]]:
    loaded = _invoke(forge, "list_comments", number)
    if not isinstance(loaded, list):
        raise _Unavailable(f"dependency #{number}: comments unavailable")
    comments: list[Mapping[str, Any]] = []
    for item in loaded:
        if not isinstance(item, Mapping):
            raise _Unavailable(f"dependency #{number}: invalid comment payload")
        comments.append(item)
    return comments


def _summary_for_record(
    comments: list[Mapping[str, Any]],
    dep_number: int,
    target: OutcomeRecord,
) -> str | None:
    best_at: str | None = None
    best_summary: str | None = None
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        parsed = parse_from_comments((comment,))
        if parsed is None or parsed.issue != dep_number or parsed != target:
            continue
        created = _created_at(comment)
        if best_at is None or created >= best_at:
            best_at = created
            best_summary = _summary_from_body(body)
    return best_summary


def _created_at(comment: Mapping[str, Any]) -> str:
    created = comment.get("created_at") or comment.get("createdAt") or ""
    if isinstance(created, str):
        return created
    return ""


def _summary_from_body(body: str) -> str | None:
    start = body.find(STATEMENTS_MARKER)
    if start < 0:
        return None
    rest = body[start + len(STATEMENTS_MARKER) :]
    fence = rest.find("```json")
    if fence < 0:
        return None
    content_start = fence + len("```json")
    fence_end = rest.find("```", content_start)
    if fence_end < 0:
        return None
    try:
        payload = json.loads(rest[content_start:fence_end])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary")
    if isinstance(summary, str):
        return summary
    return None


def _search_tier2(
    issue: IssueRecord,
    tier1: Tier1Context,
    index_path: str | Path | None,
    limit: int,
    *,
    repository: str | None,
    min_score: float,
) -> tuple[str, tuple[Tier2Hit, ...]]:
    path = DEFAULT_INDEX_PATH if index_path is None else Path(index_path)
    try:
        view = open_context_index(path)
        if view.status != "ready":
            return TIER2_UNAVAILABLE, ()
        candidates = _filter_by_repository(view.artifacts, repository)
        if candidates is None:
            return TIER2_UNAVAILABLE, ()
        found = search_artifacts(
            _exclude_self_issue(candidates, issue.number),
            _query(issue, tier1),
            limit=limit,
            min_score=min_score,
            min_score_ratio=DEFAULT_TIER2_MIN_SCORE_RATIO,
        )
        return TIER2_READY, tuple(_hit(record) for record in found)
    except Exception as exc:  # noqa: BLE001 - boundary: index search keeps Tier 1
        logger.warning("tier2 search unavailable: %s", type(exc).__name__)
        return TIER2_UNAVAILABLE, ()


def _exclude_self_issue(
    artifacts: tuple[Any, ...], issue_number: int
) -> tuple[Any, ...]:
    return tuple(
        record
        for record in artifacts
        if getattr(getattr(record, "artifact", record), "issue_number", None)
        != issue_number
    )


def _filter_by_repository(
    artifacts: tuple[Any, ...], repository: str | None
) -> tuple[Any, ...] | None:
    if repository is not None:
        return tuple(
            record
            for record in artifacts
            if getattr(getattr(record, "artifact", record), "repository", None)
            == repository
        )
    repos = {
        getattr(getattr(record, "artifact", record), "repository", None)
        for record in artifacts
    }
    if len(repos) > 1:
        return None
    return artifacts


def _query(issue: IssueRecord, tier1: Tier1Context) -> str:
    return " ".join((issue.title, tier1.purpose, " ".join(tier1.symbols)))


def _hit(record: object) -> Tier2Hit:
    artifact = getattr(record, "artifact", record)
    evidence = getattr(artifact, "evidence_refs", None)
    summary = getattr(artifact, "summary", None)
    source_url = getattr(artifact, "source_url", None)
    stale = getattr(artifact, "stale_status", None)
    stale_text = getattr(stale, "value", stale)
    superseded_raw = getattr(artifact, "superseded_by", ()) or ()
    superseded = tuple(_format_artifact_key(item) for item in superseded_raw)
    attempt = getattr(artifact, "attempt", None)
    return Tier2Hit(
        issue_number=int(getattr(artifact, "issue_number", 0)),
        title=str(getattr(record, "title", "")),
        summary=summary if isinstance(summary, str) else None,
        source_url=source_url if isinstance(source_url, str) else None,
        pr_url=_optional_text(getattr(evidence, "pr_url", None)),
        comment_url=_optional_text(getattr(evidence, "comment_url", None)),
        target_commit=_optional_text(getattr(artifact, "target_commit", None)),
        recorded_at=_optional_text(getattr(artifact, "recorded_at", None)),
        attempt=attempt
        if isinstance(attempt, int) and not isinstance(attempt, bool)
        else None,
        superseded_by=superseded,
        stale_status=stale_text if isinstance(stale_text, str) and stale_text else None,
    )


def _format_artifact_key(key: object) -> str:
    repo = getattr(key, "repository", "")
    issue = getattr(key, "issue_number", "")
    comment = getattr(key, "source_comment_id", "")
    return f"{repo}#{issue}:{comment}"


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _measure_local_bytes(
    document: dict[str, Any], max_bytes: int, intro_bytes: int
) -> int:
    size = 0
    for _ in range(3):
        stored = dict(document)
        stored["size_bytes"] = size
        stored["max_bytes"] = max_bytes
        rendered = json.dumps(stored, ensure_ascii=False, indent=2) + "\n"
        measured = len(rendered.encode("utf-8")) + intro_bytes
        if measured == size:
            return measured
        size = measured
    return size


def _fit(
    tier1: Tier1Context,
    search_state: str,
    hits: tuple[Tier2Hit, ...],
    max_bytes: int,
    *,
    intro_bytes: int = 0,
    prior_dropped: bool = False,
) -> LaunchContext:
    kept = list(hits)
    dropped = prior_dropped
    while True:
        status, tier2_status, reason = _statuses(
            search_state, hits, kept, dropped, over=False
        )
        document = _document_for(status, tier2_status, reason, tier1, kept)
        size = _measure_local_bytes(document, max_bytes, intro_bytes)
        if size <= max_bytes:
            return _context(status, tier2_status, reason, tier1, kept, size, max_bytes)
        if search_state != TIER2_READY or not kept:
            return _over_budget(
                tier1, search_state, hits, dropped, max_bytes, intro_bytes
            )
        kept.pop()
        dropped = True


def _over_budget(
    tier1: Tier1Context,
    search_state: str,
    hits: tuple[Tier2Hit, ...],
    dropped: bool,
    max_bytes: int,
    intro_bytes: int,
) -> LaunchContext:
    _status, tier2_status, _reason = _statuses(
        search_state, hits, [], dropped, over=True
    )
    document = _document_for(
        CONTEXT_OVER_BUDGET,
        tier2_status,
        REDUCTION_TIER1_EXCEEDS_BUDGET,
        tier1,
        [],
    )
    size = _measure_local_bytes(document, max_bytes, intro_bytes)
    return _context(
        CONTEXT_OVER_BUDGET,
        tier2_status,
        REDUCTION_TIER1_EXCEEDS_BUDGET,
        tier1,
        [],
        size,
        max_bytes,
    )


def _statuses(
    search_state: str,
    original: tuple[Tier2Hit, ...],
    kept: list[Tier2Hit],
    dropped: bool,
    *,
    over: bool,
) -> tuple[str, str, str | None]:
    tier2_status = _tier2_status(search_state, original, dropped)
    if over:
        return CONTEXT_OVER_BUDGET, tier2_status, REDUCTION_TIER1_EXCEEDS_BUDGET
    if dropped or len(kept) < len(original):
        return CONTEXT_READY, tier2_status, REDUCTION_TIER2_TAIL_DROPPED
    return CONTEXT_READY, tier2_status, None


def _tier2_status(
    search_state: str, original: tuple[Tier2Hit, ...], dropped: bool
) -> str:
    if search_state != TIER2_READY:
        return TIER2_UNAVAILABLE
    if not original:
        return TIER2_EMPTY
    if dropped:
        return TIER2_REDUCED
    return TIER2_READY


def _context(
    status: str,
    tier2_status: str,
    reason: str | None,
    tier1: Tier1Context | None,
    hits: list[Tier2Hit],
    size_bytes: int,
    max_bytes: int,
    unavailable_reason: str | None = None,
) -> LaunchContext:
    kept = tuple(hits)
    return LaunchContext(
        schema_version=LAUNCH_CONTEXT_SCHEMA,
        context_status=status,
        tier2_status=tier2_status,
        reduction_reason=reason,
        tier1=tier1,
        tier2=kept,
        tier3_urls=tuple(_tier3(kept)),
        size_bytes=size_bytes,
        max_bytes=max_bytes,
        unavailable_reason=unavailable_reason,
    )


def _unavailable_context(max_bytes: int, reason: str | None = None) -> LaunchContext:
    document = _document_for(CONTEXT_UNAVAILABLE, TIER2_UNAVAILABLE, None, None, [])
    if reason is not None:
        document["unavailable_reason"] = reason
    size = _measure_local_bytes(document, max_bytes, 0)
    return _context(
        CONTEXT_UNAVAILABLE,
        TIER2_UNAVAILABLE,
        None,
        None,
        [],
        size,
        max_bytes,
        unavailable_reason=reason,
    )


def _document_for(
    status: str,
    tier2_status: str,
    reason: str | None,
    tier1: Tier1Context | None,
    hits: list[Tier2Hit] | tuple[Tier2Hit, ...],
) -> dict[str, Any]:
    kept = tuple(hits)
    return {
        "schema_version": LAUNCH_CONTEXT_SCHEMA,
        "context_status": status,
        "tier2_status": tier2_status,
        "reduction_reason": reason,
        "tier1": _tier1_payload(tier1),
        "tier2": [_tier2_payload(hit) for hit in kept],
        "tier3_urls": _tier3(kept),
    }


def _tier1_payload(tier1: Tier1Context | None) -> dict[str, Any] | None:
    if tier1 is None:
        return None
    return {
        "purpose": tier1.purpose,
        "acceptance": tier1.acceptance,
        "footprint": list(tier1.footprint),
        "symbols": list(tier1.symbols),
        "parent_purpose": tier1.parent_purpose,
        "parent_acceptance": tier1.parent_acceptance,
        "dependencies": [_dependency_payload(item) for item in tier1.dependencies],
    }


def _dependency_payload(item: DependencyContext) -> dict[str, Any]:
    return {
        "issue_number": item.issue_number,
        "subtask_id": item.subtask_id,
        "result": item.result,
        "summary": item.summary,
        "pr": item.pr,
        "reason": item.reason,
        "base_sha": item.base_sha,
        "head_sha": item.head_sha,
        "claim_id": item.claim_id,
        "completion_id": item.completion_id,
        "attempt": item.attempt,
        "ci": item.ci,
        "review_verdict": item.review_verdict,
    }


def _tier2_payload(hit: Tier2Hit) -> dict[str, Any]:
    return {
        "issue_number": hit.issue_number,
        "title": hit.title,
        "summary": hit.summary,
        "source_url": hit.source_url,
        "pr_url": hit.pr_url,
        "comment_url": hit.comment_url,
        "target_commit": hit.target_commit,
        "recorded_at": hit.recorded_at,
        "attempt": hit.attempt,
        "superseded_by": list(hit.superseded_by),
        "stale_status": hit.stale_status,
    }


def _tier3(hits: tuple[Tier2Hit, ...]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        for url in (hit.source_url, hit.pr_url, hit.comment_url):
            if isinstance(url, str) and url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _cloud_prefix_bytes(base_prompt: str) -> int:
    if not base_prompt:
        return 0
    sep = 0 if base_prompt.endswith("\n") else 1
    return len(base_prompt.encode("utf-8")) + sep


def _render_cloud_body(
    tier1_text: str,
    hits: list[Tier2Hit] | tuple[Tier2Hit, ...],
    *,
    over_budget: bool,
) -> str:
    parts = [tier1_text]
    if over_budget:
        parts.append(
            f"context_status={CONTEXT_OVER_BUDGET} reduction_reason={REDUCTION_TIER1_EXCEEDS_BUDGET}"
        )
    for hit in hits:
        parts.append(_cloud_hit_block(hit))
    return "\n".join(parts)


def _cloud_hit_block(hit: Tier2Hit) -> str:
    superseded = ",".join(hit.superseded_by) if hit.superseded_by else "none"
    attempt = str(hit.attempt) if hit.attempt is not None else "none"
    header = f"関連: #{hit.issue_number} {hit.title} {hit.summary or ''}".rstrip()
    provenance = (
        f"出典: source_url={hit.source_url or 'none'} "
        f"pr_url={hit.pr_url or 'none'} "
        f"comment_url={hit.comment_url or 'none'} "
        f"target_commit={hit.target_commit or 'none'} "
        f"recorded_at={hit.recorded_at or 'none'} "
        f"attempt={attempt} "
        f"superseded_by={superseded} "
        f"stale_status={hit.stale_status or 'none'}"
    )
    return f"{header}\n{provenance}"


def _cloud_tier1(tier1: Tier1Context) -> str:
    lines = [
        "目的:",
        tier1.purpose,
        "受け入れ:",
        tier1.acceptance,
        "footprint:",
        " ".join(tier1.footprint),
        "symbols:",
        " ".join(tier1.symbols),
        "親の目的:",
        tier1.parent_purpose,
        "親の受け入れ:",
        tier1.parent_acceptance,
    ]
    lines.extend(_cloud_dependency_lines(tier1))
    return "\n".join(lines)


def _cloud_dependency_lines(tier1: Tier1Context) -> list[str]:
    lines: list[str] = []
    for dep in tier1.dependencies:
        label = dep.subtask_id or str(dep.issue_number or "")
        lines.append(
            f"依存: {label} result={dep.result or ''} summary={dep.summary or ''}"
        )
    return lines
