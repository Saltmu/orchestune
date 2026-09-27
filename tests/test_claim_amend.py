"""Tests for expanding a held interactive claim reservation footprint."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestune.claim.amend import amend_claim_footprint
from orchestune.claim.contracts import (
    ClaimFailureReason,
    ClaimStage,
    OwnerKind,
    ReservationKind,
)
from orchestune.claim.ownership import new_owner_token, owner_token_digest
from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from tests.claim_helpers import MockForge, _make_issue

CLAIM_ID = "claim-amend-201"


def _issue_body(footprint: list[str]) -> str:
    items = "".join(f"  - {path}\n" for path in footprint)
    return (
        "Intro text.\n\n## Footprint\n\n```yaml\n"
        f"subtask_id: test-task\nfootprint:\n{items}```\n\nTrailing text.\n"
    )


def _active(
    issue_number: int,
    worktree: Path,
    footprint: tuple[str, ...],
    *,
    base_sha: str | None,
    identity: str,
    token_digest: str | None = None,
    **overrides: object,
) -> ActiveWorktree:
    fields: dict[str, object] = dict(
        issue_number=issue_number,
        branch=f"claude/issue-{issue_number}-test-task",
        worktree_path=str(worktree),
        pid=None,
        started_at=None,
        declared_footprint=footprint,
        owner_kind=OwnerKind.INTERACTIVE.value,
        claim_id=f"claim-amend-{issue_number}",
        claim_stage=ClaimStage.COMPLETED.value,
        reservation_kind=ReservationKind.FOOTPRINT.value,
        base_sha=base_sha,
        repository_id=identity,
        owner_token_digest=token_digest,
    )
    fields.update(overrides)
    return ActiveWorktree(**fields)  # type: ignore[arg-type]


@pytest.fixture
def amend_env(claim_env: dict[str, Path]):
    repo_root = claim_env["repo_root"]
    state_path = claim_env["state_path"]
    base_sha = run_git(["rev-parse", "HEAD"], cwd=repo_root).stdout.strip()
    worktree = claim_env["worktrees_dir"] / "issue-201"
    run_git(
        ["worktree", "add", "-b", "claude/issue-201-test-task", str(worktree)],
        cwd=repo_root,
    )
    identity = resolve_claim_workspace(
        repo_root, explicit_state_path=state_path
    ).repository_identity
    token = new_owner_token().value
    active = _active(
        201,
        worktree,
        ("orchestune/foo.py",),
        base_sha=base_sha,
        identity=identity,
        token_digest=owner_token_digest(token),
    )
    _save({"201": active}, state_path)
    forge = MockForge(
        {201: _make_issue(number=201, body=_issue_body(["orchestune/foo.py"]))}
    )
    return {
        "repo_root": repo_root,
        "state_path": state_path,
        "worktree": worktree,
        "identity": identity,
        "base_sha": base_sha,
        "token": token,
        "forge": forge,
    }


def _save(actives: dict[str, ActiveWorktree], state_path: Path) -> None:
    with run_state_lock(state_path.with_suffix(".lock")):
        save_run_state(RunState(active_worktrees=actives), state_path)


def _amend(env: dict, *, apply: bool = True, token: str | None = "default"):
    resolved = env["token"] if token == "default" else token
    return amend_claim_footprint(
        201,
        read_owner_token=lambda claim_id: resolved if claim_id == CLAIM_ID else None,
        apply=apply,
        forge=env["forge"],
        cwd=env["repo_root"],
        state_path=env["state_path"],
    )


def _held_footprint(env: dict, key: str = "201") -> tuple[str, ...]:
    return load_run_state(env["state_path"]).active_worktrees[key].declared_footprint


def test_amend_merges_issue_footprint_and_uncommitted_and_untracked_changes(amend_env):
    worktree = amend_env["worktree"]
    (worktree / "README.md").write_text("edited but not committed")
    (worktree / "new_module.py").write_text("untracked")
    amend_env["forge"].issues[201] = _make_issue(
        number=201, body=_issue_body(["orchestune/foo.py", "docs/plan.md"])
    )

    outcome = _amend(amend_env)

    assert outcome.success is True, outcome.failure
    assert set(outcome.amended_footprint) == {
        "orchestune/foo.py",
        "docs/plan.md",
        "README.md",
        "new_module.py",
    }
    assert set(outcome.added) == {"docs/plan.md", "README.md", "new_module.py"}
    assert set(_held_footprint(amend_env)) == set(outcome.amended_footprint)
    active = load_run_state(amend_env["state_path"]).active_worktrees["201"]
    assert active.claim_id == CLAIM_ID
    assert active.claim_stage == ClaimStage.COMPLETED.value
    assert active.worktree_path == str(worktree)


def test_amend_includes_committed_changes_since_base(amend_env):
    worktree = amend_env["worktree"]
    (worktree / "committed.py").write_text("x")
    run_git(["add", "committed.py"], cwd=worktree)
    run_git(["commit", "-m", "add committed"], cwd=worktree)

    outcome = _amend(amend_env)

    assert outcome.success is True, outcome.failure
    assert "committed.py" in outcome.amended_footprint


def test_amend_never_shrinks_held_footprint(amend_env):
    amend_env["forge"].issues[201] = _make_issue(
        number=201, body=_issue_body(["docs/other.md"])
    )

    outcome = _amend(amend_env)

    assert outcome.success is True, outcome.failure
    assert "orchestune/foo.py" in _held_footprint(amend_env)
    assert "docs/other.md" in _held_footprint(amend_env)


def test_amend_rewrites_issue_body_footprint_with_missing_files(amend_env):
    (amend_env["worktree"] / "extra.py").write_text("x")

    outcome = _amend(amend_env)

    assert outcome.success is True, outcome.failure
    assert outcome.issue_body_updated is True
    body = amend_env["forge"].issues[201].body
    assert "extra.py" in body
    assert "orchestune/foo.py" in body
    assert body.startswith("Intro text.")
    assert body.rstrip().endswith("Trailing text.")
    assert "subtask_id: test-task" in body


def test_amend_leaves_issue_body_when_it_already_covers_footprint(amend_env):
    outcome = _amend(amend_env)

    assert outcome.success is True, outcome.failure
    assert outcome.issue_body_updated is False
    assert outcome.added == ()
    assert amend_env["forge"].bodies_updated == []


def test_amend_rejects_overlap_with_another_reservation_without_changes(amend_env):
    other_worktree = amend_env["worktree"].parent / "issue-202"
    other = _active(
        202,
        other_worktree,
        ("shared.py",),
        base_sha=amend_env["base_sha"],
        identity=amend_env["identity"],
    )
    state = load_run_state(amend_env["state_path"])
    _save({"201": state.active_worktrees["201"], "202": other}, amend_env["state_path"])
    amend_env["forge"].issues[202] = _make_issue(
        number=202, body=_issue_body(["shared.py"])
    )
    (amend_env["worktree"] / "shared.py").write_text("touched")

    outcome = _amend(amend_env)

    assert outcome.success is False
    assert outcome.failure is not None
    assert outcome.failure.reason == ClaimFailureReason.CLAIM_CONFLICT
    assert outcome.failure.conflicting_issue_number == 202
    assert _held_footprint(amend_env) == ("orchestune/foo.py",)
    assert amend_env["forge"].bodies_updated == []


def test_amend_no_apply_reports_plan_without_changes(amend_env):
    (amend_env["worktree"] / "extra.py").write_text("x")

    outcome = _amend(amend_env, apply=False)

    assert outcome.success is True, outcome.failure
    assert "extra.py" in outcome.added
    assert _held_footprint(amend_env) == ("orchestune/foo.py",)
    assert amend_env["forge"].bodies_updated == []


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_amend_rejects_missing_or_wrong_owner_token(amend_env, token):
    outcome = _amend(amend_env, token=token)

    assert outcome.success is False
    assert outcome.failure is not None
    assert outcome.failure.reason == ClaimFailureReason.INVALID_RESUME
    assert amend_env["forge"].bodies_updated == []


def test_amend_rejects_when_issue_has_no_active_claim(amend_env):
    _save({}, amend_env["state_path"])

    outcome = _amend(amend_env)

    assert outcome.success is False
    assert outcome.failure is not None
    assert outcome.failure.reason == ClaimFailureReason.INVALID_RESUME


@pytest.mark.parametrize(
    "overrides",
    [
        {"claim_stage": ClaimStage.ACTIVE_SAVED.value},
        {"owner_kind": OwnerKind.DISPATCH.value},
        {"reservation_kind": ReservationKind.REPOSITORY.value},
        {"completion_id": "completion-1"},
        {"repository_id": "/somewhere/else/.git"},
    ],
)
def test_amend_rejects_ineligible_reservations(amend_env, overrides):
    state = load_run_state(amend_env["state_path"])
    active = state.active_worktrees["201"]
    for key, value in overrides.items():
        setattr(active, key, value)
    _save({"201": active}, amend_env["state_path"])

    outcome = _amend(amend_env)

    assert outcome.success is False
    assert outcome.failure is not None
    assert outcome.failure.reason == ClaimFailureReason.INVALID_RESUME
    assert outcome.failure.next_actions
    assert amend_env["forge"].bodies_updated == []


def test_amend_rejects_repository_reservation_declaration_in_issue(amend_env):
    amend_env["forge"].issues[201] = _make_issue(
        number=201, body="## Footprint\n\n```yaml\nsubtask_id: test-task\n```\n"
    )

    outcome = _amend(amend_env)

    assert outcome.success is False
    assert outcome.failure is not None
    assert outcome.failure.reason == ClaimFailureReason.INVALID_RESUME
    assert _held_footprint(amend_env) == ("orchestune/foo.py",)


def test_amend_rejects_closed_issue(amend_env):
    amend_env["forge"].issues[201] = _make_issue(
        number=201, body=_issue_body(["orchestune/foo.py"]), state="CLOSED"
    )

    outcome = _amend(amend_env)

    assert outcome.success is False
    assert outcome.failure is not None
    assert outcome.failure.reason == ClaimFailureReason.ISSUE_CLOSED


def test_amend_fails_closed_when_changed_files_cannot_be_listed(amend_env):
    state = load_run_state(amend_env["state_path"])
    active = state.active_worktrees["201"]
    active.base_sha = None
    _save({"201": active}, amend_env["state_path"])

    outcome = _amend(amend_env)

    assert outcome.success is False
    assert outcome.failure is not None
    assert _held_footprint(amend_env) == ("orchestune/foo.py",)


def test_amend_does_not_save_state_when_issue_update_fails(amend_env):
    (amend_env["worktree"] / "extra.py").write_text("x")
    amend_env["forge"].fail_update_body = True

    outcome = _amend(amend_env)

    assert outcome.success is False
    assert outcome.failure is not None
    assert outcome.failure.reason == ClaimFailureReason.STATE_SAVE_FAILED
    assert _held_footprint(amend_env) == ("orchestune/foo.py",)


def test_reclaim_of_held_issue_reports_recovery_next_actions(amend_env):
    from orchestune.claim.contracts import ClaimRequest
    from orchestune.claim.service import claim_task

    outcome = claim_task(
        ClaimRequest(issue_number=201, state_path=amend_env["state_path"]),
        forge=amend_env["forge"],
        cwd=amend_env["repo_root"],
    )

    assert outcome.success is False
    assert outcome.failure is not None
    assert outcome.failure.reason == ClaimFailureReason.EXISTING_CLAIM_UNRECOVERED
    actions = "\n".join(outcome.failure.next_actions)
    assert str(amend_env["worktree"]) in actions
    assert f"orchestune claim 201 --resume {CLAIM_ID}" in actions
    assert "orchestune claim 201 --amend-footprint" in actions


def test_amend_rejects_missing_issue(amend_env):
    del amend_env["forge"].issues[201]

    outcome = _amend(amend_env)

    assert outcome.success is False
    assert outcome.failure is not None
    assert outcome.failure.reason == ClaimFailureReason.ISSUE_NOT_FOUND


def test_amend_reports_state_save_failure(amend_env):
    from unittest.mock import patch

    (amend_env["worktree"] / "extra.py").write_text("x")
    with patch(
        "orchestune.claim.amend.save_run_state", side_effect=OSError("disk full")
    ):
        outcome = _amend(amend_env)

    assert outcome.success is False
    assert outcome.failure is not None
    assert outcome.failure.reason == ClaimFailureReason.STATE_SAVE_FAILED
    assert _held_footprint(amend_env) == ("orchestune/foo.py",)


def test_amend_reports_lock_contention(amend_env):
    from unittest.mock import patch

    from orchestune.infra.process_utils import FileLockContentionError

    with patch(
        "orchestune.claim.amend.run_state_lock",
        side_effect=FileLockContentionError("lock is busy"),
    ):
        outcome = _amend(amend_env)

    assert outcome.success is False
    assert outcome.failure is not None
    assert outcome.failure.reason == ClaimFailureReason.STATE_LOCK_FAILED


def test_amend_records_non_ascii_changed_paths_verbatim(amend_env):
    worktree = amend_env["worktree"]
    (worktree / "設計.md").write_text("untracked")
    (worktree / "README.md").write_text("changed")
    run_git(["mv", "README.md", "読んで.md"], cwd=worktree)

    outcome = _amend(amend_env)

    assert outcome.success is True, outcome.failure
    assert "設計.md" in outcome.amended_footprint
    assert "読んで.md" in outcome.amended_footprint
    assert "README.md" in outcome.amended_footprint
    assert not any('"' in path or "\\" in path for path in outcome.amended_footprint)


@pytest.mark.parametrize(
    ("overrides", "expected", "unexpected"),
    [
        (
            {"owner_kind": OwnerKind.DISPATCH.value},
            "--result blocked --reason footprint-expansion-required",
            "--amend-footprint",
        ),
        (
            {"reservation_kind": ReservationKind.REPOSITORY.value},
            "already reserves the whole repository",
            "--amend-footprint",
        ),
    ],
)
def test_held_claim_next_actions_offer_amend_only_when_eligible(
    tmp_path, overrides, expected, unexpected
):
    from orchestune.claim.ownership import held_claim_next_actions

    active = _active(
        201, tmp_path, ("a.py",), base_sha="abc", identity="repo", **overrides
    )

    actions = "\n".join(held_claim_next_actions(active))

    assert expected in actions
    assert unexpected not in actions
