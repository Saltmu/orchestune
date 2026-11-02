"""Grok CLI (`grok-cli`) local dispatch target (#1416).

No real `grok` binary is executed: `subprocess.Popen` and `shutil.which` are mocked.
The NDJSON fixture is synthetic (the official docs do not publish the JSON schema),
so these tests pin that the generic usage parser yields no usage for it.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.dispatch.execution_profiles import ExecutionSelection
from orchestune.dispatch.targets import (
    GROK_CLI_LOCAL_CMD_TEMPLATE,
    LOCAL_CLI_CANDIDATES,
    LocalProcessDispatchTarget,
    TargetBuildConfig,
    _local_cli_name,
    build_dispatch_target,
    detect_installed_local_cli,
)
from orchestune.models import Task
from orchestune.targets.completion_policy import token_limit_decision

FIXTURE = Path(__file__).parent / "fixtures" / "grok_streaming_json_no_usage.ndjson"


def _task() -> Task:
    return Task(
        issue_number=7,
        subtask_id="task-a",
        footprint=("src/foo.py",),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:queued",),
        created_at="2026-01-01T00:00:00+00:00",
    )


def _launch(tmp_path: Path, selection: ExecutionSelection | None = None):
    target = LocalProcessDispatchTarget(
        log_dir=tmp_path / "logs", local_cmd=GROK_CLI_LOCAL_CMD_TEMPLATE
    )
    with patch("orchestune.dispatch.targets.subprocess.Popen") as popen:
        popen.return_value.pid = 4_301_001
        handle = target.launch(
            _task(),
            "claude/issue-7-task-a",
            tmp_path / "wt",
            execution_selection=selection,
        )
    return target, handle, popen


def test_default_template_uses_headless_flags():
    template = GROK_CLI_LOCAL_CMD_TEMPLATE
    assert template.startswith('grok -p "')
    for flag in (
        "--always-approve",
        "--no-plan",
        "--no-auto-update",
        "--output-format streaming-json",
    ):
        assert flag in template
    assert "--dangerously-skip-permissions" not in template
    assert "--cwd" not in template


def test_launch_embeds_issue_branch_and_base_and_runs_in_worktree(tmp_path):
    _, _, popen = _launch(tmp_path)
    cmd = popen.call_args[0][0]
    assert cmd[0] == "grok"
    assert cmd[1] == "-p"
    assert "#7" in cmd[2]
    assert "`claude/issue-7-task-a`" in cmd[2]
    assert "gh pr create --base" in cmd[2]
    assert cmd[3:] == [
        "--always-approve",
        "--no-plan",
        "--no-auto-update",
        "--output-format",
        "streaming-json",
    ]
    assert Path(popen.call_args.kwargs["cwd"]) == tmp_path / "wt"


def test_launch_adds_model_and_effort_without_validating_effort(tmp_path):
    selection = ExecutionSelection(
        profile="p", model="grok-model-x", reasoning_effort="whatever", reason="t"
    )
    _, _, popen = _launch(tmp_path, selection)
    cmd = popen.call_args[0][0]
    assert cmd[cmd.index("--model") + 1] == "grok-model-x"
    assert cmd[cmd.index("--effort") + 1] == "whatever"


def test_launch_without_selection_adds_neither_model_nor_effort(tmp_path):
    _, _, popen = _launch(tmp_path)
    cmd = popen.call_args[0][0]
    assert "--model" not in cmd
    assert "--effort" not in cmd


def test_local_cli_name_recognizes_grok():
    assert "grok" in LOCAL_CLI_CANDIDATES
    assert _local_cli_name(["/opt/bin/GROK.exe"]) == "grok"


def test_build_dispatch_target_requires_unsafe_flag(tmp_path):
    with pytest.raises(ValueError, match="allow-unsafe-agent-execution"):
        build_dispatch_target(
            TargetBuildConfig("grok-cli", None, None, tmp_path / "logs")
        )


def test_build_dispatch_target_uses_grok_preset(tmp_path):
    target = build_dispatch_target(
        TargetBuildConfig(
            "grok-cli",
            None,
            None,
            tmp_path / "logs",
            allow_unsafe_agent_execution=True,
        )
    )
    assert isinstance(target, LocalProcessDispatchTarget)
    assert target._local_cmd == GROK_CLI_LOCAL_CMD_TEMPLATE


def test_usage_missing_from_unknown_schema_makes_token_limit_unknown(tmp_path):
    target, handle, _ = _launch(tmp_path)
    log = tmp_path / "logs" / "claude-issue-7-task-a.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE, log)

    usage = target.collect_usage(handle)

    assert usage is None
    assert token_limit_decision(1000, usage) == "unknown"


def _only(*names: str):
    return patch(
        "orchestune.dispatch.targets.shutil.which",
        side_effect=lambda name: f"/usr/bin/{name}" if name in names else None,
    )


def test_detects_grok_when_only_grok_installed():
    with _only("grok"):
        assert detect_installed_local_cli() == "grok"


def test_existing_cli_priority_is_unchanged_by_grok():
    with _only("codex", "grok"):
        assert detect_installed_local_cli() == "codex"
    with _only("agy", "grok"):
        assert detect_installed_local_cli() == "agy"
    with _only("claude", "agy", "codex", "grok"):
        assert detect_installed_local_cli() == "claude"


def test_auto_dispatch_uses_grok_preset_when_only_grok_installed(tmp_path):
    with _only("grok"):
        target = build_dispatch_target(
            TargetBuildConfig(
                "auto",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
    assert isinstance(target, LocalProcessDispatchTarget)
    assert target._local_cmd == GROK_CLI_LOCAL_CMD_TEMPLATE


def test_auto_warning_mentions_grok_when_no_cli_installed(tmp_path, capsys):
    with patch("orchestune.dispatch.targets.shutil.which", return_value=None):
        build_dispatch_target(TargetBuildConfig("auto", None, None, tmp_path / "logs"))
    assert "grok" in capsys.readouterr().err
