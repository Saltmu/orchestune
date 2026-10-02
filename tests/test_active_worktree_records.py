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


def test_nested_factory_stores_core_and_subrecords_as_the_only_fields() -> None:
    records = _records()
    active = ActiveWorktree.from_records(
        core=records[0], launch=records[1], claim=records[2], completion=records[3]
    )

    assert active.core is records[0]
    assert active.launch is records[1]
    assert active.claim is records[2]
    assert active.completion is records[3]
    assert [item.name for item in fields(active)] == [
        "core",
        "launch",
        "claim",
        "completion",
    ]
    assert not hasattr(active, "claim_id")
    assert not hasattr(active, "pid")
    assert asdict(active)["completion"]["completion_id"] == "completion-17"


def test_core_update_returns_a_copy_with_other_subrecords_shared() -> None:
    core, launch, claim, completion = _records()
    active = ActiveWorktree.from_records(
        core=core, launch=launch, claim=claim, completion=completion
    )
    updated_core = replace(core, branch="task/17-amended")

    updated = active.with_core(updated_core)

    assert updated.core.branch == "task/17-amended"
    assert updated.claim is active.claim
    assert updated is not active
    assert active.core.branch == "task/17"


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


def test_direct_construction_checks_record_types_and_defaults_owner_records() -> None:
    core, _, _, _ = _records()

    active = ActiveWorktree(core=core)

    assert active.launch == LaunchInfo()
    assert active.claim == ClaimInfo()
    assert active.completion == ActiveCompletionJournal()
    with pytest.raises(TypeError, match="launch must be LaunchInfo"):
        ActiveWorktree(core=core, launch={"pid": 1})  # type: ignore[arg-type]


def test_flat_constructor_and_attributes_are_removed() -> None:
    with pytest.raises(TypeError):
        ActiveWorktree(  # type: ignore[call-arg]
            issue_number=18,
            branch="task/18",
            worktree_path="worktrees/task-18",
            pid=None,
            started_at=None,
            declared_footprint=(),
        )


def test_stale_writes_to_removed_flat_attributes_fail_loudly() -> None:
    active = ActiveWorktree(core=_records()[0])

    with pytest.raises(AttributeError):
        active.pid = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        active.claim_id = "stale"  # type: ignore[attr-defined]


def test_canonical_factory_restricts_payload_objects_to_json_objects() -> None:
    core, launch, claim, _ = _records()
    from orchestune.ledger.active_codec import decode_active_worktree

    legacy = decode_active_worktree(
        {
            "issue_number": 17,
            "branch": "b",
            "worktree_path": "p",
            "declared_footprint": [],
            "completion_policy_config": ["legacy-shape"],
        }
    ).completion

    assert legacy.completion_policy_config == ("legacy-shape",)
    with pytest.raises(ValueError, match="completion_policy_config must be an object"):
        ActiveWorktree.from_records(
            core=core, launch=launch, claim=claim, completion=legacy
        )


def test_frozen_completion_payload_is_deeply_immutable_and_detached() -> None:
    source = {"events": [{"labels": ["queued"]}]}
    journal = ActiveCompletionJournal(
        completion_id="completion-17", completion_payload=source
    )
    source["events"][0]["labels"].append("mutated-at-source")

    assert journal.completion_payload == {"events": ({"labels": ("queued",)},)}
    frozen_payload = cast(Any, journal.completion_payload)
    with pytest.raises(TypeError):
        frozen_payload["new"] = "value"
    with pytest.raises(TypeError):
        frozen_payload["events"][0]["labels"][0] = "changed"
    with pytest.raises(FrozenInstanceError):
        journal.completion_id = "changed"  # type: ignore[misc]


def test_nested_factory_detaches_json_payloads_from_the_source() -> None:
    source = {"events": [{"labels": ["queued"]}]}
    core, launch, claim, completion = _records(payload=source)
    active = ActiveWorktree.from_records(
        core=core, launch=launch, claim=claim, completion=completion
    )
    source["events"][0]["labels"].append("later")

    assert active.completion.completion_payload == {
        "events": ({"labels": ("queued",)},)
    }


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


def test_shared_active_worktree_factory_normalizes_list_footprints() -> None:
    active = make_test_active_worktree(declared_footprint=["src/example.py"])

    assert active.core.declared_footprint == ("src/example.py",)
