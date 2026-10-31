"""Read-only fixtures and explicit-session contract tests."""

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestune.ledger.active_records import (
    ActiveWorktree,
    ActiveWorktreeCore,
    ClaimInfo,
)
from orchestune.ledger.run_state import RunState
from orchestune.models import IssueRecord
from orchestune.workflow.contracts import WorkflowContext
from orchestune.workflow.readers import read_session


class ReadForge:
    """Only observations; an accidental lifecycle mutation fails immediately."""

    def __init__(self, context):
        self.context = context
        self.comments = [
            {
                "id": 1,
                "body": "\n".join(
                    (
                        "<!-- orchestune:review-trigger bot=claude -->",
                        "<!-- orchestune:review-round 1 -->",
                        f"<!-- orchestune:review-head {context.head_sha} -->",
                    )
                ),
                "created_at": "2026-10-31T00:00:00Z",
                "user": {"login": "worker"},
            },
            {
                "id": 2,
                "body": "No required findings.",
                "created_at": "2026-10-31T00:01:00Z",
                "user": {"login": "claude[bot]"},
            },
        ]

    def get_repository_slug(self):
        return self.context.repository

    def get_issue(self, number):
        return self.context.issue if int(number) == 1496 else None

    def get_pull_request(self, number):
        assert int(number) == 42
        return self.context.pr

    def list_all_issue_comments(self, number):
        return self.comments

    def list_pull_request_reviews(self, number):
        return []

    def list_pull_request_review_comments(self, number):
        return []

    def __getattr__(self, name):
        raise AssertionError(f"Forbidden Forge operation: {name}")


def prepared_context(tmp_path: Path) -> WorkflowContext:
    session = tmp_path / "session"
    session.mkdir()
    table = "| Reference | Decision | Rationale |\n|---|---|---|\n| x.py | out of scope | Uses unchanged API |\n"
    (session / "impact.md").write_text(table)
    state = {
        "schema_version": 1,
        "repository": "owner/repo",
        "issue_number": 1496,
        "claim_id": "claim-current",
        "mode": "auto",
        "change_kind": "code",
        "backend": "gh",
        "reviewer": "claude",
        "selection_basis": "prompt",
        "plan_approval": {
            "required": False,
            "status": "not_required",
            "evidence": "existing issue",
        },
        "impact_initial": {
            "relative_path": "impact.md",
            "digest": hashlib.sha256(table.encode()).hexdigest(),
        },
    }
    (session / "workflow-state.json").write_text(json.dumps(state))
    (session / "implementation-plan.md").write_text(
        "Plan\n```orchestune-workflow\nschema_version: 1\nstate: workflow-state.json\n```\n"
    )
    active = ActiveWorktree(
        core=ActiveWorktreeCore(1496, "task", str(tmp_path), ("x.py",), "main"),
        claim=ClaimInfo(
            claim_id="claim-current",
            claim_stage="completed",
            repository_id="local-repo",
            base_ref="main",
            claimed_at=1.0,
            owner_token_digest="a" * 64,
        ),
    )
    issue = IssueRecord(
        number=1496,
        title="Task",
        labels=(),
        created_at="",
        body="## Footprint\n```yaml\nsubtask_id: task\nfootprint: [x.py]\ndepends_on: []\nclaim_id: claim-current\n```\n",
    )
    return WorkflowContext(
        issue_number=1496,
        session_dir=session,
        root=tmp_path,
        repository="owner/repo",
        repository_identity="local-repo",
        state=state,
        issue=issue,
        active=active,
        ledger=RunState(active_worktrees={"1496": active}),
        head_sha="a" * 40,
        branch="task",
        worktree_status="clean",
        dependencies_ready=True,
    )


def test_session_reads_explicit_marker(tmp_path):
    context = prepared_context(tmp_path)
    state, artifacts = read_session(context.session_dir)
    assert state == context.state
    assert artifacts["state"]["path"] == "workflow-state.json"


