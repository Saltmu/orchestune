from pathlib import Path

import pytest
import yaml

from orchestune.remediation.spec import load_remediation_spec


def write_spec(tmp_path: Path, **changes: object) -> Path:
    task: dict[str, object] = {
        "id": "fix",
        "description": "Repair",
        "footprint": ["src/a.py"],
        "kind": "integration-bug",
        "parent_acceptance": ["Works"],
        "origins": ["#10"],
        "reason": "Required to finish",
    }
    task.update(changes)
    path = tmp_path / "remediation.md"
    path.write_text(
        "---\n"
        + yaml.safe_dump({"parent_issue_number": 1, "subtasks": [task]})
        + "---\nNotes\n"
    )
    return path


def test_revision_ignores_number_writeback_and_markdown(tmp_path: Path) -> None:
    first = load_remediation_spec(write_spec(tmp_path))
    second = load_remediation_spec(write_spec(tmp_path, issue_number=42))
    assert first.spec_revision == second.spec_revision
    assert first.spec_revision.startswith("sha256:")
    assert second.tasks[0].subtask.issue_number == 42


@pytest.mark.parametrize(
    "changes",
    [
        {"typo": True},
        {"parent_acceptance": []},
        {"origins": "#10"},
        {"reason": " "},
        {"issue_number": True},
        {"execution_profile": "BAD"},
    ],
)
def test_rejects_bad_task(tmp_path: Path, changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        load_remediation_spec(write_spec(tmp_path, **changes))


def test_unknown_top_level_key(tmp_path: Path) -> None:
    path = write_spec(tmp_path)
    path.write_text(path.read_text().replace("parent_issue_number:", "typo:"))
    with pytest.raises(ValueError, match="unknown"):
        load_remediation_spec(path)


def test_external_dependencies_are_deferred_to_parent_snapshot(tmp_path: Path) -> None:
    spec = load_remediation_spec(write_spec(tmp_path, depends_on=["old"]))
    assert spec.tasks[0].subtask.depends_on == ("old",)


@pytest.mark.parametrize("tier", ["weak", "middle", "strong", "", False, 0, [], {}])
def test_spec_rejects_retired_model_tier(tmp_path, tier):
    with pytest.raises(ValueError, match="model_tier.*retired.*execution_profile"):
        load_remediation_spec(write_spec(tmp_path, model_tier=tier))


def test_spec_null_model_tier_does_not_change_revision(tmp_path):
    from dataclasses import asdict

    first = load_remediation_spec(
        write_spec(tmp_path, execution_profile="deep-reasoning")
    )
    second = load_remediation_spec(
        write_spec(tmp_path, execution_profile="deep-reasoning", model_tier=None)
    )
    assert first.spec_revision == second.spec_revision
    assert "model_tier" not in asdict(second.tasks[0].subtask)
