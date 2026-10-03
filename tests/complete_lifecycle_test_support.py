"""Isolated publication environment with a durable real ledger and fake Forge."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from orchestune.claim.ownership import owner_token_digest
from orchestune.complete.contracts import CompleteRequest
from orchestune.complete.journal import completion_journal_lock
from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktreeCore,
    ClaimInfo,
    LaunchInfo,
)
from orchestune.ledger.run_state import ActiveWorktree, RunState, save_run_state
from orchestune.models import PrRecord
from orchestune.review.markers import review_selection_marker


class PublicationForge:
    def __init__(self):
        self.labels = {"status:in-progress", "status:queued", "status:blocked"}
        self.comments = []
        self.operations = []
        self.inject = lambda operation, after: None
        self.pr = PrRecord(
            42,
            "task-1110",
            ("code.py",),
            base_ref="parent/issue-1059",
            head_sha="a" * 40,
        )

    def list_all_issue_comments(self, issue):
        self.inject("search", False)
        if issue == 42:
            return [{"body": review_selection_marker("skip", self.pr.head_sha)}]
        return list(self.comments)

    def get_issue_labels(self, issue):
        self.inject("get", False)
        self.operations.append("get")
        result = tuple(self.labels)
        self.inject("get", True)
        return result

    def get_issue_state(self, issue):
        return "OPEN"

    def get_pull_request(self, issue):
        return self.pr

    def create_issue_comment(self, issue, body):
        self.inject("post", False)
        comment = {
            "id": len(self.comments) + 1,
            "html_url": f"https://example.test/comments/{len(self.comments)+1}",
            "body": body,
        }
        self.comments.append(comment)
        self.operations.append("post")
        self.inject("post", True)
        return comment

    def add_label(self, issue, label):
        self.inject("add", False)
        self.labels.add(label)
        self.operations.append("add:" + label)
        self.inject("add", True)

    def remove_label(self, issue, label):
        self.inject("remove:" + label, False)
        self.labels.discard(label)
        self.operations.append("remove:" + label)
        self.inject("remove:" + label, True)


def lifecycle_environment(tmp_path: Path, monkeypatch, result="not-needed"):
    state_path = tmp_path / "run_state.json"
    active = ActiveWorktree.from_records(
        core=ActiveWorktreeCore(
            issue_number=1110,
            branch="task-1110",
            worktree_path=str(tmp_path),
            declared_footprint=(),
        ),
        launch=LaunchInfo(
            pid=None,
            started_at=1.0,
        ),
        claim=ClaimInfo(
            owner_kind="interactive",
            claim_id="claim-1110",
            claim_stage="reserved",
            base_ref="parent/issue-1059",
            base_sha="b" * 40,
            repository_id="repo",
            claimed_at=1.0,
            owner_token_digest=owner_token_digest("token"),
        ),
        completion=ActiveCompletionJournal(
            completion_policy_config={"max_tokens_per_task": None, "source": "test"},
        ),
    )
    with completion_journal_lock(state_path):
        save_run_state(RunState(active_worktrees={"1110": active}), state_path)
    workspace = SimpleNamespace(run_state_path=state_path, repository_identity="repo")
    monkeypatch.setattr(
        "orchestune.complete.service.resolve_claim_workspace", lambda **_: workspace
    )
    check = Mock()
    monkeypatch.setattr("orchestune.complete.service._check", check)
    monkeypatch.setattr(
        "orchestune.complete.service.validate_local_claim", lambda *a, **kw: None
    )
    monkeypatch.setattr("orchestune.complete.service._head_sha", lambda _: "a" * 40)
    ci = SimpleNamespace(to_dict=lambda: {"head_sha": "a" * 40, "definition": "fixed"})
    monkeypatch.setattr(
        "orchestune.complete.service.run_local_ci_if_needed", lambda _: ci
    )
    monkeypatch.setattr(
        "orchestune.complete.service.validate_ci_evidence", lambda _: ci
    )
    common: dict[str, Any] = dict(
        owner_token="token",
        claim_id="claim-1110",
        state_path=state_path,
        worktree_root=tmp_path,
    )
    if result == "done":
        request = CompleteRequest.done(1110, 42, reviewer="skip", **common)
    elif result == "blocked":
        request = CompleteRequest.blocked(1110, "blocked", **common)
    else:
        request = CompleteRequest.not_needed(1110, **common)
    return request, PublicationForge(), check
