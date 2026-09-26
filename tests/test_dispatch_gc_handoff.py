from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.dispatch.claim_marker import claim_marker_path, write_claim_marker
from orchestune.infra.git_cli import run_git
from orchestune.outcome_record import OutcomeRecord
from tests.test_dispatch_gc_handoff_integration import (
    FakeHandoffForge,
    _create_repo,
    _forge,
    _head_sha,
    _make_active,
)


def _inspect(active, repo: Path, forge, *, cwd: Path | None = None):
    from orchestune.dispatch.gc.handoff import inspect_handoff

    previous = Path.cwd()
    os.chdir(repo)
    try:
        return inspect_handoff(
            active,
            resolve_claim_workspace(repo),
            forge,
            cwd=cwd or repo,
        )
    finally:
        os.chdir(previous)


def test_inspect_releases_clean_done_with_exact_merged_pr(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "release"
    assert plan.reason == "already_merged"
    assert plan.worktree_action == "remove"
    assert plan.removal_request is not None
    assert forge.pr_calls == [125]
    assert forge.reachability_calls == [(_head_sha(active), "main")]


def test_inspect_strips_configured_remote_prefix_from_expected_base(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    run_git(
        ["remote", "add", "custom-remote", "https://example.invalid/upstream.git"],
        cwd=repo,
    )
    active, comment = _make_active(repo, worktree, branch)
    active.base_ref = "custom-remote/release/next"
    forge = _forge(comment, branch, _head_sha(active))
    forge.pr = replace(forge.pr, base_ref="release/next")

    plan = _inspect(active, repo, forge)

    assert plan.action == "release"
    assert forge.reachability_calls == [(_head_sha(active), "release/next")]


@pytest.mark.parametrize("base_ref", ["claude/issue-123-task", "parent/issue-123"])
def test_inspect_preserves_slash_containing_local_base_branch(
    tmp_path: Path, base_ref: str
):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    active.base_ref = base_ref
    forge = _forge(comment, branch, _head_sha(active))
    forge.pr = replace(forge.pr, base_ref=base_ref)

    plan = _inspect(active, repo, forge)

    assert plan.action == "release"
    assert forge.reachability_calls == [(_head_sha(active), base_ref)]


def test_inspect_holds_when_journaled_comment_is_absent(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    comment["id"] = 100
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "outcome_absent"
    assert forge.pr_calls == []


def test_inspect_holds_when_comment_url_does_not_match_journal(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    comment["html_url"] = "https://github.com/another/comment"
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "outcome_mismatch"
    assert forge.pr_calls == []


def test_inspect_holds_when_comment_body_disagrees_with_journaled_payload(
    tmp_path: Path,
):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    active.completion_payload = {"outcome": comment["body"] + "tampered"}
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "outcome_mismatch"
    assert forge.pr_calls == []


def test_inspect_holds_when_outcome_does_not_match_claim_and_completion_ids(
    tmp_path: Path,
):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    comment["body"] = OutcomeRecord(
        result="done",
        issue=250,
        pr=125,
        claim_id="another-claim",
        head_sha=_head_sha(active),
        completion_id=active.completion_id,
    ).render()
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "outcome_mismatch"
    assert forge.pr_calls == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"number": 126}, "pr_mismatch"),
        ({"head_ref": "another/branch"}, "pr_mismatch"),
        ({"base_ref": "release"}, "pr_mismatch"),
        ({"is_cross_repository": True}, "pr_mismatch"),
        ({"merged_at": ""}, "merge_unverified"),
        ({"merge_commit_oid": ""}, "merge_unverified"),
    ],
)
def test_inspect_holds_for_pull_request_identity_or_merge_evidence_mismatch(
    tmp_path: Path, change: dict, reason: str
):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    forge = _forge(comment, branch, _head_sha(active))
    forge.pr = replace(forge.pr, **change)

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == reason
    if reason == "pr_mismatch":
        assert forge.reachability_calls == []


def test_inspect_holds_when_clean_worktree_head_changed_after_completion(
    tmp_path: Path,
):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    (worktree / "README.md").write_text("later commit\n", encoding="utf-8")
    from orchestune.infra.git_cli import run_git

    run_git(["add", "README.md"], cwd=worktree)
    run_git(["commit", "-m", "later commit"], cwd=worktree)
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "head_changed"


def test_inspect_holds_when_ownership_marker_is_missing_for_existing_worktree(
    tmp_path: Path,
):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    claim_marker_path(worktree).unlink()
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "owner_unknown"


@pytest.mark.parametrize("marker_content", ["{", "[]"])
def test_inspect_holds_when_ownership_marker_cannot_be_read(
    tmp_path: Path, marker_content: str
):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    claim_marker_path(worktree).write_text(marker_content, encoding="utf-8")
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "owner_unknown"


