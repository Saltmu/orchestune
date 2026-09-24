"""Tests for complete CI evidence recording and validation (#1000)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

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
    (repo / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("lockfile content\n", encoding="utf-8")
    (repo / "file.txt").write_text("initial\n", encoding="utf-8")

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
        # Should be inside .git
        assert ".git" in str(ev_path)
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

    def test_validate_rejects_base_sha_mismatch_when_base_advances(
        self, git_worktree: Path
    ):
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
        with pytest.raises(CiEvidenceMismatchError, match="Base SHA mismatch"):
            validate_ci_evidence(req)

    def test_validate_rejects_ci_definition_mismatch(self, git_worktree: Path):
        record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        # Change local-ci.sh definition
        (git_worktree / "scripts" / "local-ci.sh").write_text(
            "#!/bin/sh\n# modified\nexit 0\n", encoding="utf-8"
        )

        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        with pytest.raises(
            CiEvidenceMismatchError, match="CI definition digest mismatch"
        ):
            validate_ci_evidence(req)

    def test_validate_rejects_lockfile_mismatch(self, git_worktree: Path):
        record_ci_evidence(
            worktree_root=git_worktree,
            started_at="2026-09-24T12:00:00Z",
            exit_code=0,
        )
        # Change uv.lock
        (git_worktree / "uv.lock").write_text("modified lockfile\n", encoding="utf-8")

        req = CompleteRequest.done(
            issue_number=1000, pr=100, worktree_root=git_worktree
        )
        with pytest.raises(CiEvidenceMismatchError, match="Lockfile digest mismatch"):
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
            "python3",
            "-c",
            f"from orchestune.complete.ci_evidence import record_ci_evidence; "
            f"record_ci_evidence(worktree_root='{git_worktree}', exit_code=0)",
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
        failing_cmd = ["python3", "-c", "import sys; sys.exit(2)"]
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
                "python3",
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
                "python3",
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
        py_cmd = (
            f'python3 -c "from orchestune.complete.ci_evidence import record_ci_evidence; '
            f"record_ci_evidence(worktree_root='{git_worktree}', exit_code=0)\""
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
            assert d == tmp_path / ".git"
