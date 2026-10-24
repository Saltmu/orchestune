"""Tests for child issue body parsing, reversibility validation, and update_issue_body callers."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from orchestune.dag.models import SubTask
from orchestune.issue_parsing import FOOTPRINT_BLOCK_PATTERN
from orchestune.plan_identity import PlanGeneration, PlanRevision
from orchestune.provisioning.body_parsing import (
    extract_subtask_section,
    parse_subtask_issue_body,
    validate_reversible_subtask,
)
from orchestune.provisioning.rendering import (
    _render_issue_body,
    subtask_issue_title,
)

DEFAULT_TEMPLATE = Path(".github/issue_template.md").read_text(encoding="utf-8")

CUSTOM_TEMPLATE = """# Custom Subtask: {{subtask_id}} - {{description}}

### 概要
{{overview}}

### 変更点
{{proposed_changes}}

### 基準
{{acceptance_criteria}}

### 検証
{{verification_plan}}

### メタデータ
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


def _sample_subtask(**kwargs: Any) -> SubTask:
    defaults: dict[str, Any] = {
        "id": "sample-task",
        "description": "Implement sample task",
        "overview": "Overview of sample task.\nContains multiple lines.",
        "proposed_changes": ("Change line 1", "Change line 2\n  continuation"),
        "acceptance_criteria": ("Criteria 1", "Criteria 2"),
        "verification_plan": ("Run pytest tests/",),
        "footprint": ("orchestune/sample.py",),
        "symbols": ("sample_func",),
        "depends_on": ("dep-1",),
        "risk": False,
        "risk_reasons": (),
        "priority": "high",
        "shared_contract": "contract.md",
        "writes_shared_contract": True,
        "execution_profile": "deep-reasoning",
        "model_tier": "strong",
    }
    defaults.update(kwargs)
    return SubTask(**defaults)


def test_roundtrip_default_template() -> None:
    subtask = _sample_subtask()
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)
    title = subtask_issue_title(subtask)

    parsed = parse_subtask_issue_body(DEFAULT_TEMPLATE, title, body)

    assert parsed["subtask_id"] == subtask.id
    assert parsed["overview"] == subtask.overview
    assert parsed["proposed_changes"] == list(subtask.proposed_changes)
    assert parsed["acceptance_criteria"] == list(subtask.acceptance_criteria)
    assert parsed["verification_plan"] == list(subtask.verification_plan)
    assert parsed["footprint"] == list(subtask.footprint)
    assert parsed["symbols"] == list(subtask.symbols)
    assert parsed["depends_on"] == list(subtask.depends_on)
    assert parsed["shared_contract"] == subtask.shared_contract
    assert parsed["writes_shared_contract"] == subtask.writes_shared_contract
    assert parsed["parent_issue_number"] == 100
    assert parsed["execution_profile"] == subtask.execution_profile
    assert parsed["model_tier"] == subtask.model_tier


def test_roundtrip_empty_lists_and_defaults() -> None:
    subtask = _sample_subtask(
        overview="特になし",
        proposed_changes=(),
        acceptance_criteria=(),
        verification_plan=(),
        shared_contract=None,
        writes_shared_contract=False,
        execution_profile=None,
        model_tier=None,
    )
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE)
    title = subtask_issue_title(subtask)

    parsed = parse_subtask_issue_body(DEFAULT_TEMPLATE, title, body)
    assert parsed["overview"] == "特になし"
    assert parsed["proposed_changes"] == []
    assert parsed["acceptance_criteria"] == []
    assert parsed["verification_plan"] == []
    assert parsed["shared_contract"] is None
    assert parsed["writes_shared_contract"] is False
    assert parsed["parent_issue_number"] is None
    assert parsed["execution_profile"] is None
    assert parsed["model_tier"] is None


def test_item_named_tokuninashi() -> None:
    subtask = _sample_subtask(
        proposed_changes=("特になし",),
    )
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE)
    title = subtask_issue_title(subtask)

    parsed = parse_subtask_issue_body(DEFAULT_TEMPLATE, title, body)
    assert parsed["proposed_changes"] == ["特になし"]


def test_roundtrip_custom_template() -> None:
    subtask = _sample_subtask()
    body = _render_issue_body(subtask, CUSTOM_TEMPLATE, parent_issue_number=100)
    title = f"Custom Subtask: {subtask.id} - {subtask.description}"

    parsed = parse_subtask_issue_body(CUSTOM_TEMPLATE, title, body)
    assert parsed["subtask_id"] == subtask.id
    assert parsed["overview"] == subtask.overview