@pytest.mark.parametrize(
    "marker",
    [
        "",
        "```orchestune-workflow\nschema_version: 1\nstate: ../other.json\n```",
        "```orchestune-workflow\nschema_version: 1\nstate: /other.json\n```",
        "```orchestune-workflow\nschema_version: 1\nstate: workflow-state.json\nextra: true\n```",
        "```orchestune-workflow\nschema_version: 2\nstate: workflow-state.json\n```",
    ],
)
def test_bad_marker_is_invalid_evidence(tmp_path, marker):
    context = prepared_context(tmp_path)
    (context.session_dir / "implementation-plan.md").write_text(marker)
    with pytest.raises(ValueError):
        read_session(context.session_dir)


def test_duplicate_markers_are_rejected(tmp_path):
    context = prepared_context(tmp_path)
    plan = context.session_dir / "implementation-plan.md"
    plan.write_text(plan.read_text() * 2)
    with pytest.raises(ValueError):
        read_session(context.session_dir)


def ready_context(tmp_path):
    from orchestune.complete.ci_evidence import record_ci_evidence
    from orchestune.infra.git_cli import run_git
    from orchestune.models import PrRecord

    c = prepared_context(tmp_path)
    for args in (
        ["init", "-b", "task"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.com"],
    ):
        run_git(args, cwd=tmp_path)
    (tmp_path / ".gitignore").write_text("session/\n.orchestune/\nrun_state.json\n")
    (tmp_path / "x.py").write_text("x = 1\n")
    run_git(["add", "."], cwd=tmp_path)
    run_git(["commit", "-m", "fixture"], cwd=tmp_path)
    c.head_sha = run_git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
    c.repository_identity = (tmp_path / ".git").as_posix()
    c.active.claim = replace(c.active.claim, repository_id=c.repository_identity)
    c.pr_body = "\n".join(
        f"## {heading}\nVerified.\n"
        for heading in (
            "Acceptance Criteria & Scope Guard",
            "Walkthrough",
            "Verification Evidence",
            "Reproducer",
            "Baseline",
        )
    )
    c.pr_number = 42
    c.pr = PrRecord(
        42,
        "task",
        ("x.py",),
        base_ref="main",
        head_sha=c.head_sha,
        body=c.pr_body,
        is_cross_repository=False,
    )
    c.review_reply = c.session_dir / "reply.md"
    c.review_reply.write_text(
        "<!-- orchestune:review-reply -->\n```orchestune-review-judgments\nround: 1\nfindings:\n  - {source: 'issue_comment:2', location: summary, judgment: already_addressed, status: resolved, basis: 'No findings reported', evidence: 'Read the current summary'}\n```\n"
    )
    c.forge = ReadForge(c)
    c.forge.comments.append(
        {
            "id": 3,
            "body": c.review_reply.read_text(),
            "created_at": "2026-10-31T00:02:00Z",
            "user": {"login": "worker"},
        }
    )
    record_ci_evidence(tmp_path, base_sha=c.head_sha)
    return c


def workflow_snapshot(c):
    now = datetime(2026, 10, 31, 0, 3, tzinfo=UTC)
    moment = now.isoformat()
    review = {
        "snapshot_version": 1,
        "repository": c.repository,
        "pr_number": 42,
        "acquisition": {
            "started_at": moment,
            "observed_at": moment,
            "head_before": {"sha": c.head_sha, "fetched_at": moment},
            "head_after": {"sha": c.head_sha, "fetched_at": moment},
        },
        "issue_comments": c.forge.comments,
        "reviews": [],
        "inline_comments": [],
        "completeness": dict.fromkeys(
            ("issue_comments", "reviews", "inline_comments"), "complete"
        ),
    }
    value = {
        "snapshot_version": 1,
        "repository": c.repository,
        "observed_at": moment,
        "issue": asdict(c.issue),
        "claim_id": c.state["claim_id"],
        "pr": asdict(c.pr),
        "dependency_issues": [],
        "dependencies_complete": True,
        "review": review,
    }
    return json.loads(json.dumps(value)), now


def test_reader_uses_get_list_only_and_preserves_all_files(tmp_path, monkeypatch):
    from orchestune.ledger.active_codec import encode_active_worktree
    from orchestune.workflow.check import check_gate
    from orchestune.workflow.readers import read_context

    c = ready_context(tmp_path)
    ledger = tmp_path / "run_state.json"
    ledger.write_text(
        json.dumps({"active_worktrees": {"1496": encode_active_worktree(c.active)}})
    )
    before = {
        p.relative_to(tmp_path): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    got = read_context(
        issue_number=1496,
        session_dir=c.session_dir,
        root=tmp_path,
        pr_number=42,
        review_reply=c.review_reply,
        forge=c.forge,
    )
    assert got.errors == {}
    for gate in ("pre-claim", "pre-edit", "pre-pr", "pre-complete"):
        assert check_gate(got, gate).status == "ready"
    after = {
        p.relative_to(tmp_path): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert after == before
    import os

    assert os.environ["GIT_OPTIONAL_LOCKS"] == "1"


def test_mcp_snapshot_matches_live_read_checks(tmp_path):
    from orchestune.workflow.check import check_gate
    from orchestune.workflow.readers import SnapshotForge

    c = ready_context(tmp_path)
    value, now = workflow_snapshot(c)
    c.state["backend"] = "mcp"
    c.forge = SnapshotForge(value, issue=1496, pr=42, now=now)
    assert check_gate(c, "pre-complete").status == "ready"


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "partial",
        "head",
        "repository",
        "claim",
        "issue",
        "version",
        "missing_period",
    ],
)
def test_snapshot_invalidity_is_unmet_without_network(tmp_path, fault):
    from orchestune.workflow.check import check_gate
    from orchestune.workflow.readers import read_context

    c = ready_context(tmp_path)
    value, now = workflow_snapshot(c)
    if fault == "stale":
        value["observed_at"] = "2020-01-01T00:00:00Z"
    elif fault == "partial":
        value["review"]["completeness"]["reviews"] = "partial"
    elif fault == "head":
        value["review"]["acquisition"]["head_after"]["sha"] = "b" * 40
    elif fault == "repository":
        value["review"]["repository"] = "elsewhere/repo"
    elif fault == "claim":
        value["claim_id"] = "old-claim"
    elif fault == "issue":
        value["issue"]["number"] = 1
    elif fault == "version":
        value["snapshot_version"] = True
    else:
        value["review"].pop("acquisition")
    snapshot = c.session_dir / "snapshot.json"
    snapshot.write_text(json.dumps(value))
    c.state["backend"] = "mcp"
    (c.session_dir / "workflow-state.json").write_text(json.dumps(c.state))
    got = read_context(
        issue_number=1496,
        session_dir=c.session_dir,
        root=tmp_path,
        pr_number=42,
        review_reply=c.review_reply,
        snapshot=snapshot,
        now=now,
    )
    assert "forge" in got.errors
    assert check_gate(got, "pre-complete").status == "unmet"


def test_unreadable_local_and_session_evidence_is_reported(tmp_path, monkeypatch):
    from orchestune.workflow.readers import read_context

    c = prepared_context(tmp_path)
    (c.session_dir / "workflow-state.json").write_text("[")

    def unavailable_workspace(*args, **kwargs):
        raise ValueError("Git workspace unavailable")

    monkeypatch.setattr(
        "orchestune.workflow.readers.resolve_claim_workspace", unavailable_workspace
    )
    got = read_context(
        issue_number=1496, session_dir=c.session_dir, root=tmp_path, forge=ReadForge(c)
    )
    assert "context" in got.errors
    assert "local" in got.errors


def test_dependency_resolution_does_not_treat_unknown_as_empty(tmp_path):
    from orchestune.workflow.readers import read_dependencies

    c = prepared_context(tmp_path)
    issue = replace(
        c.issue, body=c.issue.body.replace("depends_on: []", "depends_on: [missing]")
    )
    c.forge = ReadForge(c)
    c.forge.list_sub_issues = lambda _: []
    c.forge.find_issues_by_parent_metadata = lambda _: []
    assert read_dependencies(issue, c.forge) is False
