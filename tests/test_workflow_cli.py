"""JSON results and CLI failures are distinct, including shared exit 2."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.workflow.cli import main
from orchestune.workflow.contracts import WorkflowResult
from tests.test_workflow_readers import prepared_context


def test_json_status_controls_exit(tmp_path, capsys):
    context = prepared_context(tmp_path)
    with patch("orchestune.workflow.cli.read_context", return_value=context):
        assert (
            main(
                [
                    "check",
                    "pre-claim",
                    "--issue",
                    "1496",
                    "--session-dir",
                    str(context.session_dir),
                    "--json",
                ]
            )
            == 0
        )
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1
    assert result["gate_id"] == "pre-claim"
    assert result["status"] == "ready"
    assert result["observed_head_sha"] == "a" * 40


def test_next_command_reads_context_and_emits_stage_json(tmp_path, capsys):
    context = prepared_context(tmp_path)
    next_result = WorkflowResult(
        issue_number=context.issue_number,
        claim_id=context.state["claim_id"],
        stage_id="scope",
        gate_id=None,
        status="ready",
        reason_codes=("impact_missing",),
        checks=(),
        actions=(),
        references=(),
        artifacts={},
        observed_head_sha=context.head_sha,
    )
    with (
        patch("orchestune.workflow.cli.read_context", return_value=context) as read,
        patch("orchestune.workflow.cli.next_step", return_value=next_result) as run,
    ):
        assert (
            main(
                [
                    "next",
                    "--issue",
                    "1496",
                    "--session-dir",
                    str(context.session_dir),
                    "--json",
                ]
            )
            == 0
        )
    read.assert_called_once()
    run.assert_called_once()
    result = json.loads(capsys.readouterr().out)
    assert result["stage_id"] == "scope"
    assert result["gate_id"] is None
    assert result["status"] == "ready"


def test_next_forwards_optional_pr_evidence_arguments(tmp_path, capsys):
    context = prepared_context(tmp_path)
    next_result = WorkflowResult(
        issue_number=context.issue_number,
        claim_id=context.state["claim_id"],
        stage_id="review",
        gate_id=None,
        status="ready",
        reason_codes=(),
        checks=(),
        actions=(),
        references=(),
        artifacts={},
        observed_head_sha=context.head_sha,
    )
    with (
        patch("orchestune.workflow.cli.read_context", return_value=context) as read,
        patch("orchestune.workflow.cli.next_step", return_value=next_result),
    ):
        assert (
            main(
                [
                    "next",
                    "--issue",
                    "1496",
                    "--session-dir",
                    str(context.session_dir),
                    "--pr",
                    "42",
                    "--pr-body",
                    "pr-body.md",
                    "--review-reply",
                    "review-reply.md",
                    "--snapshot",
                    "snapshot.json",
                    "--json",
                ]
            )
            == 0
        )
    read.assert_called_once_with(
        issue_number=1496,
        session_dir=context.session_dir,
        pr_number=42,
        pr_body=Path("pr-body.md"),
        review_reply=Path("review-reply.md"),
        snapshot=Path("snapshot.json"),
    )
    assert json.loads(capsys.readouterr().out)["stage_id"] == "review"


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["check", "bad"],
        ["check", "pre-pr", "--issue", "0", "--session-dir", "s"],
        ["next", "--issue", "0", "--session-dir", "s"],
    ],
)
def test_usage_errors_produce_no_workflow_json(args, capsys):
    with pytest.raises(SystemExit) as error:
        main([*args, "--json"])
    assert error.value.code == 2
    assert capsys.readouterr().out == ""


def test_internal_tool_failure_has_no_json(tmp_path, capsys):
    with patch(
        "orchestune.workflow.cli.load_stage_definitions",
        side_effect=ValueError("invalid package stages"),
    ):
        assert (
            main(
                [
                    "check",
                    "pre-pr",
                    "--issue",
                    "1",
                    "--session-dir",
                    str(tmp_path),
                    "--json",
                ]
            )
            == 1
        )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid package stages" in captured.err
