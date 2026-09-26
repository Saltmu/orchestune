"""Tests for complete CI evidence recording and validation (#1000)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.complete.ci_evidence import (
    CI_EVIDENCE_FILENAME,
    CURRENT_SCHEMA_VERSION,
    CiEvidence,
    CiEvidenceError,
    CiEvidenceInvalidError,
    CiEvidenceMismatchError,
    CiEvidenceMissingError,
    CiExecutionError,
    _resolve_runner_environment,
    compute_lockfile_digest,
    invalidate_ci_evidence,
    record_ci_evidence,
    resolve_evidence_dir,
    resolve_evidence_path,
    run_local_ci_if_needed,
    validate_ci_evidence,
)
from orchestune.complete.contracts import CompleteRequest


@pytest.fixture
def git_worktree(tmp_path: Path) -> Path:
    """Create a temporary git repository simulating a worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)

    # Create CI scripts and lockfile
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "local-ci.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (scripts_dir / "local-ci.ps1").write_text("exit 0\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'test'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    (repo / "uv.lock").write_text("lockfile content\n", encoding="utf-8")
    (repo / "file.txt").write_text("initial\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".orchestune/ci/\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


class TestCiEvidenceDataclass:
    """Test CiEvidence dataclass creation, validation, and JSON serialization."""

    def test_construction_and_defaults(self):
        ev = CiEvidence(
            head_sha="a" * 40,
            base_sha="b" * 40,
            succeeded=True,
            schema_version=1,
            ci_definition_digest="c" * 64,
            lockfile_digest="d" * 64,
            os="Linux",
            architecture="x86_64",
            python_version="CPython 3.13.0",
            started_at="2026-09-24T12:00:00Z",
            completed_at="2026-09-24T12:01:00Z",
            exit_code=0,
            status="passed",
        )
        assert ev.head_sha == "a" * 40
        assert ev.base_sha == "b" * 40
        assert ev.succeeded is True
        assert ev.schema_version == CURRENT_SCHEMA_VERSION
        assert ev.exit_code == 0
        assert ev.status == "passed"

    def test_to_and_from_dict_roundtrip(self):
        ev = CiEvidence(
            head_sha="a" * 40,
            base_sha="b" * 40,
            succeeded=True,
            schema_version=1,
            ci_definition_digest="c" * 64,
            lockfile_digest="d" * 64,
            os="Linux",
            architecture="x86_64",
            python_version="CPython 3.13.0",
            started_at="2026-09-24T12:00:00Z",
            completed_at="2026-09-24T12:01:00Z",
            exit_code=0,
            status="passed",
        )
        data = ev.to_dict()
        assert isinstance(data, dict)
        assert data["schema_version"] == 1
        assert data["exit_code"] == 0
        assert data["status"] == "passed"
        assert data["succeeded"] is True

        restored = CiEvidence.from_dict(data)
        assert restored == ev

    def test_from_dict_rejects_unknown_schema(self):
        data = {
            "schema_version": 999,
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "succeeded": True,
            "ci_definition_digest": "c" * 64,
            "lockfile_digest": "d" * 64,
            "os": "Linux",
            "architecture": "x86_64",
            "python_version": "CPython 3.13.0",
            "started_at": "2026-09-24T12:00:00Z",
            "completed_at": "2026-09-24T12:01:00Z",
            "exit_code": 0,
            "status": "passed",
        }
        with pytest.raises(CiEvidenceInvalidError, match="schema version"):
            CiEvidence.from_dict(data)

    def test_from_dict_rejects_missing_or_failed_status(self):
        base_data = {
            "schema_version": 1,
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "succeeded": True,
            "ci_definition_digest": "c" * 64,
            "lockfile_digest": "d" * 64,
            "os": "Linux",
            "architecture": "x86_64",
            "python_version": "CPython 3.13.0",
            "started_at": "2026-09-24T12:00:00Z",
            "completed_at": "2026-09-24T12:01:00Z",
            "exit_code": 0,
            "status": "passed",
            "worktree_clean": True,
            "tree_sha": "e" * 40,
        }

        # Failed exit code
        failed_exit = dict(base_data, exit_code=1)
        with pytest.raises(CiEvidenceInvalidError, match="exit code"):
            CiEvidence.from_dict(failed_exit)

        # succeeded is False
        failed_succeeded = dict(base_data, succeeded=False)
        with pytest.raises(CiEvidenceInvalidError, match="succeeded"):
            CiEvidence.from_dict(failed_succeeded)

        # status is not passed/success
        invalid_status = dict(base_data, status="failed")
        with pytest.raises(CiEvidenceInvalidError, match="status"):
            CiEvidence.from_dict(invalid_status)

        # Missing required field
        missing_sha = dict(base_data)
        del missing_sha["head_sha"]
        with pytest.raises(CiEvidenceInvalidError, match="head_sha"):
            CiEvidence.from_dict(missing_sha)


class TestEvidenceStorageAndAtomicRename:
    """Test resolution of evidence path, invalidation, and atomic persistence."""

    def test_evidence_stored_outside_worktree_tracking_area(self, git_worktree: Path):
        ev_path = resolve_evidence_path(git_worktree)
        # Should be inside .orchestune/ci
        assert ev_path == git_worktree / ".orchestune" / "ci" / CI_EVIDENCE_FILENAME
        assert ev_path.name == CI_EVIDENCE_FILENAME

        # Record evidence
        record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )

        assert ev_path.is_file()

        # git status must remain clean
        res = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=git_worktree,
            capture_output=True,
            text=True,
            check=True,
        )
        assert res.stdout.strip() == "", "Evidence file must not appear in git status"

    def test_concurrent_worktrees_have_isolated_evidence(
        self, git_worktree: Path, tmp_path: Path
    ):
        """Issue #1049: Concurrent worktrees do not share or overwrite each other's evidence."""
        wt1 = tmp_path / "wt1"
        wt2 = tmp_path / "wt2"
        subprocess.run(
            ["git", "worktree", "add", str(wt1), "-b", "branch-wt1"],
            cwd=git_worktree,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "worktree", "add", str(wt2), "-b", "branch-wt2"],
            cwd=git_worktree,
            check=True,
            capture_output=True,
        )

        ev_path1 = resolve_evidence_path(wt1)
        ev_path2 = resolve_evidence_path(wt2)
        assert ev_path1 != ev_path2
        assert ev_path1 == wt1 / ".orchestune" / "ci" / CI_EVIDENCE_FILENAME
        assert ev_path2 == wt2 / ".orchestune" / "ci" / CI_EVIDENCE_FILENAME

        record_ci_evidence(
            worktree_root=wt1,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        assert ev_path1.is_file()
        assert not ev_path2.exists()

    def test_legacy_git_evidence_not_reused_without_new_ci(self, git_worktree: Path):
        """Issue #1049: Legacy evidence under .git is not reused by default path."""
        legacy_path = git_worktree / ".git" / CI_EVIDENCE_FILENAME
        legacy_path.write_text("{}", encoding="utf-8")
        req = CompleteRequest.done(issue_number=1049, pr=1, worktree_root=git_worktree)
        with pytest.raises(CiEvidenceMissingError, match="No CI evidence found"):
            validate_ci_evidence(req)

    def test_evidence_storage_fails_with_clear_error_when_storage_not_writable(
        self, git_worktree: Path, tmp_path: Path
    ):
        """Issue #1049: When evidence storage directory is not writable, fails with clear error."""
        non_writable_dir = tmp_path / "ro_storage"
        non_writable_dir.mkdir()
        ro_evidence = non_writable_dir / "sub" / CI_EVIDENCE_FILENAME
        with patch.dict(os.environ, {"ORCHESTUNE_CI_EVIDENCE_PATH": str(ro_evidence)}):
            with patch("os.replace", side_effect=OSError("Permission denied")):
                with pytest.raises(
                    CiEvidenceError, match="Failed to persist CI evidence"
                ):
                    record_ci_evidence(
                        worktree_root=git_worktree,
                        started_at="2026-09-24T12:00:00Z",
                        exit_code=0,
                    )

    def test_evidence_storage_succeeds_when_git_dir_is_read_only(
        self, git_worktree: Path, tmp_path: Path
    ):
        """Issue #1049: evidence invalidation, recording, and validation must succeed

        even when git metadata directory (e.g. .git/worktrees/<name>) is read-only.
        """
        linked_wt = tmp_path / "linked_wt"
        subprocess.run(
            ["git", "worktree", "add", str(linked_wt), "-b", "linked-branch"],
            cwd=git_worktree,
            check=True,
            capture_output=True,
        )
        (linked_wt / ".gitignore").write_text(".orchestune/ci/\n", encoding="utf-8")
        gitdir_content = (linked_wt / ".git").read_text(encoding="utf-8").strip()
        assert gitdir_content.startswith("gitdir:")
        gitdir_rel = gitdir_content.split("gitdir:", 1)[1].strip()
        gitdir_path = Path(gitdir_rel)
        if not gitdir_path.is_absolute():
            gitdir_path = (linked_wt / gitdir_path).resolve()

        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip(
                "Root user bypasses DAC permission checks for read-only directory"
            )

        original_mode = gitdir_path.stat().st_mode
        try:
            os.chmod(gitdir_path, 0o555)
            # Verify that gitdir genuinely blocks raw writes before trusting reproducer
            probe_file = gitdir_path / "probe.tmp"
            try:
                probe_file.write_text("probe", encoding="utf-8")
                probe_file.unlink(missing_ok=True)
                is_read_only = False
            except OSError:
                is_read_only = True

            if not is_read_only:
                pytest.skip(
                    "Filesystem does not enforce DAC read-only permissions in this environment"
                )

            ev = record_ci_evidence(
                worktree_root=linked_wt,
                started_at="2026-09-24T12:00:00Z",
                exit_code=0,
            )
            assert ev.succeeded is True
            req = CompleteRequest.done(issue_number=1049, pr=1, worktree_root=linked_wt)
            validated = validate_ci_evidence(req)
            assert validated.succeeded is True
            assert (
                resolve_evidence_path(linked_wt)
                == linked_wt / ".orchestune" / "ci" / CI_EVIDENCE_FILENAME
            )
        finally:
            os.chmod(gitdir_path, original_mode)

    def test_invalidate_removes_old_evidence_and_temp_files(self, git_worktree: Path):
        ev_path = resolve_evidence_path(git_worktree)
        ev_path.parent.mkdir(parents=True, exist_ok=True)
        ev_path.write_text("{}", encoding="utf-8")

        temp_file = ev_path.with_name(f"{CI_EVIDENCE_FILENAME}.tmp.12345.xyz")
        temp_file.write_text("partial", encoding="utf-8")

        assert ev_path.exists()
        assert temp_file.exists()

        invalidate_ci_evidence(git_worktree)

        assert not ev_path.exists()
        assert not temp_file.exists()

    def test_atomic_rename_ensures_no_partial_write(self, git_worktree: Path):
        record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        ev_path = resolve_evidence_path(git_worktree)
        assert ev_path.is_file()
        content = json.loads(ev_path.read_text(encoding="utf-8"))
        assert content["succeeded"] is True
        assert content["exit_code"] == 0

        # No residual .tmp files
        tmp_files = list(ev_path.parent.glob(f"{CI_EVIDENCE_FILENAME}.tmp.*"))
        assert len(tmp_files) == 0


class TestValidateCiEvidence:
    """Test validation of CI evidence against worktree state and acceptance criteria."""

    def test_validate_succeeds_when_matching(self, git_worktree: Path):
        record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        ev = validate_ci_evidence(req)
        assert ev.succeeded is True
        assert ev.exit_code == 0

    def test_validate_rejects_missing_evidence(self, git_worktree: Path):
        invalidate_ci_evidence(git_worktree)
        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        with pytest.raises(CiEvidenceMissingError, match="No CI evidence found"):
            validate_ci_evidence(req)

    def test_validate_rejects_corrupted_json(self, git_worktree: Path):
        ev_path = resolve_evidence_path(git_worktree)
        ev_path.parent.mkdir(parents=True, exist_ok=True)
        ev_path.write_text("{corrupted json", encoding="utf-8")

        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        with pytest.raises(CiEvidenceInvalidError, match="Corrupt"):
            validate_ci_evidence(req)

    def test_validate_rejects_head_sha_mismatch(self, git_worktree: Path):
        record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        # Create a new commit to advance HEAD
        (git_worktree / "file.txt").write_text("updated\n", encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-am", "new commit"],
            cwd=git_worktree,
            check=True,
            capture_output=True,
        )

        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        with pytest.raises(CiEvidenceMismatchError, match="HEAD SHA mismatch"):
            validate_ci_evidence(req)

    def test_validate_accepts_evidence_when_base_advances(self, git_worktree: Path):
        # Create a base branch with an initial commit
        subprocess.run(
            ["git", "branch", "parent/issue-894", "HEAD"], cwd=git_worktree, check=True
        )
        record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )

        # Advance parent/issue-894 with a new commit (simulate base advancing)
        subprocess.run(
            ["git", "checkout", "parent/issue-894"],
            cwd=git_worktree,
            check=True,
            capture_output=True,
        )
        (git_worktree / "base_file.txt").write_text("base update\n", encoding="utf-8")
        subprocess.run(["git", "add", "base_file.txt"], cwd=git_worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "advance base"],
            cwd=git_worktree,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_worktree,
            check=True,
            capture_output=True,
        )

        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        # HEAD and tree are unchanged, so the evidence still covers what CI tested.
        ev = validate_ci_evidence(req)
        assert ev.succeeded is True
        # A sibling merge into the base must not force a full CI rerun (#1047).
        ev = run_local_ci_if_needed(req, ci_command=["false"])
        assert ev.succeeded is True

    def test_validate_rejects_ci_definition_mismatch(self, git_worktree: Path):
        ev_obj = record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        ev_path = resolve_evidence_path(git_worktree)
        data = ev_obj.to_dict()
        data["ci_definition_digest"] = "0" * 64
        ev_path.write_text(json.dumps(data), encoding="utf-8")

        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        with pytest.raises(
            CiEvidenceMismatchError, match="CI definition digest mismatch"
        ):
            validate_ci_evidence(req)

    def test_validate_rejects_lockfile_mismatch(self, git_worktree: Path):
        ev_obj = record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        ev_path = resolve_evidence_path(git_worktree)
        data = ev_obj.to_dict()
        data["lockfile_digest"] = "0" * 64
        ev_path.write_text(json.dumps(data), encoding="utf-8")

        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        with pytest.raises(CiEvidenceMismatchError, match="Lockfile digest mismatch"):
            validate_ci_evidence(req)

    def test_validate_rejects_dirty_worktree(self, git_worktree: Path):
        record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        (git_worktree / "uncommitted.txt").write_text("uncommitted\n", encoding="utf-8")
        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        with pytest.raises(
            CiEvidenceMismatchError, match="Worktree has uncommitted changes"
        ):
            validate_ci_evidence(req)

    def test_validate_rejects_evidence_recorded_from_dirty_worktree(
        self, git_worktree: Path
    ):
        (git_worktree / "uncommitted.txt").write_text("uncommitted\n", encoding="utf-8")
        record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        # Discard uncommitted changes to make current worktree clean
        (git_worktree / "uncommitted.txt").unlink()
        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        with pytest.raises(
            CiEvidenceMismatchError, match="recorded from a dirty worktree"
        ):
            validate_ci_evidence(req)

    def test_validate_rejects_tree_sha_mismatch(self, git_worktree: Path):
        ev_obj = record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        ev_path = resolve_evidence_path(git_worktree)
        data = ev_obj.to_dict()
        data["tree_sha"] = "0" * 40
        ev_path.write_text(json.dumps(data), encoding="utf-8")

        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        with pytest.raises(CiEvidenceMismatchError, match="Tree SHA mismatch"):
            validate_ci_evidence(req)

    def test_validate_rejects_environment_mismatch(self, git_worktree: Path):
        ev_obj = record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        # Tamper OS in evidence
        ev_path = resolve_evidence_path(git_worktree)
        data = ev_obj.to_dict()
        data["os"] = "NonExistentOS"
        ev_path.write_text(json.dumps(data), encoding="utf-8")

        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        with pytest.raises(CiEvidenceMismatchError, match="environment mismatch"):
            validate_ci_evidence(req)


