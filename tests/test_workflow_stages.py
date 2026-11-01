"""Stage metadata is a validated immutable package resource, never executable state."""

from __future__ import annotations

import copy
import itertools
import os
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import FrozenInstanceError
from importlib import resources
from pathlib import Path

import pytest
import tomlkit

from orchestune.workflow.stages import load_stage_definitions

ROOT = Path(__file__).resolve().parents[1]
STAGE_IDS = (
    "prepare",
    "claim",
    "scope",
    "implement",
    "pr",
    "review",
    "complete",
    "blocked",
    "handed-off",
)


@pytest.fixture
def document() -> dict:
    return tomllib.loads(
        resources.files("orchestune.workflow").joinpath("stages.toml").read_text()
    )


def _load_document(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: dict):
    (tmp_path / "stages.toml").write_text(tomlkit.dumps(data), encoding="utf-8")
    monkeypatch.setattr(
        "orchestune.workflow.stages.resources.files", lambda _: tmp_path
    )
    return load_stage_definitions()


def test_packaged_stages_follow_contract_and_are_deeply_immutable():
    stages = load_stage_definitions()
    assert tuple(stage.id for stage in stages) == STAGE_IDS
    # Preparation is entered while context is still missing, not after it is ready.
    assert stages[0].entry_conditions == ("context_incomplete",)
    assert {stage.gate for stage in stages} == {
        "",
        "pre-claim",
        "pre-edit",
        "pre-pr",
        "pre-complete",
    }
    for stage in stages:
        assert stage.entry_conditions and stage.actions and stage.exit_conditions
        assert stage.on_failure and stage.references and stage.artifacts
        assert all(action.reason_codes for action in stage.on_failure)
        with pytest.raises(FrozenInstanceError):
            stage.order = 99
        with pytest.raises(FrozenInstanceError):
            stage.actions[0].description = "changed"
        assert isinstance(stage.actions[0].argv, tuple)
        assert isinstance(stage.branches.mode, tuple)
    for terminal in stages[-2:]:
        assert terminal.exit_conditions == ("terminal_receipt",)


@pytest.mark.parametrize(
    "mode,kind,backend",
    tuple(itertools.product(("interactive", "auto"), ("code", "docs"), ("gh", "mcp"))),
)
def test_every_execution_branch_retains_required_stages(mode, kind, backend):
    selected = [
        stage.id
        for stage in load_stage_definitions()
        if mode in stage.branches.mode
        and kind in stage.branches.change_kind
        and backend in stage.branches.backend
    ]
    assert tuple(selected) == STAGE_IDS


def test_explicit_order_controls_loading(monkeypatch, tmp_path, document):
    document["stage"].reverse()
    assert (
        tuple(s.id for s in _load_document(monkeypatch, tmp_path, document))
        == STAGE_IDS
    )


@pytest.mark.parametrize(
    "field",
    (
        "id",
        "order",
        "gate",
        "entry_conditions",
        "actions",
        "exit_conditions",
        "on_failure",
        "references",
        "artifacts",
        "branches",
    ),
)
def test_missing_stage_fields_are_errors(monkeypatch, tmp_path, document, field):
    del document["stage"][0][field]
    with pytest.raises(ValueError, match=field):
        _load_document(monkeypatch, tmp_path, document)


@pytest.mark.parametrize(
    "field,value",
    (
        ("id", ""),
        ("id", "invented"),
        ("order", True),
        ("order", 0),
        ("order", "1"),
        ("gate", "unknown"),
        ("entry_conditions", []),
        ("entry_conditions", ["eval('True')"]),
        ("exit_conditions", [1]),
        ("actions", []),
        ("actions", ["claim"]),
        ("on_failure", []),
        ("references", ["../outside.md"]),
        ("references", ["/etc/file"]),
        ("references", ["C:/file"]),
        ("references", ["file:///etc/file"]),
        ("references", ["https:///missing-host"]),
        ("references", ["a\\b.md"]),
        ("artifacts", ["unknown"]),
        ("branches", []),
    ),
)
def test_invalid_stage_values_are_errors(monkeypatch, tmp_path, document, field, value):
    document["stage"][0][field] = value
    with pytest.raises(ValueError, match=field):
        _load_document(monkeypatch, tmp_path, document)


@pytest.mark.parametrize(
    "field,value",
    (
        ("mode", ["batch"]),
        ("change_kind", ["minor"]),
        ("backend", ["web"]),
        ("mode", []),
        ("backend", "gh"),
        ("backend", ["gh", "gh"]),
    ),
)
def test_invalid_branch_values_are_errors(
    monkeypatch, tmp_path, document, field, value
):
    document["stage"][0]["branches"][field] = value
    with pytest.raises(ValueError, match=field):
        _load_document(monkeypatch, tmp_path, document)


