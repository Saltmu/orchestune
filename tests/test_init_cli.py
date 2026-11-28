"""Guided project init CLI."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import orchestune.init_cli as init_command
from orchestune.config_wizard.wizard import WizardResult
from orchestune.dispatch.doctor.check import DoctorRequest
from orchestune.dispatch.doctor.models import Diagnostic, DiagnosticStatus, DoctorReport
from orchestune.init_cli import main

_BASE_RULES = (
    "/orchestune.toml",
    "/.orchestune/",
    "/.orchestune-installer/",
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "init-test@example.com")
    _git(path, "config", "user.name", "init-test")
    (path / "README").write_text("readme\n", encoding="utf-8")
    _git(path, "add", "README")
    _git(path, "commit", "-m", "init")
    return path


@dataclass
class _Calls:
    labels: list[Path] = field(default_factory=list)
    install: list[list[str]] = field(default_factory=list)
    wizard: list[tuple[str, Path]] = field(default_factory=list)
    doctor: list[DoctorRequest] = field(default_factory=list)


@pytest.fixture(autouse=True)
def _isolate_side_effects(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    calls = _Calls()

    def labels(*_args: object, cwd: Path = Path(), **_kwargs: object) -> int:
        calls.labels.append(cwd)
        return 0

    def install(argv: list[str]) -> int:
        calls.install.append(argv)
        return 0

    def wizard(mode: str, project_dir: Path, _prompt: object) -> WizardResult:
        calls.wizard.append((mode, project_dir))
        return WizardResult(status="saved")

    def doctor(request: DoctorRequest) -> DoctorReport:
        calls.doctor.append(request)
        return DoctorReport("local", (Diagnostic("sample", "ok", "ok"),))

    monkeypatch.setattr(init_command, "prepare_github_labels", labels)
    monkeypatch.setattr(init_command, "installer_main", install)
    monkeypatch.setattr(init_command, "run_config_wizard", wizard)
    monkeypatch.setattr(init_command, "run_doctor", doctor)
    return calls


def _interactive(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    pending = list(answers)
    monkeypatch.setattr(init_command, "_interactive", lambda: True)

    def answer(_prompt: str = "") -> str:
        if not pending:
            raise AssertionError("unexpected prompt")
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", answer)


def _run(root: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    monkeypatch.chdir(root)
    return main(argv)


def test_non_tty_exits_2_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path / "repo")
    assert _run(root, monkeypatch, []) == 2
    captured = capsys.readouterr()
    assert "--dry-run" in captured.out
    assert "skills" in captured.out
    assert not (root / ".gitignore").exists()


def test_dry_run_performs_no_write_lock_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _isolate_side_effects: _Calls,
) -> None:
    root = _repo(tmp_path / "repo")
    (root / "dirty.txt").write_text("keep\n", encoding="utf-8")
    assert _run(root, monkeypatch, ["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "未実施" in output
    assert "/orchestune.toml" in output
    assert not (root / ".gitignore").exists()
    assert not (root / ".git" / "orchestune-init-gitignore.lock").exists()
    assert _isolate_side_effects.labels == []
    assert _isolate_side_effects.install == []
    assert _isolate_side_effects.wizard == []
    request = _isolate_side_effects.doctor[0]
    assert request.online is False
    assert (root / "dirty.txt").read_text(encoding="utf-8") == "keep\n"
    assert _git(root, "diff", "--cached") == ""


def test_rejects_non_git_bare_unborn_and_linked_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _run(plain, monkeypatch, ["--dry-run"]) == 2
    bare = tmp_path / "bare.git"
    _git(tmp_path, "init", "--bare", str(bare))
    assert _run(bare, monkeypatch, ["--dry-run"]) == 2
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(unborn, "init")
    assert _run(unborn, monkeypatch, ["--dry-run"]) == 2
    root = _repo(tmp_path / "primary")
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "--detach", str(linked))
    assert _run(linked, monkeypatch, ["--dry-run"]) == 2
    output = capsys.readouterr().out
    assert "Gitリポジトリではありません" in output
    assert "bare" in output
    assert "HEAD" in output
    assert "linked worktree" in output


def test_yes_appends_base_ignores_without_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "repo")
    _interactive(monkeypatch, ["y", "skip", "n", "n"])
    assert _run(root, monkeypatch, []) == 0
    text = (root / ".gitignore").read_text(encoding="utf-8")
    for rule in _BASE_RULES:
        assert rule in text
    assert ".gitignore" in _git(root, "status", "--porcelain=v1")
    assert _git(root, "diff", "--cached", "--name-only") == ""


def test_declining_base_ignores_skips_skill_and_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _isolate_side_effects: _Calls,
) -> None:
    root = _repo(tmp_path / "repo")
    _interactive(monkeypatch, ["n", "n"])
    assert _run(root, monkeypatch, []) == 0
    assert "見送り" in capsys.readouterr().out
    assert not (root / ".gitignore").exists()
    assert _isolate_side_effects.install == []
    assert _isolate_side_effects.wizard == []


def test_gitignore_conflict_stops_later_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_side_effects: _Calls,
) -> None:
    root = _repo(tmp_path / "repo")

    def answer(prompt: str) -> str:
        if "基本" in prompt:
            (root / ".gitignore").write_text("changed\n", encoding="utf-8")
            return "y"
        raise AssertionError(prompt)

    monkeypatch.setattr(init_command, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", answer)
    assert _run(root, monkeypatch, []) == 3
    assert _isolate_side_effects.install == []
    assert _isolate_side_effects.doctor == []


def test_wizard_cancel_returns_130_before_later_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _isolate_side_effects: _Calls,
) -> None:
    root = _repo(tmp_path / "repo")

    def wizard(_mode: str, _project_dir: Path, _prompt: object) -> WizardResult:
        return WizardResult(status="cancelled")

    monkeypatch.setattr(init_command, "run_config_wizard", wizard)
    _interactive(monkeypatch, ["y", "skip", "y"])
    assert _run(root, monkeypatch, []) == 130
    assert "ランタイムパス" not in capsys.readouterr().out
    assert _isolate_side_effects.labels == []


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_input_interrupt_returns_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: type[BaseException],
) -> None:
    root = _repo(tmp_path / "repo")
    monkeypatch.setattr(init_command, "_interactive", lambda: True)

    def boom(_prompt: str = "") -> str:
        raise error

    monkeypatch.setattr("builtins.input", boom)
    assert _run(root, monkeypatch, []) == 130
    assert not (root / ".gitignore").exists()


def test_offline_skips_labels_and_online_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_side_effects: _Calls,
) -> None:
    root = _repo(tmp_path / "repo")
    _git(root, "remote", "add", "origin", "git@github.com:acme/widgets.git")
    _interactive(monkeypatch, ["n", "n"])
    assert _run(root, monkeypatch, ["--offline"]) == 0
    assert _isolate_side_effects.labels == []
    assert _isolate_side_effects.doctor[0].online is False


def test_online_confirmation_prepares_labels_for_the_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _isolate_side_effects: _Calls,
) -> None:
    root = _repo(tmp_path / "repo")
    _git(root, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    _interactive(monkeypatch, ["n", "n", "y"])
    assert _run(root, monkeypatch, []) == 0
    assert "acme/widgets" in capsys.readouterr().out
    assert _isolate_side_effects.labels == [root]


def test_unresolvable_origin_skips_label_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _isolate_side_effects: _Calls,
) -> None:
    root = _repo(tmp_path / "repo")
    _git(root, "remote", "add", "origin", "https://example.com/acme/widgets.git")
    _interactive(monkeypatch, ["n", "n"])
    assert _run(root, monkeypatch, []) == 0
    assert "照合できない" in capsys.readouterr().out
    assert _isolate_side_effects.labels == []


def test_label_failure_still_runs_doctor_and_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _isolate_side_effects: _Calls,
) -> None:
    root = _repo(tmp_path / "repo")
    _git(root, "remote", "add", "origin", "git@github.com:acme/widgets.git")

    def failed(*_args: object, cwd: Path = Path(), **_kwargs: object) -> int:
        _isolate_side_effects.labels.append(cwd)
        return 1

    monkeypatch.setattr(init_command, "prepare_github_labels", failed)
    _interactive(monkeypatch, ["n", "n", "y"])
    assert _run(root, monkeypatch, []) == 1
    assert "不足分" in capsys.readouterr().out
    assert _isolate_side_effects.doctor


@pytest.mark.parametrize(
    ("status", "expected"),
    [("warning", 0), ("error", 1)],
)
def test_doctor_warning_exits_0_and_error_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: DiagnosticStatus,
    expected: int,
) -> None:
    root = _repo(tmp_path / "repo")

    def doctor(_request: DoctorRequest) -> DoctorReport:
        return DoctorReport("local", (Diagnostic("sample", status, status),))

    monkeypatch.setattr(init_command, "run_doctor", doctor)
    _interactive(monkeypatch, ["n", "n"])
    assert _run(root, monkeypatch, []) == expected


def test_skill_install_previews_then_confirms_project_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_side_effects: _Calls,
) -> None:
    root = _repo(tmp_path / "repo")
    _interactive(monkeypatch, ["y", "codex", "y", "n", "n"])
    assert _run(root, monkeypatch, []) == 0
    preview, apply = _isolate_side_effects.install
    assert preview[-1] == "--dry-run"
    assert "--force" not in preview
    assert "--force" not in apply
    assert "--scope" in apply and "project" in apply
    assert "--project-dir" in apply and str(root) in apply
    assert "--target" in apply and "codex" in apply


def test_subdirectory_writes_the_root_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "repo")
    nested = root / "nested"
    nested.mkdir()
    _interactive(monkeypatch, ["y", "skip", "n", "n"])
    monkeypatch.chdir(nested)
    assert main([]) == 0
    assert Path.cwd() == nested
    assert (root / ".gitignore").is_file()


def test_help_lists_dry_run_and_offline(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "--dry-run" in output
    assert "--offline" in output
