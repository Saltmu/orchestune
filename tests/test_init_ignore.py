"""Gitignore planning for orchestune init."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import orchestune.init_ignore as init_ignore
from orchestune.infra.git_cli import IgnoreQuery
from orchestune.init_ignore import (
    IgnoreApplyError,
    apply_ignore_plan,
    base_candidates,
    build_ignore_plan,
    plan_diff,
    runtime_candidates,
)

_RUNTIME_PATHS = {
    "run_state.json",
    "run_state.lock",
    "run_state.status-intents.json",
    "run_state.promotion-reports.json",
    "events.jsonl",
    "not_needed_review_state.json",
    "logs",
    "worktrees",
    ".orchestune/reports/dispatch",
}


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


def test_appends_base_rules_to_a_missing_gitignore(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    plan = build_ignore_plan(root, base_candidates(root))
    apply_ignore_plan(root, plan)
    assert (root / ".gitignore").read_bytes() == (
        b"/orchestune.toml\n/.orchestune/\n/.orchestune-installer/\n"
    )


def test_preserves_crlf_and_existing_bytes(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    path = root / ".gitignore"
    path.write_bytes(b"# keep\r\n")
    plan = build_ignore_plan(root, base_candidates(root))
    apply_ignore_plan(root, plan)
    assert path.read_bytes() == (
        b"# keep\r\n/orchestune.toml\r\n/.orchestune/\r\n/.orchestune-installer/\r\n"
    )


def test_already_ignored_paths_stay_present(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    path = root / ".gitignore"
    original = b"/orchestune.toml\n/.orchestune/\n/.orchestune-installer/\n"
    path.write_bytes(original)
    plan = build_ignore_plan(root, base_candidates(root))
    assert plan_diff(plan) == "(no .gitignore changes)"
    assert {item.action for item in plan.decisions} == {"present"}
    apply_ignore_plan(root, plan)
    assert path.read_bytes() == original


def test_negation_is_manual(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / ".gitignore").write_text("*\n!secret.txt\n", encoding="utf-8")
    plan = build_ignore_plan(root, ((root / "secret.txt", False),))
    decision = plan.decisions[0]
    assert decision.action == "manual"
    assert decision.rule is None


def test_tracked_path_is_manual(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    target = root / "orchestune.toml"
    target.write_text("x\n", encoding="utf-8")
    _git(root, "add", "orchestune.toml")
    plan = build_ignore_plan(root, base_candidates(root))
    decision = next(item for item in plan.decisions if item.path == "orchestune.toml")
    assert decision.action == "manual"


def test_escapes_space_and_star(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    plan = build_ignore_plan(root, ((root / "a b*", False),))
    assert plan.decisions[0].action == "append"
    assert plan.decisions[0].rule == "/a\\ b\\*"


def test_symlink_outside_the_repository_is_manual(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "linked-out"
    link.symlink_to(outside, target_is_directory=True)
    plan = build_ignore_plan(root, ((link, True),))
    assert plan.decisions[0].action == "manual"
    assert plan.decisions[0].reason == "path leaves the repository"


def test_repository_root_path_is_manual(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    plan = build_ignore_plan(root, ((root, True),))
    assert plan.decisions[0].action == "manual"
    assert plan.decisions[0].reason == "path covers the repository"


def test_changed_gitignore_raises_conflict_and_keeps_bytes(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    path = root / ".gitignore"
    path.write_text("# old\n", encoding="utf-8")
    plan = build_ignore_plan(root, base_candidates(root))
    path.write_text("# new\n", encoding="utf-8")
    with pytest.raises(IgnoreApplyError) as caught:
        apply_ignore_plan(root, plan)
    assert caught.value.code == 3
    assert path.read_text(encoding="utf-8") == "# new\n"


def test_null_byte_gitignore_is_not_written(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    path = root / ".gitignore"
    path.write_bytes(b"keep\0\n")
    plan = build_ignore_plan(root, base_candidates(root))
    assert {item.action for item in plan.decisions} == {"held"}
    apply_ignore_plan(root, plan)
    assert path.read_bytes() == b"keep\0\n"


def test_symlink_gitignore_is_not_replaced(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    target = tmp_path / "elsewhere"
    target.write_text("leave\n", encoding="utf-8")
    (root / ".gitignore").symlink_to(target)
    plan = build_ignore_plan(root, base_candidates(root))
    assert plan.file_kind == "symlink"
    apply_ignore_plan(root, plan)
    assert target.read_text(encoding="utf-8") == "leave\n"


def test_rerun_reports_present_rules(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    apply_ignore_plan(root, build_ignore_plan(root, base_candidates(root)))
    again = build_ignore_plan(root, base_candidates(root))
    assert {item.action for item in again.decisions} == {"present"}


def test_unreadable_ignore_query_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "repo")
    monkeypatch.setattr(
        init_ignore,
        "query_path_ignore",
        lambda _cwd, _relative: IgnoreQuery(None, False, None),
    )
    plan = build_ignore_plan(root, base_candidates(root))
    assert {item.action for item in plan.decisions} == {"held"}


def test_runtime_paths_use_loader_and_base_covers_report_dir(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    candidates = runtime_candidates(root)
    assert candidates is not None
    names = {path.relative_to(root).as_posix() for path, _directory in candidates}
    assert names == _RUNTIME_PATHS
    apply_ignore_plan(root, build_ignore_plan(root, base_candidates(root)))
    runtime = build_ignore_plan(root, candidates)
    by_path = {item.path: item.action for item in runtime.decisions}
    assert by_path[".orchestune/reports/dispatch"] == "present"
    assert by_path["run_state.json"] == "append"
    assert by_path["events.jsonl"] == "append"


def test_unreadable_config_returns_no_runtime_candidates(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "orchestune.toml").write_text("[\n", encoding="utf-8")
    assert runtime_candidates(root) is None