@pytest.mark.parametrize(
    "field,value",
    (
        ("id", "unknown"),
        ("description", " "),
        ("argv", [1]),
        ("argv", "echo unsafe"),
        ("references", ["../outside.md"]),
    ),
)
def test_invalid_actions_are_errors(monkeypatch, tmp_path, document, field, value):
    document["stage"][0]["actions"][0][field] = value
    with pytest.raises(ValueError, match=field):
        _load_document(monkeypatch, tmp_path, document)


def test_failure_actions_require_reason_code_mapping(monkeypatch, tmp_path, document):
    del document["stage"][0]["on_failure"][0]["reason_codes"]
    with pytest.raises(ValueError, match="reason_codes"):
        _load_document(monkeypatch, tmp_path, document)


@pytest.mark.parametrize("field", ("id", "order", "gate"))
def test_duplicate_identifiers_are_errors(monkeypatch, tmp_path, document, field):
    document["stage"][1][field] = document["stage"][0][field]
    with pytest.raises(ValueError, match=field):
        _load_document(monkeypatch, tmp_path, document)


@pytest.mark.parametrize("schema", (2, True, "1", 0))
def test_unsupported_schema_is_explicit(monkeypatch, tmp_path, document, schema):
    document["schema_version"] = schema
    with pytest.raises(ValueError, match="schema_version"):
        _load_document(monkeypatch, tmp_path, document)


@pytest.mark.parametrize("missing", ("schema_version", "stage"))
def test_missing_top_level_fields_are_errors(monkeypatch, tmp_path, document, missing):
    del document[missing]
    with pytest.raises(ValueError, match=missing):
        _load_document(monkeypatch, tmp_path, document)


def test_missing_stage_and_terminal_condition_are_errors(
    monkeypatch, tmp_path, document
):
    incomplete = copy.deepcopy(document)
    incomplete["stage"].pop()
    with pytest.raises(ValueError, match="stage"):
        _load_document(monkeypatch, tmp_path, incomplete)
    document["stage"][-1]["exit_conditions"] = ["ci_current"]
    with pytest.raises(ValueError, match="terminal_receipt"):
        _load_document(monkeypatch, tmp_path, document)


@pytest.mark.parametrize(
    "data",
    ({"schema_version": 1, "stage": []}, {"schema_version": 1, "stage": "invalid"}),
)
def test_empty_or_invalid_stage_array_is_an_error(monkeypatch, tmp_path, data):
    with pytest.raises(ValueError, match="stage"):
        _load_document(monkeypatch, tmp_path, data)


def test_unknown_fields_are_errors(monkeypatch, tmp_path, document):
    document["stage"][0]["branches"]["surprise"] = ["value"]
    with pytest.raises(ValueError, match="surprise"):
        _load_document(monkeypatch, tmp_path, document)


def test_missing_or_malformed_resource_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "orchestune.workflow.stages.resources.files", lambda _: tmp_path
    )
    with pytest.raises(ValueError, match="stages.toml"):
        load_stage_definitions()
    (tmp_path / "stages.toml").write_text("stage = [", encoding="utf-8")
    with pytest.raises(ValueError, match="stages.toml"):
        load_stage_definitions()


def test_real_wheel_and_sdist_include_loadable_resource(tmp_path):
    output = tmp_path / "dist"
    result = subprocess.run(
        [
            "uv",
            "--cache-dir",
            str(ROOT / ".orchestune/uv-cache"),
            "build",
            "--out-dir",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    (wheel,) = output.glob("*.whl")
    (sdist,) = output.glob("*.tar.gz")
    resource = "orchestune/workflow/stages.toml"
    with tarfile.open(sdist) as archive:
        assert any(name.endswith("/" + resource) for name in archive.getnames())
    isolated = tmp_path / "isolated"
    with zipfile.ZipFile(wheel) as archive:
        assert resource in archive.namelist()
        archive.extractall(isolated)
    # -I ignores the source checkout and PYTHONPATH; only the extracted wheel is added.
    # pytest-cov's .pth hook still sees environment variables under -I. Do not
    # instrument a second copy of the package and inflate CI's coverage denominator.
    isolated_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COV_CORE_") and key != "COVERAGE_PROCESS_START"
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from orchestune.workflow.stages import load_stage_definitions; "
            "print(','.join(s.id for s in load_stage_definitions()))",
            str(isolated),
        ],
        cwd=isolated,
        env=isolated_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ",".join(STAGE_IDS)
