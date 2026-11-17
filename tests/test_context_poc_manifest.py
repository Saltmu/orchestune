"""Pin the PoC evaluation manifest without calling a live model."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

from orchestune.context.contracts import (
    CONTEXT_OVER_BUDGET,
    CONTEXT_READY,
    CONTEXT_UNAVAILABLE,
    REDUCTION_TIER1_EXCEEDS_BUDGET,
    REDUCTION_TIER2_TAIL_DROPPED,
    TASK_KIND_UNSPECIFIED,
)
from orchestune.models import Usage
from orchestune.targets.contracts import DispatchHandle
from orchestune.targets.usage import LocalUsageProvider

SCHEMA = "context-poc-manifest-v1"
CONDITIONS = ("baseline", "tier1_only", "tier1_plus_tier2")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cost_usd")
SNAPSHOT_ID = "snap-poc-2026-11-16"
BASE_SHA = "6e8cbf5dbaf435af60690e997de333cf6d18a895"
STARTED = "2026-11-16T00:00:00Z"
_STATUSES = {None, CONTEXT_READY, CONTEXT_OVER_BUDGET, CONTEXT_UNAVAILABLE}
_REASONS = {None, REDUCTION_TIER1_EXCEEDS_BUDGET, REDUCTION_TIER2_TAIL_DROPPED}
_REPO_ROOT = Path(__file__).resolve().parents[1]
_HEX = frozenset("0123456789abcdef")
_TASKS = (
    (9101, "single-fix", CONTEXT_READY, None, 3),
    (9102, "dependent-child", CONTEXT_READY, None, 2),
    (9103, "failed-resume", CONTEXT_READY, None, 0),
    (9104, "over-budget", CONTEXT_OVER_BUDGET, REDUCTION_TIER1_EXCEEDS_BUDGET, 0),
    (9105, "tail-drop", CONTEXT_READY, REDUCTION_TIER2_TAIL_DROPPED, 2),
    (9106, "blocked-interruption", CONTEXT_UNAVAILABLE, None, 0),
    (9107, "unspecified-record", CONTEXT_READY, None, 1),
)


def usage_metrics(usage: Usage | None, injected_bytes: int) -> dict[str, object]:
    """Keep a missing usage record null. ``injected_bytes`` stays its own field."""
    metrics: dict[str, object] = {name: None for name in TOKEN_FIELDS}
    metrics["injected_bytes"] = injected_bytes
    if usage is None:
        return metrics
    metrics["input_tokens"] = usage.input_tokens
    metrics["output_tokens"] = usage.output_tokens
    metrics["total_tokens"] = usage.total_tokens
    metrics["cost_usd"] = usage.cost_usd
    return metrics


def task_kind_of(raw: Mapping[str, object]) -> str:
    """Leave a missing or blank kind as ``unspecified``. Do not classify a title."""
    kind = raw.get("task_kind", TASK_KIND_UNSPECIFIED)
    if isinstance(kind, str) and kind.strip():
        return kind
    return TASK_KIND_UNSPECIFIED


def proxy_errors(metrics: Mapping[str, object]) -> list[str]:
    """Reject a byte count stored in, or summed into, a usage field."""
    errors: list[str] = []
    injected = metrics.get("injected_bytes")
    if isinstance(injected, bool) or not isinstance(injected, int):
        return ["injected_bytes must stay an int field"]
    for name in TOKEN_FIELDS:
        if name not in metrics:
            errors.append(f"missing {name}")
        elif metrics[name] == injected:
            errors.append(f"{name} repeats injected_bytes")
    tokens_missing = (
        metrics.get("input_tokens") is None and metrics.get("output_tokens") is None
    )
    if tokens_missing and metrics.get("total_tokens") is not None:
        errors.append("total_tokens filled from injected_bytes")
    return errors


def _int_error(value: object, label: str) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{label} must be an int"
    return None


def condition_errors(condition: Mapping[str, object]) -> list[str]:
    """Require the recorded budget, query, count, status, and reason keys."""
    errors = proxy_errors(condition)
    for key in (
        "budget_bytes",
        "query",
        "returned_count",
        "context_status",
        "reduction_reason",
    ):
        if key not in condition:
            errors.append(f"missing {key}")
    budget_bytes = condition.get("budget_bytes")
    returned_count = condition.get("returned_count")
    budget = _int_error(budget_bytes, "budget_bytes")
    returned = _int_error(returned_count, "returned_count")
    errors.extend(item for item in (budget, returned) if item is not None)
    if isinstance(budget_bytes, int) and not isinstance(budget_bytes, bool):
        if budget_bytes < 1:
            errors.append("budget_bytes must be positive")
    if isinstance(returned_count, int) and not isinstance(returned_count, bool):
        if returned_count < 0:
            errors.append("returned_count must be zero or more")
    query = condition.get("query")
    if query is not None and not isinstance(query, str):
        errors.append("query must be a string or null")
    if condition.get("context_status") not in _STATUSES:
        errors.append("context_status")
    if condition.get("reduction_reason") not in _REASONS:
        errors.append("reduction_reason")
    return errors


def history_errors(
    task: Mapping[str, object], *, snapshot_id: str, started_at: str
) -> list[str]:
    """Drop the subject task and anything recorded at or after evaluation start."""
    errors: list[str] = []
    history = task.get("reference_history")
    if not isinstance(history, list):
        return ["reference_history"]
    for item in history:
        if not isinstance(item, dict):
            errors.append("history item")
            continue
        if item.get("issue_number") == task.get("issue_number"):
            errors.append("subject task is in reference history")
        if item.get("snapshot_id") != snapshot_id:
            errors.append("history snapshot differs")
        recorded = item.get("recorded_at")
        if not isinstance(recorded, str) or recorded >= started_at:
            errors.append("history is not before evaluation start")
    return errors


def task_errors(
    task: Mapping[str, object], *, snapshot_id: str, started_at: str
) -> list[str]:
    """Check one fixed task definition and its three comparison arms."""
    errors = history_errors(task, snapshot_id=snapshot_id, started_at=started_at)
    if task.get("task_kind") != task_kind_of(task):
        errors.append("task_kind")
    conditions = task.get("conditions")
    if not isinstance(conditions, dict) or tuple(conditions) != CONDITIONS:
        return [*errors, "conditions"]
    for name in CONDITIONS:
        arm = conditions[name]
        if isinstance(arm, dict):
            errors.extend(condition_errors(arm))
        else:
            errors.append(name)
    return errors


def manifest_errors(manifest: Mapping[str, object]) -> list[str]:
    """Validate the comparison manifest. An empty list means the shape holds."""
    errors: list[str] = []
    if manifest.get("schema_version") != SCHEMA:
        errors.append("schema_version")
    snapshot = manifest.get("history_snapshot_id")
    started = manifest.get("evaluation_started_at")
    base = manifest.get("base_sha")
    if not isinstance(snapshot, str) or not snapshot:
        errors.append("history_snapshot_id")
    if not isinstance(started, str) or not started:
        errors.append("evaluation_started_at")
    if not isinstance(base, str) or len(base) != 40 or any(c not in _HEX for c in base):
        errors.append("base_sha")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not 5 <= len(tasks) <= 10:
        return [*errors, "task count"]
    if not isinstance(snapshot, str) or not isinstance(started, str):
        return errors
    for task in tasks:
        if isinstance(task, dict):
            errors.extend(task_errors(task, snapshot_id=snapshot, started_at=started))
        else:
            errors.append("task shape")
    return errors


def _arm(
    injected: int,
    *,
    query: str | None,
    returned: int,
    status: str | None,
    reason: str | None,
) -> dict[str, object]:
    metrics = usage_metrics(None, injected)
    metrics["budget_bytes"] = 32768
    metrics["query"] = query
    metrics["returned_count"] = returned
    metrics["context_status"] = status
    metrics["reduction_reason"] = reason
    return metrics


def _fixed_task(
    issue: int, name: str, status: str | None, reason: str | None, returned: int
) -> dict[str, object]:
    query = f"{name} {issue}"
    return {
        "task_id": name,
        "issue_number": issue,
        "title": f"bugfix {name}",
        "task_kind": task_kind_of({"title": f"bugfix {name}"}),
        "conditions": {
            "baseline": _arm(40, query=None, returned=0, status=None, reason=None),
            "tier1_only": _arm(
                200, query=query, returned=0, status=status, reason=reason
            ),
            "tier1_plus_tier2": _arm(
                800, query=query, returned=returned, status=status, reason=reason
            ),
        },
        "reference_history": [
            {
                "issue_number": 100,
                "snapshot_id": SNAPSHOT_ID,
                "recorded_at": "2026-11-01T00:00:00Z",
            }
        ],
    }


def fixed_manifest() -> dict[str, object]:
    """Return the fixed 7-task comparison set. CI does not execute it."""
    return {
        "schema_version": SCHEMA,
        "history_snapshot_id": SNAPSHOT_ID,
        "base_sha": BASE_SHA,
        "evaluation_started_at": STARTED,
        "tasks": [_fixed_task(*row) for row in _TASKS],
    }


def _skill_markdown_lines() -> int:
    root = _REPO_ROOT / "skills" / "local-ci-developer"
    return sum(
        sum(1 for _ in path.open(encoding="utf-8")) for path in root.rglob("*.md")
    )


def test_missing_usage_stays_null_beside_injected_bytes(tmp_path: Path) -> None:
    usage = LocalUsageProvider(tmp_path).collect_usage(
        DispatchHandle(branch_name="claude/issue-9101-single-fix")
    )
    metrics = usage_metrics(usage, 120)

    assert usage is None
    assert [metrics[name] for name in TOKEN_FIELDS] == [None, None, None, None]
    assert metrics["injected_bytes"] == 120
    assert set(TOKEN_FIELDS).isdisjoint({"injected_bytes"})
    assert proxy_errors(metrics) == []


def test_proxy_copy_and_proxy_sum_are_rejected() -> None:
    metrics = usage_metrics(None, 120)
    copied = dict(metrics, input_tokens=120)
    summed = dict(metrics, total_tokens=120)
    same_field: dict[str, object] = {name: None for name in TOKEN_FIELDS}
    same_field["input_tokens"] = 120

    assert "input_tokens repeats injected_bytes" in proxy_errors(copied)
    assert "total_tokens filled from injected_bytes" in proxy_errors(summed)
    assert proxy_errors(same_field) == ["injected_bytes must stay an int field"]


def test_observed_usage_is_copied_without_replacing_injected_bytes() -> None:
    metrics = usage_metrics(Usage(3, 4, 7, model="demo", cost_usd=0.1), 120)

    assert metrics["input_tokens"] == 3
    assert metrics["output_tokens"] == 4
    assert metrics["total_tokens"] == 7
    assert metrics["cost_usd"] == 0.1
    assert metrics["injected_bytes"] == 120


def test_missing_task_kind_stays_unspecified() -> None:
    assert task_kind_of({"title": "bugfix the parser"}) == TASK_KIND_UNSPECIFIED
    assert task_kind_of({"task_kind": "  "}) == TASK_KIND_UNSPECIFIED


def test_fixed_manifest_records_the_comparison_shape() -> None:
    manifest = fixed_manifest()
    tasks = manifest["tasks"]

    assert manifest_errors(manifest) == []
    assert isinstance(tasks, list)
    assert 5 <= len(tasks) <= 10
    assert manifest["history_snapshot_id"] == SNAPSHOT_ID
    assert manifest["base_sha"] == BASE_SHA
    first = tasks[0]
    assert isinstance(first, dict)
    assert tuple(first["conditions"]) == CONDITIONS
    assert first["task_kind"] == TASK_KIND_UNSPECIFIED


def test_subject_task_and_later_history_are_excluded() -> None:
    manifest = fixed_manifest()
    tasks = manifest["tasks"]
    assert isinstance(tasks, list)
    subject = deepcopy(tasks[0])
    later = deepcopy(tasks[0])
    other_snapshot = deepcopy(tasks[0])
    assert isinstance(subject, dict)
    assert isinstance(later, dict)
    assert isinstance(other_snapshot, dict)
    subject["reference_history"][0]["issue_number"] = subject["issue_number"]
    later["reference_history"][0]["recorded_at"] = STARTED
    other_snapshot["reference_history"][0]["snapshot_id"] = "snap-other"

    assert "subject task is in reference history" in manifest_errors(
        {**manifest, "tasks": [subject, *tasks[1:]]}
    )
    assert "history is not before evaluation start" in manifest_errors(
        {**manifest, "tasks": [later, *tasks[1:]]}
    )
    assert "history snapshot differs" in manifest_errors(
        {**manifest, "tasks": [other_snapshot, *tasks[1:]]}
    )


def test_skill_reads_export_without_replacing_claim() -> None:
    skill = (_REPO_ROOT / "skills/local-ci-developer/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "orchestune context export --issue <issue_number>" in skill
    assert "context.json" in skill
    assert "orchestune claim <issue_number> --apply" in skill
    assert "stale-read" in skill
    assert _skill_markdown_lines() <= 500
