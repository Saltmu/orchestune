"""#1381: the next claim takes over a worktree that GC released but retained."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.claim.contracts import ClaimRequest, ClaimStage, OwnerKind
from orchestune.claim.service import claim_task
from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.dispatch.gc.handoff import GcRequest
from orchestune.dispatch.gc_service import run_handoff_gc
from orchestune.infra.git_cli import run_git
from orchestune.labels import StatusLabel
from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktreeCore,
    ClaimInfo,
    LaunchInfo,
)
from orchestune.ledger.run_state import ActiveWorktree
from orchestune.models import IssueRecord, PrRecord
from orchestune.outcome_record import OutcomeRecord
from orchestune.worktree_ops.claim_marker import read_claim_marker, write_claim_marker
from tests.claim_helpers import MockForge
from tests.test_dispatch_gc_handoff_integration import (
    FakeHandoffForge,
    _confirmed_contract_fields,
    _flat_dict,
)

ISSUE = 250
BRANCH = "claude/issue-250-task"
FOOTPRINT = ("README.md", "notes.txt")
OLD_CLAIM = "claim-old"


@dataclass(frozen=True)
class Retained:
    repo: Path
    worktree: Path
    state_path: Path
    base_sha: str


def _issue(footprint: tuple[str, ...] = FOOTPRINT) -> IssueRecord:
    paths = "".join(f"  - {path}\n" for path in footprint)
    return IssueRecord(
        number=ISSUE,
        title="[BUG] retained worktree",
        body=f"## Footprint\n\n```yaml\nsubtask_id: task\nfootprint:\n{paths}```\n",
        labels=(StatusLabel.QUEUED,),
        created_at="2026-10-01T00:00:00Z",
        state="OPEN",
    )


def _git(cwd: Path, *args: str) -> str:
    return run_git(list(args), cwd=cwd).stdout


def _create_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(
        repo, "remote", "add", "origin", "https://example.invalid/Saltmu/orchestune.git"
    )
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    worktree = repo / "worktrees" / BRANCH.replace("/", "-")
    worktree.parent.mkdir()
    _git(repo, "worktree", "add", "-b", BRANCH, str(worktree), "main")
    return repo, worktree


def _blocked_generation(repo: Path, worktree: Path) -> tuple[ActiveWorktree, dict]:
    base_sha = _git(worktree, "rev-parse", "HEAD").strip()
    outcome = OutcomeRecord(
        result="blocked",
        issue=ISSUE,
        reason="footprint-expansion-required",
        claim_id=OLD_CLAIM,
        head_sha=base_sha,
        completion_id="completion-old",
    )
    comment_url = f"https://github.com/Saltmu/orchestune/issues/{ISSUE}#issuecomment-1"
    comment = {
        "id": 1,
        "html_url": comment_url,
        "created_at": "2026-10-01T00:00:00Z",
        "body": outcome.render(),
    }
    active = ActiveWorktree.from_records(
        core=ActiveWorktreeCore(
            issue_number=ISSUE,
            branch=BRANCH,
            worktree_path=str(worktree),
            declared_footprint=FOOTPRINT,
            base_branch="main",
        ),
        launch=LaunchInfo(pid=None, started_at=1.0),
        claim=ClaimInfo(
            owner_kind="interactive",
            claim_id=OLD_CLAIM,
            claim_stage="completed",
            base_ref="origin/main",
            reservation_kind="footprint",
            claimed_at=1.0,
            base_sha=base_sha,
            owner_token_digest="d" * 64,
            repository_id=resolve_claim_workspace(repo).repository_identity,
        ),
        completion=ActiveCompletionJournal(
            completion_id="completion-old",
            completion_result="blocked",
            completion_stage="handed_off",
            completion_payload={"outcome": outcome.render()},
            completion_comment_id="1",
            completion_comment_url=comment_url,
            completion_handoff_ready=True,
        ),
    )
    write_claim_marker(
        worktree,
        claim_id=OLD_CLAIM,
        branch=BRANCH,
        base_sha=base_sha,
        branch_created=True,
    )
    return active, comment


def _handoff_forge(comment: dict) -> FakeHandoffForge:
    pr = PrRecord(
        number=125,
        head_ref=BRANCH,
        changed_files=(),
        state="OPEN",
        base_ref="main",
        is_cross_repository=False,
    )
    return FakeHandoffForge(comment, pr)


def _release_dirty_blocked(tmp_path: Path, monkeypatch) -> Retained:
    """GC releases a blocked generation and keeps its dirty worktree and marker."""
    repo, worktree = _create_repo(tmp_path)
    active, comment = _blocked_generation(repo, worktree)
    state = {
        "active_worktrees": {str(ISSUE): _flat_dict(active)},
        "launch_history": [],
        "completed_worktrees": [],
        **_confirmed_contract_fields(active),
    }
    state_path = repo / "run_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    (worktree / "README.md").write_text("staged work\n", encoding="utf-8")
    _git(worktree, "add", "README.md")
    (worktree / "README.md").write_text("staged and unstaged work\n", encoding="utf-8")
    (worktree / "notes.txt").write_text("untracked work\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    released = run_handoff_gc(
        GcRequest(), forge_factory=lambda: _handoff_forge(comment)
    )

    assert [(i.action, i.worktree_action) for i in released.items] == [
        ("released", "retain")
    ]
    assert _marker_claim(worktree) == OLD_CLAIM
    assert active.claim.base_sha is not None
    return Retained(repo, worktree, state_path, active.claim.base_sha)


def _marker_claim(worktree: Path) -> object:
    marker = read_claim_marker(worktree)
    assert marker is not None
    return marker.get("claim_id")


def _snapshot(worktree: Path) -> tuple[str, str, str, str]:
    return (
        _git(worktree, "rev-parse", "HEAD"),
        _git(worktree, "status", "--porcelain", "--untracked-files=all"),
        _git(worktree, "diff", "--cached"),
        _git(worktree, "diff"),
    )


def _claim(retained: Retained, issue: IssueRecord | None = None):
    request = ClaimRequest(
        issue_number=ISSUE,
        owner_kind=OwnerKind.DISPATCH,
        state_path=retained.state_path,
    )
    with patch("orchestune.claim.service._perform_git_fetch", return_value=None):
        return claim_task(
            request, forge=MockForge({ISSUE: issue or _issue()}), cwd=retained.repo
        )


def test_claim_after_gc_retain_release_takes_over_the_dirty_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retained = _release_dirty_blocked(tmp_path, monkeypatch)
    before = _snapshot(retained.worktree)

    outcome = _claim(retained)

    assert outcome.success, outcome.failure
    assert outcome.stage == ClaimStage.COMPLETED
    assert outcome.worktree_path == retained.worktree
    assert outcome.claim_id not in (None, OLD_CLAIM)
    assert _snapshot(retained.worktree) == before
    marker = read_claim_marker(retained.worktree)
    assert marker is not None
    assert (marker["claim_id"], marker["branch"], marker["base_sha"]) == (
        outcome.claim_id,
        BRANCH,
        retained.base_sha,
    )
    stored = json.loads(retained.state_path.read_text(encoding="utf-8"))
    active = stored["active_worktrees"][str(ISSUE)]
    assert (active["claim_id"], active["base_sha"]) == (
        outcome.claim_id,
        retained.base_sha,
    )


def _stored(retained: Retained) -> dict:
    state: dict = json.loads(retained.state_path.read_text(encoding="utf-8"))
    return state


def _rewrite(retained: Retained, edit) -> None:
    state = _stored(retained)
    edit(state)
    retained.state_path.write_text(json.dumps(state), encoding="utf-8")


def _receipt(state: dict) -> dict:
    receipt: dict
    (receipt,) = state["recovery_receipts"].values()
    return receipt


def test_gc_release_receipt_records_the_retained_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retained = _release_dirty_blocked(tmp_path, monkeypatch)

    state = _stored(retained)
    receipt = _receipt(state)
    workspace = resolve_claim_workspace(retained.repo)
    assert list(state["recovery_receipts"]) == [
        f"{workspace.repository_identity}::{ISSUE}::{OLD_CLAIM}::gc-release"
    ]
    assert {k: v for k, v in receipt.items() if k not in ("recorded_at", "active")} == {
        "schema_version": 1,
        "operation": "gc-release",
        "repository_id": workspace.repository_identity,
        "issue_number": ISSUE,
        "claim_id": OLD_CLAIM,
        "completion_id": "completion-old",
        "completion_result": "blocked",
        "branch": BRANCH,
        "worktree_path": str(retained.worktree.resolve()),
        "base_sha": retained.base_sha,
        "worktree_action": "retain",
        "stop_confirmed": True,
    }
    assert receipt["active"]["claim_id"] == OLD_CLAIM
    assert str(ISSUE) not in state["active_worktrees"]
    # completion journal and replay evidence stay exactly as complete wrote them
    assert len(state["completion_journal"]) == len(state["completion_replay_receipts"])


def test_released_generation_is_never_resurrected() -> None:
    from orchestune.ledger.run_state import (
        RunState,
        attempt_was_released,
        claim_was_released,
    )

    state = RunState(
        recovery_receipts={
            "repo::250::claim-old::gc-release": {
                "operation": "gc-release",
                "issue_number": ISSUE,
                "claim_id": OLD_CLAIM,
                "active": {"launch_attempt_id": "attempt-old"},
            }
        }
    )

    assert claim_was_released(state, ISSUE, OLD_CLAIM)
    assert attempt_was_released(state, ISSUE, "attempt-old")
    assert not claim_was_released(state, ISSUE, "claim-new")
    assert not attempt_was_released(state, ISSUE, "attempt-new")


def test_takeover_records_each_phase_and_keeps_old_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retained = _release_dirty_blocked(tmp_path, monkeypatch)
    before = _stored(retained)

    outcome = _claim(retained)

    assert outcome.success, outcome.failure
    after = _stored(retained)
    reclaim = _receipt(after)["reclaim"]
    assert (reclaim["new_claim_id"], reclaim["phase"]) == (
        outcome.claim_id,
        "completed",
    )
    assert reclaim["superseded_claim_ids"] == []
    for name in ("completion_journal", "completion_replay_receipts"):
        assert after[name] == before[name]


def test_clean_worktree_with_committed_work_is_taken_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retained = _release_dirty_blocked(tmp_path, monkeypatch)
    _git(retained.worktree, "add", "-A")
    _git(retained.worktree, "commit", "-m", "work of the released claim")
    before = _snapshot(retained.worktree)
    assert before[1] == ""

    outcome = _claim(retained)

    assert outcome.success, outcome.failure
    assert _snapshot(retained.worktree) == before


def _assert_untouched(retained: Retained, before, outcome) -> None:
    assert not outcome.success
    assert _snapshot(retained.worktree) == before
    marker = read_claim_marker(retained.worktree)
    assert marker is not None and marker["claim_id"] == OLD_CLAIM


def _drop_receipt(state: dict) -> None:
    state["recovery_receipts"] = {}


def _break_snapshot(state: dict) -> None:
    _receipt(state)["active"] = "not an active record"


def _unconfirmed_stop(state: dict) -> None:
    _receipt(state)["stop_confirmed"] = False


def _other_repository(state: dict) -> None:
    _receipt(state)["repository_id"] = "other-repository"


def _other_branch(state: dict) -> None:
    _receipt(state)["branch"] = "claude/issue-250-other"


def _other_path(state: dict) -> None:
    _receipt(state)["worktree_path"] = "/elsewhere/claude-issue-250-task"


def _other_completion(state: dict) -> None:
    _receipt(state)["completion_id"] = "completion-forged"


def _other_owner(state: dict) -> None:
    _receipt(state)["active"]["owner_token_digest"] = "0" * 64


def _live_process(state: dict) -> None:
    import os

    _receipt(state)["active"]["pid"] = os.getpid()


def _saved_takeover(state: dict) -> None:
    _receipt(state)["reclaim"] = {
        "new_claim_id": "claim-saved",
        "phase": "active_saved",
        "superseded_claim_ids": [],
    }


@pytest.mark.parametrize(
    ("corrupt", "expected"),
    [
        (_drop_receipt, "no GC release receipt"),
        (_other_repository, "no GC release receipt"),
        (_break_snapshot, "snapshot is unreadable"),
        (_unconfirmed_stop, "does not match this claim"),
        (_other_branch, "does not match this claim"),
        (_other_path, "path differs from the receipt"),
        (_other_completion, "snapshot differs from the receipt"),
        (_other_owner, "completion evidence does not match"),
        (_live_process, "of the released claim is alive"),
        (_saved_takeover, "claim-saved already took over"),
    ],
)
def test_takeover_is_refused_without_matching_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt, expected: str
) -> None:
    retained = _release_dirty_blocked(tmp_path, monkeypatch)
    _rewrite(retained, corrupt)
    before = _snapshot(retained.worktree)

    outcome = _claim(retained)

    _assert_untouched(retained, before, outcome)
    assert outcome.failure is not None
    message = outcome.failure.message
    assert message.startswith("prepare_task_worktree rejected: claim_id_mismatch")
    assert expected in message
    assert OLD_CLAIM in message and str(retained.worktree) in message
    assert "do not rewrite the claim marker by hand" in message


def test_changes_outside_the_new_footprint_are_listed_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retained = _release_dirty_blocked(tmp_path, monkeypatch)
    before = _snapshot(retained.worktree)

    outcome = _claim(retained, _issue(footprint=("README.md",)))

    _assert_untouched(retained, before, outcome)
    assert outcome.failure is not None
    assert outcome.failure.message == (
        "prepare_task_worktree rejected: takeover_rejected: retained changes are "
        "outside the reservation footprint: notes.txt"
    )


def test_marker_of_a_third_generation_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retained = _release_dirty_blocked(tmp_path, monkeypatch)
    write_claim_marker(
        retained.worktree,
        claim_id="claim-third",
        branch=BRANCH,
        base_sha=retained.base_sha,
        branch_created=True,
    )

    outcome = _claim(retained)

    assert not outcome.success
    assert outcome.failure is not None
    assert "claim-third" in outcome.failure.message
    assert "no GC release receipt" in outcome.failure.message
    assert _marker_claim(retained.worktree) == "claim-third"


def _failing_save(fail_at: int):
    """Fail the n-th claim save: 1 reservation, 2 prepared, 3 active_saved."""
    from orchestune.claim import service

    real = service.save_run_state
    calls = {"n": 0}

    def save(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == fail_at:
            raise OSError("disk full")
        return real(*args, **kwargs)

    return patch.object(service, "save_run_state", side_effect=save)


@pytest.mark.parametrize(
    ("fail_at", "marker_moved"), [(2, False), (3, True)], ids=["prepared", "marker"]
)
def test_interrupted_takeover_resumes_with_the_same_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: int, marker_moved: bool
) -> None:
    from orchestune.claim.service import resume_claim

    retained = _release_dirty_blocked(tmp_path, monkeypatch)
    before = _snapshot(retained.worktree)
    with _failing_save(fail_at):
        failed = _claim(retained)
    assert not failed.success and failed.claim_id
    marker = read_claim_marker(retained.worktree)
    assert marker is not None
    assert marker["claim_id"] == (failed.claim_id if marker_moved else OLD_CLAIM)

    with patch("orchestune.claim.service._perform_git_fetch", return_value=None):
        resumed = resume_claim(
            failed.claim_id,
            failed.owner_token or "",
            forge=MockForge({ISSUE: _issue()}),
            cwd=retained.repo,
            state_path=retained.state_path,
        )

    assert resumed.success, resumed.failure
    assert resumed.claim_id == failed.claim_id
    assert _marker_claim(retained.worktree) == failed.claim_id
    assert _snapshot(retained.worktree) == before
    assert _receipt(_stored(retained))["reclaim"]["phase"] == "completed"


@pytest.mark.parametrize(
    ("fail_at", "persisted"), [(2, False), (3, True)], ids=["prepared", "marker"]
)
def test_abandoned_prepared_takeover_is_superseded_by_the_next_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: int, persisted: bool
) -> None:
    retained = _release_dirty_blocked(tmp_path, monkeypatch)
    with _failing_save(fail_at):
        abandoned = _claim(retained)
    # e.g. zombie GC dropped the never-launched reservation
    _rewrite(retained, lambda state: state["active_worktrees"].clear())

    outcome = _claim(retained)

    assert outcome.success, outcome.failure
    assert outcome.claim_id != abandoned.claim_id
    reclaim = _receipt(_stored(retained))["reclaim"]
    assert reclaim["new_claim_id"] == outcome.claim_id
    # a failed prepared save never recorded the abandoned claim
    assert reclaim["superseded_claim_ids"] == (
        [abandoned.claim_id] if persisted else []
    )


def test_takeover_marks_the_worktree_as_reused_so_rollback_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestune.dispatch.worktree import rollback_task_worktree
    from orchestune.worktree_ops.preparation import (
        MarkerTakeover,
        prepare_task_worktree,
    )

    retained = _release_dirty_blocked(tmp_path, monkeypatch)
    before = _snapshot(retained.worktree)

    prep = prepare_task_worktree(
        BRANCH,
        retained.repo / "worktrees",
        None,
        "claim-new",
        cwd=retained.repo,
        takeover=MarkerTakeover(frozenset({OLD_CLAIM}), lambda _path: None),
    )

    assert (prep.accepted, prep.created, prep.base_sha) == (
        True,
        False,
        retained.base_sha,
    )
    assert rollback_task_worktree(prep, "claim-new") == (
        "reused_existing_worktree_not_rolled_back"
    )
    assert retained.worktree.exists()
    assert _snapshot(retained.worktree) == before


@pytest.mark.parametrize(
    ("previous", "problem", "reason"),
    [
        (frozenset({"claim-other"}), None, "claim_id_mismatch"),
        (frozenset({OLD_CLAIM}), "nope", "takeover_rejected: nope"),
    ],
)
def test_takeover_requires_the_named_generation_and_its_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous: frozenset[str],
    problem: str | None,
    reason: str,
) -> None:
    from orchestune.worktree_ops.preparation import (
        MarkerTakeover,
        prepare_task_worktree,
    )

    retained = _release_dirty_blocked(tmp_path, monkeypatch)

    prep = prepare_task_worktree(
        BRANCH,
        retained.repo / "worktrees",
        None,
        "claim-new",
        cwd=retained.repo,
        takeover=MarkerTakeover(previous, lambda _path: problem),
    )

    assert (prep.accepted, prep.rejection_reason) == (False, reason)
    assert _marker_claim(retained.worktree) == OLD_CLAIM


def test_invalid_branch_name_is_left_to_preparation(tmp_path: Path) -> None:
    from dataclasses import replace

    from orchestune.claim.worktree_reclaim import assess_worktree_reclaim
    from orchestune.ledger.run_state import RunState

    repo, worktree = _create_repo(tmp_path)
    active, _comment = _blocked_generation(repo, worktree)
    invalid = active.with_core(replace(active.core, branch="--evil"))

    assessment = assess_worktree_reclaim(
        RunState(), resolve_claim_workspace(repo), invalid
    )

    assert (assessment.takeover, assessment.diagnostic) == (None, None)
