from pathlib import Path

from test_remediation_spec import write_spec

from orchestune.issue_parsing import DECOMPOSITION_PLAN_BLOCK_PATTERN
from orchestune.remediation.body import append_plan, registration_body
from orchestune.remediation.spec import load_remediation_spec


def test_parent_block_preserves_surrounding_bytes() -> None:
    original = "Before\r\n<!-- orchestune:decomposition-plan -->\r\n```yaml\r\nsubtasks: []\r\n```\r\n\r\n## Human Notes\r\nKeep exactly\r\n"
    updated = append_plan(original, [{"id": "fix", "issue_number": 42}])
    assert DECOMPOSITION_PLAN_BLOCK_PATTERN.sub(
        "", updated
    ) == DECOMPOSITION_PLAN_BLOCK_PATTERN.sub("", original)


def test_existing_body_preserves_non_metadata(tmp_path: Path) -> None:
    spec = load_remediation_spec(write_spec(tmp_path))
    original = "Before\n```yaml\ndepends_on: [old]\n```\nAfter\nblocked-by #10"
    updated = registration_body(original, spec.tasks[0], 1, spec.spec_revision)
    assert updated.startswith("Before\n```yaml\n")
    assert "\nAfter\nblocked-by #10" in updated
    assert registration_body(updated, spec.tasks[0], 1, spec.spec_revision) == updated
