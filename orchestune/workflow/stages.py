"""Immutable stage metadata and strict loading of the versioned package resource.

The TOML describes guidance only. Executable checks and lifecycle state belong to
their existing owners; loading definitions never executes actions or writes state.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import PurePosixPath
from urllib.parse import urlsplit

_STAGE_IDS = frozenset(
    {
        "prepare",
        "claim",
        "scope",
        "implement",
        "pr",
        "review",
        "complete",
        "blocked",
        "handed-off",
    }
)
_GATES = frozenset({"pre-claim", "pre-edit", "pre-pr", "pre-complete"})
_CONDITIONS = frozenset(
    {
        "context_incomplete",
        "context_recorded",
        "footprint_valid",
        "reviewer_selected",
        "plan_approved",
        "claim_current",
        "impact_initial",
        "implementation_ready",
        "ci_current",
        "worktree_clean",
        "pr_body_ready",
        "base_correct",
        "pr_current",
        "review_acquired",
        "findings_judged",
        "review_current",
        "reply_posted",
        "completion_ready",
        "terminal_receipt",
    }
)
_ACTIONS = frozenset(
    {
        "prepare",
        "claim",
        "scope",
        "implement",
        "create_pr",
        "review",
        "complete",
        "recover",
        "inspect_handoff",
        "rerun_ci",
        "rereview",
        "reconcile",
    }
)
_ARTIFACTS = frozenset(
    {
        "plan",
        "state",
        "footprint",
        "claim",
        "impact_initial",
        "baseline",
        "reproducer",
        "ci",
        "pr_body",
        "pr",
        "review_result",
        "review_reply",
        "outcome",
    }
)
_BRANCHES = {
    "mode": frozenset({"interactive", "auto"}),
    "change_kind": frozenset({"code", "docs"}),
    "backend": frozenset({"gh", "mcp"}),
}


@dataclass(frozen=True)
class StageAction:
    """Display-only action; reason codes map failure guidance to diagnostics."""

    id: str
    description: str
    references: tuple[str, ...]
    argv: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageBranches:
    """Enumerated applicability predicates; no expressions are evaluated."""

    mode: tuple[str, ...]
    change_kind: tuple[str, ...]
    backend: tuple[str, ...]


@dataclass(frozen=True)
class StageDefinition:
    """One stage of the advisory workflow, ordered by its explicit order."""

    id: str
    order: int
    gate: str
    entry_conditions: tuple[str, ...]
    actions: tuple[StageAction, ...]
    exit_conditions: tuple[str, ...]
    on_failure: tuple[StageAction, ...]
    references: tuple[str, ...]
    artifacts: tuple[str, ...]
    branches: StageBranches


def _table(value: object, required: set[str], optional: set[str], where: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected a table")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing or unknown:
        raise ValueError(
            f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}"
        )
    return value


def _text(value: object, where: str, allowed: frozenset[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: expected a nonempty string")
    if allowed is not None and value not in allowed:
        raise ValueError(f"{where}: unknown ID {value!r}")
    return value


def _strings(
    value: object,
    where: str,
    allowed: frozenset[str] | None = None,
    *,
    nonempty: bool = True,
    unique: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{where}: expected a {'nonempty ' if nonempty else ''}array")
    items = tuple(_text(item, where, allowed) for item in value)
    if unique and len(set(items)) != len(items):
        raise ValueError(f"{where}: duplicate values")
    return items


def _references(value: object, where: str) -> tuple[str, ...]:
    refs = _strings(value, where)
    for ref in refs:
        parsed = urlsplit(ref)
        if parsed.scheme == "https" and parsed.netloc:
            continue
        path = PurePosixPath(parsed.path)
        if (
            parsed.scheme
            or parsed.netloc
            or not parsed.path
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in ref
        ):
            raise ValueError(
                f"{where}: expected a repository-relative path or HTTPS URL: {ref!r}"
            )
    return refs


def _action(value: object, where: str, *, failure: bool) -> StageAction:
    required = {"id", "description", "references"}
    if failure:
        required.add("reason_codes")
    data = _table(value, required, {"argv", "reason_codes"}, where)
    return StageAction(
        id=_text(data["id"], f"{where}.id", _ACTIONS),
        description=_text(data["description"], f"{where}.description"),
        references=_references(data["references"], f"{where}.references"),
        argv=_strings(
            data.get("argv", []), f"{where}.argv", nonempty=False, unique=False
        ),
        reason_codes=_strings(
            data.get("reason_codes", []), f"{where}.reason_codes", nonempty=failure
        ),
    )


def _actions(
    value: object, where: str, *, failure: bool = False
) -> tuple[StageAction, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where}: expected a nonempty array of actions")
    return tuple(
        _action(item, f"{where}[{index}]", failure=failure)
        for index, item in enumerate(value)
    )


def _stage(value: object, index: int) -> StageDefinition:
    where = f"stage[{index}]"
    data = _table(value, set(StageDefinition.__dataclass_fields__), set(), where)
    stage_id = _text(data["id"], f"{where}.id", _STAGE_IDS)
    order = data["order"]
    if isinstance(order, bool) or not isinstance(order, int) or order <= 0:
        raise ValueError(f"{where}.order: expected a positive integer")
    gate = data["gate"]
    if not isinstance(gate, str) or (gate and gate not in _GATES):
        raise ValueError(f"{where}.gate: unknown gate {gate!r}")
    branch = _table(data["branches"], set(_BRANCHES), set(), f"{where}.branches")
    branches = StageBranches(
        **{
            name: _strings(branch[name], f"{where}.branches.{name}", allowed)
            for name, allowed in _BRANCHES.items()
        }
    )
    return StageDefinition(
        id=stage_id,
        order=order,
        gate=gate,
        entry_conditions=_strings(
            data["entry_conditions"], f"{where}.entry_conditions", _CONDITIONS
        ),
        actions=_actions(data["actions"], f"{where}.actions"),
        exit_conditions=_strings(
            data["exit_conditions"], f"{where}.exit_conditions", _CONDITIONS
        ),
        on_failure=_actions(data["on_failure"], f"{where}.on_failure", failure=True),
        references=_references(data["references"], f"{where}.references"),
        artifacts=_strings(data["artifacts"], f"{where}.artifacts", _ARTIFACTS),
        branches=branches,
    )


def _validate_collection(stages: tuple[StageDefinition, ...]) -> None:
    for field in ("id", "order", "gate"):
        values = [
            getattr(stage, field) for stage in stages if field != "gate" or stage.gate
        ]
        if len(values) != len(set(values)):
            raise ValueError(f"stage: duplicate {field}")
    if {stage.id for stage in stages} != _STAGE_IDS:
        raise ValueError("stage: expected all nine schema-version-1 stage IDs")
    if {stage.gate for stage in stages if stage.gate} != _GATES:
        raise ValueError("stage.gate: expected all four gates")
    for stage in stages:
        if stage.id in {"blocked", "handed-off"} and stage.exit_conditions != (
            "terminal_receipt",
        ):
            raise ValueError(
                f"stage {stage.id}: exit_conditions must be terminal_receipt"
            )


def load_stage_definitions() -> tuple[StageDefinition, ...]:
    """Load schema v1 metadata from package data, sorted by order.

    Raise ValueError for missing/malformed resources or schema violations. No
    permissive fallback, caching, state writes, or action execution is performed.
    """
    try:
        content = (
            resources.files("orchestune.workflow")
            .joinpath("stages.toml")
            .read_text(encoding="utf-8")
        )
        document = tomllib.loads(content)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Cannot load workflow stages.toml: {exc}") from exc
    data = _table(document, {"schema_version", "stage"}, set(), "stages.toml")
    version = data["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError("stages.toml.schema_version: only version 1 is supported")
    if not isinstance(data["stage"], list) or not data["stage"]:
        raise ValueError("stages.toml.stage: expected a nonempty array")
    stages = tuple(_stage(value, index) for index, value in enumerate(data["stage"]))
    _validate_collection(stages)
    return tuple(sorted(stages, key=lambda stage: stage.order))
