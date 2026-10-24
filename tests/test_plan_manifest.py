"""Tests for plan_manifest format definitions and detection."""

from typing import Any

import pytest

from orchestune.provisioning.plan_manifest import (
    PLAN_MANIFEST_FORMAT,
    SUPPORTED_PLAN_FORMATS,
    is_manifest_plan,
)


def test_plan_manifest_constants() -> None:
    assert PLAN_MANIFEST_FORMAT == "manifest-v1"
    assert PLAN_MANIFEST_FORMAT in SUPPORTED_PLAN_FORMATS


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