class TestRunLocalCiIfNeeded:
    """Test run_local_ci_if_needed behavior, dry-run safety, and execution rules."""

    def test_reuses_valid_evidence_without_rerun(self, git_worktree: Path):
        record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )

        # Mock runner command that fails if called
        ev = run_local_ci_if_needed(req, ci_command=["false"])
        assert ev.succeeded is True

    def test_reruns_ci_on_missing_or_mismatched_evidence(self, git_worktree: Path):
        invalidate_ci_evidence(git_worktree)
        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )

        # Provide a mock CI command that records evidence
        mock_cmd = [
            sys.executable,
            "-c",
            "from orchestune.complete.ci_evidence import record_ci_evidence; "
            "record_ci_evidence(worktree_root='.', exit_code=0)",
        ]
        ev = run_local_ci_if_needed(req, ci_command=mock_cmd)
        assert ev.succeeded is True
        assert ev.exit_code == 0

    def test_ci_failure_raises_and_rejects_done(self, git_worktree: Path):
        invalidate_ci_evidence(git_worktree)
        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )

        # CI command that fails
        failing_cmd = [sys.executable, "-c", "import sys; sys.exit(2)"]
        with pytest.raises(CiExecutionError, match="Local CI execution failed"):
            run_local_ci_if_needed(req, ci_command=failing_cmd)

    def test_dry_run_never_executes_ci_and_only_reads(self, git_worktree: Path):
        invalidate_ci_evidence(git_worktree)
        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree, dry_run=True
        )

        # Must not run CI command
        with pytest.raises(CiEvidenceMissingError):
            run_local_ci_if_needed(req, ci_command=["touch", "should_not_run"])

        assert not (git_worktree / "should_not_run").exists()

    def test_not_needed_and_blocked_do_not_run_ci(self, git_worktree: Path):
        # not-needed request
        req_nn = CompleteRequest.not_needed(
            issue_number=1000, worktree_root=git_worktree
        )
        with pytest.raises(ValueError, match="not applicable"):
            run_local_ci_if_needed(req_nn, ci_command=["touch", "should_not_run_nn"])

        assert not (git_worktree / "should_not_run_nn").exists()

        # blocked request
        req_bl = CompleteRequest.blocked(
            issue_number=1000, reason="some-reason", worktree_root=git_worktree
        )
        with pytest.raises(ValueError, match="not applicable"):
            run_local_ci_if_needed(req_bl, ci_command=["touch", "should_not_run_bl"])

        assert not (git_worktree / "should_not_run_bl").exists()


