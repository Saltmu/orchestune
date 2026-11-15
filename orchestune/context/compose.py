"""Assemble a bounded launch context from Forge text and the local index.

Tier 1 stays whole. Tier 2 drops from the tail. Forge and index failures
become an unavailable context instead of an exception.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

from orchestune.context.budget import (
    DEFAULT_CLOUD_PROMPT_MAX_BYTES,
    DEFAULT_LOCAL_CONTEXT_MAX_BYTES,
    DEFAULT_TIER2_LIMIT,
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
from orchestune.models import IssueRecord
from orchestune.outcome_record import (
    RESULT_DONE,
    RESULT_NOT_NEEDED,
    OutcomeRecord,
    parse_from_comments,
)

logger = logging.getLogger(__name__)

DEFAULT_INDEX_PATH = Path(".orchestune/context-index.sqlite3")
_CONFIRMED = frozenset({RESULT_DONE, RESULT_NOT_NEEDED})
_YAML_FENCE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_PURPOSE = frozenset({"概要", "目的", "overview", "purpose"})
_ACCEPTANCE = frozenset(
    {"受け入れ", "受け入れ条件", "acceptance", "acceptance criteria"}
)


class _Unavailable(Exception):
    """Tier 1 could not be read. Callers turn this into a status."""


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
        )
    except _Unavailable:
        return _unavailable_context(max_bytes)
    except Exception as exc:  # noqa: BLE001 - boundary: launch context must not escape
        logger.warning("launch context degraded to unavailable: %s", type(exc).__name__)
        return _unavailable_context(max_bytes)


def compose_for_dispatch(
    forge: object,
    issue_number: int,
    *,
    parent_issue_number: int | None = None,
    index_path: str | Path | None = None,
    tier2_limit: int = DEFAULT_TIER2_LIMIT,
    max_bytes: int = DEFAULT_LOCAL_CONTEXT_MAX_BYTES,
) -> LaunchContext | None:
    """Return a usable context, or None when Tier 1 could not be fetched."""
    composed = compose_launch_context(
        forge,
        issue_number,
        parent_issue_number=parent_issue_number,
        index_path=index_path,
        tier2_limit=tier2_limit,
        max_bytes=max_bytes,
    )
    if composed.context_status == CONTEXT_UNAVAILABLE:
        return None
    return composed


def format_cloud_context(
    launch_context: LaunchContext, *, max_bytes: int = DEFAULT_CLOUD_PROMPT_MAX_BYTES
) -> str:
    """Render Tier 1 whole, then fill leftover bytes with Tier 2 and URLs."""
    if launch_context.tier1 is None:
        return ""
    tier1 = _cloud_tier1(launch_context.tier1)
    if len(tier1.encode("utf-8")) > max_bytes:
        return tier1
    extra = _cloud_extra(launch_context, max_bytes - len(tier1.encode("utf-8")))
    if not extra:
        return tier1
    return f"{tier1}\n{extra}"


def append_cloud_context(
    prompt: str,
    launch_context: object | None,
    *,
    max_bytes: int = DEFAULT_CLOUD_PROMPT_MAX_BYTES,
) -> str:
    """Append a cloud context block. ``None`` leaves ``prompt`` unchanged."""
    if not isinstance(launch_context, LaunchContext) or launch_context.tier1 is None:
        return prompt
    if launch_context.context_status == CONTEXT_UNAVAILABLE:
        return prompt
    extra = format_cloud_context(launch_context, max_bytes=max_bytes)
    if not extra:
        return prompt
    if prompt.endswith("\n"):
        return f"{prompt}{extra}"
    return f"{prompt}\n{extra}"


def local_prompt_reference(relative_path: str) -> str:
    """Short prompt note that points at a worktree ``context.json``."""
    return f" 起動コンテキストは `{relative_path}` を参照してください。"


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
    return stored


def _compose_checked(
    forge: object,
    issue_number: int,
    *,
    parent_issue_number: int | None,
    index_path: str | Path | None,
    tier2_limit: int,
    max_bytes: int,
) -> LaunchContext:
    issue = _require_issue(forge, issue_number)
    parent_number = _parent_number(issue, parent_issue_number)
    parent = _parent_issue(forge, parent_number)
    tier1 = _tier1_from(forge, issue, parent, parent_number)
    search_state, hits = _search_tier2(issue, tier1, index_path, tier2_limit)
    return _fit(tier1, search_state, hits, max_bytes)


def _require_issue(forge: object, issue_number: int) -> IssueRecord:
    if isinstance(issue_number, bool) or not isinstance(issue_number, int):
        raise _Unavailable
    loaded = _invoke(forge, "get_issue", issue_number)
    if not isinstance(loaded, IssueRecord):
        raise _Unavailable
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
        raise _Unavailable
    return loaded


def _invoke(forge: object, name: str, number: int) -> object:
    method = getattr(forge, name, None)
    if not callable(method):
        raise _Unavailable
    try:
        return cast(object, method(number))
    except Exception as exc:  # noqa: BLE001 - boundary: forge reads degrade together
        raise _Unavailable from exc


def _tier1_from(
    forge: object,
    issue: IssueRecord,
    parent: IssueRecord | None,
    parent_number: int | None,
) -> Tier1Context:
    loaded = _yaml_mapping(issue.body) or {}
    parent_body = parent.body if parent is not None else ""
    return Tier1Context(
        purpose=_heading_section(issue.body, _PURPOSE),
        acceptance=_heading_section(issue.body, _ACCEPTANCE),
        footprint=_string_list(loaded.get("footprint")),
        symbols=_string_list(loaded.get("symbols")),
        parent_purpose=_heading_section(parent_body, _PURPOSE),
        parent_acceptance=_heading_section(parent_body, _ACCEPTANCE),
        dependencies=_resolve_dependencies(
            forge, parent_number, _dependency_refs(issue.body)
        ),
    )


def _yaml_mapping(body: str) -> dict[str, Any] | None:
    if not isinstance(body, str):
        return None
    match = _YAML_FENCE.search(body)
    if match is None:
        return None
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


def _string_list(value: object) -> tuple[str, ...]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def _dependency_refs(body: str) -> tuple[str, ...]:
    loaded = _yaml_mapping(body)
    if loaded is None:
        return ()
    return _ref_tuple(loaded.get("depends_on"))


def _ref_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _Unavailable
    refs: list[str] = []
    for item in value:
        if isinstance(item, bool):
            raise _Unavailable
        if isinstance(item, int):
            refs.append(str(item))
        elif isinstance(item, str) and item.strip():
            refs.append(item.strip())
        else:
            raise _Unavailable
    return tuple(refs)


def _heading_section(body: str, names: frozenset[str]) -> str:
    if not isinstance(body, str):
        return ""
    matches = list(_HEADING.finditer(body))
    for index, match in enumerate(matches):
        if _heading_keys(match.group(2)).isdisjoint(names):
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[start:end].strip()
    return ""


def _heading_keys(title: str) -> frozenset[str]:
    folded = title.strip().casefold().rstrip(":：")
    keys = {folded}
    if folded.endswith(")") and " (" in folded:
        left, right = folded.rsplit(" (", 1)
        keys.add(left.strip())
        keys.add(right[:-1].strip())
    return frozenset(keys)


def _resolve_dependencies(
    forge: object, parent_number: int | None, refs: tuple[str, ...]
) -> tuple[DependencyContext, ...]:
    if not refs:
        return ()
    children = _children(forge, parent_number, refs)
    return tuple(
        _dependency_context(forge, _dependency_issue(forge, ref, children), ref)
        for ref in refs
    )


def _children(
    forge: object, parent_number: int | None, refs: tuple[str, ...]
) -> tuple[IssueRecord, ...]:
    if parent_number is None or all(ref.isdigit() for ref in refs):
        return ()
    loaded = _invoke(forge, "list_sub_issues", parent_number)
    if not isinstance(loaded, list) or any(
        not isinstance(item, IssueRecord) for item in loaded
    ):
        raise _Unavailable
    return tuple(loaded)


def _dependency_issue(
    forge: object, ref: str, children: tuple[IssueRecord, ...]
) -> IssueRecord:
    if ref.isdigit():
        loaded = _invoke(forge, "get_issue", int(ref))
        if isinstance(loaded, IssueRecord):
            return loaded
        raise _Unavailable
    for child in children:
        if _subtask_id(child.body) == ref:
            return child
    raise _Unavailable


def _subtask_id(body: str) -> str | None:
    loaded = _yaml_mapping(body)
    if loaded is None:
        return None
    value = loaded.get("subtask_id")
    if isinstance(value, str):
        return value
    return None


def _dependency_context(
    forge: object, issue: IssueRecord, ref: str
) -> DependencyContext:
    found = _confirmed(_comments(forge, issue.number))
    record = None if found is None else found[0]
    summary = None if found is None else found[1]
    subtask_id = _subtask_id(issue.body)
    if subtask_id is None and not ref.isdigit():
        subtask_id = ref
    return _dependency_from_record(issue.number, subtask_id, record, summary)


def _dependency_from_record(
    issue_number: int,
    subtask_id: str | None,
    record: OutcomeRecord | None,
    summary: str | None,
) -> DependencyContext:
    if record is None:
        return DependencyContext(issue_number=issue_number, subtask_id=subtask_id)
    return DependencyContext(
        issue_number=issue_number,
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
        raise _Unavailable
    comments: list[Mapping[str, Any]] = []
    for item in loaded:
        if not isinstance(item, Mapping):
            raise _Unavailable
        comments.append(item)
    return comments


def _confirmed(
    comments: list[Mapping[str, Any]],
) -> tuple[OutcomeRecord, str | None] | None:
    best_at: str | None = None
    best: tuple[OutcomeRecord, str | None] | None = None
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        record = parse_from_comments((comment,))
        if record is None or record.result not in _CONFIRMED:
            continue
        created = _created_at(comment)
        if best_at is None or created >= best_at:
            best_at = created
            best = (record, _summary_from_body(body))
    return best


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
) -> tuple[str, tuple[Tier2Hit, ...]]:
    path = DEFAULT_INDEX_PATH if index_path is None else Path(index_path)
    try:
        view = open_context_index(path)
        if view.status != "ready":
            return TIER2_UNAVAILABLE, ()
        found = search_artifacts(view.artifacts, _query(issue, tier1), limit=limit)
        return TIER2_READY, tuple(_hit(record) for record in found)
    except Exception as exc:  # noqa: BLE001 - boundary: index search keeps Tier 1
        logger.warning("tier2 search unavailable: %s", type(exc).__name__)
        return TIER2_UNAVAILABLE, ()


def _query(issue: IssueRecord, tier1: Tier1Context) -> str:
    return " ".join((issue.title, tier1.purpose, " ".join(tier1.symbols)))


def _hit(record: object) -> Tier2Hit:
    artifact = getattr(record, "artifact", record)
    evidence = getattr(artifact, "evidence_refs", None)
    summary = getattr(artifact, "summary", None)
    source_url = getattr(artifact, "source_url", None)
    return Tier2Hit(
        issue_number=int(getattr(artifact, "issue_number", 0)),
        title=str(getattr(record, "title", "")),
        summary=summary if isinstance(summary, str) else None,
        source_url=source_url if isinstance(source_url, str) else None,
        pr_url=_optional_text(getattr(evidence, "pr_url", None)),
        comment_url=_optional_text(getattr(evidence, "comment_url", None)),
    )


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _fit(
    tier1: Tier1Context,
    search_state: str,
    hits: tuple[Tier2Hit, ...],
    max_bytes: int,
) -> LaunchContext:
    kept = list(hits)
    dropped = False
    while True:
        status, tier2_status, reason = _statuses(
            search_state, hits, kept, dropped, over=False
        )
        document = _document_for(status, tier2_status, reason, tier1, kept)
        fit = fit_context_budget(document, max_bytes=max_bytes)
        if fit.fits:
            return _context(
                status, tier2_status, reason, tier1, kept, fit.size_bytes, max_bytes
            )
        if search_state != TIER2_READY or not kept:
            return _over_budget(tier1, search_state, hits, dropped, max_bytes)
        kept.pop()
        dropped = True


def _over_budget(
    tier1: Tier1Context,
    search_state: str,
    hits: tuple[Tier2Hit, ...],
    dropped: bool,
    max_bytes: int,
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
    size = fit_context_budget(document, max_bytes=max_bytes).size_bytes
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
    )


def _unavailable_context(max_bytes: int) -> LaunchContext:
    document = _document_for(CONTEXT_UNAVAILABLE, TIER2_UNAVAILABLE, None, None, [])
    size = fit_context_budget(document, max_bytes=max_bytes).size_bytes
    return _context(
        CONTEXT_UNAVAILABLE, TIER2_UNAVAILABLE, None, None, [], size, max_bytes
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


def _cloud_extra(context: LaunchContext, remaining: int) -> str:
    if remaining <= 1:
        return ""
    budget = remaining - 1
    tier2 = truncate_utf8(_tier2_block(context), budget)
    if tier2 and len(tier2.encode("utf-8")) >= budget:
        return tier2
    return _join_tier2_and_urls(tier2, context.tier3_urls, budget)


def _join_tier2_and_urls(tier2: str, urls: tuple[str, ...], budget: int) -> str:
    used = len(tier2.encode("utf-8"))
    separator = 1 if tier2 else 0
    fitted = _fit_urls(urls, budget - used - separator)
    if tier2 and fitted:
        return f"{tier2}\n{fitted}"
    return tier2 or fitted


def _tier2_block(context: LaunchContext) -> str:
    return "\n".join(
        f"関連: #{hit.issue_number} {hit.title} {hit.summary or ''}".rstrip()
        for hit in context.tier2
    )


def _fit_urls(urls: tuple[str, ...], max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    kept: list[str] = []
    for url in urls:
        candidate = url if not kept else "\n".join((*kept, url))
        if len(candidate.encode("utf-8")) > max_bytes:
            break
        kept.append(url)
    return "\n".join(kept)
