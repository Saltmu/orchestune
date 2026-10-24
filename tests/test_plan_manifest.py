"""Tests for plan_manifest format definitions and detection."""

from typing import Any

import pytest

from orchestune.provisioning.plan_manifest import (
    PLAN_MANIFEST_FORMAT,
    PROJECTED_ISSUE_NUMBER,
    SUPPORTED_PLAN_FORMATS,
    is_manifest_plan,
    project_plan_manifest,
)


def test_plan_manifest_constants() -> None:
    assert PLAN_MANIFEST_FORMAT == "manifest-v1"
    assert PLAN_MANIFEST_FORMAT in SUPPORTED_PLAN_FORMATS
    assert PROJECTED_ISSUE_NUMBER == 10**10 - 1


def test_is_manifest_plan_with_manifest() -> None:
    plan: dict[str, Any] = {"plan_format": "manifest-v1", "subtasks": []}
    assert is_manifest_plan(plan) is True


def test_is_manifest_plan_with_legacy_plan() -> None:
    plan: dict[str, Any] = {"subtasks": []}
    assert is_manifest_plan(plan) is False


def test_is_manifest_plan_with_none_format() -> None:
    plan: dict[str, Any] = {"plan_format": None, "subtasks": []}
    assert is_manifest_plan(plan) is False


def test_is_manifest_plan_with_unsupported_format() -> None:
    plan: dict[str, Any] = {"plan_format": "manifest-v2", "subtasks": []}
    with pytest.raises(ValueError, match="unsupported plan_format"):
        is_manifest_plan(plan)


def test_project_plan_manifest_strips_provisioned_keys() -> None:
    original: dict[str, Any] = {
        "title": "Sample Plan",
        "subtasks": [
            {
                "id": "task-provisioned",
                "issue_number": 42,
                "description": "Short desc",
                "overview": "Long overview text",
                "proposed_changes": ["change 1", "change 2"],
                "acceptance_criteria": ["criterion 1"],
                "verification_plan": ["verify 1"],
                "footprint": ["src/app.py"],
                "symbols": ["app.run"],
                "depends_on": [],
                "shared_contract": None,
                "writes_shared_contract": False,
                "custom_future_key": "preserved",
            },
            {
                "id": "task-unprovisioned",
                "issue_number": None,
                "description": "Unprovisioned task",
                "overview": "Keep this overview",
                "proposed_changes": ["keep change"],
                "acceptance_criteria": ["keep criterion"],
                "verification_plan": ["keep verify"],
                "footprint": ["src/other.py"],
                "symbols": ["other.helper"],
                "depends_on": ["task-provisioned"],
            },
        ],
    }

    projected = project_plan_manifest(original)

    # Original is untouched
    assert "overview" in original["subtasks"][0]
    assert "plan_format" not in original

    # Projected has manifest format
    assert projected["plan_format"] == "manifest-v1"
    assert projected["title"] == "Sample Plan"

    # Provisioned entry has 6 keys removed
    prov = projected["subtasks"][0]
    for key in (
        "overview",
        "proposed_changes",
        "acceptance_criteria",
        "verification_plan",
        "footprint",
        "symbols",
    ):
        assert key not in prov
    assert prov["id"] == "task-provisioned"
    assert prov["issue_number"] == 42
    assert prov["description"] == "Short desc"
    assert prov["custom_future_key"] == "preserved"

    # Unprovisioned entry retains all keys
    unprov = projected["subtasks"][1]
    assert unprov["overview"] == "Keep this overview"
    assert unprov["proposed_changes"] == ["keep change"]
    assert unprov["footprint"] == ["src/other.py"]
    assert unprov["symbols"] == ["other.helper"]


def test_project_plan_manifest_idempotence() -> None:
    plan: dict[str, Any] = {
        "title": "Idempotent Plan",
        "subtasks": [
            {
                "id": "task-1",
                "issue_number": 101,
                "description": "Desc 1",
                "overview": "Text",
                "proposed_changes": ["c1"],
                "acceptance_criteria": ["a1"],
                "verification_plan": ["v1"],
                "footprint": ["f1.py"],
                "symbols": ["s1"],
            },
            {
                "id": "task-2",
                "issue_number": None,
                "description": "Desc 2",
                "overview": "Text 2",
                "proposed_changes": ["c2"],
                "acceptance_criteria": ["a2"],
                "verification_plan": ["v2"],
                "footprint": ["f2.py"],
                "symbols": ["s2"],
            },
        ],
    }

    proj1 = project_plan_manifest(plan)
    proj2 = project_plan_manifest(proj1)
    assert proj1 == proj2


def test_project_plan_manifest_rejects_invalid_issue_number() -> None:
    for invalid in (True, False, 0, -5, "42"):
        plan: dict[str, Any] = {
            "subtasks": [{"id": "bad-issue", "issue_number": invalid}]
        }
        with pytest.raises(ValueError, match="invalid issue_number"):
            project_plan_manifest(plan)


def test_project_plan_manifest_rejects_unsupported_format() -> None:
    plan: dict[str, Any] = {"plan_format": "unknown-v99", "subtasks": []}
    with pytest.raises(ValueError, match="unsupported plan_format"):
        project_plan_manifest(plan)