class TestCliEntrypoint:
    """Test python -m orchestune.complete.ci_evidence CLI commands."""

    def test_cli_invalidate_and_record(self, git_worktree: Path):
        # 1. Invalidate
        res_inv = subprocess.run(
            [
                sys.executable,
                "-m",
                "orchestune.complete.ci_evidence",
                "invalidate",
                "--worktree",
                str(git_worktree),
            ],
            capture_output=True,
            text=True,
        )
        assert res_inv.returncode == 0

        # 2. Record
        res_rec = subprocess.run(
            [
                sys.executable,
                "-m",
                "orchestune.complete.ci_evidence",
                "record",
                "--worktree",
                str(git_worktree),
                "--started-at",
                "2026-09-24T12:00:00Z",
                "--exit-code",
                "0",
            ],
            capture_output=True,
            text=True,
        )
        assert res_rec.returncode == 0

        ev_path = resolve_evidence_path(git_worktree)
        assert ev_path.is_file()
        data = json.loads(ev_path.read_text(encoding="utf-8"))
        assert data["exit_code"] == 0
        assert data["succeeded"] is True


class TestRunnerScriptsEvidenceHook:
    """Verify local-ci.sh and local-ci.ps1 include evidence invalidation and recording."""

    def test_local_ci_sh_invokes_ci_evidence(self):
        script = Path("scripts/local-ci.sh").read_text(encoding="utf-8")
        assert "orchestune.complete.ci_evidence invalidate" in script
        assert "orchestune.complete.ci_evidence record" in script

    def test_local_ci_ps1_invokes_ci_evidence(self):
        script = Path("scripts/local-ci.ps1").read_text(encoding="utf-8")
        assert "orchestune.complete.ci_evidence invalidate" in script
        assert "orchestune.complete.ci_evidence record" in script


