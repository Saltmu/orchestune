"""Read-only PoC selector CLI for planning-history-cards-v1 (Issue #1873)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestune.context.budget import fit_context_budget
from orchestune.context.search import search_artifacts

CORPUS_SCHEMA_VERSION = "planning-history-corpus-v1"
QUERY_SCHEMA_VERSION = "planning-history-query-v1"
CARDS_SCHEMA_VERSION = "planning-history-cards-v1"
DEFAULT_LIMIT = 3
DEFAULT_MAX_BYTES = 12288

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_CORPUS_KEYS = frozenset({"schema_version", "repository", "cases"})
_CASE_KEYS = frozenset(
    {
        "case_id",
        "parent_issue",
        "plan_revision_digest",
        "title",
        "purpose",
        "acceptance_criteria",
        "tasks",
        "lineage_issue_numbers",
        "pr_numbers",
        "sources",
        "available_at",
    }
)
_TASK_KEYS = frozenset(
    {
        "subtask_id",
        "issue_number",
        "description",
        "depends_on",
        "footprint",
        "symbols",
        "result",
        "missing_reason",
        "source_ids",
    }
)
_SOURCE_KEYS = frozenset({"source_id", "kind", "url", "digest", "available_at"})
_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "repository",
        "as_of",
        "query",
        "exclude_issue_numbers",
        "exclude_pr_numbers",
    }
)
_VALID_RESULTS = frozenset({"done", "not-needed", "blocked", "unknown"})
_OUTCOME_RESULTS = frozenset({"done", "not-needed", "blocked"})
_VALID_SOURCE_KINDS = frozenset({"plan", "task", "outcome", "pr"})


class ValidationError(ValueError):
    """Raised when CLI inputs, schemas, invariants, or envelope budget fail."""


class FileIOError(OSError):
    """Raised when reading an input file fails."""


@dataclass(frozen=True, slots=True)
class SelectResult:
    """Deterministic selection payload and exact UTF-8 rendered bytes."""

    payload: dict[str, Any]
    rendered_bytes: bytes


@dataclass(frozen=True, slots=True)
class _SearchDocument:
    """Adapter record passed to ``orchestune.context.search.search_artifacts``."""

    title: str
    description: str
    summary: None
    task_kind: str
    footprint: tuple[str, ...]
    symbols: tuple[str, ...]
    issue_number: int
    source_comment_id: str
    completion_id: None
    case: dict[str, Any]


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValidationError(f"Duplicate JSON key: {key!r}")
        obj[key] = value
    return obj


def _reject_constant(token: str) -> Any:
    raise ValidationError(f"Invalid JSON constant: {token}")


def _load_strict_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FileIOError(f"Failed to read {label} '{path}': {exc}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValidationError(f"{label} must be UTF-8 without BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} is not valid UTF-8: {exc}") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError(f"{label} top-level must be a JSON object")
    return parsed, digest


def _require_keys(obj: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValidationError(f"{label} must be an object")
    actual = frozenset(obj.keys())
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise ValidationError(f"{label} missing required keys: {', '.join(missing)}")
    if unknown:
        raise ValidationError(f"{label} has unknown keys: {', '.join(unknown)}")
    return obj


def _require_non_empty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValidationError(f"{label} must be a non-empty string")
    return value


def _require_no_edge_ws_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or value == "" or value != value.strip():
        raise ValidationError(
            f"{label} must be a non-empty string without leading/trailing whitespace"
        )
    return value


def _require_pos_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{label} must be a positive integer (>= 1)")
    return int(value)


def _require_unique_pos_int_list(value: Any, label: str) -> list[int]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    seen: set[int] = set()
    out: list[int] = []
    for idx, item in enumerate(value):
        num = _require_pos_int(item, f"{label}[{idx}]")
        if num in seen:
            raise ValidationError(f"{label} contains duplicate integer: {num}")
        seen.add(num)
        out.append(num)
    return out


def _require_unique_str_list(value: Any, label: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    if not allow_empty and not value:
        raise ValidationError(f"{label} must contain at least 1 item")
    seen: set[str] = set()
    out: list[str] = []
    for idx, item in enumerate(value):
        text = _require_non_empty_str(item, f"{label}[{idx}]")
        if text in seen:
            raise ValidationError(f"{label} contains duplicate entry: {text!r}")
        seen.add(text)
        out.append(text)
    return out


def _require_repo(value: Any, label: str) -> str:
    text = _require_non_empty_str(value, label)
    if not _REPO_RE.fullmatch(text):
        raise ValidationError(f"{label} must match 'owner/repo': {text!r}")
    owner, repo = text.split("/", 1)
    if owner in {".", ".."} or repo in {".", ".."}:
        raise ValidationError(f"{label} has invalid owner or repo segment: {text!r}")
    return text


def _require_utc_timestamp(value: Any, label: str) -> str:
    text = _require_non_empty_str(value, label)
    if not _UTC_RE.fullmatch(text):
        raise ValidationError(f"{label} must be YYYY-MM-DDTHH:MM:SSZ: {text!r}")
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValidationError(
            f"{label} is not a valid calendar UTC time: {text!r}"
        ) from exc
    return text


def _require_sha256(value: Any, label: str) -> str:
    text = _require_non_empty_str(value, label)
    if not _SHA256_RE.fullmatch(text):
        raise ValidationError(f"{label} must be 64-char lowercase hex SHA-256")
    return text


def _has_query_token(query: str) -> bool:
    normalized = unicodedata.normalize("NFKC", query)
    return any(
        not unicodedata.category(ch).startswith(("Z", "C", "P", "S"))
        for ch in normalized
    )


def _validate_request(raw: dict[str, Any]) -> None:
    req = _require_keys(raw, _REQUEST_KEYS, "request")
    if req["schema_version"] != QUERY_SCHEMA_VERSION:
        raise ValidationError(
            f"request.schema_version must be {QUERY_SCHEMA_VERSION!r}"
        )
    _require_repo(req["repository"], "request.repository")
    _require_utc_timestamp(req["as_of"], "request.as_of")
    query = _require_non_empty_str(req["query"], "request.query")
    if not _has_query_token(query):
        raise ValidationError("request.query must produce at least one search token")
    _require_unique_pos_int_list(
        req["exclude_issue_numbers"], "request.exclude_issue_numbers"
    )
    _require_unique_pos_int_list(
        req["exclude_pr_numbers"], "request.exclude_pr_numbers"
    )


def _validate_sources(sources: Any, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(sources, list) or not sources:
        raise ValidationError(f"{label} must be a non-empty array")
    by_id: dict[str, dict[str, Any]] = {}
    kinds: set[str] = set()
    for idx, raw_src in enumerate(sources):
        src = _require_keys(raw_src, _SOURCE_KEYS, f"{label}[{idx}]")
        sid = _require_no_edge_ws_str(src["source_id"], f"{label}[{idx}].source_id")
        if sid in by_id:
            raise ValidationError(f"{label} has duplicate source_id: {sid!r}")
        kind = src["kind"]
        if not isinstance(kind, str) or kind not in _VALID_SOURCE_KINDS:
            raise ValidationError(f"{label}[{idx}].kind is invalid: {kind!r}")
        url = _require_non_empty_str(src["url"], f"{label}[{idx}].url")
        if not url.startswith("https://github.com/"):
            raise ValidationError(
                f"{label}[{idx}].url must start with https://github.com/"
            )
        _require_sha256(src["digest"], f"{label}[{idx}].digest")
        _require_utc_timestamp(src["available_at"], f"{label}[{idx}].available_at")
        by_id[sid] = src
        kinds.add(kind)
    if "plan" not in kinds or "task" not in kinds:
        raise ValidationError(
            f"{label} must include at least one 'plan' and one 'task'"
        )
    return by_id


def _compute_plan_revision_digest(sources: list[dict[str, Any]]) -> str:
    pairs = [
        [src["source_id"], src["digest"]]
        for src in sorted(sources, key=lambda s: s["source_id"])
        if src["kind"] in {"plan", "task"}
    ]
    encoded = json.dumps(pairs, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _check_dag(tasks_by_id: dict[str, dict[str, Any]], label: str) -> None:
    state: dict[str, int] = {}

    def _visit(node: str) -> None:
        mark = state.get(node, 0)
        if mark == 1:
            raise ValidationError(f"{label} contains a dependency cycle at {node!r}")
        if mark == 2:
            return
        state[node] = 1
        for dep in tasks_by_id[node]["depends_on"]:
            _visit(dep)
        state[node] = 2

    for sid in tasks_by_id:
        if state.get(sid, 0) == 0:
            _visit(sid)


def _validate_tasks(
    tasks: Any,
    *,
    parent_issue: int,
    lineage_set: set[int],
    sources_by_id: dict[str, dict[str, Any]],
    label: str,
) -> None:
    if not isinstance(tasks, list) or not tasks:
        raise ValidationError(f"{label} must be a non-empty array")
    by_id: dict[str, dict[str, Any]] = {}
    seen_issues: set[int] = set()
    for idx, raw_task in enumerate(tasks):
        t_label = f"{label}[{idx}]"
        task = _require_keys(raw_task, _TASK_KEYS, t_label)
        sid = _require_no_edge_ws_str(task["subtask_id"], f"{t_label}.subtask_id")
        if sid in by_id:
            raise ValidationError(f"{label} has duplicate subtask_id: {sid!r}")
        _validate_task_issue(
            task["issue_number"], parent_issue, lineage_set, seen_issues, t_label
        )
        _require_non_empty_str(task["description"], f"{t_label}.description")
        _require_unique_str_list(
            task["depends_on"], f"{t_label}.depends_on", allow_empty=True
        )
        _require_unique_str_list(
            task["footprint"], f"{t_label}.footprint", allow_empty=True
        )
        _require_unique_str_list(
            task["symbols"], f"{t_label}.symbols", allow_empty=True
        )
        _validate_task_result(task, sources_by_id, t_label)
        by_id[sid] = task
    for sid, task in by_id.items():
        for dep in task["depends_on"]:
            if dep == sid or dep not in by_id:
                raise ValidationError(
                    f"{label} task {sid!r} has invalid dependency {dep!r}"
                )
    _check_dag(by_id, label)


def _validate_task_issue(
    issue_number: Any,
    parent_issue: int,
    lineage_set: set[int],
    seen_issues: set[int],
    label: str,
) -> None:
    if issue_number is None:
        return
    num = _require_pos_int(issue_number, f"{label}.issue_number")
    if num == parent_issue:
        raise ValidationError(f"{label}.issue_number cannot equal parent_issue")
    if num in seen_issues:
        raise ValidationError(f"{label}.issue_number duplicates {num}")
    if num not in lineage_set:
        raise ValidationError(
            f"{label}.issue_number {num} missing from lineage_issue_numbers"
        )
    seen_issues.add(num)


def _validate_task_result(
    task: dict[str, Any],
    sources_by_id: dict[str, dict[str, Any]],
    label: str,
) -> None:
    result = task["result"]
    if not isinstance(result, str) or result not in _VALID_RESULTS:
        raise ValidationError(f"{label}.result is invalid: {result!r}")
    missing_reason = task["missing_reason"]
    if result == "unknown":
        _require_non_empty_str(missing_reason, f"{label}.missing_reason")
    elif missing_reason is not None:
        raise ValidationError(
            f"{label}.missing_reason must be null when result != 'unknown'"
        )
    src_ids = _require_unique_str_list(
        task["source_ids"], f"{label}.source_ids", allow_empty=False
    )
    has_outcome = False
    for sid in src_ids:
        if sid not in sources_by_id:
            raise ValidationError(
                f"{label}.source_ids references unknown source {sid!r}"
            )
        if sources_by_id[sid]["kind"] == "outcome":
            has_outcome = True
    if result in _OUTCOME_RESULTS and not has_outcome:
        raise ValidationError(
            f"{label} with result={result!r} must reference an 'outcome' source"
        )


def _validate_acceptance_criteria(criteria: Any, label: str) -> None:
    if not isinstance(criteria, list) or not criteria:
        raise ValidationError(f"{label} must be a non-empty array")
    for idx, item in enumerate(criteria):
        _require_non_empty_str(item, f"{label}[{idx}]")


def _validate_case(raw_case: Any, idx: int, seen_case_ids: set[str]) -> None:
    label = f"corpus.cases[{idx}]"
    case = _require_keys(raw_case, _CASE_KEYS, label)
    cid = _require_no_edge_ws_str(case["case_id"], f"{label}.case_id")
    if not _CASE_ID_RE.fullmatch(cid):
        raise ValidationError(f"{label}.case_id has invalid characters: {cid!r}")
    if cid in seen_case_ids:
        raise ValidationError(f"Duplicate case_id in corpus: {cid!r}")
    seen_case_ids.add(cid)
    parent_issue = _require_pos_int(case["parent_issue"], f"{label}.parent_issue")
    _require_sha256(case["plan_revision_digest"], f"{label}.plan_revision_digest")
    _require_non_empty_str(case["title"], f"{label}.title")
    _require_non_empty_str(case["purpose"], f"{label}.purpose")
    _validate_acceptance_criteria(
        case["acceptance_criteria"], f"{label}.acceptance_criteria"
    )
    lineage = _require_unique_pos_int_list(
        case["lineage_issue_numbers"], f"{label}.lineage_issue_numbers"
    )
    lineage_set = set(lineage)
    if parent_issue not in lineage_set:
        raise ValidationError(
            f"{label}.lineage_issue_numbers must include parent_issue"
        )
    _require_unique_pos_int_list(case["pr_numbers"], f"{label}.pr_numbers")
    sources_by_id = _validate_sources(case["sources"], f"{label}.sources")
    _validate_tasks(
        case["tasks"],
        parent_issue=parent_issue,
        lineage_set=lineage_set,
        sources_by_id=sources_by_id,
        label=f"{label}.tasks",
    )
    avail = _require_utc_timestamp(case["available_at"], f"{label}.available_at")
    max_src_avail = max(src["available_at"] for src in case["sources"])
    if avail != max_src_avail:
        raise ValidationError(
            f"{label}.available_at must equal max(sources.available_at)"
        )
    expected_rev = _compute_plan_revision_digest(case["sources"])
    if case["plan_revision_digest"] != expected_rev:
        raise ValidationError(f"{label}.plan_revision_digest mismatch")


def _validate_corpus(raw: dict[str, Any]) -> None:
    corpus = _require_keys(raw, _CORPUS_KEYS, "corpus")
    if corpus["schema_version"] != CORPUS_SCHEMA_VERSION:
        raise ValidationError(
            f"corpus.schema_version must be {CORPUS_SCHEMA_VERSION!r}"
        )
    _require_repo(corpus["repository"], "corpus.repository")
    cases = corpus["cases"]
    if not isinstance(cases, list):
        raise ValidationError("corpus.cases must be an array")
    seen_case_ids: set[str] = set()
    for idx, raw_case in enumerate(cases):
        _validate_case(raw_case, idx, seen_case_ids)


def _filter_and_deduplicate(
    corpus: dict[str, Any], request: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    excluded = {
        "repository_mismatch": 0,
        "lineage_excluded": 0,
        "after_as_of": 0,
        "superseded_revision": 0,
    }
    req_issues = set(request["exclude_issue_numbers"])
    req_prs = set(request["exclude_pr_numbers"])
    repo_match = corpus["repository"] == request["repository"]
    as_of = request["as_of"]

    eligible: list[dict[str, Any]] = []
    for case in corpus["cases"]:
        if not repo_match:
            excluded["repository_mismatch"] += 1
            continue
        case_issues = (
            set(case["lineage_issue_numbers"])
            | {case["parent_issue"]}
            | {
                t["issue_number"]
                for t in case["tasks"]
                if t["issue_number"] is not None
            }
        )
        if (case_issues & req_issues) or (set(case["pr_numbers"]) & req_prs):
            excluded["lineage_excluded"] += 1
            continue
        if case["available_at"] >= as_of:
            excluded["after_as_of"] += 1
            continue
        eligible.append(case)

    by_parent: dict[int, dict[str, Any]] = {}
    for case in eligible:
        parent = case["parent_issue"]
        prev = by_parent.get(parent)
        if prev is None:
            by_parent[parent] = case
            continue
        excluded["superseded_revision"] += 1
        if case["available_at"] > prev["available_at"] or (
            case["available_at"] == prev["available_at"]
            and case["case_id"] < prev["case_id"]
        ):
            by_parent[parent] = case

    representatives = [by_parent[p] for p in sorted(by_parent)]
    return representatives, excluded


def _to_search_document(case: dict[str, Any]) -> _SearchDocument:
    sorted_tasks = sorted(case["tasks"], key=lambda t: t["subtask_id"])
    description = "\n".join(
        [
            case["purpose"],
            *case["acceptance_criteria"],
            *(task["description"] for task in sorted_tasks),
        ]
    )
    footprint = tuple(sorted({fp for task in sorted_tasks for fp in task["footprint"]}))
    symbols = tuple(sorted({sym for task in sorted_tasks for sym in task["symbols"]}))
    return _SearchDocument(
        title=case["title"],
        description=description,
        summary=None,
        task_kind="",
        footprint=footprint,
        symbols=symbols,
        issue_number=case["parent_issue"],
        source_comment_id=case["case_id"],
        completion_id=None,
        case=case,
    )


def _normalize_card(case: dict[str, Any]) -> dict[str, Any]:
    sorted_tasks = sorted(case["tasks"], key=lambda t: t["subtask_id"])
    tasks_out = [
        {
            "subtask_id": task["subtask_id"],
            "issue_number": task["issue_number"],
            "description": task["description"],
            "depends_on": sorted(task["depends_on"]),
            "footprint": sorted(task["footprint"]),
            "symbols": sorted(task["symbols"]),
            "result": task["result"],
            "missing_reason": task["missing_reason"],
            "source_ids": sorted(task["source_ids"]),
        }
        for task in sorted_tasks
    ]
    edges = sorted(
        (
            {"from_subtask": dep, "to_subtask": task["subtask_id"]}
            for task in sorted_tasks
            for dep in task["depends_on"]
        ),
        key=lambda e: (e["from_subtask"], e["to_subtask"]),
    )
    sources_out = [
        {
            "source_id": src["source_id"],
            "kind": src["kind"],
            "url": src["url"],
            "digest": src["digest"],
            "available_at": src["available_at"],
        }
        for src in sorted(case["sources"], key=lambda s: s["source_id"])
    ]
    return {
        "case_id": case["case_id"],
        "parent_issue": case["parent_issue"],
        "plan_revision_digest": case["plan_revision_digest"],
        "available_at": case["available_at"],
        "title": case["title"],
        "purpose": case["purpose"],
        "acceptance_criteria": list(case["acceptance_criteria"]),
        "tasks": tasks_out,
        "declared_edges": edges,
        "sources": sources_out,
    }


def _render_envelope(
    *,
    status: str,
    corpus_digest: str,
    request_digest: str,
    excluded_counts: dict[str, int],
    budget_dropped: int,
    cards: list[dict[str, Any]],
) -> tuple[dict[str, Any], bytes]:
    payload: dict[str, Any] = {
        "schema_version": CARDS_SCHEMA_VERSION,
        "status": status,
        "corpus_digest": corpus_digest,
        "request_digest": request_digest,
        "returned_count": len(cards),
        "excluded_counts": {
            "repository_mismatch": excluded_counts["repository_mismatch"],
            "lineage_excluded": excluded_counts["lineage_excluded"],
            "after_as_of": excluded_counts["after_as_of"],
            "superseded_revision": excluded_counts["superseded_revision"],
        },
        "budget_dropped": budget_dropped,
        "cards": cards,
    }
    rendered = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    return payload, rendered


def select_cards(
    corpus: dict[str, Any],
    request: dict[str, Any],
    *,
    corpus_digest: str,
    request_digest: str,
    limit: int = DEFAULT_LIMIT,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> SelectResult:
    """Filter, deduplicate, BM25-rank, and budget-fit planning history cards."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValidationError("--limit must be an integer >= 0")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValidationError("--max-bytes must be a positive integer (>= 1)")
    _require_sha256(corpus_digest, "corpus_digest")
    _require_sha256(request_digest, "request_digest")
    _validate_corpus(corpus)
    _validate_request(request)

    representatives, excluded = _filter_and_deduplicate(corpus, request)
    if limit == 0:
        base_status = "disabled"
        cards: list[dict[str, Any]] = []
    else:
        docs = [_to_search_document(case) for case in representatives]
        hits = search_artifacts(docs, request["query"], limit=limit)
        if not hits:
            base_status = "no_matches"
            cards = []
        else:
            base_status = "ready"
            cards = [_normalize_card(hit.case) for hit in hits]

    budget_dropped = 0
    while True:
        status = "over_budget" if base_status == "ready" and not cards else base_status
        payload, rendered = _render_envelope(
            status=status,
            corpus_digest=corpus_digest,
            request_digest=request_digest,
            excluded_counts=excluded,
            budget_dropped=budget_dropped,
            cards=cards,
        )
        if fit_context_budget(rendered, max_bytes=max_bytes).fits:
            return SelectResult(payload=payload, rendered_bytes=rendered)
        if cards:
            cards.pop()
            budget_dropped += 1
            continue
        raise ValidationError(
            f"Envelope with 0 cards ({len(rendered)} bytes) exceeds max-bytes={max_bytes}"
        )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.planning_history_poc",
        description="Select planning-history-cards-v1 from fixed corpus and query JSON.",
    )
    parser.add_argument("--corpus", required=True, help="Path to corpus JSON")
    parser.add_argument("--request", required=True, help="Path to request JSON")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help="Max cards (>= 0)"
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="Max UTF-8 output bytes (>= 1)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint returning 0 on success, 1 on I/O error, 2 on validation error."""
    args = _parse_args(argv)
    if args.limit < 0:
        print("Error: --limit must be >= 0", file=sys.stderr)
        return 2
    if args.max_bytes < 1:
        print("Error: --max-bytes must be >= 1", file=sys.stderr)
        return 2
    try:
        corpus, corpus_digest = _load_strict_json(Path(args.corpus), "corpus")
        request, request_digest = _load_strict_json(Path(args.request), "request")
        result = select_cards(
            corpus,
            request,
            corpus_digest=corpus_digest,
            request_digest=request_digest,
            limit=args.limit,
            max_bytes=args.max_bytes,
        )
    except FileIOError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    sys.stdout.buffer.write(result.rendered_bytes)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
