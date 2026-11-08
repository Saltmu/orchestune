"""Prompt templates and discipline document injection for local trial (#1734, #1735)."""

from __future__ import annotations

import os
from pathlib import Path

from orchestune.local_trial.contracts import agent_payload_schema
from orchestune.local_trial.models import (
    AgentRequest,
    AttemptInput,
    AttemptKind,
    AttemptResult,
    Finding,
    FindingResponse,
    JsonValue,
    TrialPlan,
    TrialState,
)

_PLAN_INSTRUCTION = """\
You are an autonomous coding agent executing the planning stage of a local trial.
Your task:
1. Review the discipline guidelines and explicit local references listed in the context.
2. Read the request and determine the goal, acceptance criteria, write paths, and discipline notes.
3. Do NOT modify or write any production files during this planning stage.
4. If GitHub connectivity or credentials are strictly required and cannot be satisfied locally, set discipline_status to 'incompatible'. If discipline cannot be verified, set it to 'unverified'. Otherwise set it to 'compatible'.
5. Produce a valid structured output conforming to the required schema.\
"""

_IMPLEMENT_INSTRUCTION = """\
You are an autonomous coding agent executing the implementation stage of a local trial.
Your task:
1. Implement the planned changes strictly within the approved write_paths listed in the plan.
2. Do NOT touch files outside the approved write_paths.
3. Forbidden actions: do NOT run git commit, push, pr, stash, or reset; do NOT start background processes or subagents; do NOT edit state or ledger files.
4. CI execution and Git commits will be managed by the supervisor, so do not self-report CI evidence.
5. Produce a valid structured output conforming to the required schema with a summary of changes.\
"""

_FIX_INSTRUCTION = """\
You are an autonomous coding agent executing the fix stage of a local trial.
Your task:
1. Review the reported findings in the context and apply corrective changes strictly within approved write_paths.
2. For each finding in findings, provide a response with finding_id, action ('fixed' or 'declined'), and explanation.
3. Forbidden actions: do NOT run git commit, push, pr, stash, or reset; do NOT start background processes or subagents; do NOT edit state files.
4. Produce a valid structured output conforming to the required schema.\
"""

_REVIEW_INSTRUCTION = """\
You are an autonomous code reviewer executing the review stage of a local trial.
Your task:
1. Inspect the provided diff, request, acceptance criteria, CI results, and discipline requirements.
2. Do NOT modify any code or files in the repository.
3. Set verdict to 'pass' if and only if findings list is empty. If there are any findings, set verdict to 'changes_requested'.
4. Ensure each finding references a valid path and line number grounded in the diff or affected files.
5. Produce a valid structured output conforming to the required schema.\
"""


def _encode_plan_dict(plan: TrialPlan | None) -> dict[str, JsonValue] | None:
    if plan is None:
        return None
    return {
        "goal": plan.goal,
        "acceptance_criteria": list(plan.acceptance_criteria),
        "write_paths": list(plan.write_paths),
        "discipline_paths": list(plan.discipline_paths),
        "discipline_notes": list(plan.discipline_notes),
        "ci_argv": list(plan.ci_argv) if plan.ci_argv is not None else None,
        "discipline_status": str(plan.discipline_status),
    }


def _extract_ci_result(
    previous_results: tuple[AttemptResult, ...],
) -> dict[str, JsonValue] | None:
    for res in reversed(previous_results):
        if res.input.kind == AttemptKind.CI:
            return {
                "outcome": str(res.process.outcome),
                "returncode": res.process.returncode,
                "stop_confirmed": res.process.stop_confirmed,
                "elapsed_seconds": res.process.elapsed_seconds,
                "timeout_seconds": res.process.timeout_seconds,
                "stdout_tail": res.process.stdout_tail,
                "stderr_tail": res.process.stderr_tail,
                "detail": res.process.detail,
                "target_sha": res.input.input_head_sha,
            }
    return None


def _agent_full_schema(kind: AttemptKind) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer"},
            "trial_id": {"type": "string"},
            "attempt_id": {"type": "string"},
            "kind": {"type": "string", "enum": [str(kind)]},
            "input_head_sha": {"type": "string"},
            "plan_digest": {"type": ["string", "null"]},
            "payload": agent_payload_schema(kind),
        },
        "required": [
            "schema_version",
            "trial_id",
            "attempt_id",
            "kind",
            "input_head_sha",
            "plan_digest",
            "payload",
        ],
        "additionalProperties": False,
    }


