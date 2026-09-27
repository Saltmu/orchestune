"""Snapshots produced by main before the ledger extraction (#1061)."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest

from orchestune.infra.process_utils import run_state_lock

FIXTURES = Path(__file__).parent / "fixtures" / "ledger_state"
pytestmark = pytest.mark.uses_run_state_lock_assertion


@pytest.mark.parametrize("case_name", ["minimal", "completing", "retention"])
def test_pre_move_main_state_reads_and_saves_unchanged(
    tmp_path: Path, case_name: str
) -> None:
    ledger = import_module("orchestune.ledger.run_state")
    case = json.loads((FIXTURES / f"{case_name}.json").read_text())
    path = tmp_path / "run_state.json"
    path.write_text(json.dumps(case["input"]))
    state = ledger.load_run_state(path)
    with run_state_lock(path.with_suffix(".lock")):
        ledger.save_run_state(state, path, now=case["now"])
    assert json.loads(path.read_text()) == case["normalized"]
    assert ledger.load_run_state(path) == ledger.prune_run_state(state, now=case["now"])


def test_ledger_save_still_requires_the_same_sibling_lock(tmp_path: Path) -> None:
    ledger = import_module("orchestune.ledger.run_state")
    path = tmp_path / "run_state.json"
    with run_state_lock(tmp_path / "different.lock"):
        with pytest.raises(RuntimeError, match="run_state lock must be held"):
            ledger.save_run_state(ledger.RunState(), path)
    with run_state_lock(path.with_suffix(".lock")):
        with run_state_lock(path.with_suffix(".lock")):
            ledger.save_run_state(ledger.RunState(), path)
    assert ledger.load_run_state(path) == ledger.RunState()


@pytest.mark.parametrize(
    "field,value", [("owner_kind", "unknown"), ("completion_handoff_ready", "true")]
)
def test_ledger_rejects_invalid_main_state_fields(
    tmp_path: Path, field: str, value: str
) -> None:
    ledger = import_module("orchestune.ledger.run_state")
    case = json.loads((FIXTURES / "completing.json").read_text())
    case["input"]["active_worktrees"]["10"][field] = value
    path = tmp_path / "run_state.json"
    path.write_text(json.dumps(case["input"]))
    with pytest.raises(ValueError, match=r"active_worktrees\[10\] schema error"):
        ledger.load_run_state(path)
