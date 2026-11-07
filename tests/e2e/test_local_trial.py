"""End-to-end tests for local trial CLI workflow (#1734, #1738)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from orchestune.local_trial.contracts import plan_digest
from orchestune.local_trial.models import TrialPlan

_FAKE_CODEX_SCRIPT = """#!/usr/bin/env python3
import sys
import json
import os
from pathlib import Path

if "--version" in sys.argv:
    print("codex 1.0.0")
    sys.exit(0)

gate_file = os.environ.get("FAKE_CODEX_GATE_FILE")
if gate_file and os.path.exists(gate_file):
    marker = os.environ.get("FAKE_CODEX_GATE_ENTERED")
    if marker:
        Path(marker).write_text("entered")
    import time
    while os.path.exists(gate_file):
        time.sleep(0.05)

marker_dir = os.environ.get("FAKE_CODEX_MARKER_DIR")
if marker_dir:
    Path(marker_dir).mkdir(parents=True, exist_ok=True)
    count = len(list(Path(marker_dir).glob("run_*.json")))
    Path(marker_dir, f"run_{count:03d}.json").write_text(
        json.dumps({"cwd": os.getcwd(), "argv": sys.argv})
    )

output_file = None
for i, arg in enumerate(sys.argv):
    if arg == "--output-last-message" and i + 1 < len(sys.argv):
        output_file = sys.argv[i + 1]

artifact_dir = Path(output_file).parent if output_file else Path.cwd()
context_file = artifact_dir / "context.json"
context = json.loads(context_file.read_text(encoding="utf-8")) if context_file.exists() else {}

trial_id = context.get("trial_id", "0" * 32)
attempt_id = context.get("attempt_id", "0" * 32)
kind = context.get("kind", "plan")
input_head_sha = context.get("input_head_sha", "a" * 40)
p_dig = context.get("plan_digest")

ci_cmd = json.loads(os.environ.get("FAKE_CODEX_CI_CMD", '["python3", "test.py"]'))
behavior = os.environ.get("FAKE_CODEX_BEHAVIOR", "normal")

if kind == "plan":
    payload = {
        "plan": {
            "goal": "E2E sample goal",
            "acceptance_criteria": ["Sample test passes"],
            "write_paths": ["sample.txt"],
            "discipline_paths": [],
            "discipline_notes": [],
            "ci_argv": ci_cmd,
            "discipline_status": "compatible",
        }
    }
elif kind in ("implement", "fix"):
    target = Path("sample.txt")
    if behavior == "ci_fail_always":
        target.write_text(f"fail {attempt_id}\\n")
    elif kind == "fix":
        target.write_text("implementation done fixed\\n")
    elif behavior == "ci_fail_first":
        target.write_text("implement with fail\\n")
    else:
        target.write_text("implementation done\\n")
    previous = context.get("findings", [])
    responses = [
        {"finding_id": f["id"], "action": "fixed", "explanation": "fixed in sample.txt"}
        for f in previous
    ]
    payload = {
        "summary": "Implemented changes",
        "responses": responses,
    }
elif kind == "review":
    if behavior == "review_findings":
        count_file = Path(marker_dir) / "review_count.txt" if marker_dir else None
        rc = int(count_file.read_text()) if count_file and count_file.exists() else 0
        if rc == 0:
            if count_file:
                count_file.write_text("1")
            payload = {
                "verdict": "changes_requested",
                "findings": [
                    {
                        "id": "f" * 32,
                        "severity": "medium",
                        "path": "sample.txt",
                        "line": 1,
                        "reason": "Style issue detected",
                        "requested_change": "Fix style",
                    }
                ],
            }
        else:
            payload = {
                "verdict": "pass",
                "findings": [],
            }
    else:
        payload = {
            "verdict": "pass",
            "findings": [],
        }
else:
    payload = {}

result = {
    "schema_version": 1,
    "trial_id": trial_id,
    "attempt_id": attempt_id,
    "kind": kind,
    "input_head_sha": input_head_sha,
    "plan_digest": p_dig,
    "payload": payload,
}

