"""Stale-read workflow regressions against real history and atomic remote refs."""

import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from orchestune.integrator import attempt, semantic_claim
from orchestune.integrator.attempt import publish_parent_under_claim
from orchestune.integrator.read_gate import (
    prepare_read_gate,
    validate_read_finalization,
)
from orchestune.integrator.read_publish import prepare_parent_read_publication
from orchestune.integrator.read_reports import restore_read_report, save_read_report
from orchestune.integrator.semantic_claim import PushResult, PushStatus
from orchestune.read_dependencies.contracts import ReadDirection, StaleVerdict
from orchestune.read_dependencies.storage import read_snapshot
from orchestune.review.semantic_gate import SemanticReviewPhase
from tests.stale_read_e2e_support import (
    PARENT,
    PARENT_REF,
    SNAPSHOT_REF,
    StaleReadScenario,
    git,
)

pytestmark = pytest.mark.e2e


@pytest.fixture
def scenario(tmp_path, in_memory_forge, monkeypatch):
    return StaleReadScenario(tmp_path, in_memory_forge, monkeypatch)


def reverse_attempt(s):
    reader = s.commit("adapter.py", "uses v1\n")
    s.record(1101, s.base, reader, ("registry.py",), integrated=True)
    git(s.root, "push", "origin", f"HEAD:{PARENT_REF}")
    head = s.commit("registry.py", "v2\n")
    writer = s.record(1102, reader, head)
    assert s.attempt(reader, writer) == "semantic_review_pending"
    return reader, head


@pytest.mark.parametrize("order", ["writer_first", "reader_first", "same_attempt"])
def test_both_orders_and_same_attempt_block_until_exact_validation(scenario, order):
    s = scenario
    if order == "reader_first":
        parent, head = reverse_attempt(s)
        direction = ReadDirection.REVERSE
    else:
        reader_head = s.commit("adapter.py", "uses v1\n")
        git(s.root, "checkout", "--detach", s.base)
        writer_head = s.commit("registry.py", "v2\n")
        writer = s.record(1102, s.base, writer_head, integrated=order == "writer_first")
        if order == "writer_first":
            git(s.root, "push", "origin", f"HEAD:{PARENT_REF}")
            git(s.root, "checkout", "--detach", reader_head)
            git(s.root, "rebase", writer_head)
            head = git(s.root, "rev-parse", "HEAD")
            reader = s.record(1101, s.base, head, ("registry.py",))
            parent, children, direction = writer_head, (reader,), ReadDirection.FORWARD
        else:
            reader = s.record(1101, s.base, reader_head, ("registry.py",))
            git(s.root, "merge", "--no-edit", reader_head)
            head = git(s.root, "rev-parse", "HEAD")
            parent, children, direction = (
                s.base,
                (writer, reader),
                ReadDirection.SAME_ATTEMPT,
            )
        assert s.attempt(parent, *children) == "semantic_review_pending"
    before = s.forge.list_all_issue_comments(1101)
    report = s.report()
    assert report.verdict is StaleVerdict.CANDIDATE_STALE
    assert direction in {c.identity.direction for c in report.candidates}
    assert s.ctx.semantic_review_state.phase is SemanticReviewPhase.NOT_STARTED
    assert not publish_parent_under_claim(s.ctx).pushed
    assert s.refs() == (parent, "")
    approved = s.approve()
    assert approved.digest == report.digest
    result = publish_parent_under_claim(s.ctx)
    assert result.pushed, result.detail
    assert s.refs()[0] == head
    snapshot = read_snapshot("owner/repo", PARENT, head, cwd=s.root)
    assert {r.issue_number for r in snapshot.readers} == {1101, 1102}
    assert all(
        any(
            v.candidate_head_sha == head and v.report_digest == report.digest
            for v in r.validations
        )
        for r in snapshot.readers
    )
    assert s.forge.list_all_issue_comments(1101) == before
    assert s.outcome(1101).read_evidence.input_base_sha == s.base
    if order == "reader_first":
        assert s.forge.get_issue_state(1101) == "CLOSED"
        assert set(s.forge.get_issue_labels(1101)) == {
            "status:done",
            "integration:included",
        }


