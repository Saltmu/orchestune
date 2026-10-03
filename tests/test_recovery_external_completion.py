"""Recovery retains each valid publication state and rejects contradictions."""

import copy
from dataclasses import replace

import pytest

from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.run_state import (
    RunState,
    load_run_state_readonly,
    save_run_state,
)
from orchestune.recovery.external_completion import completion_retention
from orchestune.recovery.service import recover_claim
from tests.dispatch_test_support import replace_flat

pytest_plugins = ["tests.test_recovery_external_stop"]


def journal(active, stage="reserved", completion_id="completion"):
    record = {
        "schema_version": 1,
        "repository_id": active.claim.repository_id,
        "issue_number": active.core.issue_number,
        "generation_id": active.claim.claim_id,
        "completion_id": completion_id,
        "owner_token_digest": active.claim.owner_token_digest,
        "request_fingerprint": "request",
        "result": "blocked",
        "target_label": "status:blocked-human-review",
        "outcome_payload": {"reason": "stopped"},
        "stage": stage,
        "posting_evidence": {
            "comment_id": "comment",
            "comment_url": "https://example.test/comment",
        },
        "label_evidence": {
            "status": "confirmed",
            "observed_labels": ["status:blocked-human-review"],
        },
    }
    key = "::".join(
        str(record[n])
        for n in ("repository_id", "issue_number", "generation_id", "completion_id")
    )
    return key, record


@pytest.mark.parametrize(
    "stage", ["reserved", "outcome_posted", "label_confirmed", "handed_off"]
)
def test_each_valid_reservation_retains_active(external_claim, stage):
    workspace, active, _, request = external_claim
    key, record = journal(active, stage)
    state = load_run_state_readonly(workspace.run_state_path)
    state.completion_journal[key] = record
    state.completion_reservations[f"{active.claim.repository_id}::7"] = copy.deepcopy(
        record
    )
    if stage == "handed_off":
        state.completion_replay_receipts[key] = copy.deepcopy(record)
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    result = recover_claim(replace(request, apply=True), cwd=workspace.repository_root)
    assert result.success and result.action == "external_stop_confirmed_active_retained"
    saved = load_run_state_readonly(workspace.run_state_path)
    assert len(saved.recovery_receipts) == 1 and "7" in saved.active_worktrees
    assert saved.completion_journal == state.completion_journal


def test_journal_without_reservation_requires_resume(external_claim):
    workspace, active, _, request = external_claim
    state = load_run_state_readonly(workspace.run_state_path)
    key, record = journal(active)
    state.completion_journal[key] = record
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    result = recover_claim(replace(request, apply=True), cwd=workspace.repository_root)
    assert result.action == "external_stop_confirmed_active_retained"
    assert result.diagnostics["completion_decision"] == "completion_resume_required"


@pytest.mark.parametrize(
    "problem", ["reservation", "generation", "owner", "completion", "ambiguous", "key"]
)
def test_invalid_completion_never_writes(external_claim, problem):
    workspace, active, _, request = external_claim
    state = load_run_state_readonly(workspace.run_state_path)
    key, record = journal(active)
    if problem == "reservation":
        state.completion_reservations[f"{active.claim.repository_id}::7"] = {
            "issue_number": 7
        }
    elif problem == "generation":
        record["generation_id"] = "new"
        key = key.replace(active.claim.claim_id, "new")
        state.completion_reservations[f"{active.claim.repository_id}::7"] = (
            copy.deepcopy(record)
        )
    elif problem == "owner":
        record["owner_token_digest"] = "wrong"
    elif problem == "completion":
        state.active_worktrees["7"] = replace_flat(active, completion_id="different")
    elif problem == "ambiguous":
        other_key, other = journal(active, completion_id="another")
        state.completion_journal[other_key] = other
    elif problem == "key":
        key = "wrong"
    state.completion_journal[key] = record
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    before = workspace.run_state_path.read_bytes()
    result = recover_claim(replace(request, apply=True), cwd=workspace.repository_root)
    assert not result.success and "completion" in result.reason
    assert workspace.run_state_path.read_bytes() == before


def test_other_generation_history_is_not_current_completion(local_claim):
    _, active, _ = local_claim
    old = replace_flat(active, claim_id="old")
    key, record = journal(old, "handed_off")
    state = RunState(completion_journal={key: record})
    assert completion_retention(active, state) == (False, "release")
