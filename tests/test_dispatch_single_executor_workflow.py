"""Regression tests for docs/examples/dispatch-single-executor.yml."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path
from subprocess import run
from typing import Any

import pytest

from orchestune.cli import main as cli_main
from orchestune.dispatch.doctor import (
    DoctorRequest,
    discover_workflow_files,
    load_workflow_yaml,
    run_doctor,
)
from orchestune.dispatch.doctor_models import ALL_CODES

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "docs/examples/dispatch-single-executor.yml"
WORKFLOW_REL = ".github/workflows/orchestune-dispatch.yml"
SETUP_DOCS = (REPO_ROOT / "docs/ja/setup.md", REPO_ROOT / "docs/en/setup.md")

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="requires bash",
)


def _workflow() -> dict[str, Any]:
    data = load_workflow_yaml(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _steps() -> list[dict[str, Any]]:
    jobs = _workflow()["jobs"]
    assert len(jobs) == 1
    steps: list[dict[str, Any]] = next(iter(jobs.values()))["steps"]
    return steps


def _step(name_prefix: str) -> dict[str, Any]:
    return next(s for s in _steps() if s.get("name", "").startswith(name_prefix))


def _triggers() -> dict[str, Any]:
    wf: dict[Any, Any] = _workflow()
    triggers: dict[str, Any] = wf["on"] if "on" in wf else wf[True]
    return triggers


def test_triggers_and_concurrency() -> None:
    triggers = _triggers()
    assert "schedule" in triggers
    parent = triggers["workflow_dispatch"]["inputs"]["parent_issue"]
    assert parent["required"] is False
    concurrency = _workflow()["concurrency"]
    assert concurrency == {
        "group": "orchestune-control-${{ github.repository }}",
        "cancel-in-progress": False,
    }
    assert concurrency["cancel-in-progress"] is False


def test_single_job_with_timeout_and_no_expression_in_run() -> None:
    jobs = _workflow()["jobs"]
    assert len(jobs) == 1
    timeout = next(iter(jobs.values()))["timeout-minutes"]
    assert isinstance(timeout, int) and timeout > 0
    for step in _steps():
        assert "${{" not in step.get("run", "")
    env = _step("Dispatch")["env"]
    assert "INPUT_PARENT_ISSUE" in env and "VAR_PARENT_ISSUES" in env


def test_gate_script_static_contract() -> None:
    run_text = _step("Self-diagnose")["run"]
    assert "GITHUB_WORKFLOW_REF" in run_text
    assert "docs/examples" not in run_text
    assert "orchestune doctor --execution-mode actions --workflow" in run_text
    assert "-z" in run_text
    for line in run_text.splitlines():
        if "echo" in line:
            printed = line.split("echo", 1)[1].split(";", 1)[0]
            assert "${!name" not in printed
            assert "$ORCHESTUNE_ROUTINE" not in printed


def _copy_example(root: Path) -> None:
    dest = root / WORKFLOW_REL
    dest.parent.mkdir(parents=True)
    dest.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")


def test_example_is_diagnosed_without_errors(tmp_path: Path) -> None:
    _copy_example(tmp_path)
    report = run_doctor(
        DoctorRequest(mode="actions", repo_root=tmp_path, workflows=(WORKFLOW_REL,))
    )
    assert not report.has_error, [d for d in report.diagnostics if d.status == "error"]
    by_code = {d.code: d.status for d in report.diagnostics}
    for code in (
        "dispatch.actions.group",
        "dispatch.actions.cancel",
        "dispatch.actions.parallelism",
        "dispatch.actions.entrypoint",
        "dispatch.actions.target",
        "dispatch.actions.credentials",
    ):
        assert by_code[code] == "ok", code


def test_examples_directory_is_not_discovered(tmp_path: Path) -> None:
    dest = tmp_path / "docs/examples/dispatch-single-executor.yml"
    dest.parent.mkdir(parents=True)
    dest.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    assert discover_workflow_files(tmp_path) == ()


STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
if [ "$1" = "doctor" ]; then exit "${STUB_DOCTOR_EXIT:-0}"; fi
if [ "$1" = "dispatch" ] && [ "$3" = "${STUB_FAIL_PARENT:-none}" ]; then exit 1; fi
exit 0
"""


def _run_step(
    tmp_path: Path, step_prefix: str, env: dict[str, str]
) -> tuple[int, str, list[str]]:
    script = tmp_path / "step.sh"
    script.write_text(_step(step_prefix)["run"], encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "orchestune"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    full_env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        **env,
    }
    result = run(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=full_env,
    )
    calls = log.read_text(encoding="utf-8").splitlines()
    return result.returncode, result.stdout + result.stderr, calls


