"""Protocols, strict JSON codecs, schemas, and digests for local trial (#1734)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import AbstractContextManager as ContextManager
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from orchestune.local_trial.models import (
    SCHEMA_VERSION,
    AgentInfo,
    AgentPayload,
    AgentRequest,
    AttemptInput,
    AttemptKind,
    AttemptResult,
    Budget,
    Checkpoint,
    ControllerInfo,
    Decision,
    DecisionValue,
    DisciplineStatus,
    Evidence,
    Finding,
    FindingAction,
    FindingResponse,
    FindingSeverity,
    GitIntent,
    GitOperation,
    ImplementationPayload,
    JsonValue,
    Observation,
    PlanPayload,
    ProcessRecord,
    ProcessStatus,
    RepositoryIdentity,
    RequestSnapshot,
    ReviewPayload,
    ReviewVerdict,
    StopConfirmation,
    TrialPhase,
    TrialPlan,
    TrialState,
    WorkspaceIdentity,
    WorkspaceSnapshot,
)

KNOWN_TRIAL_ERROR_CODES: frozenset[str] = frozenset(
    (
        "invalid_input not_found state_invalid schema_unsupported stale_revision "
        "already_active execution_unknown repository_invalid identity_mismatch "
        "source_dirty exclude_unwritable base_changed head_changed out_of_scope "
        "worktree_dirty commit_identity_missing no_changes agent_unavailable "
        "agent_failed artifact_invalid ci_missing config_invalid ci_failed "
        "review_failed approval_required approval_stale discipline_incompatible "
        "discipline_unverified budget_exhausted interrupted"
    ).split()
)

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_OID_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


class TrialError(Exception):
    """Domain exception for local trial errors."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@runtime_checkable
class AgentRunner(Protocol):
    def detect(self) -> tuple[AgentInfo, ...]: ...
    def run(self, request: AgentRequest, agent: AgentInfo) -> AttemptResult: ...


@runtime_checkable
class TrialStore(Protocol):
    def lock(self) -> ContextManager[None]: ...
    def active_trial_id(self) -> str | None: ...
    def reserve(self, state: TrialState) -> None: ...
    def load(self, trial_id: str) -> TrialState: ...
    def save(self, state: TrialState, *, expected_revision: int | None) -> None: ...
    def record_attempt(self, result: AttemptResult) -> None: ...
    def load_attempt(self, trial_id: str, attempt_id: str) -> AttemptResult | None: ...
    def close(self, state: TrialState) -> None: ...


@runtime_checkable
class TrialWorkspace(Protocol):
    def storage_roots(self, repo: str) -> tuple[str, str]: ...
    def discover(self, repo: str) -> RepositoryIdentity: ...
    def paths(self, repo: RepositoryIdentity, trial_id: str) -> WorkspaceIdentity: ...
    def ensure_storage_ignored(self, repo: RepositoryIdentity) -> None: ...
    def prepare(
        self, repo: RepositoryIdentity, workspace: WorkspaceIdentity
    ) -> None: ...
    def inspect(
        self, repo: RepositoryIdentity, workspace: WorkspaceIdentity
    ) -> WorkspaceSnapshot: ...
    def stage_commit(self, state: TrialState) -> GitIntent: ...
    def finish_commit(self, state: TrialState, intent: GitIntent) -> str: ...
    def reconcile_commit(self, state: TrialState, intent: GitIntent) -> str | None: ...
    def prepare_review(self, state: TrialState) -> None: ...
    def inspect_review(self, state: TrialState) -> WorkspaceSnapshot: ...
    def diff(self, state: TrialState) -> str: ...
    def working_diff(self, state: TrialState) -> str: ...
    def includes_head(self, state: TrialState) -> bool: ...


