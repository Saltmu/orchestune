"""Completion identity and immutable receipt lookup."""

from orchestune.complete.contracts import CompleteRequest
from orchestune.complete.replay import find_replay
from orchestune.ledger.run_state import RunState


def test_unknown_explicit_id_never_selects_latest_claim():
    request = CompleteRequest.blocked(1110, "blocked", completion_id="unknown")
    assert find_replay(request, RunState(), "repo") is None
