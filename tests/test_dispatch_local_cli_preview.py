"""Preview of local CLI dispatch targets without launching them (#1892)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.dispatch import targets as dispatch_targets
from orchestune.dispatch.config import loader as config_loader
from orchestune.dispatch.dispatcher import main
from orchestune.dispatch.phase import rebase as rebase_phase
from orchestune.dispatch.reporting.report_output import ReportOutput
from tests.dispatch_test_support import make_footprint_issue as _issue
from tests.dispatch_test_support import (
    stub_forge_check_auth,
    stub_label_actor_permission,
)

_APPLY_WARNING = (
    "Local CLI launch with --apply requires explicit --allow-unsafe-agent-execution."
)


@pytest.fixture(autouse=True)
def _stub_forge_check_auth_by_default(fake_forge):
    """`GitHubForge.check_auth()`が実際のgh認証エラーを投げないようスタブする。"""
    return stub_forge_check_auth(fake_forge)


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default(fake_forge):
    """#119のactor権限検証が実際の`gh api`を叩かないようスタブする。"""
    stub_label_actor_permission(fake_forge)


@pytest.fixture(autouse=True)
def _resolve_legacy_temp_cwds(monkeypatch):
    """Route outside-repo cwd through this checkout, matching the CLI config tests."""
    from orchestune.dispatch import dispatcher

    resolve = dispatcher._resolve_dispatch_shared_paths
    repository_cwd = Path.cwd()

    def _resolve(args, cwd):
        if cwd is not None and (
            not cwd.resolve().is_relative_to(repository_cwd)
            or cwd.resolve().is_relative_to(repository_cwd / ".orchestune/tmp")
        ):
            cwd = repository_cwd
        return resolve(args, cwd)

    monkeypatch.setattr(dispatcher, "_resolve_dispatch_shared_paths", _resolve)


def _queue_nothing(fake_forge) -> None:
    fake_forge.list_issues_by_label.side_effect = lambda label, **_: []
    fake_forge.list_open_prs.return_value = []


@pytest.mark.parametrize(
    "toml_key",
    ["allow_unsafe_agent_execution", "allow-unsafe-agent-execution"],
)
@pytest.mark.parametrize("apply_flag", [[], ["--apply"]])
def test_toml_unsafe_key_is_rejected_for_preview_and_apply(
    tmp_path, capsys, toml_key, apply_flag
):
    (tmp_path / "orchestune.toml").write_text(f"{toml_key} = true\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        main(
            ["--parent-issue", "100", "--dispatch-target", "grok-cli", *apply_flag],
            cwd=tmp_path,
        )
    assert excinfo.value.code == 2
    assert "prohibited" in capsys.readouterr().err


def test_local_cli_preview_without_flag_reports_plan_without_launch(
    tmp_path, fake_forge, capsys, monkeypatch
):
    report_path = tmp_path / "result.json"
    summary_path = tmp_path / "summary.md"
    events_log = tmp_path / "events.jsonl"
    run_state = tmp_path / "run_state.json"
    # Outside-repo cwd is remapped onto this checkout, so a relative
    # run-state-path would read the primary ledger and exhaust quota.
    (tmp_path / "orchestune.toml").write_text(
        "max-concurrent = 2\n"
        "max-launches-per-window = 10\n"
        f"run-state-path = '{run_state}'\n"
        f"events-log-path = '{events_log}'\n"
        f"log-dir = '{tmp_path / 'logs'}'\n"
        f"worktree-root = '{tmp_path / 'worktrees'}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCHESTUNE_DISPATCH_REPORT_PATH", str(report_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    queued = _issue(10)
    fake_forge.list_issues_by_label.reset_mock(side_effect=True)
    fake_forge.list_open_prs.reset_mock(side_effect=True)
    fake_forge.list_open_prs.return_value = []
    fake_forge.add_label.reset_mock(side_effect=True)
    fake_forge.list_issues_by_label.side_effect = lambda label, **_: (
        [queued] if label == "status:queued" else []
    )
    monkeypatch.setattr(
        config_loader.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"claude", "codex"} else None,
    )
    monkeypatch.setattr(rebase_phase, "list_remote_branches", lambda: [])
    code = main(
        ["--parent-issue", "100", "--dispatch-target", "claude-cli"],
        cwd=tmp_path,
    )
    assert code == 0
    assert not (tmp_path / "logs").exists()
    fake_forge.add_label.assert_not_called()
    captured = capsys.readouterr()
    assert _APPLY_WARNING in captured.err
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["applied"] is False
    assert payload["quota_slots_available"] > 0
    assert payload["selected"]
    assert payload["local_cli_preview"] == {
        "target": "claude-cli",
        "reviewer": "codex",
        "command_source": "builtin",
        "requires_unsafe_opt_in": True,
        "warning_code": "unsafe_agent_execution_opt_in_required",
    }
    summary = summary_path.read_text(encoding="utf-8")
    assert _APPLY_WARNING in summary
    assert "builtin CLI=claude" in summary
    assert "reviewer=codex" in captured.err
    assert not events_log.exists()
    assert not run_state.exists()


def test_local_cli_preview_with_flag_omits_warning_and_does_not_launch(
    tmp_path, fake_forge, capsys, monkeypatch
):
    report_path = tmp_path / "result.json"
    monkeypatch.setenv("ORCHESTUNE_DISPATCH_REPORT_PATH", str(report_path))
    _queue_nothing(fake_forge)
    code = main(
        [
            "--parent-issue",
            "100",
            "--dispatch-target",
            "grok-cli",
            "--allow-unsafe-agent-execution",
        ],
        cwd=tmp_path,
    )
    assert code == 0
    assert not (tmp_path / "logs").exists()
    assert _APPLY_WARNING not in capsys.readouterr().err
    plan = json.loads(report_path.read_text(encoding="utf-8"))["local_cli_preview"]
    assert plan["target"] == "grok-cli"
    assert plan["requires_unsafe_opt_in"] is False
    assert "warning_code" not in plan
    assert plan["command_source"] == "builtin"


def test_auto_preview_without_flag_reports_resolved_target(
    tmp_path, fake_forge, capsys, monkeypatch
):
    report_path = tmp_path / "result.json"
    monkeypatch.setenv("ORCHESTUNE_DISPATCH_REPORT_PATH", str(report_path))
    _queue_nothing(fake_forge)
    monkeypatch.setattr(
        config_loader.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"claude", "codex"} else None,
    )
    monkeypatch.setattr(dispatch_targets, "detect_installed_local_cli", lambda: "codex")
    code = main(["--parent-issue", "100", "--dispatch-target", "auto"], cwd=tmp_path)
    assert code == 0
    plan = json.loads(report_path.read_text(encoding="utf-8"))["local_cli_preview"]
    assert plan["target"] == "codex-cli"
    assert plan["reviewer"] == "claude"
    assert plan["warning_code"] == "unsafe_agent_execution_opt_in_required"
    assert "codex" in capsys.readouterr().err