def canonical_digest(data: JsonValue) -> str:
    """Canonical sha256 digest of JSON value."""
    serialized = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def request_digest(text: str) -> str:
    """UTF-8 bytes sha256 hex digest, without newline conversion."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan_digest(plan: TrialPlan, request_digest_val: str, base_sha: str) -> str:
    """Compute plan digest over plan content, request digest, and base sha."""
    obj: dict[str, JsonValue] = {
        "base_sha": base_sha.lower(),
        "plan": _encode_plan(plan),
        "request_digest": request_digest_val,
    }
    return canonical_digest(obj)


def workspace_digest(snapshot: WorkspaceSnapshot) -> str:
    """Compute workspace digest from snapshot fields."""
    return canonical_digest(_encode_workspace_snapshot(snapshot))


# --- Strict Validation Helpers ---


def _req_dict(val: object, expected: set[str], name: str = "object") -> dict[str, Any]:
    if not isinstance(val, dict):
        raise TrialError("invalid_input", f"{name} must be a JSON object")
    keys = set(val.keys())
    if keys != expected:
        diff_m = expected - keys
        msg = (
            f"{name} missing required keys: {sorted(diff_m)}"
            if diff_m
            else f"{name} contains unknown keys: {sorted(keys - expected)}"
        )
        raise TrialError("invalid_input", msg)
    return val


def _req_str(val: object, name: str) -> str:
    if not isinstance(val, str):
        raise TrialError("invalid_input", f"{name} must be a string")
    return val


def _req_id(val: object, name: str) -> str:
    s = _req_str(val, name)
    if not _ID_RE.fullmatch(s):
        raise TrialError(
            "invalid_input", f"{name} must be a 32-character lowercase hex string"
        )
    return s


def _req_oid(val: object, name: str) -> str:
    s = _req_str(val, name).lower()
    if not _OID_RE.fullmatch(s):
        raise TrialError(
            "invalid_input", f"{name} must be a 40- or 64-character hex string"
        )
    return s


def _req_oid_opt(val: object, name: str) -> str | None:
    return _req_oid(val, name) if val is not None else None


def _req_int(val: object, name: str, min_val: int | None = None) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise TrialError("invalid_input", f"{name} must be an integer")
    if min_val is not None and val < min_val:
        raise TrialError("invalid_input", f"{name} must be >= {min_val}")
    return val


def _req_float(val: object, name: str) -> float:
    if isinstance(val, bool) or not isinstance(val, int | float):
        raise TrialError("invalid_input", f"{name} must be a float")
    if not math.isfinite(val):
        raise TrialError("invalid_input", f"{name} must be finite")
    return float(val)


def _req_bool(val: object, name: str) -> bool:
    if not isinstance(val, bool):
        raise TrialError("invalid_input", f"{name} must be a boolean")
    return val


def _req_enum[T: StrEnum](val: object, enum_cls: type[T], name: str) -> T:
    s = _req_str(val, name)
    try:
        return enum_cls(s)
    except ValueError:
        raise TrialError(
            "invalid_input", f"{name} has invalid enum value: {s!r}"
        ) from None


def _req_str_tuple(val: object, name: str) -> tuple[str, ...]:
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise TrialError("invalid_input", f"{name} must be a list of strings")
    return tuple(val)


# --- DTO Encoders ---


def _encode_plan(plan: TrialPlan) -> dict[str, JsonValue]:
    return {
        "goal": plan.goal,
        "acceptance_criteria": list(plan.acceptance_criteria),
        "write_paths": list(plan.write_paths),
        "discipline_paths": list(plan.discipline_paths),
        "discipline_notes": list(plan.discipline_notes),
        "ci_argv": list(plan.ci_argv) if plan.ci_argv is not None else None,
        "discipline_status": str(plan.discipline_status),
    }


def _encode_workspace_snapshot(snap: WorkspaceSnapshot) -> dict[str, JsonValue]:
    return {
        "head_sha": snap.head_sha.lower(),
        "base_tip_sha": snap.base_tip_sha.lower(),
        "index_digest": snap.index_digest,
        "content_digest": snap.content_digest,
        "branch": snap.branch,
        "changes": [
            {"status": c.status, "path": c.path, "old_path": c.old_path}
            for c in snap.changes
        ],
    }


def _encode_attempt_input(inp: AttemptInput) -> dict[str, JsonValue]:
    return {
        "trial_id": inp.trial_id,
        "attempt_id": inp.attempt_id,
        "kind": str(inp.kind),
        "input_head_sha": inp.input_head_sha.lower(),
        "base_sha": inp.base_sha.lower(),
        "plan_digest": inp.plan_digest,
        "plan_revision": inp.plan_revision,
        "cwd": inp.cwd,
        "artifact_dir": inp.artifact_dir,
        "started_at": inp.started_at,
    }


def _encode_evidence(ev: Evidence) -> dict[str, JsonValue]:
    return {
        "attempt_id": ev.attempt_id,
        "base_sha": ev.base_sha.lower(),
        "head_sha": ev.head_sha.lower(),
        "plan_digest": ev.plan_digest,
        "ci_argv_digest": ev.ci_argv_digest,
        "passed": ev.passed,
    }


def _encode_agent_inner(
    inner: PlanPayload | ImplementationPayload | ReviewPayload,
) -> dict[str, JsonValue]:
    if isinstance(inner, PlanPayload):
        return {"plan": _encode_plan(inner.plan)}
    if isinstance(inner, ImplementationPayload):
        resps = [
            {
                "finding_id": r.finding_id,
                "action": str(r.action),
                "explanation": r.explanation,
            }
            for r in inner.responses
        ]
        return {"summary": inner.summary, "responses": resps}
    if isinstance(inner, ReviewPayload):
        findings = [
            {
                "id": f.id,
                "severity": str(f.severity),
                "path": f.path,
                "line": f.line,
                "reason": f.reason,
                "requested_change": f.requested_change,
            }
            for f in inner.findings
        ]
        return {"verdict": str(inner.verdict), "findings": findings}
    raise TrialError("invalid_input", f"Unknown payload type: {type(inner)}")


def _encode_agent_payload(payload: AgentPayload | None) -> dict[str, JsonValue] | None:
    if payload is None:
        return None
    return {
        "schema_version": payload.schema_version,
        "trial_id": payload.trial_id,
        "attempt_id": payload.attempt_id,
        "kind": str(payload.kind),
        "input_head_sha": payload.input_head_sha.lower(),
        "plan_digest": payload.plan_digest,
        "payload": _encode_agent_inner(payload.payload),
    }


def encode_attempt(result: AttemptResult) -> dict[str, JsonValue]:
    """Encode AttemptResult to JSON-compatible dict."""
    inp = result.input
    proc = result.process
    return {
        "schema_version": result.schema_version,
        "input": _encode_attempt_input(inp),
        "process": {
            "outcome": str(proc.outcome),
            "returncode": proc.returncode,
            "stop_confirmed": proc.stop_confirmed,
            "elapsed_seconds": proc.elapsed_seconds,
            "timeout_seconds": proc.timeout_seconds,
            "stdout_tail": proc.stdout_tail,
            "stderr_tail": proc.stderr_tail,
            "detail": proc.detail,
        },
        "agent_payload": _encode_agent_payload(result.agent_payload),
        "ended_at": result.ended_at,
    }


def _encode_git_intent(intent: GitIntent | None) -> dict[str, JsonValue] | None:
    if intent is None:
        return None
    tree_sha = intent.expected_tree_sha.lower() if intent.expected_tree_sha else None
    return {
        "operation": str(intent.operation),
        "pre_head_sha": intent.pre_head_sha.lower(),
        "expected_tree_sha": tree_sha,
        "target_sha": intent.target_sha.lower(),
        "allowed_paths": list(intent.allowed_paths),
    }


def _encode_repo_dict(r: RepositoryIdentity) -> dict[str, JsonValue]:
    return {
        "source_root": r.source_root,
        "primary_root": r.primary_root,
        "common_dir": r.common_dir,
        "base_ref": r.base_ref,
        "base_sha": r.base_sha.lower(),
    }


def _encode_ws_dict(w: WorkspaceIdentity) -> dict[str, JsonValue]:
    return {
        "implementation_path": w.implementation_path,
        "review_path": w.review_path,
        "branch": w.branch,
    }


def _encode_state_identities(state: TrialState) -> dict[str, JsonValue]:
    ci_override = (
        list(state.request.ci_argv_override)
        if state.request.ci_argv_override is not None
        else None
    )
    return {
        "schema_version": state.schema_version,
        "trial_id": state.trial_id,
        "repository": _encode_repo_dict(state.repository),
        "workspace": _encode_ws_dict(state.workspace),
        "request": {
            "text": state.request.text,
            "digest": state.request.digest,
            "ci_argv_override": ci_override,
        },
        "agent": {
            "name": str(state.agent.name),
            "executable": state.agent.executable,
            "version": state.agent.version,
        },
        "plan": _encode_plan(state.plan) if state.plan else None,
        "approved_plan_digest": state.approved_plan_digest,
        "approved_worktree_digest": state.approved_worktree_digest,
        "approved_index_digest": state.approved_index_digest,
    }


def _encode_state_runtime(state: TrialState) -> dict[str, JsonValue]:
    return {
        "phase": str(state.phase),
        "checkpoint": str(state.checkpoint),
        "reason": state.reason,
        "revision": state.revision,
        "budget": {
            "plan_revision": state.budget.plan_revision,
            "cycles_started": state.budget.cycles_started,
            "reviews_started": state.budget.reviews_started,
        },
        "head_sha": state.head_sha.lower(),
        "pending_attempt": _encode_attempt_input(state.pending_attempt)
        if state.pending_attempt
        else None,
        "pending_git": _encode_git_intent(state.pending_git),
        "attempt_ids": list(state.attempt_ids),
        "ci_evidence": _encode_evidence(state.ci_evidence)
        if state.ci_evidence
        else None,
        "review_evidence": _encode_evidence(state.review_evidence)
        if state.review_evidence
        else None,
    }


def _encode_state_records(state: TrialState) -> dict[str, JsonValue]:
    ctrl = state.controller
    ctrl_dict = (
        {"pid": ctrl.pid, "started_at": ctrl.started_at, "token": ctrl.token}
        if ctrl
        else None
    )
    dec = state.decision
    dec_dict = (
        {"value": str(dec.value), "recorded_at": dec.recorded_at} if dec else None
    )
    return {
        "findings": [
            {
                "id": f.id,
                "severity": str(f.severity),
                "path": f.path,
                "line": f.line,
                "reason": f.reason,
                "requested_change": f.requested_change,
            }
            for f in state.findings
        ],
        "responses": [
            {
                "finding_id": r.finding_id,
                "action": str(r.action),
                "explanation": r.explanation,
            }
            for r in state.responses
        ],
        "stop_confirmations": [
            {"attempt_id": sc.attempt_id, "recorded_at": sc.recorded_at}
            for sc in state.stop_confirmations
        ],
        "controller": ctrl_dict,
        "observation": {
            "started_at": state.observation.started_at,
            "updated_at": state.observation.updated_at,
            "elapsed_seconds": state.observation.elapsed_seconds,
            "setup_steps": list(state.observation.setup_steps),
            "notes": list(state.observation.notes),
        },
        "decision": dec_dict,
    }


def encode_state(state: TrialState) -> dict[str, JsonValue]:
    """Encode TrialState to JSON-compatible dict."""
    return {
        **_encode_state_identities(state),
        **_encode_state_runtime(state),
        **_encode_state_records(state),
    }


# --- Decoders ---


def _decode_agent_inner_payload(
    kind: AttemptKind, payload_data: object
) -> PlanPayload | ImplementationPayload | ReviewPayload:
    if kind == AttemptKind.PLAN:
        p_dict = _req_dict(payload_data, {"plan"}, "PlanPayload")
        return PlanPayload(plan=_decode_plan(p_dict["plan"]))
    if kind in (AttemptKind.IMPLEMENT, AttemptKind.FIX):
        i_dict = _req_dict(
            payload_data, {"summary", "responses"}, "ImplementationPayload"
        )
        summary = _req_str(i_dict["summary"], "summary")
        return ImplementationPayload(
            summary=summary, responses=_decode_responses(i_dict["responses"])
        )
    if kind == AttemptKind.REVIEW:
        r_dict = _req_dict(payload_data, {"verdict", "findings"}, "ReviewPayload")
        verdict = _req_enum(r_dict["verdict"], ReviewVerdict, "verdict")
        findings = _decode_findings(r_dict["findings"])
        if len(findings) > 0 and verdict == ReviewVerdict.PASS:
            raise TrialError(
                "invalid_input",
                "Review with findings must have verdict changes_requested",
            )
        return ReviewPayload(verdict=verdict, findings=findings)
    raise TrialError("invalid_input", f"Unsupported attempt kind: {kind}")


def decode_agent_payload(data: object) -> AgentPayload:
    """Strictly decode an agent payload dictionary."""
    keys = set(
        "schema_version trial_id attempt_id kind input_head_sha plan_digest payload".split()
    )
    d = _req_dict(data, keys, "AgentPayload")
    version = _req_int(d["schema_version"], "schema_version")
    if version != SCHEMA_VERSION:
        raise TrialError("schema_unsupported", f"Unsupported schema version: {version}")

    kind = _req_enum(d["kind"], AttemptKind, "kind")
    if kind == AttemptKind.CI:
        raise TrialError("invalid_input", "CI attempts cannot have agent payloads")

    return AgentPayload(
        schema_version=version,
        trial_id=_req_id(d["trial_id"], "trial_id"),
        attempt_id=_req_id(d["attempt_id"], "attempt_id"),
        kind=kind,
        input_head_sha=_req_oid(d["input_head_sha"], "input_head_sha"),
        plan_digest=_req_str(d["plan_digest"], "plan_digest")
        if d["plan_digest"] is not None
        else None,
        payload=_decode_agent_inner_payload(kind, d["payload"]),
    )


def _decode_plan(data: object) -> TrialPlan:
    keys = set(
        "goal acceptance_criteria write_paths discipline_paths discipline_notes ci_argv discipline_status".split()
    )
    d = _req_dict(data, keys, "TrialPlan")
    ci = _req_str_tuple(d["ci_argv"], "ci_argv") if d["ci_argv"] is not None else None
    return TrialPlan(
        goal=_req_str(d["goal"], "goal"),
        acceptance_criteria=_req_str_tuple(
            d["acceptance_criteria"], "acceptance_criteria"
        ),
        write_paths=_req_str_tuple(d["write_paths"], "write_paths"),
        discipline_paths=_req_str_tuple(d["discipline_paths"], "discipline_paths"),
        discipline_notes=_req_str_tuple(d["discipline_notes"], "discipline_notes"),
        ci_argv=ci,
        discipline_status=_req_enum(
            d["discipline_status"], DisciplineStatus, "discipline_status"
        ),
    )


def _decode_responses(data: object) -> tuple[FindingResponse, ...]:
    if not isinstance(data, list):
        raise TrialError("invalid_input", "responses must be a list")
    out: list[FindingResponse] = []
    for item in data:
        r = _req_dict(item, {"finding_id", "action", "explanation"}, "FindingResponse")
        out.append(
            FindingResponse(
                finding_id=_req_str(r["finding_id"], "finding_id"),
                action=_req_enum(r["action"], FindingAction, "action"),
                explanation=_req_str(r["explanation"], "explanation"),
            )
        )
    return tuple(out)


def _decode_finding_item(f_dict: dict[str, Any], seen_ids: set[str]) -> Finding:
    fid = _req_str(f_dict["id"], "id")
    if not fid.strip():
        raise TrialError("invalid_input", "finding id cannot be empty")
    if fid in seen_ids:
        raise TrialError("invalid_input", f"duplicate finding id: {fid}")
    seen_ids.add(fid)

    path = _req_str(f_dict["path"], "path")
    if not path.strip():
        raise TrialError("invalid_input", "finding path cannot be empty")
    reason = _req_str(f_dict["reason"], "reason")
    if not reason.strip():
        raise TrialError("invalid_input", "finding reason cannot be empty")

    line = (
        _req_int(f_dict["line"], "line", min_val=1)
        if f_dict["line"] is not None
        else None
    )
    return Finding(
        id=fid,
        severity=_req_enum(f_dict["severity"], FindingSeverity, "severity"),
        path=path,
        line=line,
        reason=reason,
        requested_change=_req_str(f_dict["requested_change"], "requested_change"),
    )


def _decode_findings(data: object) -> tuple[Finding, ...]:
    if not isinstance(data, list):
        raise TrialError("invalid_input", "findings must be a list")
    out: list[Finding] = []
    seen_ids: set[str] = set()
    for item in data:
        f_dict = _req_dict(
            item,
            set("id severity path line reason requested_change".split()),
            "Finding",
        )
        out.append(_decode_finding_item(f_dict, seen_ids))
    return tuple(out)


def _decode_attempt_input(data: object) -> AttemptInput:
    keys = set(
        "trial_id attempt_id kind input_head_sha base_sha plan_digest plan_revision cwd artifact_dir started_at".split()
    )
    d = _req_dict(data, keys, "AttemptInput")
    p_digest = (
        _req_str(d["plan_digest"], "plan_digest")
        if d["plan_digest"] is not None
        else None
    )
    return AttemptInput(
        trial_id=_req_id(d["trial_id"], "trial_id"),
        attempt_id=_req_id(d["attempt_id"], "attempt_id"),
        kind=_req_enum(d["kind"], AttemptKind, "kind"),
        input_head_sha=_req_oid(d["input_head_sha"], "input_head_sha"),
        base_sha=_req_oid(d["base_sha"], "base_sha"),
        plan_digest=p_digest,
        plan_revision=_req_int(d["plan_revision"], "plan_revision", min_val=0),
        cwd=_req_str(d["cwd"], "cwd"),
        artifact_dir=_req_str(d["artifact_dir"], "artifact_dir"),
        started_at=_req_str(d["started_at"], "started_at"),
    )


def _decode_process_record(data: object) -> ProcessRecord:
    keys = set(
        "outcome returncode stop_confirmed elapsed_seconds timeout_seconds stdout_tail stderr_tail detail".split()
    )
    d = _req_dict(data, keys, "ProcessRecord")
    ret = (
        _req_int(d["returncode"], "returncode") if d["returncode"] is not None else None
    )
    stop = (
        _req_bool(d["stop_confirmed"], "stop_confirmed")
        if d["stop_confirmed"] is not None
        else None
    )
    return ProcessRecord(
        outcome=_req_enum(d["outcome"], ProcessStatus, "outcome"),
        returncode=ret,
        stop_confirmed=stop,
        elapsed_seconds=_req_float(d["elapsed_seconds"], "elapsed_seconds"),
        timeout_seconds=_req_float(d["timeout_seconds"], "timeout_seconds"),
        stdout_tail=_req_str(d["stdout_tail"], "stdout_tail"),
        stderr_tail=_req_str(d["stderr_tail"], "stderr_tail"),
        detail=_req_str(d["detail"], "detail"),
    )


def decode_attempt(data: object) -> AttemptResult:
    """Strictly decode an AttemptResult object."""
    keys = set("schema_version input process agent_payload ended_at".split())
    d = _req_dict(data, keys, "AttemptResult")
    version = _req_int(d["schema_version"], "schema_version")
    if version != SCHEMA_VERSION:
        raise TrialError("schema_unsupported", f"Unsupported schema version: {version}")

    inp = _decode_attempt_input(d["input"])
    proc = _decode_process_record(d["process"])
    p_data = d["agent_payload"]
    if inp.kind == AttemptKind.CI and p_data is not None:
        raise TrialError("invalid_input", "CI attempt must have agent_payload as null")

    return AttemptResult(
        schema_version=version,
        input=inp,
        process=proc,
        agent_payload=decode_agent_payload(p_data) if p_data is not None else None,
        ended_at=_req_str(d["ended_at"], "ended_at"),
    )


def _decode_repository(repo_raw: object) -> RepositoryIdentity:
    d = _req_dict(
        repo_raw,
        set("source_root primary_root common_dir base_ref base_sha".split()),
        "RepositoryIdentity",
    )
    return RepositoryIdentity(
        source_root=_req_str(d["source_root"], "source_root"),
        primary_root=_req_str(d["primary_root"], "primary_root"),
        common_dir=_req_str(d["common_dir"], "common_dir"),
        base_ref=_req_str(d["base_ref"], "base_ref"),
        base_sha=_req_oid(d["base_sha"], "base_sha"),
    )


def _decode_workspace(ws_raw: object) -> WorkspaceIdentity:
    d = _req_dict(
        ws_raw,
        set("implementation_path review_path branch".split()),
        "WorkspaceIdentity",
    )
    return WorkspaceIdentity(
        implementation_path=_req_str(d["implementation_path"], "implementation_path"),
        review_path=_req_str(d["review_path"], "review_path"),
        branch=_req_str(d["branch"], "branch"),
    )


def _decode_request(req_raw: object) -> RequestSnapshot:
    d = _req_dict(req_raw, {"text", "digest", "ci_argv_override"}, "RequestSnapshot")
    ci = (
        _req_str_tuple(d["ci_argv_override"], "ci_argv_override")
        if d["ci_argv_override"] is not None
        else None
    )
    return RequestSnapshot(
        text=_req_str(d["text"], "text"),
        digest=_req_str(d["digest"], "digest"),
        ci_argv_override=ci,
    )


def _decode_agent(agent_raw: object) -> AgentInfo:
    d = _req_dict(agent_raw, {"name", "executable", "version"}, "AgentInfo")
    name = _req_str(d["name"], "name")
    if name not in ("claude", "codex"):
        raise TrialError(
            "invalid_input", f"Agent name must be claude or codex, got: {name!r}"
        )
    return AgentInfo(
        name=name,
        executable=_req_str(d["executable"], "executable"),
        version=_req_str(d["version"], "version"),
    )


def _decode_budget(budget_raw: object) -> Budget:
    d = _req_dict(
        budget_raw,
        set("plan_revision cycles_started reviews_started".split()),
        "Budget",
    )
    return Budget(
        plan_revision=_req_int(d["plan_revision"], "plan_revision", min_val=0),
        cycles_started=_req_int(d["cycles_started"], "cycles_started", min_val=0),
        reviews_started=_req_int(d["reviews_started"], "reviews_started", min_val=0),
    )


def _decode_git_intent(git_raw: object) -> GitIntent | None:
    if git_raw is None:
        return None
    keys = set(
        "operation pre_head_sha expected_tree_sha target_sha allowed_paths".split()
    )
    d = _req_dict(git_raw, keys, "GitIntent")
    return GitIntent(
        operation=_req_enum(d["operation"], GitOperation, "operation"),
        pre_head_sha=_req_oid(d["pre_head_sha"], "pre_head_sha"),
        expected_tree_sha=_req_oid_opt(d["expected_tree_sha"], "expected_tree_sha"),
        target_sha=_req_oid(d["target_sha"], "target_sha"),
        allowed_paths=_req_str_tuple(d["allowed_paths"], "allowed_paths"),
    )


def _decode_stop_confirmations(sc_raw: object) -> tuple[StopConfirmation, ...]:
    if not isinstance(sc_raw, list):
        raise TrialError("invalid_input", "stop_confirmations must be a list")
    out: list[StopConfirmation] = []
    for item in sc_raw:
        sc_d = _req_dict(item, {"attempt_id", "recorded_at"}, "StopConfirmation")
        out.append(
            StopConfirmation(
                attempt_id=_req_id(sc_d["attempt_id"], "attempt_id"),
                recorded_at=_req_str(sc_d["recorded_at"], "recorded_at"),
            )
        )
    return tuple(out)


def _decode_controller(ctrl_raw: object) -> ControllerInfo | None:
    if ctrl_raw is None:
        return None
    d = _req_dict(ctrl_raw, {"pid", "started_at", "token"}, "ControllerInfo")
    return ControllerInfo(
        pid=_req_int(d["pid"], "pid"),
        started_at=_req_str(d["started_at"], "started_at"),
        token=_req_str(d["token"], "token"),
    )


def _decode_observation(obs_raw: object) -> Observation:
    d = _req_dict(
        obs_raw,
        set("started_at updated_at elapsed_seconds setup_steps notes".split()),
        "Observation",
    )
    return Observation(
        started_at=_req_str(d["started_at"], "started_at"),
        updated_at=_req_str(d["updated_at"], "updated_at"),
        elapsed_seconds=_req_float(d["elapsed_seconds"], "elapsed_seconds"),
        setup_steps=_req_str_tuple(d["setup_steps"], "setup_steps"),
        notes=_req_str_tuple(d["notes"], "notes"),
    )


def _decode_decision(dec_raw: object) -> Decision | None:
    if dec_raw is None:
        return None
    d = _req_dict(dec_raw, {"value", "recorded_at"}, "Decision")
    return Decision(
        value=_req_enum(d["value"], DecisionValue, "value"),
        recorded_at=_req_str(d["recorded_at"], "recorded_at"),
    )


def _decode_state_identities(d: dict[str, Any]) -> dict[str, Any]:
    plan = _decode_plan(d["plan"]) if d["plan"] is not None else None
    return {
        "trial_id": _req_id(d["trial_id"], "trial_id"),
        "repository": _decode_repository(d["repository"]),
        "workspace": _decode_workspace(d["workspace"]),
        "request": _decode_request(d["request"]),
        "agent": _decode_agent(d["agent"]),
        "plan": plan,
        "approved_plan_digest": _req_str(
            d["approved_plan_digest"], "approved_plan_digest"
        )
        if d["approved_plan_digest"] is not None
        else None,
        "approved_worktree_digest": _req_str(
            d["approved_worktree_digest"], "approved_worktree_digest"
        )
        if d["approved_worktree_digest"] is not None
        else None,
        "approved_index_digest": _req_str(
            d["approved_index_digest"], "approved_index_digest"
        )
        if d["approved_index_digest"] is not None
        else None,
    }


def _decode_state_runtime(d: dict[str, Any]) -> dict[str, Any]:
    p_att = (
        _decode_attempt_input(d["pending_attempt"])
        if d["pending_attempt"] is not None
        else None
    )
    ci_ev = _decode_evidence(d["ci_evidence"]) if d["ci_evidence"] is not None else None
    rev_ev = (
        _decode_evidence(d["review_evidence"])
        if d["review_evidence"] is not None
        else None
    )
    return {
        "phase": _req_enum(d["phase"], TrialPhase, "phase"),
        "checkpoint": _req_enum(d["checkpoint"], Checkpoint, "checkpoint"),
        "reason": _req_str(d["reason"], "reason") if d["reason"] is not None else None,
        "revision": _req_int(d["revision"], "revision", min_val=0),
        "budget": _decode_budget(d["budget"]),
        "head_sha": _req_oid(d["head_sha"], "head_sha"),
        "pending_attempt": p_att,
        "pending_git": _decode_git_intent(d["pending_git"]),
        "attempt_ids": _req_str_tuple(d["attempt_ids"], "attempt_ids"),
        "ci_evidence": ci_ev,
        "review_evidence": rev_ev,
    }


def _decode_state_records(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "findings": _decode_findings(d["findings"]),
        "responses": _decode_responses(d["responses"]),
        "stop_confirmations": _decode_stop_confirmations(d["stop_confirmations"]),
        "controller": _decode_controller(d["controller"]),
        "observation": _decode_observation(d["observation"]),
        "decision": _decode_decision(d["decision"]),
    }


def decode_state(data: object) -> TrialState:
    """Strictly decode a TrialState object."""
    expected = set(
        "schema_version trial_id repository workspace request agent plan "
        "approved_plan_digest approved_worktree_digest approved_index_digest "
        "phase checkpoint reason revision budget head_sha pending_attempt "
        "pending_git attempt_ids ci_evidence review_evidence findings responses "
        "stop_confirmations controller observation decision".split()
    )
    d = _req_dict(data, expected, "TrialState")
    version = _req_int(d["schema_version"], "schema_version")
    if version != SCHEMA_VERSION:
        raise TrialError("schema_unsupported", f"Unsupported schema version: {version}")

    fields = {
        "schema_version": version,
        **_decode_state_identities(d),
        **_decode_state_runtime(d),
        **_decode_state_records(d),
    }
    return TrialState(**fields)


def _decode_evidence(data: object) -> Evidence:
    keys = set("attempt_id base_sha head_sha plan_digest ci_argv_digest passed".split())
    d = _req_dict(data, keys, "Evidence")
    return Evidence(
        attempt_id=_req_id(d["attempt_id"], "attempt_id"),
        base_sha=_req_oid(d["base_sha"], "base_sha"),
        head_sha=_req_oid(d["head_sha"], "head_sha"),
        plan_digest=_req_str(d["plan_digest"], "plan_digest"),
        ci_argv_digest=_req_str(d["ci_argv_digest"], "ci_argv_digest"),
        passed=_req_bool(d["passed"], "passed"),
    )


# --- JSON Schema Generators ---


def _schema_obj(props: dict[str, Any], required: list[str]) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": props,
    }


def _schema_str_list() -> dict[str, JsonValue]:
    return {"type": "array", "items": {"type": "string"}}


def _plan_schema() -> dict[str, JsonValue]:
    str_arr = _schema_str_list()
    plan_props = {
        "goal": {"type": "string"},
        "acceptance_criteria": str_arr,
        "write_paths": str_arr,
        "discipline_paths": str_arr,
        "discipline_notes": str_arr,
        "ci_argv": {"type": ["array", "null"], "items": {"type": "string"}},
        "discipline_status": {
            "type": "string",
            "enum": ["compatible", "incompatible", "unverified"],
        },
    }
    return _schema_obj(
        {"plan": _schema_obj(plan_props, list(plan_props.keys()))}, ["plan"]
    )


def _implement_schema() -> dict[str, JsonValue]:
    resp_props = {
        "finding_id": {"type": "string"},
        "action": {"type": "string", "enum": ["fixed", "declined"]},
        "explanation": {"type": "string"},
    }
    resp_item = _schema_obj(resp_props, list(resp_props.keys()))
    props = {
        "summary": {"type": "string"},
        "responses": {"type": "array", "items": resp_item},
    }
    return _schema_obj(props, ["summary", "responses"])


def _review_schema() -> dict[str, JsonValue]:
    finding_props = {
        "id": {"type": "string"},
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "path": {"type": "string"},
        "line": {"type": ["integer", "null"]},
        "reason": {"type": "string"},
        "requested_change": {"type": "string"},
    }
    finding_item = _schema_obj(finding_props, list(finding_props.keys()))
    props = {
        "verdict": {"type": "string", "enum": ["pass", "changes_requested"]},
        "findings": {"type": "array", "items": finding_item},
    }
    return _schema_obj(props, ["verdict", "findings"])


def agent_payload_schema(kind: AttemptKind | str) -> dict[str, JsonValue]:
    """Return JSON Schema for the given agent attempt kind."""
    k = AttemptKind(kind) if isinstance(kind, str) else kind
    if k == AttemptKind.CI:
        raise TrialError(
            "invalid_input", "CI attempts do not have an agent payload schema"
        )
    if k == AttemptKind.PLAN:
        return _plan_schema()
    if k in (AttemptKind.IMPLEMENT, AttemptKind.FIX):
        return _implement_schema()
    if k == AttemptKind.REVIEW:
        return _review_schema()
    raise TrialError("invalid_input", f"Unsupported attempt kind: {kind}")