def test_inspect_holds_for_symlink_worktree_path(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    target = tmp_path / "different-worktree"
    target.mkdir()
    worktree.rename(tmp_path / "original-worktree")
    worktree.symlink_to(target, target_is_directory=True)
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "symlink_mismatch"


@pytest.mark.parametrize(
    ("ready_flag", "stage"),
    [
        (False, "handed_off_to_gc"),
        (True, "journaling"),
    ],
)
def test_inspect_keeps_existing_handoff_ready_or_contract(
    tmp_path: Path, ready_flag: bool, stage: str
):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    active.completion_handoff_ready = ready_flag
    active.completion_stage = stage
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "release"


def test_inspect_holds_when_comment_lookup_is_unknown_even_with_cached_done(
    tmp_path: Path,
):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)

    class UnknownForge(FakeHandoffForge):
        def list_all_issue_comments(self, issue_number):
            raise RuntimeError("network unavailable")

    forge = UnknownForge(comment, _forge(comment, branch, "a" * 40).pr)
    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "outcome_unknown"
    assert forge.pr_calls == []


def test_inspect_holds_before_forge_when_handoff_evidence_is_missing(
    tmp_path: Path,
):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    active.completion_comment_id = None
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "handoff_evidence_missing"
    assert forge.comment_calls == 0


def test_inspect_holds_before_forge_when_journaled_outcome_body_is_missing(
    tmp_path: Path,
):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    active.completion_payload = {"other": "payload"}
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "handoff_evidence_missing"
    assert forge.comment_calls == 0


def test_inspect_holds_on_repository_mismatch_before_forge_calls(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    active.repository_id = "/another/repository"
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "repository_mismatch"
    assert forge.comment_calls == 0


def test_inspect_holds_for_unmerged_pull_request(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    forge = _forge(comment, branch, _head_sha(active))
    forge.pr = forge.pr.__class__(
        number=125,
        head_ref=branch,
        changed_files=(),
        state="CLOSED",
        base_ref="main",
        is_cross_repository=False,
    )

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "pr_not_merged"
    assert forge.reachability_calls == []


def test_inspect_holds_when_pull_request_lookup_is_unknown(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)

    class UnknownPrForge(FakeHandoffForge):
        def get_pull_request(self, pr_number):
            raise RuntimeError("PR unavailable")

    forge = UnknownPrForge(comment, _forge(comment, branch, "a" * 40).pr)
    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "pr_unknown"


def test_inspect_holds_for_merge_commit_not_reachable_from_base(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    forge = _forge(comment, branch, _head_sha(active))
    forge.reachable = False

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "merge_unverified"
    assert plan.worktree_action == "retain"


def test_inspect_holds_when_merge_reachability_is_unknown(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)

    class UnknownReachabilityForge(FakeHandoffForge):
        def is_merge_commit_reachable_from(self, commit_oid, base):
            raise RuntimeError("compare unavailable")

    forge = UnknownReachabilityForge(
        comment, _forge(comment, branch, _head_sha(active)).pr
    )
    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "merge_unverified"


def test_inspect_holds_for_owner_mismatch(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    write_claim_marker(
        worktree,
        claim_id="different-claim",
        branch=branch,
        base_sha=None,
        branch_created=True,
    )
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "owner_mismatch"


def test_inspect_holds_for_dirty_done_worktree(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    (worktree / "README.md").write_text("uncommitted\n", encoding="utf-8")
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "dirty_worktree"
    assert plan.worktree_action == "retain"


def test_inspect_releases_dirty_not_needed_but_retains_worktree(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    head_sha = _head_sha(active)
    comment["body"] = OutcomeRecord(
        result="not-needed",
        issue=250,
        claim_id=active.claim_id,
        head_sha=head_sha,
        completion_id=active.completion_id,
    ).render()
    active.completion_result = "not-needed"
    active.completion_payload = {"outcome": comment["body"]}
    (worktree / "README.md").write_text("work in progress\n", encoding="utf-8")
    forge = _forge(comment, branch, head_sha)

    plan = _inspect(active, repo, forge)

    assert plan.action == "release"
    assert plan.reason == "outcome_verified"
    assert plan.worktree_action == "retain"
    assert plan.removal_request is None
    assert forge.pr_calls == []


def test_inspect_releases_absent_unregistered_worktree(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    from orchestune.infra.git_cli import run_git

    run_git(["worktree", "remove", str(worktree)], cwd=repo)
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "release"
    assert plan.worktree_action == "absent"
    assert plan.removal_request is None


def test_inspect_holds_when_worktree_directory_is_missing_but_registered(
    tmp_path: Path,
):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    shutil.rmtree(worktree)
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge)

    assert plan.action == "hold"
    assert plan.reason == "registered_missing_worktree"


def test_inspect_holds_if_run_from_worktree_that_would_be_removed(tmp_path: Path):
    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    forge = _forge(comment, branch, _head_sha(active))

    plan = _inspect(active, repo, forge, cwd=worktree)

    assert plan.action == "hold"
    assert plan.reason == "current_worktree"