def test_custom_preview_hides_command_text(tmp_path, fake_forge, capsys, monkeypatch):
    secret = "do-not-print-secret-argv"
    (tmp_path / "orchestune.toml").write_text(
        f"dispatch-target = 'claude-cli'\nlocal-cmd = '{secret} {{issue_number}}'\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "result.json"
    monkeypatch.setenv("ORCHESTUNE_DISPATCH_REPORT_PATH", str(report_path))
    _queue_nothing(fake_forge)
    code = main(["--parent-issue", "100"], cwd=tmp_path)
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["local_cli_preview"]["command_source"] == "custom"
    assert secret not in captured.err
    assert secret not in report_path.read_text(encoding="utf-8")


def test_non_local_cli_preview_omits_plan_key(
    tmp_path, fake_forge, capsys, monkeypatch
):
    report_path = tmp_path / "result.json"
    monkeypatch.setenv("ORCHESTUNE_DISPATCH_REPORT_PATH", str(report_path))
    _queue_nothing(fake_forge)
    monkeypatch.setattr(dispatch_targets.shutil, "which", lambda _name: None)
    code = main(
        ["--parent-issue", "100", "--dispatch-target", "auto"],
        cwd=tmp_path,
    )
    assert code == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert "local_cli_preview" not in payload
    assert _APPLY_WARNING not in capsys.readouterr().err


@pytest.mark.parametrize("dispatch_target", ["local", "cloud-routine", "codex-cloud"])
def test_explicit_non_local_cli_preview_omits_plan_key(
    tmp_path, fake_forge, capsys, monkeypatch, dispatch_target
):
    report_path = tmp_path / "result.json"
    monkeypatch.setenv("ORCHESTUNE_DISPATCH_REPORT_PATH", str(report_path))
    _queue_nothing(fake_forge)
    code = main(
        ["--parent-issue", "100", "--dispatch-target", dispatch_target],
        cwd=tmp_path,
    )
    assert code == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert "local_cli_preview" not in payload
    assert _APPLY_WARNING not in capsys.readouterr().err


def test_preview_save_failure_keeps_exit_code(
    tmp_path, fake_forge, capsys, monkeypatch
):
    report_path = tmp_path / "result.json"
    monkeypatch.setenv("ORCHESTUNE_DISPATCH_REPORT_PATH", str(report_path))
    _queue_nothing(fake_forge)
    with patch.object(ReportOutput, "save", side_effect=OSError("disk full")):
        code = main(
            ["--parent-issue", "100", "--dispatch-target", "agy-cli"],
            cwd=tmp_path,
        )
    assert code == 1
    assert "report save failed" in capsys.readouterr().err


def test_auto_apply_without_flag_exits_config_error(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        dispatch_targets.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )
    with pytest.raises(SystemExit) as excinfo:
        main(
            ["--parent-issue", "100", "--apply", "--dispatch-target", "auto"],
            cwd=tmp_path,
        )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "完全権限実行となります" in err
    assert "プレビューを含むすべての実行" not in err