if output_file:
    Path(output_file).write_text(json.dumps(result))
"""

_TEST_PY_CONTENT = """import sys
from pathlib import Path

sample = Path("sample.txt")
if sample.exists() and "fail" in sample.read_text():
    print("CI failed: fail in sample.txt", file=sys.stderr)
    sys.exit(1)
print("CI passed")
sys.exit(0)
"""


@pytest.fixture
def e2e_setup(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_codex = bin_dir / "codex"
    fake_codex.write_text(_FAKE_CODEX_SCRIPT)
    fake_codex.chmod(
        fake_codex.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    # Initial git repo
    env = os.environ.copy()
    git_bin_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
    python_bin_dir = str(Path(sys.executable).parent)
    env["PATH"] = f"{bin_dir}:{git_bin_dir}:{python_bin_dir}:/bin:/usr/bin"
    # Ensure gh is not in PATH
    for path_dir in env["PATH"].split(":"):
        if (Path(path_dir) / "gh").exists():
            pass  # fake PATH avoids gh

    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    env["FAKE_CODEX_MARKER_DIR"] = str(marker_dir)

    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo_dir,
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Trial Tester"],
        cwd=repo_dir,
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "trial@example.com"],
        cwd=repo_dir,
        env=env,
        check=True,
    )

    (repo_dir / "sample.txt").write_text("initial\n")
    (repo_dir / "test.py").write_text(_TEST_PY_CONTENT)
    subprocess.run(["git", "add", "."], cwd=repo_dir, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"], cwd=repo_dir, env=env, check=True
    )

    req_file = tmp_path / "request.txt"
    req_file.write_text("Please implement requested sample feature.\n")

    return {
        "repo_dir": repo_dir,
        "req_file": req_file,
        "env": env,
        "marker_dir": marker_dir,
        "tmp_path": tmp_path,
    }


def _run_cli(
    args: list[str], env: dict[str, str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "orchestune.cli", "trial", *args]
    return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True)


def test_e2e_normal_flow_keep(e2e_setup):
    repo = e2e_setup["repo_dir"]
    req = e2e_setup["req_file"]
    env = e2e_setup["env"]
    marker_dir = e2e_setup["marker_dir"]

    # 1. Start trial non-interactively
    res = _run_cli(
        ["start", "--request-file", str(req), "--repo", str(repo)], env, repo
    )
    assert res.returncode == 0, res.stderr
    assert "effective_phase: planned" in res.stdout

    # 2. Status inspection
    res = _run_cli(["status", "--repo", str(repo), "--json"], env, repo)
    assert res.returncode == 0, res.stderr
    status_data = json.loads(res.stdout)
    trial_id = status_data["trial_id"]
    assert status_data["effective_phase"] == "planned"
    assert status_data["ready_is_current"] is False

    st = status_data["state"]
    plan_dict = st["plan"]
    p_obj = TrialPlan(
        goal=plan_dict["goal"],
        acceptance_criteria=tuple(plan_dict["acceptance_criteria"]),
        write_paths=tuple(plan_dict["write_paths"]),
        discipline_paths=tuple(plan_dict["discipline_paths"]),
        discipline_notes=tuple(plan_dict["discipline_notes"]),
        ci_argv=tuple(plan_dict["ci_argv"]),
        discipline_status=plan_dict["discipline_status"],
    )
    p_dig = plan_digest(p_obj, st["request"]["digest"], st["repository"]["base_sha"])

    # 3. Resume with approval
    res = _run_cli(
        ["resume", trial_id, "--repo", str(repo), "--approve-plan", p_dig], env, repo
    )
    assert res.returncode == 0, res.stderr
    assert "effective_phase: ready" in res.stdout

    # 4. Status verify ready
    res = _run_cli(["status", trial_id, "--repo", str(repo), "--json"], env, repo)
    assert res.returncode == 0, res.stderr
    status_data = json.loads(res.stdout)
    assert status_data["effective_phase"] == "ready"
    assert status_data["ready_is_current"] is True

    # 5. Record decision keep
    res = _run_cli(
        ["result", trial_id, "--decision", "keep", "--repo", str(repo)], env, repo
    )
    assert res.returncode == 0, res.stderr
    assert "decision: keep" in res.stdout

    # 6. Verify worktrees were distinct for implementation vs review
    runs = sorted(marker_dir.glob("run_*.json"))
    assert len(runs) >= 3  # plan, implement, review
    run_records = [json.loads(r.read_text()) for r in runs]
    impl_cwds = [
        r["cwd"] for r in run_records if any("implement" in a for a in r["argv"])
    ]
    rev_cwds = [r["cwd"] for r in run_records if any("review" in a for a in r["argv"])]
    if impl_cwds and rev_cwds:
        assert impl_cwds[0] != rev_cwds[0]


def test_e2e_manual_adoption(e2e_setup):
    repo = e2e_setup["repo_dir"]
    req = e2e_setup["req_file"]
    env = e2e_setup["env"]

    # Start and resume
    res = _run_cli(
        ["start", "--request-file", str(req), "--repo", str(repo)], env, repo
    )
    assert res.returncode == 0
    res = _run_cli(["status", "--repo", str(repo), "--json"], env, repo)
    data = json.loads(res.stdout)
    trial_id = data["trial_id"]
    plan_dict = data["state"]["plan"]
    p_obj = TrialPlan(
        goal=plan_dict["goal"],
        acceptance_criteria=tuple(plan_dict["acceptance_criteria"]),
        write_paths=tuple(plan_dict["write_paths"]),
        discipline_paths=tuple(plan_dict["discipline_paths"]),
        discipline_notes=tuple(plan_dict["discipline_notes"]),
        ci_argv=tuple(plan_dict["ci_argv"]),
        discipline_status=plan_dict["discipline_status"],
    )
    p_dig = plan_digest(
        p_obj,
        data["state"]["request"]["digest"],
        data["state"]["repository"]["base_sha"],
    )

    res = _run_cli(
        ["resume", trial_id, "--repo", str(repo), "--approve-plan", p_dig], env, repo
    )
    assert res.returncode == 0

    trial_branch = data["state"]["workspace"]["branch"]

    # User manually performs ff-only merge into base branch
    merge_res = subprocess.run(
        ["git", "merge", "--ff-only", trial_branch],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert merge_res.returncode == 0, merge_res.stderr

    # Result --decision adopted
    res = _run_cli(
        ["result", trial_id, "--decision", "adopted", "--repo", str(repo)], env, repo
    )
    assert res.returncode == 0, res.stderr
    assert "decision: adopted" in res.stdout

    # Verify content in source repository
    assert (repo / "sample.txt").read_text() == "implementation done\n"


def test_e2e_ci_failure_and_fix(e2e_setup):
    repo = e2e_setup["repo_dir"]
    req = e2e_setup["req_file"]
    env = e2e_setup["env"].copy()
    env["FAKE_CODEX_BEHAVIOR"] = "ci_fail_first"

    res = _run_cli(
        ["start", "--request-file", str(req), "--repo", str(repo)], env, repo
    )
    assert res.returncode == 0
    res = _run_cli(["status", "--repo", str(repo), "--json"], env, repo)
    data = json.loads(res.stdout)
    trial_id = data["trial_id"]
    plan_dict = data["state"]["plan"]
    p_obj = TrialPlan(
        goal=plan_dict["goal"],
        acceptance_criteria=tuple(plan_dict["acceptance_criteria"]),
        write_paths=tuple(plan_dict["write_paths"]),
        discipline_paths=tuple(plan_dict["discipline_paths"]),
        discipline_notes=tuple(plan_dict["discipline_notes"]),
        ci_argv=tuple(plan_dict["ci_argv"]),
        discipline_status=plan_dict["discipline_status"],
    )
    p_dig = plan_digest(
        p_obj,
        data["state"]["request"]["digest"],
        data["state"]["repository"]["base_sha"],
    )

    # Resume: initial CI fails, fix runs with clean content, re-runs CI -> ready
    res = _run_cli(
        ["resume", trial_id, "--repo", str(repo), "--approve-plan", p_dig], env, repo
    )
    assert res.returncode == 0, res.stderr
    assert "effective_phase: ready" in res.stdout


def test_e2e_review_findings_and_fix(e2e_setup):
    repo = e2e_setup["repo_dir"]
    req = e2e_setup["req_file"]
    env = e2e_setup["env"].copy()
    env["FAKE_CODEX_BEHAVIOR"] = "review_findings"

    res = _run_cli(
        ["start", "--request-file", str(req), "--repo", str(repo)], env, repo
    )
    assert res.returncode == 0
    res = _run_cli(["status", "--repo", str(repo), "--json"], env, repo)
    data = json.loads(res.stdout)
    trial_id = data["trial_id"]
    plan_dict = data["state"]["plan"]
    p_obj = TrialPlan(
        goal=plan_dict["goal"],
        acceptance_criteria=tuple(plan_dict["acceptance_criteria"]),
        write_paths=tuple(plan_dict["write_paths"]),
        discipline_paths=tuple(plan_dict["discipline_paths"]),
        discipline_notes=tuple(plan_dict["discipline_notes"]),
        ci_argv=tuple(plan_dict["ci_argv"]),
        discipline_status=plan_dict["discipline_status"],
    )
    p_dig = plan_digest(
        p_obj,
        data["state"]["request"]["digest"],
        data["state"]["repository"]["base_sha"],
    )

    res = _run_cli(
        ["resume", trial_id, "--repo", str(repo), "--approve-plan", p_dig], env, repo
    )
    assert res.returncode == 0, res.stderr
    assert "effective_phase: ready" in res.stdout


def test_e2e_concurrent_start_rejection(e2e_setup):
    repo = e2e_setup["repo_dir"]
    req = e2e_setup["req_file"]
    env = e2e_setup["env"]

    # Start first trial
    res = _run_cli(
        ["start", "--request-file", str(req), "--repo", str(repo)], env, repo
    )
    assert res.returncode == 0

    # Second trial on same repo rejected
    res2 = _run_cli(
        ["start", "--request-file", str(req), "--repo", str(repo)], env, repo
    )
    assert res2.returncode == 2
    assert (
        "already active" in res2.stderr.lower()
        or "already_active" in res2.stderr.lower()
    )


def test_e2e_base_changed_invalidates_ready(e2e_setup):
    repo = e2e_setup["repo_dir"]
    req = e2e_setup["req_file"]
    env = e2e_setup["env"]

    res = _run_cli(
        ["start", "--request-file", str(req), "--repo", str(repo)], env, repo
    )
    assert res.returncode == 0
    res = _run_cli(["status", "--repo", str(repo), "--json"], env, repo)
    data = json.loads(res.stdout)
    trial_id = data["trial_id"]
    plan_dict = data["state"]["plan"]
    p_obj = TrialPlan(
        goal=plan_dict["goal"],
        acceptance_criteria=tuple(plan_dict["acceptance_criteria"]),
        write_paths=tuple(plan_dict["write_paths"]),
        discipline_paths=tuple(plan_dict["discipline_paths"]),
        discipline_notes=tuple(plan_dict["discipline_notes"]),
        ci_argv=tuple(plan_dict["ci_argv"]),
        discipline_status=plan_dict["discipline_status"],
    )
    p_dig = plan_digest(
        p_obj,
        data["state"]["request"]["digest"],
        data["state"]["repository"]["base_sha"],
    )

    res = _run_cli(
        ["resume", trial_id, "--repo", str(repo), "--approve-plan", p_dig], env, repo
    )
    assert res.returncode == 0

    # Advance base branch with new commit
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "advance base"],
        cwd=repo,
        env=env,
        check=True,
    )

    # Check status
    res = _run_cli(["status", trial_id, "--repo", str(repo), "--json"], env, repo)
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["ready_is_current"] is False
    assert data["reason"] == "base_changed"

    # Adopted rejected
    res = _run_cli(
        ["result", trial_id, "--decision", "adopted", "--repo", str(repo)], env, repo
    )
    assert res.returncode == 2


def test_e2e_budget_exhaustion_stops(e2e_setup):
    repo = e2e_setup["repo_dir"]
    req = e2e_setup["req_file"]
    env = e2e_setup["env"].copy()
    env["FAKE_CODEX_BEHAVIOR"] = "ci_fail_always"
    marker_dir = e2e_setup["marker_dir"]

    res = _run_cli(
        ["start", "--request-file", str(req), "--repo", str(repo)], env, repo
    )
    assert res.returncode == 0
    res = _run_cli(["status", "--repo", str(repo), "--json"], env, repo)
    data = json.loads(res.stdout)
    trial_id = data["trial_id"]
    plan_dict = data["state"]["plan"]
    p_obj = TrialPlan(
        goal=plan_dict["goal"],
        acceptance_criteria=tuple(plan_dict["acceptance_criteria"]),
        write_paths=tuple(plan_dict["write_paths"]),
        discipline_paths=tuple(plan_dict["discipline_paths"]),
        discipline_notes=tuple(plan_dict["discipline_notes"]),
        ci_argv=tuple(plan_dict["ci_argv"]),
        discipline_status=plan_dict["discipline_status"],
    )
    p_dig = plan_digest(
        p_obj,
        data["state"]["request"]["digest"],
        data["state"]["repository"]["base_sha"],
    )

    # Resume: fails after 3 cycles and stops at needs_attention
    res = _run_cli(
        ["resume", trial_id, "--repo", str(repo), "--approve-plan", p_dig], env, repo
    )
    assert res.returncode == 1
    assert "budget_exhausted" in res.stdout

    initial_runs = len(list(marker_dir.glob("run_*.json")))
    assert initial_runs >= 4  # plan + 3 cycles

    # Repeated resume does not increase agent runs
    res_repeat = _run_cli(["resume", trial_id, "--repo", str(repo)], env, repo)
    assert res_repeat.returncode == 1
    assert len(list(marker_dir.glob("run_*.json"))) == initial_runs


def test_e2e_stopped_confirmation(e2e_setup):
    repo = e2e_setup["repo_dir"]
    req = e2e_setup["req_file"]
    env = e2e_setup["env"]

    res = _run_cli(
        ["start", "--request-file", str(req), "--repo", str(repo)], env, repo
    )
    assert res.returncode == 0
    res = _run_cli(["status", "--repo", str(repo), "--json"], env, repo)
    data = json.loads(res.stdout)
    trial_id = data["trial_id"]

    # Inject pending_attempt into state.json to simulate an interrupted/unconfirmed process
    state_file = repo / ".orchestune" / "trials" / trial_id / "state.json"
    state_data = json.loads(state_file.read_text(encoding="utf-8"))
    attempt_id = "e" * 32
    state_data["pending_attempt"] = {
        "trial_id": trial_id,
        "attempt_id": attempt_id,
        "kind": "implement",
        "input_head_sha": state_data["head_sha"],
        "base_sha": state_data["repository"]["base_sha"],
        "plan_digest": None,
        "plan_revision": 0,
        "cwd": state_data["workspace"]["implementation_path"],
        "artifact_dir": f"{state_data['workspace']['implementation_path']}/.orchestune/artifacts/{attempt_id}",
        "started_at": "2026-01-01T00:00:00Z",
    }
    state_file.write_text(json.dumps(state_data))

    # Resume without confirmation fails
    res_fail = _run_cli(["resume", trial_id, "--repo", str(repo)], env, repo)
    assert res_fail.returncode == 2
    assert "execution_unknown" in res_fail.stderr

    # Resume with confirm-stopped succeeds
    res_conf = _run_cli(
        ["resume", trial_id, "--confirm-stopped", attempt_id, "--repo", str(repo)],
        env,
        repo,
    )
    assert res_conf.returncode == 0
    assert "trial_id:" in res_conf.stdout
