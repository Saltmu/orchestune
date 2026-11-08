"""Workflow next actions are displayed without becoming a new decision."""

from orchestune.infra.cli_guidance import render_guidance
from orchestune.operator_guidance import NextOwner, ProgressOutlook
from orchestune.workflow.contracts import WorkflowCheck, WorkflowResult
from orchestune.workflow.guidance import guidance_from_workflow

_REVIEW = (
    "Inspect the current PR trigger, round, and wait process. "
    "Reconnect to the same wait if it is running; resume only "
    "after confirming it terminated. Do not start a concurrent wait."
)


def _result(**overrides: object) -> WorkflowResult:
    values: dict[str, object] = {
        "issue_number": 1763,
        "claim_id": "claim-1",
        "stage_id": "review",
        "gate_id": None,
        "status": "unmet",
        "reason_codes": ("evidence_missing",),
        "checks": (
            WorkflowCheck(id="review_result", status="unmet", missing=("missing",)),
        ),
        "actions": (
            {
                "id": "inspect_review",
                "description": _REVIEW,
                "argv": [],
                "references": ["skills/local-ci-developer/references/review-loop.md"],
                "reason_codes": ("evidence_missing",),
            },
        ),
        "references": ("skills/local-ci-developer/references/review-loop.md",),
        "artifacts": {},
        "observed_head_sha": "a" * 40,
    }
    values.update(overrides)
    return WorkflowResult(**values)  # type: ignore[arg-type]


def test_review_gap_points_at_the_existing_wait_and_not_a_new_one() -> None:
    text = render_guidance(guidance_from_workflow(_result()))
    assert "Reconnect to the same wait" in text
    assert "Do not start a concurrent wait" in text
    assert "コマンド:" not in text
    assert "orchestune review" not in text
    assert "次の担当: unknown" in text


def test_handoff_has_no_invented_action() -> None:
    guidance = guidance_from_workflow(
        _result(
            stage_id="handed-off",
            status="ready",
            reason_codes=("handed_off",),
            checks=(),
            actions=(),
        )
    )
    text = render_guidance(guidance)
    assert guidance.next_owner is NextOwner.NONE
    assert guidance.progress is ProgressOutlook.TERMINAL
    assert guidance.actions == ()
    assert "次の操作: なし" in text


def test_resolved_claim_argv_is_quoted_as_a_command() -> None:
    guidance = guidance_from_workflow(
        _result(
            stage_id="claim",
            status="ready",
            reason_codes=(),
            checks=(),
            actions=(
                {
                    "description": "Use the existing claim operation.",
                    "argv": ["orchestune", "claim", "1763"],
                    "references": [],
                },
            ),
        )
    )
    text = render_guidance(guidance)
    assert "コマンド: orchestune claim 1763" in text
    assert "次の担当: agent" in text
    assert "自動進行: manual" in text
