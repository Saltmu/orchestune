"""Contracts for the nested ActiveWorktree bridge records."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, fields, replace
from typing import Any, cast

import pytest

from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktree,
    ActiveWorktreeCore,
    ClaimInfo,
    LaunchInfo,
)
from tests.dispatch_test_support import make_test_active_worktree


def _records(
    *, payload: dict | None = None
) -> tuple[
    ActiveWorktreeCore,
    LaunchInfo,
    ClaimInfo,
    ActiveCompletionJournal,
]:
    core = ActiveWorktreeCore(
        issue_number=17,
        branch="task/17",
        worktree_path="worktrees/task-17",
        declared_footprint=("src/b.py", "src/a.py"),
        base_branch="origin/parent/issue-1106",
    )
    return (
        core,
        LaunchInfo(pid=321, started_at=1.5, launch_phase="launching"),
        ClaimInfo(owner_kind="dispatch", claim_id="claim-17", claim_stage="completed"),
        ActiveCompletionJournal(
            completion_id="completion-17", completion_payload=payload
        ),
    )


def test_nested_factory_builds_the_legacy_flat_dataclass_and_views() -> None:
    records = _records()
    active = ActiveWorktree.from_records(
        core=records[0], launch=records[1], claim=records[2], completion=records[3]
    )

    assert active.core == records[0]
    assert active.launch == records[1]
    assert active.claim == records[2]
    assert active.completion == records[3]
    assert [item.name for item in fields(active)][:6] == [
        "issue_number",
        "branch",
        "worktree_path",
        "pid",
        "started_at",
        "declared_footprint",
    ]
    assert asdict(active)["completion_id"] == "completion-17"


def test_core_update_returns_a_flat_compatible_copy() -> None:
    core, launch, claim, completion = _records()
    active = ActiveWorktree.from_records(
        core=core, launch=launch, claim=claim, completion=completion
    )
    updated_core = replace(core, branch="task/17-amended")

    updated = active.with_core(updated_core)

    assert updated.branch == "task/17-amended"
    assert updated.claim_id == active.claim_id
    assert updated is not active
    assert active.branch == "task/17"


def test_new_nested_construction_rejects_flat_or_invalid_core_values() -> None:
    core, launch, claim, completion = _records()

    with pytest.raises(TypeError, match="core must be ActiveWorktreeCore"):
        ActiveWorktree.from_records(
            core={"issue_number": 17},  # type: ignore[arg-type]
            launch=launch,
            claim=claim,
            completion=completion,
        )
    with pytest.raises(ValueError, match="issue_number must be a positive integer"):
        ActiveWorktree.from_records(
            core=replace(core, issue_number=0),
            launch=launch,
            claim=claim,
            completion=completion,
        )


def test_canonical_factory_restricts_payload_objects_but_flat_dto_stays_compatible() -> (
    None
):
    legacy = ActiveWorktree(
        issue_number=19,
        branch="task/19",
        worktree_path="worktrees/task-19",
        pid=None,
        started_at=None,
        declared_footprint=(),
        completion_policy_config=["legacy-shape"],  # type: ignore[arg-type]
    )

    # Existing flat construction and derived snapshots can still represent old
    # values; only the new nested factory applies the canonical object constraint.
    assert legacy.completion.completion_policy_config == ("legacy-shape",)
    with pytest.raises(ValueError, match="completion_policy_config must be an object"):
        ActiveWorktree.from_records(
            core=legacy.core,
            launch=legacy.launch,
            claim=legacy.claim,
            completion=legacy.completion,
        )


def test_frozen_completion_payload_is_deeply_immutable_and_detached() -> None:
    source = {"events": [{"labels": ["queued"]}]}
    journal = ActiveCompletionJournal(completion_payload=source)
    source["events"][0]["labels"].append("mutated-at-source")

    assert journal.completion_payload == {"events": ({"labels": ("queued",)},)}
    frozen_payload = cast(Any, journal.completion_payload)
    with pytest.raises(TypeError):
        frozen_payload["new"] = "value"
    with pytest.raises(TypeError):
        frozen_payload["events"][0]["labels"][0] = "changed"
    with pytest.raises(FrozenInstanceError):
        journal.completion_id = "changed"  # type: ignore[misc]


def test_nested_factory_keeps_json_payloads_as_detached_flat_dicts_and_lists() -> None:
    source = {"events": [{"labels": ["queued"]}]}
    core, launch, claim, completion = _records(payload=source)
    active = ActiveWorktree.from_records(
        core=core, launch=launch, claim=claim, completion=completion
    )
    source["events"][0]["labels"].append("later")

    assert active.completion_payload == {"events": [{"labels": ["queued"]}]}
    assert isinstance(active.completion_payload, dict)
    assert isinstance(active.completion_payload["events"], list)


def test_existing_flat_constructor_remains_available_during_migration() -> None:
    active = ActiveWorktree(
        issue_number=18,
        branch="task/18",
        worktree_path="worktrees/task-18",
        pid=None,
        started_at=None,
        declared_footprint=(),
    )

    assert active.core.issue_number == 18
    assert active.launch.pid is None
    assert active.claim.owner_kind == "dispatch"
    assert active.completion.completion_id is None
    assert replace(active, branch="task/18b").branch == "task/18b"


def test_shared_active_worktree_factory_rejects_unknown_override_keys() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument 'claim_stge'"):
        make_test_active_worktree(claim_stge="completed")


def test_shared_active_worktree_factory_propagates_nested_factory_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_nested_factory(cls: type[ActiveWorktree], **kwargs: Any) -> ActiveWorktree:
        del cls, kwargs
        raise TypeError("nested factory regression")

    monkeypatch.setattr(
        ActiveWorktree, "from_records", classmethod(fail_nested_factory)
    )
    with pytest.raises(TypeError, match="nested factory regression"):
        make_test_active_worktree()
