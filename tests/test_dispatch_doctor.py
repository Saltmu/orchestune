from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from orchestune.dispatch.doctor import (
    DoctorInputError,
    DoctorRequest,
    check_workflow_readable,
    discover_workflow_files,
    load_repository_config,
    load_workflow_yaml,
    normalize_workflow_path,
    ownership_diagnostics,
    read_workflow,
    resolve_repository_root,
    run_doctor,
)
from orchestune.dispatch.doctor_models import (
    ALL_CODES,
    OWNERSHIP_NOTICE,
    Diagnostic,
    DoctorReport,
    worst_status,
)


def _write(root: Path, rel: str, text: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


def test_loader_rejects_duplicate_keys_top_level_and_nested() -> None:
    with pytest.raises(yaml.YAMLError):
        load_workflow_yaml("a: 1\na: 2\n")
    with pytest.raises(yaml.YAMLError):
        load_workflow_yaml("jobs:\n  x:\n    a: 1\n    a: 2\n")


def test_loader_keeps_on_as_string_and_does_not_pollute_safe_loader() -> None:
    doc = load_workflow_yaml("on: push\nc:\n  cancel-in-progress: false\nd: yes\n")
    assert doc == {"on": "push", "c": {"cancel-in-progress": False}, "d": "yes"}
    assert yaml.safe_load("on: x") == {True: "x"}


def test_discover_workflow_files(tmp_path: Path) -> None:
    assert discover_workflow_files(tmp_path) == ()
    for rel in ("b.yml", "a.yaml", "c.txt", "sub/x.yml"):
        _write(tmp_path, f".github/workflows/{rel}", "a: 1\n")
    _write(tmp_path, "docs/examples/x.yml", "a: 1\n")
    assert discover_workflow_files(tmp_path) == (
        ".github/workflows/a.yaml",
        ".github/workflows/b.yml",
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("on: push\n", None),
        (None, "FileNotFoundError"),
        ("a: [\n", "ParserError"),
        ("a: 1\na: 2\n", "duplicate key"),
        ("- 1\n- 2\n", "top-level must be a mapping"),
        (b"\xff\xfe\x00", "UnicodeDecodeError"),
    ],
)
def test_workflow_readable(
    tmp_path: Path, content: str | bytes | None, expected: str | None
) -> None:
    rel = ".github/workflows/w.yml"
    if content is not None:
        _write(tmp_path, rel, content)
    wf = read_workflow(tmp_path, rel)
    diag = check_workflow_readable([wf])
    if expected is None:
        assert diag.status == "ok"
        assert diag.evidence == (rel,)
        return
    assert diag.status == "error"
    assert diag.evidence[0].startswith(f"{rel}: ")
    assert expected in diag.evidence[0]
    assert str(tmp_path) not in diag.evidence[0]


def test_config_missing_invalid_and_pyproject(tmp_path: Path) -> None:
    config, diag = load_repository_config(tmp_path)
    assert (config, diag.status) == ({}, "ok")

    _write(tmp_path, "pyproject.toml", '[tool.orchestune]\ndispatch_target = "local"\n')
    config, diag = load_repository_config(tmp_path)
    assert diag.status == "ok"
    assert config.get("dispatch_target") == "local"

    _write(tmp_path, "orchestune.toml", 'dispatch_target = "bogus"\n')
    config, diag = load_repository_config(tmp_path)
    assert config == {}
    assert diag.status == "error"
    assert str(tmp_path) not in json.dumps(diag.to_json())


def _diag(code: str, status: str) -> Diagnostic:
    return Diagnostic(code, status, "m")  # type: ignore[arg-type]


def test_report_json_and_configuration_status() -> None:
    codes = ALL_CODES
    err = DoctorReport("local", (_diag(codes[0], "error"),))
    assert err.configuration_status == "invalid"
    unverified = DoctorReport("local", (_diag(codes[2], "not_checked"),))
    assert unverified.configuration_status == "unverified"
    valid = DoctorReport(
        "local",
        (_diag(codes[-1], "not_checked"), _diag(codes[3], "warning")),
    )
    assert valid.configuration_status == "valid"
    data = valid.to_json()
    assert data["schema_version"] == 1
    assert data["mode"] == "local"
    assert data["ownership_status"] == "unverified"
    assert data["notice"] == OWNERSHIP_NOTICE
    assert set(data["diagnostics"][0]) == {
        "code",
        "status",
        "message",
        "evidence",
        "remediation",
    }
    assert worst_status([]) == "ok"
    assert worst_status(["ok", "warning", "not_checked"]) == "warning"


def test_ownership_diagnostics() -> None:
    local = ownership_diagnostics("local")
    actions = ownership_diagnostics("actions")
    assert len(local) == 3
    assert len(actions) == 2
    assert {d.status for d in local + actions} == {"not_checked"}


def test_normalize_workflow_path(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    assert normalize_workflow_path("w.yml", tmp_path, tmp_path / "sub") == "sub/w.yml"
    assert (
        normalize_workflow_path(str(tmp_path / "a.yml"), tmp_path, tmp_path) == "a.yml"
    )
    with pytest.raises(DoctorInputError):
        normalize_workflow_path("../../outside.yml", tmp_path, tmp_path)


def test_resolve_repository_root_maps_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(cwd: object = None) -> None:
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr("orchestune.dispatch.doctor.get_git_repository_paths", boom)
    with pytest.raises(DoctorInputError):
        resolve_repository_root(tmp_path)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() if p.is_file() else b""
        for p in sorted(root.rglob("*"))
    }


@pytest.mark.parametrize("mode", ["local", "actions"])
def test_run_doctor_is_read_only_and_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    _write(tmp_path, ".github/workflows/w.yml", "on: push\n")

    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("no process execution allowed")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    before = _snapshot(tmp_path)
    workflows = (".github/workflows/w.yml",) if mode == "actions" else ()
    report = run_doctor(DoctorRequest(mode, tmp_path, workflows))  # type: ignore[arg-type]
    assert _snapshot(tmp_path) == before
    assert not report.has_error
    assert str(tmp_path) not in json.dumps(report.to_json())


def test_run_doctor_reports_missing_workflow(tmp_path: Path) -> None:
    report = run_doctor(
        DoctorRequest("actions", tmp_path, (".github/workflows/x.yml",))
    )
    assert report.has_error
    assert report.configuration_status == "invalid"
    assert any(d.code == "dispatch.workflow.readable" for d in report.diagnostics)
