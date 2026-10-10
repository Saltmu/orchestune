"""#1270: persisted launch attribution is validated where the ledger is read."""

import json

import pytest

from orchestune.ledger.run_state import load_run_state
from tests.test_ledger_run_state import _serialized_current_active


@pytest.mark.parametrize(
    "extra",
    [
        {"launch_log_offset": "5"},
        {"launch_log_offset": 1.5},
        {"launch_log_offset": True},
        {"launch_log_offset": -1},
        {"launch_log_path": 5},
        {"launch_target": 3},
    ],
)
def test_malformed_launch_attribution_is_rejected_on_load(tmp_path, extra):
    path = tmp_path / "run_state.json"
    record = {**_serialized_current_active(), **extra}
    path.write_text(
        json.dumps({"active_worktrees": {"10": record}, "launch_history": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"active_worktrees\[10\] schema error"):
        load_run_state(path)
