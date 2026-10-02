"""Tests for ActiveWorktree subrecord and owner AST architecture guards (#1135)."""

from __future__ import annotations

from pathlib import Path

import pytest
from active_worktree_architecture_support import (
    ActiveWorktreeBoundaryException,
    active_worktree_boundary_violations,
    production_active_worktree_violations,
    unused_active_worktree_exceptions,
)

REPO_ROOT = Path(__file__).parents[1]


def test_subrecord_direct_assignment_is_rejected() -> None:
    source = """
from orchestune.ledger.active_records import ActiveWorktree, LaunchInfo, ClaimInfo, ActiveCompletionJournal, ActiveWorktreeCore

def mutate(active: ActiveWorktree):
    active.launch = LaunchInfo()
    active.claim = ClaimInfo()
    active.completion = ActiveCompletionJournal()
    active.core = ActiveWorktreeCore(1, "b", "p", ())
    active.launch.pid = 1234
    active.claim.claim_id = "claim-test"
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    targets_and_kinds = {(v.target, v.kind) for v in violations}
    assert targets_and_kinds == {
        ("launch", "subrecord-direct-assign"),
        ("claim", "subrecord-direct-assign"),
        ("completion", "subrecord-direct-assign"),
        ("core", "subrecord-direct-assign"),
        ("launch.pid", "subrecord-direct-assign"),
        ("claim.claim_id", "subrecord-direct-assign"),
    }


def test_subrecord_setattr_is_rejected() -> None:
    source = """
from orchestune.ledger.active_records import ActiveWorktree, LaunchInfo

def mutate(active: ActiveWorktree):
    setattr(active, "launch", LaunchInfo())
    setattr(active, "claim", None)
    setattr(active.launch, "pid", 123)
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    targets_and_kinds = {(v.target, v.kind) for v in violations}
    assert targets_and_kinds == {
        ("launch", "subrecord-setattr"),
        ("claim", "subrecord-setattr"),
        ("launch", "subrecord-setattr"),
    }


def test_dataclasses_replace_import_alias_and_owner_boundary() -> None:
    source = """
from dataclasses import replace as dc_replace
from orchestune.ledger.active_records import ActiveWorktree, LaunchInfo, ClaimInfo, ActiveCompletionJournal, ActiveWorktreeCore

def mutate(active: ActiveWorktree):
    a = dc_replace(active, launch=LaunchInfo())
    b = dc_replace(active, claim=ClaimInfo())
    c = dc_replace(active, completion=ActiveCompletionJournal())
    d = dc_replace(active, core=ActiveWorktreeCore(1, "b", "p", ()))
    return a, b, c, d
"""
    # In unauthorized module: all 4 are violations
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    assert {(v.target, v.kind) for v in violations} == {
        ("launch", "unauthorized-replace"),
        ("claim", "unauthorized-replace"),
        ("completion", "unauthorized-replace"),
        ("core", "unauthorized-replace"),
    }

    # In launch owner module: only claim, completion, core are violations
    launch_violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.launch_state"
    )
    assert {(v.target, v.kind) for v in launch_violations} == {
        ("claim", "unauthorized-replace"),
        ("completion", "unauthorized-replace"),
        ("core", "unauthorized-replace"),
    }

    # In claim owner module: only launch, completion, core are violations
    claim_violations = active_worktree_boundary_violations(
        source, module="orchestune.claim.ownership"
    )
    assert {(v.target, v.kind) for v in claim_violations} == {
        ("launch", "unauthorized-replace"),
        ("completion", "unauthorized-replace"),
        ("core", "unauthorized-replace"),
    }

    # In completion owner module: only launch, claim, core are violations
    complete_violations = active_worktree_boundary_violations(
        source, module="orchestune.complete.journal"
    )
    assert {(v.target, v.kind) for v in complete_violations} == {
        ("launch", "unauthorized-replace"),
        ("claim", "unauthorized-replace"),
        ("core", "unauthorized-replace"),
    }


def test_unauthorized_constructor_and_from_records_rejected() -> None:
    source = """
from orchestune.ledger.active_records import ActiveWorktree

def build(core, launch, claim, completion):
    a = ActiveWorktree(core=core, launch=launch, claim=claim, completion=completion)
    b = ActiveWorktree.from_records(core=core, launch=launch, claim=claim, completion=completion)
    return a, b
"""
    # Unauthorized module
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    assert len(violations) == 2
    assert all(v.kind == "unauthorized-constructor" for v in violations)

    # Allowed constructor modules
    assert not active_worktree_boundary_violations(
        source, module="orchestune.dispatch.launch_state"
    )
    assert not active_worktree_boundary_violations(
        source, module="orchestune.claim.ownership"
    )
    assert not active_worktree_boundary_violations(
        source, module="orchestune.ledger.active_codec"
    )