@pytest.mark.parametrize("damage", ["digest", "head", "ci", "candidates", "unknown"])
def test_required_rejects_old_or_incomplete_pass_without_moving_refs(scenario, damage):
    s = scenario
    parent, _ = reverse_attempt(s)
    changes = {"decisions": []} if damage == "candidates" else {}
    if damage == "unknown":
        report = s.report()
        changes["decisions"] = [
            {
                "candidate_id": c.candidate_id,
                "conclusion": "unknown",
                "rationale": "not verified",
            }
            for c in report.candidates
        ]
    s.approve(**changes)
    state = s.ctx.semantic_review_state
    if damage in {"digest", "ci"}:
        field = (
            "reviewed_read_report_digest" if damage == "digest" else "ci_verified_sha"
        )
        s.ctx.semantic_review_state = replace(
            state, **{field: "0" * (64 if damage == "digest" else 40)}
        )
    elif damage == "head":
        s.commit("adapter.py", "fixed after review\n")
    assert validate_read_finalization(s.ctx) is not None
    assert not publish_parent_under_claim(s.ctx).pushed
    assert s.refs() == (parent, "")


@pytest.mark.parametrize("mode", ["advisory", "off"])
def test_legacy_modes_preserve_publication_behavior(scenario, mode):
    s = scenario
    _, head = reverse_attempt(s)
    s.ctx.config.stale_read_gate = mode
    decision = prepare_read_gate(s.ctx, head)
    assert decision.mode.value == mode
    assert decision.allow_fast_path and decision.allow_parent_update
    assert validate_read_finalization(s.ctx) is None
    assert publish_parent_under_claim(s.ctx).pushed
    assert s.refs() == (head, "")


def test_lost_push_response_and_restart_verify_both_refs(scenario, monkeypatch):
    s = scenario
    _, head = reverse_attempt(s)
    s.approve()
    real = semantic_claim.push_refs_under_claim
    calls = []

    def lose_response(*args, **kwargs):
        calls.append(1)
        assert real(*args, **kwargs).status is PushStatus.PUSHED
        return PushResult(PushStatus.UNKNOWN, None, "response lost")

    monkeypatch.setattr(attempt, "push_refs_under_claim", lose_response)
    assert publish_parent_under_claim(s.ctx).pushed
    pair = s.refs()
    assert pair[0] == head and pair[1]
    assert publish_parent_under_claim(s.ctx).pushed
    assert len(calls) == 1
    assert s.refs() == pair


@pytest.mark.parametrize("ref", [PARENT_REF, SNAPSHOT_REF])
def test_real_cas_race_preserves_the_other_ref(scenario, monkeypatch, ref):
    s = scenario
    parent, _ = reverse_attempt(s)
    s.approve()
    other = s.commit("unrelated.txt", "race\n")
    git(s.root, "checkout", "--detach", s.ctx.semantic_review_state.head_sha)
    real = semantic_claim.push_refs_under_claim

    def race(*args, **kwargs):
        git(s.root, "push", "origin", f"{other}:{ref}")
        return real(*args, **kwargs)

    monkeypatch.setattr(attempt, "push_refs_under_claim", race)
    assert not publish_parent_under_claim(s.ctx).pushed
    assert s.refs() == ((other, "") if ref == PARENT_REF else (parent, other))


def test_closed_reader_survives_branch_deletion_gc_and_fresh_clone(scenario):
    s = scenario
    reader_head = s.commit("adapter.py", "uses v1\n")
    reader = s.record(1101, s.base, reader_head, ("registry.py",))
    git(s.root, "push", "origin", "HEAD:refs/heads/child-reader")
    assert s.attempt(s.base, reader) == "success"
    assert publish_parent_under_claim(s.ctx).pushed
    claim = semantic_claim.read_semantic_claim(s.root, PARENT)
    assert (
        semantic_claim.release_semantic_claim(
            s.root, claim, now=datetime.now(UTC)
        ).status
        is PushStatus.PUSHED
    )
    s.include(1101, reader_head)
    before = s.forge.list_all_issue_comments(1101)
    git(s.root, "push", "origin", ":refs/heads/child-reader")
    git(s.remote, "reflog", "expire", "--expire=now", "--all")
    git(s.remote, "gc", "--prune=now")
    fresh = s.root.parent / "fresh"
    git(s.root.parent, "clone", str(s.remote), str(fresh))
    git(fresh, "checkout", "--detach", reader_head)
    s.root = s.ctx.repository_root = fresh
    head = s.commit("registry.py", "v2\n")
    writer = s.record(1102, reader_head, head)
    assert s.attempt(reader_head, writer) == "semantic_review_pending"
    assert ReadDirection.REVERSE in {
        c.identity.direction for c in s.report().candidates
    }
    assert not publish_parent_under_claim(s.ctx).pushed
    s.approve()
    result = publish_parent_under_claim(s.ctx)
    assert result.pushed, result.detail
    assert s.forge.list_all_issue_comments(1101) == before
    assert s.forge.get_issue_state(1101) == "CLOSED"


