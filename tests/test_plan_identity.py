"""Tests for orchestune.plan_identity contracts."""

from __future__ import annotations

import pytest

from orchestune.plan_identity import (
    PlanGeneration,
    PlanRevision,
)


def test_plan_revision_validation() -> None:
    valid_hash = "a" * 64
    rev = PlanRevision(f"replan-v1:sha256:{valid_hash}")
    assert isinstance(rev, str)
    assert isinstance(rev, PlanRevision)
    assert rev == f"replan-v1:sha256:{valid_hash}"

    with pytest.raises(ValueError, match="invalid plan revision"):
        PlanRevision("invalid-revision")

    with pytest.raises(ValueError, match="invalid plan revision"):
        PlanRevision("replan-v1:sha256:too-short")


def test_plan_generation_marker_and_matching() -> None:
    revision = PlanRevision("replan-v1:sha256:" + "1" * 64)
    generation = PlanGeneration(revision, "task-1")

    assert generation.plan_revision == revision
    assert generation.subtask_id == "task-1"
    assert "replan-generation" in generation.marker
    assert generation.matches_body(f"Header\n{generation.marker}\nFooter")
    assert not generation.matches_body("Header\nNo marker\nFooter")


def test_plan_generation_coerces_revision_string() -> None:
    raw_rev = "replan-v1:sha256:" + "2" * 64
    generation = PlanGeneration(raw_rev, "task-2")  # type: ignore[arg-type]
    assert isinstance(generation.plan_revision, PlanRevision)
    assert generation.plan_revision == raw_rev


def test_plan_generation_validation() -> None:
    revision = PlanRevision("replan-v1:sha256:" + "3" * 64)

    with pytest.raises(
        ValueError, match="subtask_id must be a non-empty, trimmed string"
    ):
        PlanGeneration(revision, "")

    with pytest.raises(
        ValueError, match="subtask_id must be a non-empty, trimmed string"
    ):
        PlanGeneration(revision, "  task-3  ")