def test_known_change_replan_generation_marker() -> None:
    subtask = _sample_subtask()
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)
    rev = PlanRevision("replan-v1:sha256:" + "a" * 64)
    gen = PlanGeneration(rev, subtask.id)
    body_with_gen = f"{gen.marker}\n{body}"
    title = subtask_issue_title(subtask)

    parsed = parse_subtask_issue_body(DEFAULT_TEMPLATE, title, body_with_gen)
    assert parsed["subtask_id"] == subtask.id


def test_known_change_replan_generation_marker_mismatch_rejected() -> None:
    subtask = _sample_subtask()
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)
    rev = PlanRevision("replan-v1:sha256:" + "a" * 64)
    gen = PlanGeneration(rev, "different-id")
    body_with_gen = f"{gen.marker}\n{body}"
    title = subtask_issue_title(subtask)

    with pytest.raises(ValueError, match="replan generation subtask_id"):
        parse_subtask_issue_body(DEFAULT_TEMPLATE, title, body_with_gen)


def test_known_change_yaml_reformatting_and_runtime_keys() -> None:
    subtask = _sample_subtask()
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)
    match = FOOTPRINT_BLOCK_PATTERN.search(body)
    assert match is not None

    footprint_data = {
        "claim_id": "claim-12345",
        "recompute_count": 2,
        "subtask_id": subtask.id,
        "symbols": list(subtask.symbols),
        "footprint": list(subtask.footprint),
        "depends_on": list(subtask.depends_on),
        "shared_contract": subtask.shared_contract,
        "writes_shared_contract": subtask.writes_shared_contract,
        "parent_issue_number": 100,
        "execution_profile": subtask.execution_profile,
        "model_tier": subtask.model_tier,
    }
    dumped = yaml.safe_dump(footprint_data, sort_keys=False)
    new_body = body[: match.start()] + f"```yaml\n{dumped}```" + body[match.end() :]
    title = subtask_issue_title(subtask)

    parsed = parse_subtask_issue_body(DEFAULT_TEMPLATE, title, new_body)
    assert parsed["subtask_id"] == subtask.id
    assert "claim_id" not in parsed
    assert "recompute_count" not in parsed


def test_known_change_symbols_warning() -> None:
    subtask = _sample_subtask()
    base_body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)
    warning = (
        "\n\n---\n\n⚠️ **symbols未検出**: 以下のシンボルは、Footprintに列挙されたファイル内に"
        "見つかりませんでした。このsubtaskで新規追加する予定であれば問題ありません"
        "が、リファクタによる改名・移動で古い名称が残っている可能性もあるため、"
        "着手前にコードを確認してください。\n- `missing_sym_1`\n- `missing_sym_2`\n"
    )
    body_with_warning = base_body + warning
    title = subtask_issue_title(subtask)

    parsed = parse_subtask_issue_body(DEFAULT_TEMPLATE, title, body_with_warning)
    assert parsed["subtask_id"] == subtask.id


def test_known_change_launch_attempt_journal() -> None:
    subtask = _sample_subtask()
    base_body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)
    journal = (
        "\n\n<!-- orchestune:launch-attempt -->\n```json\n"
        '{"attempt_id": "att-1", "phase": "launched", "target": "worker", '
        '"branch": "test-branch", "base_branch": "main", "started_at": 1000.0, '
        '"external_id": "ext-1"}\n```\n'
    )
    body_with_journal = base_body + journal
    title = subtask_issue_title(subtask)

    parsed = parse_subtask_issue_body(DEFAULT_TEMPLATE, title, body_with_journal)
    assert parsed["subtask_id"] == subtask.id


def test_known_change_remediation_section() -> None:
    subtask = _sample_subtask()
    base_body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)
    remediation_section = (
        "\n\n## 補修タスクの根拠\n\n"
        "- Kind: repair\n"
        "- Parent acceptance: crit-1, crit-2\n"
        "- Origins: orig-1\n"
        "- Reason: fix bug\n\n"
        "<!-- orchestune:remediation-v1 -->\n"
        "```json\n"
        '{"kind": "repair", "origins": ["orig-1"], "parent_acceptance": ["crit-1", "crit-2"], '
        '"parent_issue_number": 100, "reason": "fix bug", "spec_revision": "rev-1", '
        '"subtask_id": "sample-task"}\n'
        "```\n"
    )
    body_with_remediation = base_body + remediation_section
    title = subtask_issue_title(subtask)

    parsed = parse_subtask_issue_body(DEFAULT_TEMPLATE, title, body_with_remediation)
    assert parsed["subtask_id"] == subtask.id


