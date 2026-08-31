from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from orchestune.dag.models import SubTask
from orchestune.issue_parsing import parent_issue_number_from_body
from orchestune.labels import StatusLabel
from orchestune.provisioning.rendering import (
    build_subtask_issue_body,
    derive_subtask_labels,
    subtask_issue_title,
)
from orchestune.replan.managed_body import (
    GENERATED_SUBTASK_END,
    GENERATED_SUBTASK_START,
    ManagedBodyConflict,
    reconcile_managed_body,
)
from orchestune.replan.models import (
    ApplyPolicy,
    ChangeKind,
    Disposition,
    EndpointRef,
    ExternalDependency,
    IssueSnapshot,
    PlanRevision,
    ReplanChange,
)
from orchestune.replan.plan import compute_plan_revision, load_replan_plan


def _subtask(
    subtask_id: str = "task-a", *, description: str = "Implement A"
) -> SubTask:
    return SubTask(
        id=subtask_id,
        description=description,
        footprint=("orchestune/a.py", "tests/test_a.py"),
        symbols=("a.run",),
        depends_on=(),
        risk=False,
        risk_reasons=(),
        priority="high",
        overview="Overview",
        proposed_changes=("First", "Second"),
        acceptance_criteria=("Works", "Is tested"),
        verification_plan=("pytest", "mypy"),
        shared_contract="contract-a",
        writes_shared_contract=True,
        execution_profile="deep-reasoning",
        model_tier="strong",
    )


def _template() -> str:
    return (
        "# [FEAT] {{subtask_id}}: {{description}}\n\n"
        "## Overview\n{{overview}}\n\n"
        "## Proposed Changes\n{{proposed_changes}}\n\n"
        "## Acceptance Criteria\n{{acceptance_criteria}}\n\n"
        "## Verification Plan\n{{verification_plan}}\n\n"
        "## Footprint\n```yaml\n"
        "subtask_id: {{subtask_id_yaml}}\n"
        "footprint: {{footprint}}\n"
        "symbols: {{symbols}}\n"
        "depends_on: {{depends_on}}\n"
        "shared_contract: {{shared_contract}}\n"
        "writes_shared_contract: {{writes_shared_contract}}\n"
        "parent_issue_number: {{parent_issue_number}}\n"
        "execution_profile: {{execution_profile}}\n"
        "model_tier: {{model_tier}}\n"
        "```\n"
    )


def _write_plan(
    path: Path,
    *,
    task_issue_number: int | None = 10,
    parent_issue_number: int = 693,
    external: str = "",
    proposed_changes: str = "[First, Second]",
) -> Path:
    issue_number = "null" if task_issue_number is None else str(task_issue_number)
    path.write_text(
        f"""---
title: Replan sample
parent_issue_number: {parent_issue_number}
parent_issue_source: adopted
subtasks:
  - id: task-b
    description: Implement B
    footprint: [tests/test_b.py, orchestune/b.py]
    symbols: [b.run]
    depends_on: [task-a]
    priority: medium
    risk: false
    proposed_changes: [Only]
    acceptance_criteria: [Works]
    verification_plan: [pytest]
    issue_number: 11
    model_tier: middle
  - id: task-a
    description: Implement A
    footprint: [tests/test_a.py, orchestune/a.py]
    symbols: [a.z, a.run]
    depends_on: []
    priority: high
    risk: false
    overview: Overview
    proposed_changes: {proposed_changes}
    acceptance_criteria: [Works, Is tested]
    verification_plan: [pytest, mypy]
    shared_contract: contract-a
    writes_shared_contract: true
    execution_profile: deep-reasoning
    model_tier: strong
    issue_number: {issue_number}
{external}---

# Plan prose
""",
        encoding="utf-8",
    )
    return path


class TestPlanRevision:
    def test_is_semantic_deterministic_and_ignores_only_subtask_issue_number(
        self, tmp_path: Path
    ) -> None:
        first = load_replan_plan(_write_plan(tmp_path / "first.md"))
        second_path = _write_plan(
            tmp_path / "second.md",
            task_issue_number=999,
            proposed_changes="[First, Second]",
        )
        text = second_path.read_text(encoding="utf-8")
        text = text.replace(
            "footprint: [tests/test_a.py, orchestune/a.py]",
            "footprint:\n      - orchestune/a.py\n      - tests/test_a.py",
        ).replace("symbols: [a.z, a.run]", "symbols: [a.run, a.z]")
        second_path.write_text(text, encoding="utf-8")
        second = load_replan_plan(second_path)

        first_revision = compute_plan_revision(first)
        second_revision = compute_plan_revision(second)

        assert isinstance(first_revision, PlanRevision)
        assert str(first_revision) == str(second_revision)
        assert re.fullmatch(r"replan-v1:sha256:[0-9a-f]{64}", str(first_revision))

    @pytest.mark.parametrize(
        ("change", "kwargs"),
        [
            ("parent", {"parent_issue_number": 694}),
            ("body-list-order", {"proposed_changes": "[Second, First]"}),
        ],
    )
    def test_changes_when_semantics_change(
        self, tmp_path: Path, change: str, kwargs: dict[str, Any]
    ) -> None:
        baseline = load_replan_plan(_write_plan(tmp_path / "base.md"))
        changed = load_replan_plan(_write_plan(tmp_path / f"{change}.md", **kwargs))
        assert compute_plan_revision(changed) != compute_plan_revision(baseline)

    def test_external_issue_numbers_are_revision_semantics(
        self, tmp_path: Path
    ) -> None:
        edge = (
            "external_dependencies:\n"
            "  - blocked: {subtask_id: task-a}\n"
            "    blocker: {issue_number: 500}\n"
        )
        changed_edge = edge.replace("500", "501")
        first = load_replan_plan(_write_plan(tmp_path / "one.md", external=edge))
        second = load_replan_plan(
            _write_plan(tmp_path / "two.md", external=changed_edge)
        )
        assert compute_plan_revision(first) != compute_plan_revision(second)


