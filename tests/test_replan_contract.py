from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from orchestune.dag.models import SubTask
from orchestune.issue_parsing import embed_decomposition_plan_in_parent_body
from orchestune.provisioning.plan_loading import load_plan
from orchestune.provisioning.rendering import (
    build_subtask_issue_body,
    derive_subtask_labels,
    subtask_issue_title,
)
from orchestune.replan import (
    PlanGeneration,
    PlanRevision,
    ReplacementResult,
    RetirementCandidate,
)
from orchestune.replan.plan import (
    build_replacement_preview,
    compute_plan_revision,
    load_replan_plan,
)


def _subtask(subtask_id: str = "task-a") -> SubTask:
    return SubTask(
        id=subtask_id,
        description="Implement the contract",
        footprint=("orchestune/replan/models.py",),
        symbols=("PlanGeneration",),
        depends_on=(),
        risk=False,
        risk_reasons=(),
        priority="high",
        overview="Keep replacement deterministic",
        acceptance_criteria=("The contract is immutable",),
        proposed_changes=("Add generation identity",),
        verification_plan=("Run focused tests",),
    )


def _write_plan(
    path: Path,
    *,
    issue_numbers: tuple[int | None, ...] = (101, 102),
    depends_on: tuple[tuple[str, ...], ...] = ((), ("task-a",)),
    prose: str = "# Replacement plan\n\nApproved semantics.",
) -> Path:
    subtasks = []
    for index, subtask_id in enumerate(("task-a", "task-b")):
        entry: dict[str, object] = {
            "id": subtask_id,
            "description": f"Task {subtask_id}",
            "overview": "Overview",
            "proposed_changes": ["One change"],
            "acceptance_criteria": ["One criterion"],
            "verification_plan": ["One check"],
            "footprint": [f"src/{subtask_id}.py"],
            "symbols": [subtask_id.replace("-", "_")],
            "depends_on": list(depends_on[index]),
            "priority": "high" if index == 0 else "medium",
            "risk": False,
        }
        if issue_numbers[index] is not None:
            entry["issue_number"] = issue_numbers[index]
        subtasks.append(entry)

    frontmatter = {
        "title": "Replacement",
        "parent_issue_number": 693,
        "parent_issue_source": "adopted",
        "subtasks": subtasks,
    }
    rendered = yaml.dump(
        frontmatter,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    path.write_text(f"---\n{rendered}---\n\n{prose}\n", encoding="utf-8")
    return path


def test_plan_revision_ignores_only_subtask_issue_number_writeback(
    tmp_path: Path,
) -> None:
    first = _write_plan(tmp_path / "first.md", issue_numbers=(101, 102))
    second = _write_plan(tmp_path / "second.md", issue_numbers=(901, None))

    assert compute_plan_revision(first) == compute_plan_revision(second)

    changed = _write_plan(
        tmp_path / "changed.md",
        issue_numbers=(901, None),
        prose="# Replacement plan\n\nChanged approved semantics.",
    )
    assert compute_plan_revision(first) != compute_plan_revision(changed)


def test_replan_reuses_the_pure_provisioning_plan_loader(tmp_path: Path) -> None:
    path = _write_plan(tmp_path / "plan.md")

    shared_subtasks, shared_metadata = load_plan(path)
    replan = load_replan_plan(path)

    assert replan.subtasks == tuple(shared_subtasks)
    assert replan.title == shared_metadata.title
    assert replan.parent_issue_number == shared_metadata.parent_issue_number
    assert replan.parent_issue_source == shared_metadata.parent_issue_source
    assert replan.description == shared_metadata.description


def test_plan_revision_is_stable_across_yaml_representation(tmp_path: Path) -> None:
    compact = _write_plan(tmp_path / "compact.md")
    data = yaml.safe_load(compact.read_text(encoding="utf-8").split("---", 2)[1])
    represented = tmp_path / "represented.md"
    represented.write_text(
        "---\n"
        + yaml.dump(data, allow_unicode=True, default_flow_style=None, sort_keys=True)
        + "---\n\n# Replacement plan\n\nApproved semantics.\n",
        encoding="utf-8",
    )

    assert compute_plan_revision(compact) == compute_plan_revision(represented)


def test_plan_revision_normalizes_set_like_order_but_keeps_body_list_order(
    tmp_path: Path,
) -> None:
    first = _write_plan(tmp_path / "first.md")
    raw = yaml.safe_load(first.read_text(encoding="utf-8").split("---", 2)[1])
    raw["subtasks"][0]["proposed_changes"] = ["First", "Second"]
    raw["subtasks"].reverse()
    raw["subtasks"][0]["footprint"] = ["src/extra.py", "src/task-b.py"]
    raw["subtasks"][0]["symbols"] = ["z", "task_b"]
    reordered = tmp_path / "reordered.md"
    reordered.write_text(
        "---\n"
        + yaml.dump(raw, allow_unicode=True, default_flow_style=False, sort_keys=True)
        + "---\n\n# Replacement plan\n\nApproved semantics.\n",
        encoding="utf-8",
    )

    original_with_same_sets = _write_plan(tmp_path / "same-sets.md")
    original_raw = yaml.safe_load(
        original_with_same_sets.read_text(encoding="utf-8").split("---", 2)[1]
    )
    original_raw["subtasks"][0]["proposed_changes"] = ["First", "Second"]
    original_raw["subtasks"][1]["footprint"] = ["src/task-b.py", "src/extra.py"]
    original_raw["subtasks"][1]["symbols"] = ["task_b", "z"]
    original_with_same_sets.write_text(
        "---\n"
        + yaml.dump(
            original_raw,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        + "---\n\n# Replacement plan\n\nApproved semantics.\n",
        encoding="utf-8",
    )

    assert compute_plan_revision(reordered) == compute_plan_revision(
        original_with_same_sets
    )

    original_raw["subtasks"][0]["proposed_changes"] = ["Second", "First"]
    body_order_changed = tmp_path / "body-order.md"
    body_order_changed.write_text(
        "---\n"
        + yaml.dump(
            original_raw,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        + "---\n\n# Replacement plan\n\nApproved semantics.\n",
        encoding="utf-8",
    )

    assert compute_plan_revision(body_order_changed) != compute_plan_revision(
        original_with_same_sets
    )


def test_replan_plan_rejects_internal_cycles(tmp_path: Path) -> None:
    path = _write_plan(tmp_path / "cycle.md", depends_on=(("task-b",), ("task-a",)))

    with pytest.raises(ValueError, match="cycle"):
        load_replan_plan(path)


def test_replan_plan_rejects_duplicate_issue_number_writeback(tmp_path: Path) -> None:
    path = _write_plan(tmp_path / "duplicate.md", issue_numbers=(101, 101))

    with pytest.raises(ValueError, match="issue_number.*duplicated"):
        load_replan_plan(path)


def test_replan_plan_rejects_parent_issue_as_a_subtask_issue(tmp_path: Path) -> None:
    path = _write_plan(tmp_path / "parent-alias.md", issue_numbers=(693, 102))

    with pytest.raises(ValueError, match="parent.*subtask issue_number"):
        load_replan_plan(path)


def test_generation_marker_identifies_revision_and_subtask_pair() -> None:
    first_revision = PlanRevision("replan-v1:sha256:" + "1" * 64)
    second_revision = PlanRevision("replan-v1:sha256:" + "2" * 64)
    first = PlanGeneration(first_revision, "task-a")

    assert str(first_revision) in first.marker
    assert first.matches_body(f"prose\n{first.marker}\nmore prose")
    assert not PlanGeneration(second_revision, "task-a").matches_body(first.marker)
    assert not PlanGeneration(first_revision, "task-b").matches_body(first.marker)


def test_preview_uses_new_generation_and_only_old_plan_issue_numbers(
    tmp_path: Path,
) -> None:
    old = load_replan_plan(_write_plan(tmp_path / "old.md", issue_numbers=(101, None)))
    new = load_replan_plan(_write_plan(tmp_path / "new.md", issue_numbers=(999, 998)))

    preview = build_replacement_preview(old, new)

    assert preview.plan_revision == compute_plan_revision(new)
    assert tuple(item.subtask_id for item in preview.generations) == (
        "task-a",
        "task-b",
    )
    assert preview.retirement_candidates == (RetirementCandidate("task-a", 101),)
    assert all(
        generation.plan_revision == preview.plan_revision
        for generation in preview.generations
    )


def test_replacement_result_is_immutable_and_deterministically_normalized() -> None:
    revision = PlanRevision("replan-v1:sha256:" + "a" * 64)
    result = ReplacementResult(
        plan_revision=revision,
        created_issue_numbers=(9, 7),
        reused_issue_numbers=(12, 11),
        retired_issue_numbers=(4, 3),
    )

    assert result.created_issue_numbers == (7, 9)
    assert result.reused_issue_numbers == (11, 12)
    assert result.retired_issue_numbers == (3, 4)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.degraded = True  # type: ignore[misc]


def test_replacement_result_rejects_overlapping_created_and_reused_issues() -> None:
    revision = PlanRevision("replan-v1:sha256:" + "a" * 64)

    with pytest.raises(ValueError, match="created.*reused"):
        ReplacementResult(
            plan_revision=revision,
            created_issue_numbers=(7,),
            reused_issue_numbers=(7,),
        )


def test_shared_provisioning_renderers_add_generation_marker(tmp_path: Path) -> None:
    subtask = _subtask()
    generation = PlanGeneration(
        PlanRevision("replan-v1:sha256:" + "b" * 64), subtask.id
    )
    template = """# [FEAT] {{subtask_id}}: {{description}}

```yaml
subtask_id: {{subtask_id_yaml}}
footprint: {{footprint}}
symbols: {{symbols}}
depends_on: {{depends_on}}
shared_contract: {{shared_contract}}
writes_shared_contract: {{writes_shared_contract}}
parent_issue_number: {{parent_issue_number}}
execution_profile: {{execution_profile}}
model_tier: {{model_tier}}
```
"""

    body = build_subtask_issue_body(
        subtask,
        template,
        tmp_path,
        parent_issue_number=693,
        generation=generation,
    )

    assert body.startswith(f"{generation.marker}\n")
    assert subtask_issue_title(subtask) == "[FEAT] task-a: Implement the contract"
    assert derive_subtask_labels(subtask, dependencies_done=False) == (
        "status:queued",
        "priority:high",
    )


def test_parent_plan_replacement_preserves_text_outside_managed_region() -> None:
    prefix = "Requirements\n\n- Keep this exact\n\n"
    suffix = "\nHuman Notes\n\nDo not rewrite this.\n"
    body = (
        prefix
        + "<!-- orchestune:decomposition-plan -->\n"
        + "```yaml\nold: value\n```\n"
        + suffix
    )

    updated = embed_decomposition_plan_in_parent_body(body, {"new": "value"})

    assert updated.startswith(prefix)
    assert updated.endswith(suffix)
    assert "old: value" not in updated


def test_public_contract_excludes_superseded_reconciliation_types() -> None:
    import orchestune.replan as contract

    removed = {
        "ApplyPolicy",
        "ChangeKind",
        "EndpointRef",
        "ExternalDependency",
        "IssueSnapshot",
        "ReplanChange",
        "ReplanPreview",
    }

    assert removed.isdisjoint(contract.__all__)
    assert all(not hasattr(contract, name) for name in removed)