def test_nested_payload_mutation_is_rejected() -> None:
    source = """
from orchestune.ledger.active_records import ActiveWorktree

def mutate_payload(active: ActiveWorktree):
    active.completion.completion_payload["key"] = "value"
    active.completion.completion_payload.update({"key": "value"})
    active.completion.completion_payload.pop("key", None)
    active.completion.completion_payload.clear()
    del active.completion.completion_payload["key"]
    active.completion.completion_policy_config["flag"] = True
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.gc"
    )
    targets_and_kinds = {(v.target, v.kind) for v in violations}
    assert targets_and_kinds == {
        ("completion_payload", "payload-mutation"),
        ("completion_policy_config", "payload-mutation"),
    }
    assert len(violations) == 6


def test_completion_id_is_none_stage_check_rejected_but_identity_and_marker_allowed() -> (
    None
):
    source = """
from orchestune.ledger.active_records import ActiveWorktree

def check_stages(active: ActiveWorktree, expected_id: str):
    # Prohibited: checking stage via completion_id is [not] None
    if active.completion.completion_id is None:
        pass
    if active.completion.completion_id is not None:
        pass

    # Allowed: identity comparisons
    if active.completion.completion_id == expected_id:
        pass
    if active.completion.completion_id != expected_id:
        pass

    # Allowed: marker / dict assembly
    marker = {"completion_id": active.completion.completion_id}
    return marker
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    assert len(violations) == 2
    assert all(
        v.target == "completion_id" and v.kind == "completion-stage-is-none"
        for v in violations
    )


def test_same_named_other_dto_not_flagged() -> None:
    source = """
from dataclasses import dataclass, replace as dc_replace

@dataclass
class OtherDTO:
    launch: str
    claim: str
    completion_id: str | None = None
    completion_payload: dict | None = None

def handle_other(dto: OtherDTO):
    dto.launch = "started"
    setattr(dto, "launch", "running")
    if dto.completion_id is None:
        pass
    dc_replace(dto, launch="done")
    if dto.completion_payload is not None:
        dto.completion_payload["result"] = "ok"
        dto.completion_payload.update({"sub": 1})

def handle_task(task: TaskMetadata, request: CompleteRequest):
    if request.completion_id is None:
        pass
    task.claim_id = "test"
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    assert not violations


def test_flat_attribute_access_rejected() -> None:
    source = """
from dataclasses import replace
from orchestune.ledger.active_records import ActiveWorktree

def access_flat(active: ActiveWorktree):
    # Writes
    active.pid = 9999
    active.claim_id = "old"
    replace(active, pid=9999)
    # Reads
    p = active.pid
    if active.claim_id:
        return active.launch_phase
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    targets_and_kinds = {(v.target, v.kind) for v in violations}
    assert targets_and_kinds == {
        ("pid", "flat-attribute-access"),
        ("claim_id", "flat-attribute-access"),
        ("launch_phase", "flat-attribute-access"),
    }


def test_union_and_optional_annotated_parameters_recognized() -> None:
    source = """
from typing import Optional
from orchestune.ledger.active_records import ActiveWorktree

def handle_pipe_union(active: ActiveWorktree | None):
    if active is not None:
        active.launch.pid = 9999

def handle_optional(worktree: Optional[ActiveWorktree]):
    if worktree:
        worktree.completion.completion_payload["flag"] = True
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    targets_and_kinds = {(v.target, v.kind) for v in violations}
    assert targets_and_kinds == {
        ("launch.pid", "subrecord-direct-assign"),
        ("completion_payload", "payload-mutation"),
    }


def test_constructor_aliases_and_qualified_access_rejected_in_unauthorized_module() -> (
    None
):
    source = """
from orchestune.ledger.active_records import ActiveWorktree as Worktree
import orchestune.ledger.active_records as ar

def build(core, launch, claim, completion):
    a = Worktree(core=core, launch=launch, claim=claim, completion=completion)
    b = ar.ActiveWorktree.from_records(core=core, launch=launch, claim=claim, completion=completion)
    return a, b
