"""Tests for orchestune doctor readiness checks (issue #1648)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestune.dispatch.doctor import cli as doctor_cli
from orchestune.dispatch.doctor.check import (
    DoctorRequest,
    run_doctor,
)
from orchestune.dispatch.doctor.models import (
    ALL_CODES,
    CODE_ACTIONS_CANCEL,
    CODE_ACTIONS_CREDENTIALS,
    CODE_ACTIONS_GROUP,
    CODE_CONFIG_READABLE,
    CODE_LOCAL_SERIALIZATION,
    CODE_READINESS_AUTH,
    CODE_READINESS_CI_COMMAND,
    CODE_READINESS_CI_DEFINITION,
    CODE_READINESS_DISCIPLINE,
    CODE_READINESS_EXECUTOR,
    CODE_READINESS_IGNORE,
    CODE_READINESS_REVIEWER,
    CODE_STATE_CONTINUITY,
    OWNERSHIP_CODES,
)
from orchestune.dispatch.doctor.readiness import run_readiness_checks
from orchestune.infra.process_utils import platform_ci_script_relative
from orchestune.session_dirs import ORCHESTUNE_GITIGNORE_LINES


def test_all_codes_ordering_and_length() -> None:
    assert len(ALL_CODES) == 20
    assert ALL_CODES[0] == CODE_CONFIG_READABLE
    assert ALL_CODES[2] == CODE_ACTIONS_GROUP
    assert ALL_CODES[3] == CODE_ACTIONS_CANCEL
    assert ALL_CODES[-1] == CODE_STATE_CONTINUITY

    idx_cred = ALL_CODES.index(CODE_ACTIONS_CREDENTIALS)
    idx_serial = ALL_CODES.index(CODE_LOCAL_SERIALIZATION)
    assert idx_serial - idx_cred == 8

    readiness_slice = ALL_CODES[idx_cred + 1 : idx_serial]
    assert readiness_slice == (
        CODE_READINESS_DISCIPLINE,
        CODE_READINESS_CI_DEFINITION,
        CODE_READINESS_CI_COMMAND,
        CODE_READINESS_EXECUTOR,
        CODE_READINESS_REVIEWER,
        CODE_READINESS_IGNORE,
        CODE_READINESS_AUTH,
    )
    # Ensure ownership codes unchanged
    assert CODE_READINESS_DISCIPLINE not in OWNERSHIP_CODES
    assert CODE_READINESS_AUTH not in OWNERSHIP_CODES


def test_cli_help_and_request_positional_compat(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        doctor_cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--online" in out
    assert "--offline" not in out

    # Positional compatibility: DoctorRequest(mode, repo_root, workflows)
    req = DoctorRequest("local", Path("/tmp"), ())
    assert req.mode == "local"
    assert req.repo_root == Path("/tmp")
    assert req.workflows == ()
    assert req.online is False

    req_online = DoctorRequest("local", Path("/tmp"), (), online=True)
    assert req_online.online is True


def test_session_dirs_gitignore_lines_constant() -> None:
    assert isinstance(ORCHESTUNE_GITIGNORE_LINES, tuple)
    assert len(ORCHESTUNE_GITIGNORE_LINES) == 14
    assert ".orchestune/tmp/" in ORCHESTUNE_GITIGNORE_LINES
    assert ".orchestune" in ORCHESTUNE_GITIGNORE_LINES


class TestDisciplineCheck:
    def test_discipline_ok_when_agents_md_exists(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("rules", encoding="utf-8")
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_DISCIPLINE)
        assert diag.status == "ok"
        assert "AGENTS.md" in diag.evidence[0]

    def test_discipline_ok_when_claude_md_exists(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("rules", encoding="utf-8")
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_DISCIPLINE)
        assert diag.status == "ok"
        assert "CLAUDE.md" in diag.evidence[0]

    def test_discipline_ok_when_dot_agents_agents_md_exists(
        self, tmp_path: Path
    ) -> None:
        agents_dir = tmp_path / ".agents"
        agents_dir.mkdir()
        (agents_dir / "AGENTS.md").write_text("rules", encoding="utf-8")
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_DISCIPLINE)
        assert diag.status == "ok"
        assert ".agents/AGENTS.md" in diag.evidence[0]

    def test_discipline_warning_when_missing(self, tmp_path: Path) -> None:
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_DISCIPLINE)
        assert diag.status == "warning"
        assert diag.remediation is not None
        assert (
            "orchestune skills install --target all --scope project --with-workflow-skill"
            in diag.remediation
        )


class TestCiDefinitionCheck:
    def test_ci_definition_ok_when_workflows_exist(self, tmp_path: Path) -> None:
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("name: CI", encoding="utf-8")
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_DEFINITION)
        assert diag.status == "ok"

    def test_ci_definition_ok_when_suggest_ci_commands_available(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n", encoding="utf-8")
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_DEFINITION)
        assert diag.status == "ok"

    def test_ci_definition_ok_when_explicit_ci_command_executable_exists(
        self, tmp_path: Path
    ) -> None:
        script = tmp_path / "run_tests.sh"
        script.write_text("#!/bin/sh\nexit 0", encoding="utf-8")
        config = {"ci_command": "./run_tests.sh"}
        diags = run_readiness_checks(tmp_path, config, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_DEFINITION)
        assert diag.status == "ok"

    def test_ci_definition_warning_when_none(self, tmp_path: Path) -> None:
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_DEFINITION)
        assert diag.status == "warning"
        assert diag.remediation is not None


class TestCiCommandCheck:
    def test_ci_command_fallback_script_exists(self, tmp_path: Path) -> None:
        rel_script = platform_ci_script_relative()
        full_path = tmp_path / rel_script
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text("#!/bin/sh\nexit 0", encoding="utf-8")
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_COMMAND)
        assert diag.status == "ok"
        assert str(rel_script) in diag.evidence[0]

    def test_ci_command_missing_fallback_script_warns(self, tmp_path: Path) -> None:
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_COMMAND)
        assert diag.status == "warning"
        assert "2" in diag.message  # apply dispatch exits with code 2
        assert diag.remediation is not None
        assert 'ci-command = "pytest"' in diag.remediation

    def test_ci_command_configured_absolute_file(self, tmp_path: Path) -> None:
        script = tmp_path / "ci.sh"
        script.write_text("#!/bin/sh", encoding="utf-8")
        config = {"ci_command": str(script.resolve())}
        diags = run_readiness_checks(tmp_path, config, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_COMMAND)
        assert diag.status == "ok"

    def test_ci_command_configured_relative_file_in_repo(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        tools.mkdir()
        script = tools / "ci.sh"
        script.write_text("#!/bin/sh", encoding="utf-8")
        config = {"ci_command": "tools/ci.sh"}
        diags = run_readiness_checks(tmp_path, config, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_COMMAND)
        assert diag.status == "ok"

    def test_ci_command_configured_relative_missing_file_warns(
        self, tmp_path: Path
    ) -> None:
        config = {"ci_command": "tools/ci.sh"}
        diags = run_readiness_checks(tmp_path, config, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_COMMAND)
        assert diag.status == "warning"

    def test_ci_command_configured_directory_warns(self, tmp_path: Path) -> None:
        tools = tmp_path / "tools"
        tools.mkdir()
        config = {"ci_command": "./tools"}
        diags = run_readiness_checks(tmp_path, config, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_COMMAND)
        assert diag.status == "warning"

    def test_ci_command_windows_drive_relative_warns(self, tmp_path: Path) -> None:
        config = {"ci_command": "C:tools/ci.sh"}
        diags = run_readiness_checks(tmp_path, config, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_COMMAND)
        assert diag.status == "warning"

    def test_ci_command_bare_name_which_found(self, tmp_path: Path) -> None:
        config = {"ci_command": "pytest -v"}
        diags = run_readiness_checks(
            tmp_path,
            config,
            True,
            which_fn=lambda cmd: "/bin/pytest" if cmd == "pytest" else None,
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_COMMAND)
        assert diag.status == "ok"
        assert "/bin/pytest" in diag.evidence[0]

    def test_ci_command_bare_name_which_missing_warns(self, tmp_path: Path) -> None:
        config = {"ci_command": "pytest -v"}
        diags = run_readiness_checks(tmp_path, config, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_CI_COMMAND)
        assert diag.status == "warning"


class TestExecutorAndReviewerChecks:
    def test_executor_local_target_is_ok(self, tmp_path: Path) -> None:
        config = {"dispatch_target": "local"}
        diags = run_readiness_checks(tmp_path, config, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_EXECUTOR)
        assert diag.status == "ok"
        assert "local" in diag.message

    def test_executor_cli_found(self, tmp_path: Path) -> None:
        config = {"dispatch_target": "claude-cli"}
        diags = run_readiness_checks(
            tmp_path,
            config,
            True,
            which_fn=lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_EXECUTOR)
        assert diag.status == "ok"

    def test_executor_cli_missing_warns(self, tmp_path: Path) -> None:
        config = {"dispatch_target": "claude-cli"}
        diags = run_readiness_checks(tmp_path, config, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_EXECUTOR)
        assert diag.status == "warning"
        assert "claude" in diag.message

    def test_executor_cloud_routine_ready(self, tmp_path: Path) -> None:
        config = {"dispatch_target": "cloud-routine", "routine_id": "rout-123"}
        env = {"ORCHESTUNE_ROUTINE_TOKEN": "token-xyz"}
        diags = run_readiness_checks(
            tmp_path, config, True, env=env, which_fn=lambda _: None
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_EXECUTOR)
        assert diag.status == "ok"
        # Secret token must not be in evidence or message
        assert "token-xyz" not in str(diag.evidence)
        assert "token-xyz" not in diag.message

    def test_executor_cloud_routine_missing_credentials_warns_and_falls_back_to_local_for_reviewer(
        self, tmp_path: Path
    ) -> None:
        config = {"dispatch_target": "cloud-routine"}
        diags = run_readiness_checks(
            tmp_path, config, True, env={}, which_fn=lambda _: None
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_EXECUTOR)
        assert diag.status == "warning"
        assert "ORCHESTUNE_ROUTINE_TOKEN" in (diag.remediation or "")

    def test_executor_codex_cloud_ready(self, tmp_path: Path) -> None:
        config = {"dispatch_target": "codex-cloud", "codex_cloud_env": "env-123"}
        diags = run_readiness_checks(
            tmp_path, config, True, env={}, which_fn=lambda _: None
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_EXECUTOR)
        assert diag.status == "ok"

    def test_executor_codex_cloud_missing_warns(self, tmp_path: Path) -> None:
        config = {"dispatch_target": "codex-cloud"}
        diags = run_readiness_checks(
            tmp_path, config, True, env={}, which_fn=lambda _: None
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_EXECUTOR)
        assert diag.status == "warning"

    def test_executor_explicit_auto_does_not_use_actions_cloud_default(
        self, tmp_path: Path
    ) -> None:
        config = {"dispatch_target": "auto"}
        env = {"GITHUB_ACTIONS": "true"}
        # Installed CLI: codex
        diags = run_readiness_checks(
            tmp_path,
            config,
            True,
            env=env,
            which_fn=lambda cmd: "/bin/codex" if cmd == "codex" else None,
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_EXECUTOR)
        assert diag.status == "ok"
        assert "codex-cli" in diag.message

    def test_reviewer_auto_resolved_and_cli_found(self, tmp_path: Path) -> None:
        config = {"dispatch_target": "claude-cli", "reviewer_bot": "auto"}
        diags = run_readiness_checks(
            tmp_path,
            config,
            True,
            which_fn=lambda cmd: "/bin/" + cmd if cmd in ("claude", "codex") else None,
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_REVIEWER)
        assert diag.status == "ok"

    def test_reviewer_auto_unresolved_warns(self, tmp_path: Path) -> None:
        # local target and no installed CLI -> unresolved reviewer
        config = {"dispatch_target": "local", "reviewer_bot": "auto"}
        diags = run_readiness_checks(tmp_path, config, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_REVIEWER)
        assert diag.status == "warning"
        assert 'reviewer-bot = "claude"' in (diag.remediation or "")

    def test_reviewer_resolved_but_cli_missing_warns_with_note(
        self, tmp_path: Path
    ) -> None:
        config = {"dispatch_target": "local", "reviewer_bot": "claude"}
        diags = run_readiness_checks(tmp_path, config, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_REVIEWER)
        assert diag.status == "warning"
        assert "claude" in diag.message


class TestIgnoreCheck:
    def test_ignore_ok_when_both_present(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text(
            "orchestune.toml\n.orchestune/\n", encoding="utf-8"
        )
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_IGNORE)
        assert diag.status == "ok"

    def test_ignore_slash_orchestune_toml_and_tmp_pattern(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text(
            "/orchestune.toml\n.orchestune/tmp/\n", encoding="utf-8"
        )
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_IGNORE)
        assert diag.status == "ok"

    def test_ignore_missing_orchestune_toml_warns(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text(".orchestune/\n", encoding="utf-8")
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_IGNORE)
        assert diag.status == "warning"
        assert "orchestune.toml" in (diag.remediation or "")

    def test_ignore_missing_dot_orchestune_warns(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("orchestune.toml\n", encoding="utf-8")
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_IGNORE)
        assert diag.status == "warning"
        assert ".orchestune/" in (diag.remediation or "")

    def test_ignore_missing_file_warns(self, tmp_path: Path) -> None:
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_IGNORE)
        assert diag.status == "warning"

    def test_ignore_invalid_utf8_warns(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_bytes(b"\xff\xfe\x00")
        diags = run_readiness_checks(tmp_path, {}, True, which_fn=lambda _: None)
        diag = next(d for d in diags if d.code == CODE_READINESS_IGNORE)
        assert diag.status == "warning"


class TestAuthCheck:
    def test_auth_omitted_when_offline(self, tmp_path: Path) -> None:
        factory_called = False

        def fake_factory() -> Any:
            nonlocal factory_called
            factory_called = True
            return None

        diags = run_readiness_checks(
            tmp_path,
            {},
            True,
            online=False,
            which_fn=lambda _: None,
            forge_factory=fake_factory,
        )
        assert not any(d.code == CODE_READINESS_AUTH for d in diags)
        assert factory_called is False

    def test_auth_warning_when_gh_missing(self, tmp_path: Path) -> None:
        diags = run_readiness_checks(
            tmp_path, {}, True, online=True, which_fn=lambda _: None
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_AUTH)
        assert diag.status == "warning"
        assert "gh auth login" in (diag.remediation or "")

    def test_auth_warning_when_check_auth_fails(self, tmp_path: Path) -> None:
        class FailingForge:
            def check_auth(self) -> None:
                raise RuntimeError("not authenticated")

            def get_authenticated_user(self) -> str:
                return "user"

        diags = run_readiness_checks(
            tmp_path,
            {},
            True,
            online=True,
            which_fn=lambda cmd: "/bin/gh" if cmd == "gh" else None,
            forge_factory=FailingForge,
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_AUTH)
        assert diag.status == "warning"
        assert "not authenticated" in diag.message

    def test_auth_warning_when_user_empty(self, tmp_path: Path) -> None:
        class EmptyUserForge:
            def check_auth(self) -> None:
                pass

            def get_authenticated_user(self) -> str:
                return ""

        diags = run_readiness_checks(
            tmp_path,
            {},
            True,
            online=True,
            which_fn=lambda cmd: "/bin/gh" if cmd == "gh" else None,
            forge_factory=EmptyUserForge,
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_AUTH)
        assert diag.status == "warning"

    def test_auth_warning_when_user_subprocess_error(self, tmp_path: Path) -> None:
        import subprocess

        class SubprocessErrorForge:
            def check_auth(self) -> None:
                pass

            def get_authenticated_user(self) -> str:
                raise subprocess.CalledProcessError(
                    1, ["gh", "api", "user"], output="", stderr="api error"
                )

        diags = run_readiness_checks(
            tmp_path,
            {},
            True,
            online=True,
            which_fn=lambda cmd: "/bin/gh" if cmd == "gh" else None,
            forge_factory=SubprocessErrorForge,
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_AUTH)
        assert diag.status == "warning"
        assert "gh auth login" in (diag.remediation or "")

    def test_auth_ok_when_success(self, tmp_path: Path) -> None:
        class GoodForge:
            def check_auth(self) -> None:
                pass

            def get_authenticated_user(self) -> str:
                return "octocat"

        diags = run_readiness_checks(
            tmp_path,
            {},
            True,
            online=True,
            which_fn=lambda cmd: "/bin/gh" if cmd == "gh" else None,
            forge_factory=GoodForge,
        )
        diag = next(d for d in diags if d.code == CODE_READINESS_AUTH)
        assert diag.status == "ok"
        assert diag.evidence == ("octocat",)


def test_invalid_config_skips_config_dependent_codes(tmp_path: Path) -> None:
    # When config_valid is False, ci_command, executor, reviewer are omitted.
    # discipline, ci_definition, ignore are diagnosed.
    diags = run_readiness_checks(
        tmp_path, {}, False, online=False, which_fn=lambda _: None
    )
    codes = {d.code for d in diags}
    assert CODE_READINESS_DISCIPLINE in codes
    assert CODE_READINESS_CI_DEFINITION in codes
    assert CODE_READINESS_IGNORE in codes
    assert CODE_READINESS_CI_COMMAND not in codes
    assert CODE_READINESS_EXECUTOR not in codes
    assert CODE_READINESS_REVIEWER not in codes
    assert CODE_READINESS_AUTH not in codes


def test_run_doctor_integration_and_clean_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bare tmp dir with git init
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    report = run_doctor(DoctorRequest("local", tmp_path, ()))
    # Readiness diagnostics are warning or ok, never error
    readiness_diags = [
        d for d in report.diagnostics if d.code.startswith("dispatch.readiness.")
    ]
    assert len(readiness_diags) >= 6
    assert all(d.status in ("ok", "warning") for d in readiness_diags)
    assert not report.has_error
