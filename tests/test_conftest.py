from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestune.dispatch_config import DispatcherConfig
from orchestune.forge import (
    BootstrapResult,
    Forge,
    IssueForge,
    LabelSpec,
    PullRequestForge,
    RepoAdminForge,
)
from tests.conftest import FakeForge


def test_guard_events_log_path_fails_on_default_init():
    """`DispatcherConfig` initialized with default `Path('events.jsonl')` should fail immediately in tests."""
    with pytest.raises(pytest.fail.Exception) as exc_info:
        DispatcherConfig()

    assert (
        "DispatcherConfig initialized with default events_log_path ('events.jsonl')"
        in str(exc_info.value)
    )


def test_guard_events_log_path_succeeds_with_explicit_tmp_path(tmp_path: Path):
    """`DispatcherConfig` initialized with explicit isolated `events_log_path` should succeed."""
    config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl")
    assert config.events_log_path == tmp_path / "events.jsonl"


def test_guard_dispatch_cycle_ensure_parent_branch_fails_when_unmocked():
    """`ensure_parent_branch` inside dispatch_cycle should fail in unit tests when unmocked."""
    import orchestune.dispatch_phase_rebase

    with pytest.raises(pytest.fail.Exception) as exc_info:
        orchestune.dispatch_phase_rebase.ensure_parent_branch(181)

    assert "called unmocked `ensure_parent_branch(181)`" in str(exc_info.value)


class TestFakeForgeFixture:
    def test_fake_forge_is_magic_mock_with_forge_spec(self, fake_forge: MagicMock):
        assert isinstance(fake_forge, MagicMock)
        # Verify RepoAdminForge methods
        assert fake_forge.check_auth() is None
        res = fake_forge.ensure_labels((LabelSpec("status:queued", "000000", "desc"),))
        assert isinstance(res, BootstrapResult)

        # Verify IssueForge default returns
        assert fake_forge.list_issues_by_label("status:queued") == []
        assert fake_forge.list_sub_issues(100) == []
        assert fake_forge.get_issue_labels(1) == ()
        assert fake_forge.get_issue(1) is None
        assert fake_forge.get_issue_state(1) == "OPEN"
        assert fake_forge.get_issue_last_reopened_at(1) is None
        assert fake_forge.get_label_actor(1, "status:queued") == ""
        assert fake_forge.get_actor_permission("user") == "none"
        assert fake_forge.list_comments(1) == []
        assert fake_forge.find_open_issues_by_exact_title("Title") == []
        assert fake_forge.find_issues_by_parent_metadata(100) == []

        # Verify PullRequestForge default returns
        assert fake_forge.list_open_prs() == []
        assert fake_forge.list_prs() == []
        assert fake_forge.branch_exists("main") is True
        assert fake_forge.is_branch_merged_into("feature", "main") is False
        assert fake_forge.is_current_branch_tip_merged_into("feature", "main") is False
        assert fake_forge.get_merged_pr_timestamp("feature", "main") is None


