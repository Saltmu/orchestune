"""Stop evidence is bound to immutable execution identity, never progress."""

import copy

import pytest

from orchestune.ledger.external_stop_receipts import (
    confirmation_key,
    confirmation_record,
    matching_confirmation,
)
from orchestune.ledger.run_state import RunState
from tests.dispatch_test_support import replace_flat

pytest_plugins = ["tests.test_local_claim_identity"]


def test_progress_does_not_invalidate_but_new_execution_does(local_claim):
    workspace, active, _ = local_claim
    active = replace_flat(active, external_id="run::a", launch_attempt_id="attempt")
    receipt = confirmation_record(active, "provider stopped")
    state = RunState(recovery_receipts={confirmation_key(active): receipt})
    progressed = replace_flat(active, completion_id="complete", recompute_count=2)
    assert matching_confirmation(state, progressed, workspace.repository_identity)
    for field, value in [
        ("external_id", "run::b"),
        ("launch_attempt_id", "new"),
        ("started_at", 99.0),
        ("claimed_at", 99.0),
        ("owner_token_digest", "new"),
        ("repository_id", "another"),
    ]:
        changed = replace_flat(active, **{field: value})
        assert not matching_confirmation(state, changed, workspace.repository_identity)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("source", "provider"),
        ("operation", "release"),
        ("confirmed_external_stopped", 1),
        ("reason", "  "),
        ("recorded_at", "invalid"),
        ("worktree_action", "remove"),
        ("execution_identity", {}),
        ("active", {}),
        ("claim_id", "different"),
    ],
)
def test_corrupt_receipts_are_not_evidence(local_claim, field, value):
    workspace, active, _ = local_claim
    active = replace_flat(active, external_id="run")
    receipt = confirmation_record(active, "stopped")
    receipt[field] = value
    state = RunState(recovery_receipts={confirmation_key(active): receipt})
    assert not matching_confirmation(state, active, workspace.repository_identity)


def test_snapshot_identity_and_key_are_both_verified(local_claim):
    workspace, active, _ = local_claim
    active = replace_flat(active, external_id="run")
    receipt = confirmation_record(active, "stopped")
    modified = copy.deepcopy(receipt)
    modified["active"]["started_at"] = 33.0
    state = RunState(recovery_receipts={confirmation_key(active): modified})
    assert not matching_confirmation(state, active, workspace.repository_identity)
    state.recovery_receipts = {"wrong-key": receipt}
    assert not matching_confirmation(state, active, workspace.repository_identity)