@needs_bash
@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"VAR_PARENT_ISSUES": "3 5"}, ["dispatch -p 3", "dispatch -p 5"]),
        (
            {"VAR_PARENT_ISSUES": "3 5", "INPUT_PARENT_ISSUE": "7"},
            ["dispatch -p 7"],
        ),
    ],
)
def test_parent_supply_orders_dispatch(
    tmp_path: Path, env: dict[str, str], expected: list[str]
) -> None:
    code, _, calls = _run_step(tmp_path, "Dispatch", env)
    assert code == 0
    assert [c.rsplit(" --dispatch-target", 1)[0] for c in calls] == expected


@needs_bash
def test_parent_supply_empty_succeeds_without_dispatch(tmp_path: Path) -> None:
    code, out, calls = _run_step(tmp_path, "Dispatch", {})
    assert code == 0
    assert calls == []
    assert "No target parent issues." in out


@needs_bash
@pytest.mark.parametrize("raw", ["3 x", "0", "03", "3 3"])
def test_parent_supply_rejects_invalid_values(tmp_path: Path, raw: str) -> None:
    code, out, calls = _run_step(tmp_path, "Dispatch", {"VAR_PARENT_ISSUES": raw})
    assert code != 0
    assert calls == []
    if raw == "3 x":
        assert "x" not in out.replace(
            "::error::parent issue #2 is not a positive integer", ""
        )


@needs_bash
def test_parent_failure_continues_then_fails(tmp_path: Path) -> None:
    code, _, calls = _run_step(
        tmp_path, "Dispatch", {"VAR_PARENT_ISSUES": "3 5", "STUB_FAIL_PARENT": "3"}
    )
    assert code != 0
    assert len(calls) == 2 and calls[1].startswith("dispatch -p 5")


GATE_BASE = {
    "GITHUB_REPOSITORY": "o/r",
    "GITHUB_WORKFLOW_REF": "o/r/.github/workflows/any-name.yml@refs/heads/main",
    "ORCHESTUNE_ROUTINE_ID": "id-sentinel-value",
    "ORCHESTUNE_ROUTINE_TOKEN": "token-sentinel-value",
}


@needs_bash
def test_gate_derives_path_from_workflow_ref(tmp_path: Path) -> None:
    code, _, calls = _run_step(tmp_path, "Self-diagnose", GATE_BASE)
    assert code == 0
    assert calls == [
        "doctor --execution-mode actions --workflow .github/workflows/any-name.yml"
    ]


@needs_bash
@pytest.mark.parametrize(
    "ref",
    [
        "o/r/docs/examples/x.yml@refs/heads/main",
        "o/r/.github/workflows/sub/x.yml@refs/heads/main",
    ],
)
def test_gate_rejects_unexpected_paths(tmp_path: Path, ref: str) -> None:
    code, _, calls = _run_step(
        tmp_path, "Self-diagnose", {**GATE_BASE, "GITHUB_WORKFLOW_REF": ref}
    )
    assert code != 0
    assert calls == []


@needs_bash
def test_gate_fails_on_empty_credential_without_leaking(tmp_path: Path) -> None:
    code, out, calls = _run_step(
        tmp_path, "Self-diagnose", {**GATE_BASE, "ORCHESTUNE_ROUTINE_TOKEN": ""}
    )
    assert code != 0
    assert calls == []
    assert "ORCHESTUNE_ROUTINE_TOKEN" in out
    assert "id-sentinel-value" not in out


@needs_bash
def test_gate_fails_when_doctor_reports_error(tmp_path: Path) -> None:
    code, _, _ = _run_step(
        tmp_path, "Self-diagnose", {**GATE_BASE, "STUB_DOCTOR_EXIT": "1"}
    )
    assert code != 0


@pytest.mark.parametrize("doc", SETUP_DOCS, ids=lambda p: p.parent.name)
def test_setup_docs_reference_example_and_all_codes(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    assert "../examples/dispatch-single-executor.yml" in text
    assert "orchestune doctor --execution-mode actions" in text
    for code in ALL_CODES:
        assert code in text, code


def test_cli_help_lists_doctor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["orchestune"])
    with pytest.raises(SystemExit):
        cli_main()
    captured = capsys.readouterr()
    assert "doctor" in captured.out + captured.err