class TestFakeForgeClass:
    def test_implements_forge_protocol(self):
        forge = FakeForge()
        assert isinstance(forge, Forge)
        assert isinstance(forge, IssueForge)
        assert isinstance(forge, PullRequestForge)
        assert isinstance(forge, RepoAdminForge)

    def test_issue_lifecycle(self):
        forge = FakeForge()
        issue_num = forge.create_issue(
            title="Feature task",
            body="Task body",
            labels=["status:queued", "priority:high"],
        )
        assert issue_num == 1
        issue = forge.get_issue(1)
        assert issue is not None
        assert issue.number == 1
        assert issue.title == "Feature task"
        assert issue.body == "Task body"
        assert set(issue.labels) == {"status:queued", "priority:high"}
        assert forge.get_issue_state(1) == "OPEN"

        # Update body and title
        forge.update_issue_body(1, "New body")
        forge.update_issue_title(1, "New title")
        issue = forge.get_issue(1)
        assert issue is not None
        assert issue.body == "New body"
        assert issue.title == "New title"

        # Close issue
        forge.close_issue(1, reason="completed", comment="All done")
        assert forge.get_issue_state(1) == "CLOSED"
        comments = forge.list_comments(1)
        assert len(comments) == 1
        assert comments[0]["body"] == "All done"

    def test_label_operations(self):
        forge = FakeForge()
        forge.create_issue(title="Task", body="Body", labels=["status:queued"])
        forge.set_label_actor(1, "status:queued", "alice")

        assert forge.get_issue_labels(1) == ("status:queued",)
        assert forge.get_label_actor(1, "status:queued") == "alice"

        forge.add_label(1, "status:in-progress", actor="bob")
        assert "status:in-progress" in forge.get_issue_labels(1)
        assert forge.get_label_actor(1, "status:in-progress") == "bob"

        forge.remove_label(1, "status:queued")
        assert "status:queued" not in forge.get_issue_labels(1)

        # list_issues_by_label
        open_tasks = forge.list_issues_by_label("status:in-progress")
        assert len(open_tasks) == 1
        assert open_tasks[0].number == 1

    def test_sub_issues_and_relationships(self):
        forge = FakeForge()
        parent_num = forge.create_issue(title="Epic", body="Parent body")
        child_num = forge.create_issue(title="Child", body="Child body")

        forge.add_sub_issue(parent_num, child_num)
        sub_issues = forge.list_sub_issues(parent_num)
        assert len(sub_issues) == 1
        assert sub_issues[0].number == child_num

        forge.set_blocked_by(child_num, 999)
        child = forge.get_issue(child_num)
        assert child is not None
        assert child.blocked_by == (999,)

        # Metadata-only child discovery
        metadata_child = forge.create_issue(
            title="Metadata Child",
            body="```yaml\nfootprint:\n  parent_issue_number: 1\n```",
        )
        found = forge.find_issues_by_parent_metadata(1)
        found_numbers = {i.number for i in found}
        assert child_num in found_numbers
        assert metadata_child in found_numbers

    def test_pull_request_operations(self):
        forge = FakeForge()
        pr_num = forge.create_pull_request(
            head="feat/foo",
            base="parent/issue-100",
            title="Add foo",
            body="PR body",
        )
        assert pr_num == 1
        open_prs = forge.list_open_prs()
        assert len(open_prs) == 1
        assert open_prs[0].number == 1
        assert open_prs[0].head_ref == "feat/foo"
        assert open_prs[0].base_ref == "parent/issue-100"
        assert open_prs[0].is_cross_repository is False

        forge.update_pull_request(1, title="New PR title", body="New PR body")
        pr = forge.get_pr(1)
        assert pr is not None
        assert pr.head_ref == "feat/foo"

        assert forge.branch_exists("feat/foo") is True
        forge.merge_pull_request(1)
        assert forge.is_branch_merged_into("feat/foo", "parent/issue-100") is True
        assert forge.get_merged_pr_timestamp("feat/foo", "parent/issue-100") is not None
        assert len(forge.list_open_prs()) == 0

        all_prs = forge.list_prs(state="all")
        assert len(all_prs) == 1
        assert all_prs[0].state == "MERGED"
        assert all_prs[0].closed_at != ""

        forge.delete_branch("feat/foo")
        assert forge.branch_exists("feat/foo") is False

    def test_seed_pr_state_inference(self):
        forge = FakeForge()
        from orchestune.models import PrRecord

        merged_pr = PrRecord(
            number=42,
            head_ref="feat/bar",
            base_ref="parent/issue-10",
            changed_files=(),
            state="MERGED",
            closed_at="2026-08-23T12:00:00Z",
        )
        forge.seed_pr(merged_pr)
        assert forge.list_open_prs() == []
        all_prs = forge.list_prs(state="all")
        assert len(all_prs) == 1
        assert all_prs[0].number == 42
        assert forge.is_branch_merged_into("feat/bar", "parent/issue-10") is True
        assert (
            forge.get_merged_pr_timestamp("feat/bar", "parent/issue-10")
            == "2026-08-23T12:00:00Z"
        )

    def test_actor_permissions_and_reopen(self):
        forge = FakeForge()
        forge.set_actor_permission("charlie", "admin")
        assert forge.get_actor_permission("charlie") == "admin"
        assert forge.get_actor_permission("unknown") == "none"

        forge.create_issue(title="Task", body="Body")
        forge.set_issue_last_reopened_at(1, "2026-08-23T10:00:00Z")
        assert forge.get_issue_last_reopened_at(1) == "2026-08-23T10:00:00Z"