def _resolve_context_rel_path(attempt: AttemptInput) -> str:
    try:
        return os.path.relpath(Path(attempt.artifact_dir) / "context.json", attempt.cwd)
    except ValueError:
        return str(Path(attempt.artifact_dir) / "context.json")


def _get_instruction(kind: AttemptKind) -> str:
    if kind == AttemptKind.PLAN:
        return _PLAN_INSTRUCTION
    if kind == AttemptKind.IMPLEMENT:
        return _IMPLEMENT_INSTRUCTION
    if kind == AttemptKind.FIX:
        return _FIX_INSTRUCTION
    if kind == AttemptKind.REVIEW:
        return _REVIEW_INSTRUCTION
    return "Execute the requested attempt kind conforming to context and schema."


def _build_prompt(attempt: AttemptInput) -> str:
    context_rel_path = _resolve_context_rel_path(attempt)
    instruction = _get_instruction(attempt.kind)

    prompt = (
        f"Stage: {attempt.kind}\n"
        f"Trial ID: {attempt.trial_id}\n"
        f"Attempt ID: {attempt.attempt_id}\n\n"
        f"{instruction}\n\n"
        f"Read complete task context and parameters from: {context_rel_path}\n"
        f"Output must strictly follow the output schema provided."
    )

    prompt_bytes = prompt.encode("utf-8")
    if len(prompt_bytes) > 4096:
        return prompt_bytes[:4096].decode("utf-8", errors="ignore")
    return prompt


def _encode_findings(findings: tuple[Finding, ...]) -> list[dict[str, JsonValue]]:
    return [
        {
            "id": f.id,
            "severity": str(f.severity),
            "path": f.path,
            "line": f.line,
            "reason": f.reason,
            "requested_change": f.requested_change,
        }
        for f in findings
    ]


def _encode_responses(
    responses: tuple[FindingResponse, ...],
) -> list[dict[str, JsonValue]]:
    return [
        {
            "finding_id": r.finding_id,
            "action": str(r.action),
            "explanation": r.explanation,
        }
        for r in responses
    ]


def _encode_summaries(
    previous_results: tuple[AttemptResult, ...], trial_id: str
) -> list[dict[str, JsonValue]]:
    return [
        {
            "attempt_id": r.input.attempt_id,
            "kind": str(r.input.kind),
            "outcome": str(r.process.outcome),
            "returncode": r.process.returncode,
            "ended_at": r.ended_at,
            "detail": r.process.detail,
        }
        for r in previous_results
        if r.input.trial_id == trial_id
    ]


def _build_context(
    state: TrialState,
    attempt: AttemptInput,
    diff: str,
    previous_results: tuple[AttemptResult, ...],
    ci_candidates: tuple[tuple[str, ...], ...],
) -> dict[str, JsonValue]:
    return {
        "trial_id": attempt.trial_id,
        "attempt_id": attempt.attempt_id,
        "kind": str(attempt.kind),
        "input_head_sha": attempt.input_head_sha,
        "base_sha": attempt.base_sha,
        "plan_digest": attempt.plan_digest,
        "request": state.request.text,
        "plan": _encode_plan_dict(state.plan),
        "ci_candidates": [list(c) for c in ci_candidates],
        "ci_result": _extract_ci_result(previous_results),
        "diff": diff,
        "findings": _encode_findings(state.findings),
        "responses": _encode_responses(state.responses),
        "previous_attempt_summaries": _encode_summaries(
            previous_results, attempt.trial_id
        ),
    }


def build_agent_request(
    state: TrialState,
    attempt: AttemptInput,
    diff: str,
    previous_results: tuple[AttemptResult, ...],
    ci_candidates: tuple[tuple[str, ...], ...] = (),
) -> AgentRequest:
    """Build an AgentRequest with bounded prompt and context dictionary."""
    prompt = _build_prompt(attempt)
    context = _build_context(state, attempt, diff, previous_results, ci_candidates)
    schema = _agent_full_schema(attempt.kind)
    return AgentRequest(
        input=attempt,
        prompt=prompt,
        schema=schema,
        context=context,
    )