class TestExternalDependencies:
    @pytest.mark.parametrize(
        "endpoint",
        [
            "{}",
            "{subtask_id: task-a, issue_number: 500}",
            "{issue_number: true}",
            "{issue_number: 0}",
            "{issue_number: -1}",
            "{subtask_id: task-a, typo: 1}",
        ],
    )
    def test_endpoint_is_an_exactly_one_strict_union(
        self, tmp_path: Path, endpoint: str
    ) -> None:
        external = (
            "external_dependencies:\n"
            f"  - blocked: {endpoint}\n"
            "    blocker: {issue_number: 500}\n"
        )
        with pytest.raises(ValueError, match="endpoint|Endpoint|issue_number|unknown"):
            load_replan_plan(_write_plan(tmp_path / "plan.md", external=external))

    @pytest.mark.parametrize(
        "external",
        [
            "external_dependencies:\n"
            "  - blocked: {subtask_id: missing}\n"
            "    blocker: {issue_number: 500}\n",
            "external_dependencies:\n"
            "  - blocked: {subtask_id: task-a}\n"
            "    blocker: {subtask_id: task-b}\n",
            "external_dependencies:\n"
            "  - blocked: {issue_number: 500}\n"
            "    blocker: {issue_number: 501}\n",
            "external_dependencies:\n"
            "  - blocked: {subtask_id: task-a}\n"
            "    blocker: {issue_number: 693}\n",
            "external_dependencies:\n"
            "  - blocked: {subtask_id: task-a}\n"
            "    blocker: {issue_number: 11}\n",
            "external_dependencies:\n"
            "  - blocked: {subtask_id: task-a}\n"
            "    blocker: {issue_number: 500}\n"
            "  - blocked: {subtask_id: task-a}\n"
            "    blocker: {issue_number: 500}\n",
            "external_dependencies:\n"
            "  - blocked: {subtask_id: task-a}\n"
            "    blocker: {issue_number: 500}\n"
            "  - blocked: {issue_number: 500}\n"
            "    blocker: {subtask_id: task-a}\n",
        ],
    )
    def test_rejects_invalid_or_cyclic_external_graphs(
        self, tmp_path: Path, external: str
    ) -> None:
        with pytest.raises(ValueError):
            load_replan_plan(_write_plan(tmp_path / "plan.md", external=external))


class TestSharedRenderingContract:
    def test_public_renderers_define_provision_and_replan_expected_state(
        self, tmp_path: Path
    ) -> None:
        subtask = _subtask()
        body = build_subtask_issue_body(
            subtask,
            _template(),
            tmp_path,
            693,
            runtime_metadata={"recompute_count": 3, "forced_serial": True},
        )

        assert subtask_issue_title(subtask) == "[FEAT] task-a: Implement A"
        assert derive_subtask_labels(subtask, dependencies_done=False) == (
            "status:queued",
            "priority:high",
        )
        assert body.count(GENERATED_SUBTASK_START) == 1
        assert body.count(GENERATED_SUBTASK_END) == 1
        assert body.endswith("\n\n## Human Notes\n")
        assert "recompute_count: 3" in body
        assert "forced_serial: true" in body
        assert parent_issue_number_from_body(body) == 693

    def test_default_template_keeps_warning_inside_managed_region(self) -> None:
        template = (Path(__file__).parents[1] / ".github/issue_template.md").read_text(
            encoding="utf-8"
        )
        repo_root = Path(__file__).parents[1]
        subtask = dataclasses.replace(
            _subtask(),
            footprint=("orchestune/provisioning/rendering.py",),
            symbols=("definitely_missing_symbol",),
        )
        body = build_subtask_issue_body(subtask, template, repo_root, 693)
        managed = body.split(GENERATED_SUBTASK_START, 1)[1].split(
            GENERATED_SUBTASK_END, 1
        )[0]
        suffix = body.split(GENERATED_SUBTASK_END, 1)[1]
        assert "symbols未検出" in managed
        assert "symbols未検出" not in suffix
        assert suffix == "\n\n## Human Notes\n"