def test_combination_symbols_remediation_launch_attempt() -> None:
    subtask = _sample_subtask()
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)
    body += (
        "\n\n---\n\n⚠️ **symbols未検出**: 以下のシンボルは、Footprintに列挙されたファイル内に"
        "見つかりませんでした。このsubtaskで新規追加する予定であれば問題ありません"
        "が、リファクタによる改名・移動で古い名称が残っている可能性もあるため、"
        "着手前にコードを確認してください。\n- `missing_sym`"
    )
    body += (
        "\n\n## 補修タスクの根拠\n\n"
        "- Kind: repair\n"
        "- Parent acceptance: crit-1\n"
        "- Origins: orig-1\n"
        "- Reason: fix bug\n\n"
        "<!-- orchestune:remediation-v1 -->\n"
        "```json\n"
        '{"kind": "repair", "origins": ["orig-1"], "parent_acceptance": ["crit-1"], '
        '"parent_issue_number": 100, "reason": "fix bug", "spec_revision": "rev-1", '
        '"subtask_id": "sample-task"}\n'
        "```\n"
    )
    body += (
        "\n\n<!-- orchestune:launch-attempt -->\n```json\n"
        '{"attempt_id": "att-1", "phase": "launched", "target": "worker", '
        '"branch": "test-branch", "base_branch": "main", "started_at": 1000.0, '
        '"external_id": "ext-1"}\n```\n'
    )
    title = subtask_issue_title(subtask)

    parsed = parse_subtask_issue_body(DEFAULT_TEMPLATE, title, body)
    assert parsed["subtask_id"] == subtask.id


def test_rejection_duplicate_yaml_keys() -> None:
    subtask = _sample_subtask()
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)
    match = FOOTPRINT_BLOCK_PATTERN.search(body)
    assert match is not None

    dup_yaml = f"```yaml\nsubtask_id: {subtask.id}\nsubtask_id: other-id\nfootprint: []\nsymbols: []\ndepends_on: []\n```"
    bad_body = body[: match.start()] + dup_yaml + body[match.end() :]
    title = subtask_issue_title(subtask)

    with pytest.raises(ValueError, match="duplicate key|ConstructorError"):
        parse_subtask_issue_body(DEFAULT_TEMPLATE, title, bad_body)


def test_rejection_external_modification_in_fixed_section() -> None:
    subtask = _sample_subtask()
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)
    # Modify fixed heading
    bad_body = body.replace("## 概要 (Overview)", "## Overview Only")
    title = subtask_issue_title(subtask)

    with pytest.raises(ValueError, match="mismatch|differing line|could not match"):
        parse_subtask_issue_body(DEFAULT_TEMPLATE, title, bad_body)


def test_rejection_reports_first_differing_line() -> None:
    subtask = _sample_subtask()
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)
    # Tamper with the title subtask_id so it mismatches the Footprint YAML subtask_id
    bad_body = body.replace("# [FEAT] sample-task:", "# [FEAT] other-task:")
    title = subtask_issue_title(subtask)

    with pytest.raises(ValueError, match="Body mismatch at line 1"):
        parse_subtask_issue_body(DEFAULT_TEMPLATE, title, bad_body)


def test_yaml_example_in_overview_distinguished() -> None:
    subtask = _sample_subtask(
        overview="Here is an example YAML:\n```yaml\nfoo: bar\n```\nEnd of overview."
    )
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)
    title = subtask_issue_title(subtask)

    # First ```yaml in body is inside overview, but template's Footprint position is later!
    # Parser should reject because first yaml fence does not align with Footprint block!
    with pytest.raises(ValueError, match="Footprint fence position"):
        parse_subtask_issue_body(DEFAULT_TEMPLATE, title, body)


def test_extract_subtask_section() -> None:
    subtask = _sample_subtask()
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE, parent_issue_number=100)

    extracted = extract_subtask_section(DEFAULT_TEMPLATE, body, "acceptance_criteria")
    assert extracted.strip() == "- Criteria 1\n- Criteria 2"


def test_validate_reversible_subtask_valid() -> None:
    subtask = _sample_subtask(
        proposed_changes=("Valid item 1", "Valid item 2"),
        acceptance_criteria=("Crit 1",),
        verification_plan=("Plan 1",),
    )
    validate_reversible_subtask(subtask)


def test_validate_reversible_subtask_newline_dash() -> None:
    subtask = _sample_subtask(
        proposed_changes=("Item with\n- nested dash",),
    )
    with pytest.raises(ValueError, match="newline followed by '- '"):
        validate_reversible_subtask(subtask)


