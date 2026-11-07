from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

from orchestune.dispatch.doctor import check as doctor_check

SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "skills"
    / "orchestune-readiness"
    / "scripts"
    / "check_readiness.py"
)


def get_check_readiness_main():
    spec = importlib.util.spec_from_file_location("check_readiness", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, f"Cannot load {SCRIPT_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def test_missing_repo_root_returns_2(capsys):
    main = get_check_readiness_main()
    assert main([]) == 2
    captured = capsys.readouterr()
    assert "error" in captured.err.lower() or "usage" in captured.err.lower()


def test_nonexistent_repo_root_returns_2(tmp_path: Path, capsys):
    main = get_check_readiness_main()
    missing_dir = tmp_path / "does_not_exist"
    assert main(["--repo-root", str(missing_dir), "--execution-mode", "local"]) == 2
    captured = capsys.readouterr()
    assert (
        "does not exist" in captured.err.lower() or "directory" in captured.err.lower()
    )


def test_repo_root_is_file_returns_2(tmp_path: Path, capsys):
    main = get_check_readiness_main()
    file_path = tmp_path / "regular_file.txt"
    file_path.write_text("hello", encoding="utf-8")
    assert main(["--repo-root", str(file_path), "--execution-mode", "local"]) == 2
    captured = capsys.readouterr()
    assert "directory" in captured.err.lower()


def test_actions_mode_requires_workflow(tmp_path: Path, capsys):
    main = get_check_readiness_main()
    assert main(["--repo-root", str(tmp_path), "--execution-mode", "actions"]) == 2
    captured = capsys.readouterr()
    assert "--workflow is required" in captured.err


def test_local_mode_forbids_workflow(tmp_path: Path, capsys):
    main = get_check_readiness_main()
    assert (
        main(
            [
                "--repo-root",
                str(tmp_path),
                "--execution-mode",
                "local",
                "--workflow",
                ".github/workflows/ci.yml",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "--workflow is only valid" in captured.err or "--workflow" in captured.err


def test_skip_readiness_check_true_skips_doctor(tmp_path: Path, capsys, monkeypatch):
    main = get_check_readiness_main()
    config = tmp_path / "orchestune.toml"
    config.write_text("skip-readiness-check = true\n", encoding="utf-8")

    mock_doctor = MagicMock()
    monkeypatch.setattr(doctor_check, "run_doctor", mock_doctor)
    rc = main(["--repo-root", str(tmp_path), "--execution-mode", "local"])
    assert rc == 0
    mock_doctor.assert_not_called()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {
        "schema_version": 1,
        "skipped": True,
        "doctor": None,
    }


def test_skip_readiness_check_underscore_alias_true_skips_doctor(
    tmp_path: Path, capsys, monkeypatch
):
    main = get_check_readiness_main()
    config = tmp_path / "orchestune.toml"
    config.write_text("skip_readiness_check = true\n", encoding="utf-8")

    mock_doctor = MagicMock()
    monkeypatch.setattr(doctor_check, "run_doctor", mock_doctor)
    rc = main(["--repo-root", str(tmp_path), "--execution-mode", "local"])
    assert rc == 0
    mock_doctor.assert_not_called()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["skipped"] is True
    assert data["doctor"] is None


def test_skip_readiness_check_pyproject_fallback(tmp_path: Path, capsys, monkeypatch):
    main = get_check_readiness_main()
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.orchestune]\nskip-readiness-check = true\n", encoding="utf-8"
    )

    mock_doctor = MagicMock()
    monkeypatch.setattr(doctor_check, "run_doctor", mock_doctor)
    rc = main(["--repo-root", str(tmp_path), "--execution-mode", "local"])
    assert rc == 0
    mock_doctor.assert_not_called()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["skipped"] is True


def test_orchestune_toml_takes_priority_over_pyproject_without_merging(
    tmp_path: Path, capsys, monkeypatch
):
    main = get_check_readiness_main()
    (tmp_path / "orchestune.toml").write_text(
        "skip-readiness-check = false\n", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        "[tool.orchestune]\nskip-readiness-check = true\n", encoding="utf-8"
    )

    fake_report = MagicMock()
    fake_report.has_error = False
    fake_report.to_json.return_value = {"status": "ok", "diagnostics": []}

    mock_doctor = MagicMock(return_value=fake_report)
    monkeypatch.setattr(doctor_check, "run_doctor", mock_doctor)
    rc = main(["--repo-root", str(tmp_path), "--execution-mode", "local"])
    assert rc == 0
    mock_doctor.assert_called_once()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["skipped"] is False
    assert data["doctor"] == {"status": "ok", "diagnostics": []}


def test_skip_readiness_check_false_calls_doctor(tmp_path: Path, capsys, monkeypatch):
    main = get_check_readiness_main()
    fake_report = MagicMock()
    fake_report.has_error = False
    fake_report.to_json.return_value = {"status": "ok", "diagnostics": []}

    mock_doctor = MagicMock(return_value=fake_report)
    monkeypatch.setattr(doctor_check, "run_doctor", mock_doctor)
    rc = main(["--repo-root", str(tmp_path), "--execution-mode", "local"])
    assert rc == 0
    mock_doctor.assert_called_once()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {
        "schema_version": 1,
        "skipped": False,
        "doctor": {"status": "ok", "diagnostics": []},
    }


def test_doctor_with_error_returns_1(tmp_path: Path, capsys, monkeypatch):
    main = get_check_readiness_main()
    fake_report = MagicMock()
    fake_report.has_error = True
    fake_report.to_json.return_value = {"status": "error", "diagnostics": []}

    mock_doctor = MagicMock(return_value=fake_report)
    monkeypatch.setattr(doctor_check, "run_doctor", mock_doctor)
    rc = main(["--repo-root", str(tmp_path), "--execution-mode", "local"])
    assert rc == 1
    mock_doctor.assert_called_once()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["schema_version"] == 1
    assert data["skipped"] is False
    assert data["doctor"] == {"status": "error", "diagnostics": []}


def test_non_boolean_skip_setting_returns_2(tmp_path: Path, capsys):
    main = get_check_readiness_main()
    config = tmp_path / "orchestune.toml"
    config.write_text('skip-readiness-check = "true"\n', encoding="utf-8")

    rc = main(["--repo-root", str(tmp_path), "--execution-mode", "local"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "must be a boolean" in captured.err or "boolean" in captured.err.lower()


def test_invalid_toml_syntax_returns_2(tmp_path: Path, capsys):
    main = get_check_readiness_main()
    config = tmp_path / "orchestune.toml"
    config.write_text("invalid toml [[[\n", encoding="utf-8")

    rc = main(["--repo-root", str(tmp_path), "--execution-mode", "local"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "failed to load" in captured.err.lower() or "toml" in captured.err.lower()


def test_actions_mode_passes_normalized_workflow(tmp_path: Path, capsys, monkeypatch):
    main = get_check_readiness_main()
    wf_file = tmp_path / ".github" / "workflows" / "ci.yml"
    wf_file.parent.mkdir(parents=True)
    wf_file.write_text("name: CI\n", encoding="utf-8")

    fake_report = MagicMock()
    fake_report.has_error = False
    fake_report.to_json.return_value = {"status": "ok"}

    mock_doctor = MagicMock(return_value=fake_report)
    monkeypatch.setattr(doctor_check, "run_doctor", mock_doctor)
    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "--execution-mode",
            "actions",
            "--workflow",
            ".github/workflows/ci.yml",
        ]
    )
    assert rc == 0
    req = mock_doctor.call_args[0][0]
    assert req.mode == "actions"
    assert req.repo_root == tmp_path.resolve()
    assert len(req.workflows) == 1