"""
    # Unauthorized module
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    assert len(violations) == 2
    assert all(v.kind == "unauthorized-constructor" for v in violations)

    # Allowed constructor module
    assert not active_worktree_boundary_violations(
        source, module="orchestune.ledger.active_codec"
    )


def test_assignment_alias_propagation_tracks_violations() -> None:
    source = """
from orchestune.ledger.active_records import ActiveWorktree

def alias_flow(active: ActiveWorktree):
    record = active
    record.claim = None
    p = record.pid
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    targets_and_kinds = {(v.target, v.kind) for v in violations}
    assert targets_and_kinds == {
        ("claim", "subrecord-direct-assign"),
        ("pid", "flat-attribute-access"),
    }


def test_imported_type_alias_in_annotation_recognized() -> None:
    source = """
from orchestune.ledger.active_records import ActiveWorktree as Worktree

def check_alias(record: Worktree):
    record.launch = None
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    targets_and_kinds = {(v.target, v.kind) for v in violations}
    assert targets_and_kinds == {
        ("launch", "subrecord-direct-assign"),
    }


def test_inner_scope_shadowing_honors_inner_binding() -> None:
    source = """
from dataclasses import dataclass
from orchestune.ledger.active_records import ActiveWorktree

@dataclass
class OtherDTO:
    claim: str

def outer(active: ActiveWorktree):
    def inner(active: OtherDTO):
        active.claim = "safe"
    return inner
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    assert not violations


def test_with_core_propagation_tracks_violations() -> None:
    source = """
from orchestune.ledger.active_records import ActiveWorktree

def update_core(active: ActiveWorktree, core):
    record = active.with_core(core)
    record.claim = None
    p = record.pid
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    targets_and_kinds = {(v.target, v.kind) for v in violations}
    assert targets_and_kinds == {
        ("claim", "subrecord-direct-assign"),
        ("pid", "flat-attribute-access"),
    }


def test_payload_and_policy_config_alias_mutation_rejected() -> None:
    source = """
from orchestune.ledger.active_records import ActiveWorktree

def mutate_aliased(active: ActiveWorktree):
    payload = active.completion.completion_payload
    payload["key"] = "val"
    payload.update({"sub": 1})
    del payload["key"]

    cfg = active.completion.completion_policy_config
    cfg["flag"] = False
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    targets_and_kinds = {(v.target, v.kind) for v in violations}
    assert targets_and_kinds == {
        ("completion_payload", "payload-mutation"),
        ("completion_policy_config", "payload-mutation"),
    }
    assert len(violations) == 4


def test_positional_only_parameters_recognized() -> None:
    source = """
from orchestune.ledger.active_records import ActiveWorktree

def mutate_posonly(record: ActiveWorktree, /):
    record.claim = None
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    assert len(violations) == 1
    assert violations[0].target == "claim"
    assert violations[0].kind == "subrecord-direct-assign"


def test_unrelated_dto_with_method_not_treated_as_active() -> None:
    source = """
from dataclasses import dataclass

@dataclass
class OtherDTO:
    claim: str
    def with_core(self, core):
        return self

def handle(dto: OtherDTO, core):
    record = dto.with_core(core)
    record.claim = "safe"
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    assert not violations


def test_rebound_local_variable_clears_stale_classification() -> None:
    source = """
from dataclasses import dataclass
from orchestune.ledger.active_records import ActiveWorktree

@dataclass
class OtherDTO:
    claim: str

def rebind_payload(active: ActiveWorktree):
    payload = active.completion.completion_payload
    payload = {}
    payload["safe"] = 1

def rebind_record(active: ActiveWorktree):
    record = active
    record = OtherDTO("safe")
    record.claim = "safe"
"""
    violations = active_worktree_boundary_violations(
        source, module="orchestune.dispatch.scoring"
    )
    assert not violations


def test_exception_requires_reason() -> None:
    with pytest.raises(ValueError, match="reason must not be empty"):
        ActiveWorktreeBoundaryException(
            module="orchestune.dispatch.rebase",
            function="_apply_auto_rebase",
            target="launch",
            kind="subrecord-direct-assign",
            reason="",
        )
    with pytest.raises(ValueError, match="reason must not be empty"):
        ActiveWorktreeBoundaryException(
            module="orchestune.dispatch.rebase",
            function="_apply_auto_rebase",
            target="launch",
            kind="subrecord-direct-assign",
            reason="   ",
        )


def test_production_active_worktree_boundary_clean() -> None:
    assert production_active_worktree_violations(REPO_ROOT) == ()
    assert unused_active_worktree_exceptions(REPO_ROOT) == ()