def test_validate_reversible_subtask_whitespace() -> None:
    subtask = _sample_subtask(
        proposed_changes=(" Leading space",),
    )
    with pytest.raises(ValueError, match="leading or trailing whitespace"):
        validate_reversible_subtask(subtask)


def test_validate_reversible_subtask_empty() -> None:
    subtask = _sample_subtask(
        proposed_changes=("",),
    )
    with pytest.raises(ValueError, match="empty string"):
        validate_reversible_subtask(subtask)


safe_chars = st.characters(
    whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_ "
)
safe_item = (
    st.text(safe_chars, min_size=1, max_size=40)
    .map(lambda s: s.strip())
    .filter(lambda s: bool(s) and not s.startswith("-"))
)


@given(
    subtask_id=st.text(
        st.characters(whitelist_categories=("Lu", "Ll", "Nd")), min_size=2, max_size=15
    ),
    description=st.text(safe_chars, min_size=2, max_size=30)
    .map(lambda s: s.strip())
    .filter(bool),
    overview=st.text(safe_chars, min_size=2, max_size=50)
    .map(lambda s: s.strip())
    .filter(bool),
    changes=st.lists(safe_item, min_size=0, max_size=3),
    criteria=st.lists(safe_item, min_size=0, max_size=3),
    verify=st.lists(safe_item, min_size=0, max_size=3),
)
def test_hypothesis_property_parse_render(
    subtask_id: str,
    description: str,
    overview: str,
    changes: list[str],
    criteria: list[str],
    verify: list[str],
) -> None:
    subtask = SubTask(
        id=subtask_id,
        description=description,
        overview=overview,
        proposed_changes=tuple(changes),
        acceptance_criteria=tuple(criteria),
        verification_plan=tuple(verify),
        footprint=("orchestune/foo.py",),
        symbols=("func_a",),
        depends_on=(),
        risk=False,
        risk_reasons=(),
    )
    validate_reversible_subtask(subtask)
    body = _render_issue_body(subtask, DEFAULT_TEMPLATE)
    title = subtask_issue_title(subtask)

    parsed = parse_subtask_issue_body(DEFAULT_TEMPLATE, title, body)
    assert parsed["subtask_id"] == subtask.id
    assert parsed["overview"] == subtask.overview
    assert parsed["proposed_changes"] == list(subtask.proposed_changes)
    assert parsed["acceptance_criteria"] == list(subtask.acceptance_criteria)
    assert parsed["verification_plan"] == list(subtask.verification_plan)


KNOWN_UPDATE_ISSUE_BODY_CALLERS = {
    # Parent issue body writers
    ("orchestune/provisioning/parent.py", "parent"),
    ("orchestune/provisioning/plan.py", "parent"),
    ("orchestune/remediation/apply.py", "parent"),
    ("orchestune/replan/apply.py", "parent"),
    # Child issue body writers
    ("orchestune/claim/amend.py", "child"),
    ("orchestune/claim/issue_metadata.py", "child"),
    ("orchestune/dispatch/attempt_record.py", "child"),
    ("orchestune/dispatch/launch.py", "child"),
    ("orchestune/dispatch/rebase.py", "child"),
    ("orchestune/provisioning/subtasks.py", "child"),
    ("orchestune/recovery/blocked_restart.py", "child"),
    ("orchestune/remediation/operations.py", "child"),
}


def test_all_update_issue_body_callers_are_known() -> None:
    """Ensure every caller of `update_issue_body` in orchestune/ matches the known list."""
    repo_root = Path(__file__).resolve().parent.parent
    orchestune_dir = repo_root / "orchestune"
    actual_callers: set[str] = set()

    for py_file in orchestune_dir.rglob("*.py"):
        rel_path = py_file.relative_to(repo_root).as_posix()
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update_issue_body"
            ):
                # Exclude forge interface definitions and issues_rest implementation
                if rel_path in {
                    "orchestune/forge/__init__.py",
                    "orchestune/forge/issues.py",
                    "orchestune/forge/issues_rest.py",
                }:
                    continue
                actual_callers.add(rel_path)

    expected_callers = {path for path, _ in KNOWN_UPDATE_ISSUE_BODY_CALLERS}
    assert actual_callers == expected_callers


def test_validate_reversible_subtask_rejects_tokuninashi_item() -> None:
    subtask = {
        "id": "item-tokuninashi",
        "proposed_changes": ["特になし"],
    }
    with pytest.raises(
        ValueError,
        match="cannot be '特になし' \\(reserved for empty section placeholder\\)",
    ):
        validate_reversible_subtask(subtask)