class TestEdgeCasesAndBoundaryConditions:
    """Test boundary conditions, environment overrides, and error fallbacks."""

    def test_from_dict_rejects_non_dict(self):
        with pytest.raises(CiEvidenceInvalidError, match="must be a dictionary"):
            CiEvidence.from_dict("not-a-dict")  # type: ignore

    def test_environment_override_path(self, tmp_path: Path):
        override_file = tmp_path / "custom_evidence.json"
        with patch.dict(
            os.environ, {"ORCHESTUNE_CI_EVIDENCE_PATH": str(override_file)}
        ):
            assert resolve_evidence_path() == override_file
            assert resolve_evidence_dir() == tmp_path

    def test_lockfile_digest_when_absent(self, tmp_path: Path):
        digest = compute_lockfile_digest(tmp_path)
        assert isinstance(digest, str)
        assert len(digest) == 64

    def test_resolve_base_sha_from_payload(self, git_worktree: Path):
        req = CompleteRequest.blocked(
            issue_number=1000,
            reason="base-branch-red",
            base_sha="f" * 40,
            worktree_root=git_worktree,
        )
        from orchestune.complete.ci_evidence import _resolve_base_sha

        assert _resolve_base_sha(git_worktree, req) == "f" * 40

    def test_resolve_head_sha_failure(self, tmp_path: Path):
        from orchestune.complete.ci_evidence import _resolve_head_sha

        with pytest.raises(CiEvidenceError, match="Failed to resolve HEAD"):
            _resolve_head_sha(tmp_path)

    def test_default_ci_command_windows(self, tmp_path: Path):
        from orchestune.complete.ci_evidence import _resolve_default_ci_command

        with patch("sys.platform", "win32"):
            cmd = _resolve_default_ci_command(tmp_path)
            assert cmd[0] == "powershell"
            assert "local-ci.ps1" in cmd[-1]

    def test_run_local_ci_string_command(self, git_worktree: Path):
        invalidate_ci_evidence(git_worktree)
        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        py_exe = sys.executable.replace("\\", "/")
        py_cmd = (
            f'"{py_exe}" -c "from orchestune.complete.ci_evidence import record_ci_evidence; '
            "record_ci_evidence(worktree_root='.', exit_code=0)\""
        )
        ev = run_local_ci_if_needed(req, ci_command=py_cmd)
        assert ev.succeeded is True

    def test_validate_ci_evidence_without_worktree_root_in_request(
        self, git_worktree: Path
    ):
        record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        req = CompleteRequest.done(issue_number=1000, pr=100)
        old_cwd = os.getcwd()
        try:
            os.chdir(git_worktree)
            ev = validate_ci_evidence(req)
            assert ev.succeeded is True
        finally:
            os.chdir(old_cwd)

    def test_default_ci_command_posix(self, tmp_path: Path):
        from orchestune.complete.ci_evidence import _resolve_default_ci_command

        with patch("sys.platform", "linux"):
            cmd = _resolve_default_ci_command(tmp_path)
            assert "local-ci.sh" in cmd[0]

    def test_main_function_direct_call(self, git_worktree: Path):
        from orchestune.complete.ci_evidence import main

        main(["invalidate", "--worktree", str(git_worktree)])
        main(
            [
                "record",
                "--worktree",
                str(git_worktree),
                "--started-at",
                "2026-09-24T12:00:00Z",
            ]
        )
        ev_path = resolve_evidence_path(git_worktree)
        assert ev_path.is_file()

    def test_resolve_evidence_dir_oserror_fallback(self, tmp_path: Path):
        from orchestune.complete.ci_evidence import resolve_evidence_dir

        with patch(
            "orchestune.complete.ci_evidence.run_git",
            side_effect=OSError("git not found"),
        ):
            d = resolve_evidence_dir(tmp_path)
            assert d == tmp_path / ".orchestune" / "ci"

    def test_query_remote_ref_tip_authoritative(
        self, git_worktree: Path, tmp_path: Path
    ):
        from orchestune.complete.ci_evidence import (
            _query_remote_ref_tip,
            _resolve_ref_tip,
        )

        upstream = tmp_path / "upstream.git"
        subprocess.run(
            ["git", "clone", "--bare", str(git_worktree), str(upstream)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(upstream)],
            cwd=git_worktree,
            check=True,
        )

        remote_tip = _query_remote_ref_tip(git_worktree, "main")
        assert remote_tip is not None
        assert len(remote_tip) == 40
        assert _resolve_ref_tip(git_worktree, "main") == remote_tip

    def test_query_remote_ref_tip_fallback_when_remote_unavailable(
        self, git_worktree: Path
    ):
        from orchestune.complete.ci_evidence import (
            _query_remote_ref_tip,
            _resolve_ref_tip,
        )

        assert _query_remote_ref_tip(git_worktree, "main") is None
        local_tip = _resolve_ref_tip(git_worktree, "main")
        assert local_tip is not None
        assert len(local_tip) == 40

    def test_query_remote_ref_tip_fails_closed_when_origin_unreachable(
        self, git_worktree: Path, tmp_path: Path
    ):
        from orchestune.complete.ci_evidence import (
            _query_remote_ref_tip,
            _resolve_ref_tip,
        )
        from orchestune.infra.git_cli import GitResult

        # Add a dummy origin remote
        subprocess.run(
            ["git", "remote", "add", "origin", "https://invalid.example.com/repo.git"],
            cwd=git_worktree,
            check=True,
        )

        with patch("orchestune.complete.ci_evidence.run_git") as mock_git:
            # remote get-url origin succeeds
            def fake_run_git(args, **kwargs):
                if args[:2] == ["remote", "get-url"]:
                    return GitResult(
                        returncode=0, stdout="https://invalid.example.com", stderr=""
                    )
                if args[0] == "ls-remote":
                    return GitResult(
                        returncode=128, stdout="", stderr="fatal: unable to access"
                    )
                return GitResult(returncode=0, stdout="a" * 40, stderr="")

            mock_git.side_effect = fake_run_git

            with pytest.raises(
                CiEvidenceError, match="Failed to query authoritative remote tip"
            ):
                _query_remote_ref_tip(git_worktree, "origin/main")

            with pytest.raises(
                CiEvidenceError, match="Failed to query authoritative remote tip"
            ):
                _resolve_ref_tip(git_worktree, "origin/main")

    def test_record_respects_custom_state_path_and_explicit_base(
        self, git_worktree: Path, tmp_path: Path
    ):
        custom_base_sha = "1" * 40
        # Explicit base_sha passed directly
        ev1 = record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
            base_sha=custom_base_sha,
        )
        assert ev1.base_sha == custom_base_sha

        # Via ORCHESTUNE_BASE_SHA environment variable
        with patch.dict(os.environ, {"ORCHESTUNE_BASE_SHA": "2" * 40}):
            ev2 = record_ci_evidence(
                worktree_root=git_worktree,
                started_at="2026-09-24T12:00:00Z",
                exit_code=0,
            )
            assert ev2.base_sha == "2" * 40

        # Via custom state_path and issue
        state_file = tmp_path / "custom_state.json"
        state_file.write_text(
            json.dumps(
                {
                    "active_worktrees": {
                        "2000": {
                            "worktree_path": str(git_worktree),
                            "base_ref": "custom-base",
                            "base_sha": "3" * 40,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        ev3 = record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
            state_path=state_file,
            issue_number=2000,
        )
        assert ev3.base_sha == "3" * 40

    def test_local_ci_scripts_invalidate_before_uv_guard(self):
        sh_text = Path("scripts/local-ci.sh").read_text(encoding="utf-8")
        ps1_text = Path("scripts/local-ci.ps1").read_text(encoding="utf-8")

        # In bash script, direct removal occurs before command -v uv check
        idx_sh_rm = sh_text.find("rm -f")
        idx_sh_uv = sh_text.find("command -v uv")
        assert idx_sh_rm != -1 and idx_sh_uv != -1
        assert (
            idx_sh_rm < idx_sh_uv
        ), "Direct removal must occur before uv check in local-ci.sh"

        # In PowerShell script, Remove-Item occurs before Get-Command uv check
        idx_ps1_rm = ps1_text.find("Remove-Item")
        idx_ps1_uv = ps1_text.find("Get-Command uv")
        assert idx_ps1_rm != -1 and idx_ps1_uv != -1
        assert (
            idx_ps1_rm < idx_ps1_uv
        ), "Direct removal must occur before uv check in local-ci.ps1"

    def test_validate_resolves_runner_environment_rather_than_verifier_process(
        self, git_worktree: Path
    ):
        ev = record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )

        with patch(
            "orchestune.complete.ci_evidence.platform.python_version",
            return_value="3.12.0",
        ):
            validated = validate_ci_evidence(req)
            assert validated.succeeded is True
            assert validated.python_version == ev.python_version

    def test_invalidate_ci_evidence_raises_when_file_cannot_be_removed(
        self, git_worktree: Path
    ):
        ev_path = resolve_evidence_path(git_worktree)
        ev_path.parent.mkdir(parents=True, exist_ok=True)
        ev_path.write_text("{}", encoding="utf-8")

        with patch.object(Path, "unlink", side_effect=OSError("Permission denied")):
            with pytest.raises(
                CiEvidenceError, match="Failed to remove prior CI evidence"
            ):
                invalidate_ci_evidence(git_worktree)

    def test_local_ci_scripts_verify_evidence_absence(self):
        sh_text = Path("scripts/local-ci.sh").read_text(encoding="utf-8")
        ps1_text = Path("scripts/local-ci.ps1").read_text(encoding="utf-8")

        assert 'echo "ERROR: Failed to remove prior CI evidence' in sh_text
        assert "Failed to remove prior CI evidence" in ps1_text
        assert ".orchestune/ci/ci_evidence.json" in sh_text
        assert ".orchestune\\ci\\ci_evidence.json" in ps1_text
        assert 'GIT_DIR="$(git rev-parse --git-dir' not in sh_text
        assert "$GitDir = (git rev-parse --git-dir" not in ps1_text

    def test_resolve_runner_environment_does_not_create_venv(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'test'\nversion = '0.1.0'\n", encoding="utf-8"
        )
        _os, _arch, py_ver = _resolve_runner_environment(tmp_path)
        assert py_ver
        assert not (tmp_path / ".venv").exists()

    def test_resolve_runner_environment_respects_uv_project_environment(
        self, tmp_path: Path
    ):
        custom_bin = tmp_path / "custom" / "bin"
        custom_bin.mkdir(parents=True)
        py_mock = custom_bin / "python"
        py_mock.write_text("#!/bin/sh\n", encoding="utf-8")

        with patch.dict(
            os.environ, {"UV_PROJECT_ENVIRONMENT": str(tmp_path / "custom")}
        ):
            with patch(
                "orchestune.complete.ci_evidence._query_python_version",
                return_value="CPython 3.13.9",
            ) as mock_query:
                _os, _arch, py_ver = _resolve_runner_environment(tmp_path)
                assert py_ver == "CPython 3.13.9"
                mock_query.assert_called_once_with(
                    [str(py_mock)], cwd=tmp_path.resolve()
                )

    def test_record_ci_evidence_rejects_head_change_during_run(
        self, git_worktree: Path
    ):
        with pytest.raises(CiEvidenceMismatchError, match="HEAD changed during CI run"):
            record_ci_evidence(
                worktree_root=git_worktree,
                expected_head="0" * 40,
            )

    def test_record_ci_evidence_rejects_tree_change_during_run(
        self, git_worktree: Path
    ):
        with pytest.raises(CiEvidenceMismatchError, match="Tree changed during CI run"):
            record_ci_evidence(
                worktree_root=git_worktree,
                expected_tree="0" * 40,
            )

    def test_record_ci_evidence_ignores_base_tip_change_during_run(
        self, git_worktree: Path
    ):
        with patch.dict(os.environ, {"ORCHESTUNE_EXPECTED_BASE": "0" * 40}):
            ev = record_ci_evidence(worktree_root=git_worktree, base_sha="1" * 40)
        assert ev.base_sha == "1" * 40

    def test_run_local_ci_accepts_base_advancing_during_run(self, git_worktree: Path):
        subprocess.run(
            ["git", "branch", "parent/issue-894", "HEAD"], cwd=git_worktree, check=True
        )
        invalidate_ci_evidence(git_worktree)
        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        # The fake CI advances the base branch before recording evidence.
        advance_and_record = [
            sys.executable,
            "-c",
            "import subprocess\n"
            "tree = subprocess.check_output(['git', 'write-tree'], text=True).strip()\n"
            "commit = subprocess.check_output(\n"
            "    ['git', 'commit-tree', tree, '-p', 'HEAD', '-m', 'advance base'],\n"
            "    text=True,\n"
            ").strip()\n"
            "subprocess.check_call(\n"
            "    ['git', 'update-ref', 'refs/heads/parent/issue-894', commit]\n"
            ")\n"
            "from orchestune.complete.ci_evidence import record_ci_evidence\n"
            "record_ci_evidence(worktree_root='.', exit_code=0)\n",
        ]
        initial_base = subprocess.check_output(
            ["git", "rev-parse", "parent/issue-894"], cwd=git_worktree, text=True
        ).strip()
        ev = run_local_ci_if_needed(req, ci_command=advance_and_record)
        assert ev.succeeded is True
        assert ev.base_sha == initial_base

    def test_local_ci_scripts_capture_and_pass_expected_head_and_tree(self):
        sh_text = Path("scripts/local-ci.sh").read_text(encoding="utf-8")
        ps1_text = Path("scripts/local-ci.ps1").read_text(encoding="utf-8")

        assert "CI_START_HEAD=$(git rev-parse HEAD" in sh_text
        assert "CI_START_TREE=$(git rev-parse 'HEAD^{tree}'" in sh_text
        assert "CI_START_BASE=$(uv run --no-sync python" in sh_text
        assert '--expected-head" "${CI_START_HEAD}"' in sh_text
        assert '--expected-tree" "${CI_START_TREE}"' in sh_text
        assert "--expected-base" not in sh_text
        assert '--base-sha" "${CI_START_BASE}"' in sh_text

        assert "$CiStartHead = (git rev-parse HEAD" in ps1_text
        assert "$CiStartTree = (git rev-parse 'HEAD^{tree}'" in ps1_text
        assert "$resolvedBase = (uv run --no-sync python" in ps1_text
        assert '"--expected-head", $CiStartHead' in ps1_text
        assert '"--expected-tree", $CiStartTree' in ps1_text
        assert "--expected-base" not in ps1_text
        assert '"--base-sha", $CiStartBase' in ps1_text

    def test_cli_resolve_base_outputs_base_sha(
        self, git_worktree: Path, capsys: pytest.CaptureFixture[str]
    ):
        from orchestune.complete.ci_evidence import main as ci_main

        ci_main(["resolve-base", "--worktree", str(git_worktree)])
        captured = capsys.readouterr()
        assert len(captured.out.strip()) == 40

    def test_find_configured_python_passes_no_downloads_and_offline(
        self, tmp_path: Path
    ):
        from orchestune.complete.ci_evidence import _find_configured_python

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=str(tmp_path / "python")
            )
            with patch.object(Path, "is_file", return_value=True):
                res = _find_configured_python(tmp_path)
                assert res == tmp_path / "python"
                mock_run.assert_called_once()
                cmd = mock_run.call_args[0][0]
                assert "--no-python-downloads" in cmd
                assert "--offline" in cmd

    def test_local_ci_scripts_fail_closed_on_base_resolution_failure(self):
        sh_text = Path("scripts/local-ci.sh").read_text(encoding="utf-8")
        ps1_text = Path("scripts/local-ci.ps1").read_text(encoding="utf-8")

        assert "Failed to resolve initial base SHA before starting CI." in sh_text
        assert "Failed to resolve initial base SHA before starting CI." in ps1_text