class TestManagedBody:
    def test_replaces_only_managed_region_and_preserves_runtime_metadata(
        self, tmp_path: Path
    ) -> None:
        current = build_subtask_issue_body(
            _subtask(),
            _template(),
            tmp_path,
            693,
            runtime_metadata={"recompute_count": 2, "forced_serial": True},
        )
        start = current.index(GENERATED_SUBTASK_START)
        end = current.index(GENERATED_SUBTASK_END) + len(GENERATED_SUBTASK_END)
        current = "  prefix\n" + current[start:end] + "\n\n## Human Notes\nkeep  \n"
        expected = build_subtask_issue_body(
            _subtask(description="New description"), _template(), tmp_path, 693
        )

        updated = reconcile_managed_body(current, expected)

        assert updated.startswith("  prefix\n" + GENERATED_SUBTASK_START)
        assert updated.endswith("\n\n## Human Notes\nkeep  \n")
        assert "New description" in updated
        assert "recompute_count: 2" in updated
        assert "forced_serial: true" in updated

    @pytest.mark.parametrize(
        "current",
        [
            f"{GENERATED_SUBTASK_START}\nbody",
            f"body\n{GENERATED_SUBTASK_END}",
            f"{GENERATED_SUBTASK_END}\n{GENERATED_SUBTASK_START}",
            f"{GENERATED_SUBTASK_START}\na\n{GENERATED_SUBTASK_START}\nb\n{GENERATED_SUBTASK_END}",
        ],
    )
    def test_malformed_markers_fail_closed(self, tmp_path: Path, current: str) -> None:
        expected = build_subtask_issue_body(_subtask(), _template(), tmp_path, 693)
        with pytest.raises(ManagedBodyConflict):
            reconcile_managed_body(current, expected)

    def test_unknown_footprint_metadata_fails_closed(self, tmp_path: Path) -> None:
        current = build_subtask_issue_body(_subtask(), _template(), tmp_path, 693)
        current = current.replace(
            "model_tier: strong", "model_tier: strong\nhuman_key: keep"
        )
        expected = build_subtask_issue_body(_subtask(), _template(), tmp_path, 693)
        with pytest.raises(ManagedBodyConflict, match="unknown"):
            reconcile_managed_body(current, expected)

    def test_legacy_migrates_only_when_known_renderer_proves_equivalence(
        self, tmp_path: Path
    ) -> None:
        expected = build_subtask_issue_body(_subtask(), _template(), tmp_path, 693)
        legacy = expected.split(GENERATED_SUBTASK_START + "\n", 1)[1].split(
            "\n" + GENERATED_SUBTASK_END, 1
        )[0]

        assert reconcile_managed_body(legacy, expected, legacy_body=legacy) == expected
        with pytest.raises(ManagedBodyConflict):
            reconcile_managed_body(
                legacy + "\nmanual edit", expected, legacy_body=legacy
            )


class TestImmutableDtos:
    def test_endpoint_and_dependency_are_immutable_and_strict(self) -> None:
        endpoint = EndpointRef(subtask_id="task-a")
        dependency = ExternalDependency(
            blocked=endpoint, blocker=EndpointRef(issue_number=500)
        )
        assert dependency.blocked.subtask_id == "task-a"
        with pytest.raises(dataclasses.FrozenInstanceError):
            endpoint.subtask_id = "task-b"  # type: ignore[misc]
        with pytest.raises(ValueError):
            ExternalDependency(
                blocked=EndpointRef(subtask_id="task-a"),
                blocker=EndpointRef(subtask_id="task-b"),
            )

    def test_snapshot_fingerprint_normalizes_order_and_newlines(self) -> None:
        first = IssueSnapshot(
            number=10,
            title="Title",
            body="a\r\nb",
            labels=("z", "a"),
            state="OPEN",
            parent_issue_number=693,
            blocked_by=(4, 2),
            merged_closing_prs=(30, 20),
        )
        second = dataclasses.replace(
            first,
            body="a\nb",
            labels=("a", "z"),
            blocked_by=(2, 4),
            merged_closing_prs=(20, 30),
        )
        assert first.fingerprint == second.fingerprint

    def test_change_kind_and_disposition_are_separate_contracts(self) -> None:
        change = ReplanChange(
            kind=ChangeKind.UPDATE_BODY,
            disposition=Disposition.SAFE,
            issue_number=10,
            reason="managed body differs",
        )
        policy = ApplyPolicy(safe_statuses=(StatusLabel.QUEUED,))
        assert change.kind is ChangeKind.UPDATE_BODY
        assert change.disposition is Disposition.SAFE
        assert policy.safe_statuses == ("status:queued",)
