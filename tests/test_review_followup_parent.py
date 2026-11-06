"""#1703: a review-limit follow-up must not become a child of the source EPIC."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import orchestune.integrator.parent_completion as parent_completion
from orchestune.infra.git_cli import MergeProbeResult, MergeProbeStatus
from orchestune.integrator.parent_completion import process_parent_completion
from orchestune.issue_parsing import find_children_by_parent
from orchestune.models import IssueRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("local-ci-developer", "workflow-template")
PARENT = 1590
FOLLOWUP = 1697
PLANNED = (1683, 1684, 1685, 1686, 1687, 1688, 1689)
HEADING = "### Terminal judgment posting (round limit)"
ISOLATION_PHRASES = (
    "new `subtask_id`",
    "`parent_issue_number: null`",
    "`depends_on: []`",
    "Do not copy the source parent number",
    "source `subtask_id`",
    "`owner_kind`",
    "`claim_id`",
    "`reservation_kind`",
    "`blocked-by #<source Issue>`",
    "source PR or finding URL",
    "even when the source has a `subtask_id`",
    "same DAG",
    "native sub-issue of the source parent EPIC",
    "parent's manifest",
    "queued label does not decide parentage",
    "subtask_id: followup-unique-id",
    "path/to/affected-file.py",
    "read back the Issue body and the native parent",
    "no native parent",
    "do not post the final judgment table",
    "do not file a duplicate",
)


def _section(skill_name: str) -> str:
    text = (
        REPO_ROOT / "skills" / skill_name / "references" / "review-loop.md"
    ).read_text(encoding="utf-8")
    assert HEADING in text
    return text.split(HEADING, 1)[1].split("\n### ", 1)[0]


def _issue(
    number: int,
    *,
    state: str,
    body: str,
    parent: dict | None = None,
    blocked_by: tuple[int, ...] = (),
    labels: tuple[str, ...] = (),
) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=f"Issue {number}",
        body=body,
        labels=labels,
        created_at="2026-11-05T00:00:00Z",
        state=state,
        parent=parent,
        blocked_by=blocked_by,
    )


def _planned(number: int) -> IssueRecord:
    body = (
        f"```yaml\nsubtask_id: planned-{number}\n"
        f"parent_issue_number: {PARENT}\n```\n"
    )
    return _issue(number, state="CLOSED", body=body)


def _followup(parent_line: str | None) -> IssueRecord:
    lines = [
        "subtask_id: followup-unique-id",
        "footprint:",
        "  - path/to/affected-file.py",
        "symbols: []",
        "depends_on: []",
    ]
    if parent_line is not None:
        lines.append(parent_line)
    body = (
        "Residual of #1686. blocked-by #1686.\n"
        "The source epic #1590 is prose context only.\n"
        "```yaml\n" + "\n".join(lines) + "\n```\n"
    )
    return _issue(
        FOLLOWUP,
        state="OPEN",
        body=body,
        blocked_by=(1686,),
    )


@pytest.mark.parametrize("skill_name", SKILLS)
def test_review_loops_isolate_independent_followups(skill_name: str) -> None:
    section = _section(skill_name)
    for phrase in ISOLATION_PHRASES:
        assert phrase in section
    assert "`depends_on: [<source subtask_id>]`" not in section


def test_followup_footprint_example_matches_across_review_loops() -> None:
    examples = []
    for skill_name in SKILLS:
        match = re.search(
            r"```text\n(subtask_id: followup-unique-id\n.*?)```",
            _section(skill_name),
            re.S,
        )
        assert match
        examples.append(match.group(1))
    assert examples[0] == examples[1]
    assert "parent_issue_number: null" in examples[0]
    assert "depends_on: []" in examples[0]


def _clean_probe(*_args: object, **_kwargs: object) -> MergeProbeResult:
    return MergeProbeResult(MergeProbeStatus.clean, (), "")


def _capture_final_pr(captured: dict[str, object]):
    def ensure(*_args: object, **kwargs: object) -> int:
        captured["children"] = kwargs["children"]
        return 777

    return ensure


@pytest.mark.parametrize("parent_line", ["parent_issue_number: null", None])
def test_open_followup_does_not_block_parent_final_pr(
    parent_line: str | None, fake_forge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata search may return the follow-up; effective parent drops it."""
    planned = [_planned(number) for number in PLANNED]
    followup = _followup(parent_line)
    captured: dict[str, object] = {}
    fake_forge.list_sub_issues.return_value = planned
    fake_forge.find_issues_by_parent_metadata.return_value = [*planned, followup]
    fake_forge.get_merged_pr_timestamp.return_value = None
    monkeypatch.setattr(parent_completion, "probe_merge_conflicts", _clean_probe)
    monkeypatch.setattr(
        parent_completion, "ensure_parent_final_pr", _capture_final_pr(captured)
    )

    discovered = find_children_by_parent(fake_forge, PARENT)

    assert followup.number not in {issue.number for issue in discovered.issues}
    assert {issue.number for issue in discovered.issues} == set(PLANNED)
    result = process_parent_completion(PARENT, apply=True, forge=fake_forge)
    assert result["status"] == "final_pr_ready"
    assert result["pr_number"] == 777
    assert FOLLOWUP not in result.get("open_children", [])
    passed = captured["children"]
    assert isinstance(passed, tuple | list)
    assert FOLLOWUP not in {child.number for child in passed}


def test_real_open_children_still_block_parent_completion(fake_forge) -> None:
    """Metadata children, unqueued children, and native children still block.

    A native parent is kept even when the body says parent_issue_number null.
    """
    metadata_child = _issue(
        1700,
        state="OPEN",
        body="```yaml\nsubtask_id: real-metadata\nparent_issue_number: 1590\n```\n",
        labels=(),
    )
    native_null_body = _issue(
        1701,
        state="OPEN",
        body="```yaml\nsubtask_id: native-added\nparent_issue_number: null\n```\n",
        parent={"number": PARENT},
    )
    reopened = _issue(
        1702,
        state="OPEN",
        body="```yaml\nsubtask_id: native-reopened\nparent_issue_number: 1590\n```\n",
        parent={"number": PARENT},
    )
    followup = _followup("parent_issue_number: null")
    planned = [_planned(number) for number in PLANNED]
    fake_forge.list_sub_issues.return_value = [*planned, native_null_body, reopened]
    fake_forge.find_issues_by_parent_metadata.return_value = [
        *planned,
        metadata_child,
        native_null_body,
        followup,
    ]

    discovered = find_children_by_parent(fake_forge, PARENT)
    numbers = {issue.number for issue in discovered.issues}

    assert numbers == {*PLANNED, 1700, 1701, 1702}
    assert FOLLOWUP not in numbers
    assert "status:queued" not in metadata_child.labels
    result = process_parent_completion(PARENT, apply=True, forge=fake_forge)
    assert result["status"] == "waiting_on_children"
    assert result["open_children"] == [1700, 1701, 1702]
