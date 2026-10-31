"""Manifest format definitions and detection for decomposition plans."""

from __future__ import annotations

import copy
from typing import Any

from orchestune.dag.parsing import reject_retired_model_tier
from orchestune.issue_parsing import PLAN_MANIFEST_FORMAT

PROJECTED_ISSUE_NUMBER = 10**10 - 1
SUPPORTED_PLAN_FORMATS = frozenset({PLAN_MANIFEST_FORMAT})
_KEYS_TO_OMIT_FROM_PROVISIONED = frozenset(
    {
        "overview",
        "proposed_changes",
        "acceptance_criteria",
        "verification_plan",
        "footprint",
        "symbols",
    }
)


def is_manifest_plan(plan: dict[str, Any]) -> bool:
    """Return True if plan specifies a supported manifest plan_format.

    Raises ValueError if plan_format is present but not in SUPPORTED_PLAN_FORMATS.
    """
    plan_format = plan.get("plan_format")
    if plan_format is None:
        return False
    if plan_format not in SUPPORTED_PLAN_FORMATS:
        raise ValueError(
            f"unsupported plan_format: {plan_format!r} (must be one of {sorted(SUPPORTED_PLAN_FORMATS)})"
        )
    return bool(plan_format == PLAN_MANIFEST_FORMAT)


def project_single_subtask(subtask: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(subtask, dict) or "id" not in subtask:
        raise ValueError("Each subtask in parent plan must be a dict with 'id'")
    reject_retired_model_tier(subtask.get("model_tier"))
    subtask = {k: v for k, v in subtask.items() if k != "model_tier"}
    subtask_id = str(subtask["id"]).strip()
    issue_num = subtask.get("issue_number")
    if issue_num is None:
        return dict(subtask)
    if isinstance(issue_num, bool) or not isinstance(issue_num, int) or issue_num <= 0:
        raise ValueError(
            f"invalid issue_number in subtask {subtask_id!r}: {issue_num!r}"
        )
    return {k: v for k, v in subtask.items() if k not in _KEYS_TO_OMIT_FROM_PROVISIONED}


def project_plan_manifest(plan: dict[str, Any]) -> dict[str, Any]:
    """Project a plan into manifest format, omitting prose and footprints from provisioned entries."""
    fmt = plan.get("plan_format")
    if fmt is not None and fmt not in SUPPORTED_PLAN_FORMATS:
        raise ValueError(
            f"unsupported plan_format: {fmt!r} (must be one of {sorted(SUPPORTED_PLAN_FORMATS)})"
        )

    subtasks = plan.get("subtasks")
    if not isinstance(subtasks, list):
        raise ValueError("parent plan 'subtasks' must be a list")

    deep_copied = copy.deepcopy(plan)
    projected_subtasks = [project_single_subtask(st) for st in deep_copied["subtasks"]]

    result: dict[str, Any] = {"plan_format": PLAN_MANIFEST_FORMAT}
    for k, v in deep_copied.items():
        if k == "plan_format":
            continue
        elif k == "subtasks":
            result["subtasks"] = projected_subtasks
        else:
            result[k] = v

    if "subtasks" not in result:
        result["subtasks"] = projected_subtasks
    return result


__all__ = [
    "PLAN_MANIFEST_FORMAT",
    "PROJECTED_ISSUE_NUMBER",
    "SUPPORTED_PLAN_FORMATS",
    "is_manifest_plan",
    "project_plan_manifest",
    "project_single_subtask",
]
