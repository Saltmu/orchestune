"""JSON results and CLI failures are distinct, including shared exit 2."""

import json
from unittest.mock import patch

import pytest

from orchestune.workflow.cli import main
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


@pytest.mark.parametrize(
    "args",
    [[], ["check", "bad"], ["check", "pre-pr", "--issue", "0", "--session-dir", "s"]],
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
