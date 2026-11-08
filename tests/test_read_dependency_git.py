"""Git-backed read and change observations for the stale-read contract."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orchestune.infra.git_cli import GitResult
from orchestune.read_dependencies import git as git_adapter
from orchestune.read_dependencies.contracts import (
    ChangeKind,
    ObservationAvailability,
    ResourcePresence,
    UnknownReason,
)
from orchestune.read_dependencies.git import (
    lookup_resource,
    observe_changed_resources,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "--all")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")

    (repo / "rename source.txt").write_text("stable rename content\n")
    (repo / "changed.txt").write_text("before\n")
    (repo / "deleted.txt").write_text("remove me\n")
    (repo / "mode.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "same.txt").write_text("unchanged\n")
    os.chmod(repo / "mode.sh", 0o644)
    old_sha = _commit(repo, "base")

    _git(repo, "mv", "rename source.txt", "renamed target.txt")
    (repo / "changed.txt").write_text("after\n")
    (repo / "deleted.txt").unlink()
    (repo / "added.txt").write_text("new file\n")
    (repo / "line break.txt").write_text("NUL-delimited path\n")
    os.chmod(repo / "mode.sh", 0o755)
    (repo / "link").symlink_to("added.txt")
    new_sha = _commit(repo, "changes")
    return repo, old_sha, new_sha


def test_observes_add_delete_rename_content_mode_symlink_and_unmodified(
    repository: tuple[Path, str, str],
) -> None:
    repo, old_sha, new_sha = repository
    changes = observe_changed_resources(
        old_sha,
        new_sha,
        [
            "added.txt",
            "changed.txt",
            "deleted.txt",
            "line break.txt",
            "link",
            "mode.sh",
            "renamed target.txt",
            "rename source.txt",
            "same.txt",
        ],
        cwd=repo,
    )

    by_path = {change.path: change for change in changes}
    assert set(by_path) == {
        "added.txt",
        "changed.txt",
        "deleted.txt",
        "line break.txt",
        "link",
        "mode.sh",
        "renamed target.txt",
    }
    assert by_path["added.txt"].kind is ChangeKind.ADD
    assert by_path["deleted.txt"].kind is ChangeKind.DELETE
    assert by_path["changed.txt"].kind is ChangeKind.MODIFY
    assert by_path["renamed target.txt"].kind is ChangeKind.RENAME
    assert by_path["renamed target.txt"].old_path == "rename source.txt"
    assert by_path["mode.sh"].kind is ChangeKind.MODE
    assert by_path["mode.sh"].old_blob_oid == by_path["mode.sh"].new_blob_oid
    assert by_path["mode.sh"].old_file_mode == "100644"
    assert by_path["mode.sh"].new_file_mode == "100755"
    assert by_path["link"].new_file_mode == "120000"
    assert by_path["line break.txt"].kind is ChangeKind.ADD
    assert all(
        change.availability is ObservationAvailability.RESOLVED for change in changes
    )
    expected_evidence = tuple(sorted((old_sha, new_sha)))
    assert all(change.evidence_commits == expected_evidence for change in changes)


def test_lookup_uses_explicit_snapshot_and_distinguishes_absent_from_unknown(
    repository: tuple[Path, str, str],
) -> None:
    repo, old_sha, new_sha = repository

    before = lookup_resource(old_sha, "changed.txt", cwd=repo)
    added_before = lookup_resource(old_sha, "added.txt", cwd=repo)
    after = lookup_resource(new_sha, "changed.txt", cwd=repo)
    missing_snapshot = lookup_resource("f" * 40, "changed.txt", cwd=repo)

    assert before.presence is ResourcePresence.PRESENT
    assert after.presence is ResourcePresence.PRESENT
    assert before.blob_oid != after.blob_oid
    assert before.snapshot_commit == old_sha
    assert added_before.presence is ResourcePresence.ABSENT
    assert added_before.snapshot_commit == old_sha
    assert missing_snapshot.presence is ResourcePresence.UNKNOWN


def test_lookup_returns_unknown_for_ambiguous_git_records(
    repository: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, old_sha, _ = repository
    oid = "1" * 40

    def ambiguous_tree(args: list[str], **_kwargs: object) -> GitResult:
        if args[:2] == ["rev-parse", "--verify"]:
            return GitResult(0, f"{old_sha}\n", "")
        entry = f"100644 blob {oid}\tfile.txt\0"
        return GitResult(0, entry + entry, "")

    monkeypatch.setattr(git_adapter, "run_git", ambiguous_tree)

    result = lookup_resource(old_sha, "file.txt", cwd=repo)

    assert result.presence is ResourcePresence.UNKNOWN


def test_lookup_returns_unknown_for_unterminated_tree_records(
    repository: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, old_sha, _ = repository

    def truncated_tree(args: list[str], **_kwargs: object) -> GitResult:
        if args[:2] == ["rev-parse", "--verify"]:
            return GitResult(0, f"{old_sha}\n", "")
        return GitResult(0, f"100644 blob {'1' * 40}\tfile.txt", "")

    monkeypatch.setattr(git_adapter, "run_git", truncated_tree)

    result = lookup_resource(old_sha, "file.txt", cwd=repo)

    assert result.presence is ResourcePresence.UNKNOWN


def test_lookup_adapter_error_stays_unknown(
    repository: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, old_sha, _ = repository

    def fail(*_args: object, **_kwargs: object) -> GitResult:
        raise OSError("git is unavailable")

    monkeypatch.setattr(git_adapter, "run_git", fail)

    assert lookup_resource(old_sha, "same.txt", cwd=repo).presence is (
        ResourcePresence.UNKNOWN
    )


def test_unknown_objects_produce_unknown_changes_for_each_requested_path(
    repository: tuple[Path, str, str],
) -> None:
    repo, old_sha, _ = repository
    missing_sha = "f" * 40

    changes = observe_changed_resources(
        old_sha, missing_sha, ["changed.txt", "added.txt"], cwd=repo
    )

    assert {change.path for change in changes} == {"changed.txt", "added.txt"}
    assert all(
        change.availability is ObservationAvailability.UNKNOWN
        and change.unknown_reason is UnknownReason.OBJECT_MISSING
        for change in changes
    )


def test_ambiguous_and_directory_paths_are_unknown(
    repository: tuple[Path, str, str],
) -> None:
    repo, old_sha, _ = repository
    (repo / "folder").mkdir()
    (repo / "folder" / "child.txt").write_text("child\n")
    snapshot = _commit(repo, "folder")

    directory = lookup_resource(snapshot, "folder", cwd=repo)
    assert directory.presence is ResourcePresence.UNKNOWN
    with pytest.raises(ValueError):
        lookup_resource(snapshot, "../outside", cwd=repo)
    assert old_sha != snapshot


def test_shallow_clone_does_not_report_an_incomplete_diff_as_empty(
    repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, old_sha, new_sha = repository
    clone = tmp_path / "shallow"
    _git(tmp_path, "clone", "--quiet", "--depth=1", f"file://{repo}", str(clone))

    changes = observe_changed_resources(old_sha, new_sha, ["changed.txt"], cwd=clone)

    assert len(changes) == 1
    assert changes[0].availability is ObservationAvailability.UNKNOWN
    assert changes[0].unknown_reason is UnknownReason.SHALLOW_HISTORY


def test_shallow_clone_with_both_snapshot_objects_can_compare_them(
    repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, old_sha, new_sha = repository
    clone = tmp_path / "two-commit-shallow"
    _git(tmp_path, "clone", "--quiet", "--depth=2", f"file://{repo}", str(clone))

    changes = observe_changed_resources(old_sha, new_sha, ["changed.txt"], cwd=clone)

    assert len(changes) == 1
    assert changes[0].kind is ChangeKind.MODIFY
    assert changes[0].availability is ObservationAvailability.RESOLVED


def test_adapter_failure_is_unknown_not_an_empty_change(
    repository: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, old_sha, new_sha = repository

    def fail(*_args: object, **_kwargs: object) -> object:
        raise OSError("git is unavailable")

    monkeypatch.setattr(git_adapter, "run_git", fail)

    changes = observe_changed_resources(old_sha, new_sha, ["changed.txt"], cwd=repo)

    assert len(changes) == 1
    assert changes[0].availability is ObservationAvailability.UNKNOWN
    assert changes[0].unknown_reason is UnknownReason.FETCH_FAILED


def test_empty_or_ambiguous_paths_do_not_become_false_clean_results(
    repository: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, old_sha, new_sha = repository
    calls = 0

    def track(*_args: object, **_kwargs: object) -> GitResult:
        nonlocal calls
        calls += 1
        return GitResult(0, "", "")

    monkeypatch.setattr(git_adapter, "run_git", track)
    assert observe_changed_resources(old_sha, new_sha, [], cwd=repo) == ()
    assert calls == 0

    unknown = observe_changed_resources(
        old_sha, new_sha, ["ambiguous\ufffdpath"], cwd=repo
    )
    assert len(unknown) == 1
    assert unknown[0].availability is ObservationAvailability.UNKNOWN
    assert unknown[0].unknown_reason is UnknownReason.EVIDENCE_MISMATCH
    assert calls == 0


def test_malformed_diff_output_is_unknown(
    repository: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, old_sha, new_sha = repository

    def malformed_diff(args: list[str], **_kwargs: object) -> GitResult:
        if args[0] == "rev-parse":
            return GitResult(0, "false\n", "")
        return GitResult(
            0,
            f":100644 100644 {'1' * 40} {'2' * 40} M\0changed.txt",
            "",
        )

    monkeypatch.setattr(git_adapter, "run_git", malformed_diff)

    changes = observe_changed_resources(old_sha, new_sha, ["changed.txt"], cwd=repo)

    assert len(changes) == 1
    assert changes[0].availability is ObservationAvailability.UNKNOWN
    assert changes[0].unknown_reason is UnknownReason.EVIDENCE_MISMATCH


def test_unresolvable_paths_are_not_passed_as_git_pathspecs(
    repository: tuple[Path, str, str],
) -> None:
    repo, old_sha, new_sha = repository

    with pytest.raises(ValueError):
        observe_changed_resources(old_sha, new_sha, ["**/*.txt"], cwd=repo)