@pytest.mark.parametrize("failure", ["checkpoint", "outcome", "receipt", "fetch"])
def test_missing_bootstrap_and_acquisition_fail_closed(scenario, monkeypatch, failure):
    s = scenario
    parent, _ = reverse_attempt(s)
    if failure == "fetch":

        def unavailable(*args, **kwargs):
            raise OSError("history unavailable")

        monkeypatch.setattr(s.forge, "find_issues_by_parent_metadata", unavailable)
    else:
        marker = {
            "checkpoint": "orchestune-read-checkpoint",
            "outcome": "orchestune:outcome",
            "receipt": "orchestune:task-integration-proof",
        }[failure]
        s.forge.event_comments[:] = [
            c
            for c in s.forge.event_comments
            if not (c["issue_number"] == 1101 and marker in c["body"])
        ]
    assert not prepare_read_gate(
        s.ctx, s.ctx.semantic_review_state.head_sha
    ).allow_parent_update
    assert s.ctx.read_report is None
    assert not publish_parent_under_claim(s.ctx).pushed
    assert s.refs() == (parent, "")


def test_report_response_loss_is_recovered_but_missing_chunk_is_unknown(
    scenario, monkeypatch
):
    s = scenario
    reverse_attempt(s)
    report = s.report()
    s.forge.event_comments[:] = [
        c for c in s.forge.event_comments if c["issue_number"] != 500
    ]
    real = s.forge.create_issue_comment

    def lose_response(*args, **kwargs):
        real(*args, **kwargs)
        chunk = re.search(r"chunk=(\d+)/(\d+)", args[1])
        if chunk and chunk[1] == chunk[2]:
            raise OSError("post response lost")

    monkeypatch.setattr(s.forge, "create_issue_comment", lose_response)
    saved = save_read_report(
        s.forge,
        500,
        report,
        target_head_sha=report.candidate_head_sha,
        max_comment_bytes=700,
    )
    assert saved.report == report
    chunks = [c for c in s.forge.event_comments if c["issue_number"] == 500]
    assert len(chunks) > 1
    s.forge.event_comments.remove(chunks[0])
    restored = restore_read_report(
        s.forge,
        500,
        report_digest=report.digest,
        target_head_sha=report.candidate_head_sha,
        expected_author=s.forge.get_authenticated_user(),
    )
    assert restored.report is None
    assert "missing" in restored.reason


def test_remote_acquisition_failure_cannot_authorize_publish(scenario, monkeypatch):
    s = scenario
    parent, _ = reverse_attempt(s)
    s.approve()
    from orchestune.integrator import read_publish

    def unavailable(*args, **kwargs):
        raise OSError("remote unavailable")

    monkeypatch.setattr(read_publish, "run_git", unavailable)
    with pytest.raises(OSError, match="remote unavailable"):
        prepare_parent_read_publication(s.ctx)
    assert not publish_parent_under_claim(s.ctx).pushed
    assert s.refs() == (parent, "")


@pytest.mark.parametrize("change", ["content", "mode"])
def test_windows_read_path_and_git_mode_changes_use_portable_index(scenario, change):
    s = scenario
    base = s.commit("src/registry.py", "v1\n")
    git(s.root, "push", "origin", f"HEAD:{PARENT_REF}")
    reader_head = s.commit("adapter.py", "uses registry\n")
    s.record(1101, base, reader_head, ("src\\registry.py",), integrated=True)
    git(s.root, "push", "origin", f"HEAD:{PARENT_REF}")
    if change == "mode":
        # Update the Git index directly: Windows need not support POSIX chmod.
        git(s.root, "update-index", "--chmod=+x", "src/registry.py")
        git(s.root, "commit", "-m", "make registry executable")
        head = git(s.root, "rev-parse", "HEAD")
    else:
        head = s.commit("src/registry.py", "v2\n")
    writer = s.record(1102, reader_head, head)
    assert s.attempt(reader_head, writer) == "semantic_review_pending"
    candidates = s.report().candidates
    assert {c.identity.observation.path for c in candidates} == {"src/registry.py"}
    if change == "mode":
        assert all(
            c.identity.change.old_blob_oid == c.identity.change.new_blob_oid
            for c in candidates
        )
        assert {c.identity.change.new_file_mode for c in candidates} == {"100755"}
    assert not publish_parent_under_claim(s.ctx).pushed
    s.approve()
    result = publish_parent_under_claim(s.ctx)
    assert result.pushed, result.detail
