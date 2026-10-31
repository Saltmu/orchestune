"""Read-only mechanical gate checks. Results are advice, never authorization."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from orchestune.claim.preflight import evaluate_claim_preflight
from orchestune.complete.ci_evidence import (
    CiEvidenceError,
    CiEvidenceMissingError,
    resolve_evidence_path,
    validate_ci_evidence,
)
from orchestune.complete.contracts import CompleteRequest, CompleteStage
from orchestune.complete.journal import CompletionJournalError
from orchestune.complete.journal_models import CompletionJournalRecord
from orchestune.complete.preflight import evaluate_complete_preflight
from orchestune.complete.review_evidence import verify_review_evidence
from orchestune.issue_parsing import FOOTPRINT_BLOCK_PATTERN, parse_task_from_issue
from orchestune.labels import StatusLabel
from orchestune.review.judgment import judgment_digest, parse_judgments
from orchestune.workflow.contracts import (
    GATES,
    WorkflowCheck,
    WorkflowContext,
    WorkflowResult,
)
from orchestune.workflow.readers import readonly_git, session_path
from orchestune.workflow.stages import StageDefinition, load_stage_definitions


@dataclass(frozen=True)
class _Observation:
    status: str = "ready"
    code: str = ""
    message: str = ""
    source: str = "workflow evidence"
    value: Any = None

    def check(self, condition: str) -> WorkflowCheck:
        return WorkflowCheck(
            condition,
            self.status,
            ({"source": self.source, "value": self.value},),
            (self.message,) if self.message else (),
        )


def _unmet(code: str, message: str, source: str = "workflow evidence") -> _Observation:
    return _Observation("unmet", code, message, source)


def _unknown(message: str, source: str) -> _Observation:
    return _Observation("unknown", "evidence_unavailable", message, source)


def _context(c: WorkflowContext) -> _Observation:
    if "context" in c.errors:
        return _unmet("context_invalid", c.errors["context"], "implementation-plan.md")
    if "forge" in c.errors:
        return _unknown(c.errors["forge"], "Forge evidence")
    state = c.state
    if (
        not isinstance(state.get("schema_version"), int)
        or isinstance(state.get("schema_version"), bool)
    ) or state["schema_version"] != 1:
        return _unmet("context_invalid", "Unsupported workflow state schema")
    if (
        state.get("mode") not in ("auto", "interactive")
        or state.get("change_kind") not in ("code", "docs")
        or state.get("backend") not in ("gh", "mcp")
    ):
        return _unmet("context_invalid", "Record valid mode, change_kind and backend")
    if not c.repository:
        return _unknown(
            c.errors.get("forge", "Repository unavailable"), "Forge repository"
        )
    if (
        state.get("repository") != c.repository
        or (
            not isinstance(state.get("issue_number"), int)
            or isinstance(state.get("issue_number"), bool)
        )
        or state["issue_number"] != c.issue_number
    ):
        return _unmet("context_conflict", "Reconcile state repository/Issue identity")
    if (
        not isinstance(state.get("selection_basis"), str)
        or not state["selection_basis"].strip()
    ):
        return _unmet("context_missing", "Record reviewer selection provenance")
    if c.active is not None and state.get("claim_id") != c.active.claim.claim_id:
        return _unmet("claim_conflict", "State names another claim generation")
    if state.get("claim_id") is not None and (
        not isinstance(state["claim_id"], str) or not state["claim_id"].strip()
    ):
        return _unmet("context_invalid", "claim_id must be nonempty text or null")
    return _Observation(
        source="workflow state",
        value={k: state[k] for k in ("mode", "change_kind", "backend")},
    )


def _reviewer(c: WorkflowContext) -> _Observation:
    reviewer = c.state.get("reviewer")
    if reviewer in ("claude", "codex", "skip"):
        return _Observation(source="workflow state reviewer", value=reviewer)
    if reviewer is None or reviewer == "":
        if c.state.get("mode") == "interactive":
            return _Observation(
                "waiting_human",
                "waiting_human",
                "Select claude, codex or skip",
                "reviewer",
            )
        return _unmet(
            "reviewer_missing",
            "Record the resolved auto reviewer policy or explicit skip",
        )
    return _unmet("reviewer_invalid", "Reviewer must be claude, codex or skip")


def _approval(c: WorkflowContext) -> _Observation:
    value = c.state.get("plan_approval")
    if not isinstance(value, dict) or not isinstance(value.get("required"), bool):
        return _unmet("approval_invalid", "Record plan_approval with boolean required")
    status = value.get("status")
    evidence = value.get("evidence")
    if status not in ("pending", "approved", "not_required") or (
        evidence is not None and not isinstance(evidence, str)
    ):
        return _unmet("approval_invalid", "Invalid approval status/evidence")
    if c.state.get("mode") == "auto" and value["required"]:
        return _unmet(
            "approval_invalid",
            "Auto/existing-Issue execution records not_required approval",
        )
    if not value["required"]:
        if (
            status != "not_required"
            or not isinstance(evidence, str)
            or not evidence.strip()
        ):
            return _unmet("approval_invalid", "Explain why approval is not required")
    elif status == "pending":
        return _Observation(
            "waiting_human",
            "waiting_human",
            "Wait for required plan approval",
            "plan_approval",
        )
    elif status != "approved" or not evidence or not evidence.strip():
        return _unmet(
            "approval_invalid", "Required approval needs approved status and evidence"
        )
    return _Observation(source="plan_approval", value=value)


def _footprint(c: WorkflowContext) -> _Observation:
    if c.issue is None:
        return _unknown(c.errors.get("forge", "Issue unavailable"), "Issue footprint")
    if c.issue.number != c.issue_number:
        return _unmet("context_conflict", "Issue identity differs")
    task = parse_task_from_issue(c.issue)
    if (
        task.yaml_error
        or task.footprint_error
        or not FOOTPRINT_BLOCK_PATTERN.search(c.issue.body)
    ):
        return _unmet(
            "footprint_invalid", task.footprint_error or "Supply valid Footprint YAML"
        )
    # Reuse preflight rules without treating our already-held claim as a new claim.
    labels = (
        tuple(x for x in c.issue.labels if x != StatusLabel.IN_PROGRESS)
        if c.active
        else c.issue.labels
    )
    decision = evaluate_claim_preflight(c.issue, labels=labels)
    if decision.failure:
        return _unmet(
            decision.failure.reason.value, decision.failure.message, "claim preflight"
        )
    if c.dependencies_ready is None:
        return _unknown(
            c.errors.get("forge", "Dependencies unavailable"), "Issue dependencies"
        )
    if not c.dependencies_ready:
        return _unmet(
            "unresolved_dependencies", "Resolve dependency evidence before claim"
        )
    return _Observation(
        source="Issue footprint/dependencies", value=list(task.footprint)
    )


def _claim(c: WorkflowContext) -> _Observation:
    if "local" in c.errors:
        return _unknown(c.errors["local"], "claim ledger/Git")
    active = c.active
    if active is None:
        return _unmet("claim_missing", "Run orchestune claim for this Issue")
    if c.issue is None:
        return _unknown("Issue claim identity unavailable", "Issue")
    block = FOOTPRINT_BLOCK_PATTERN.search(c.issue.body)
    try:
        data = yaml.safe_load(block[1]) if block else {}
    except yaml.YAMLError:
        return _unmet("claim_conflict", "Issue claim metadata is invalid YAML")
    if not isinstance(data, dict):
        return _unmet("claim_conflict", "Issue claim metadata is invalid")
    if not active.claim.claim_id or not (
        data.get("claim_id") == c.state.get("claim_id") == active.claim.claim_id
    ):
        return _unmet(
            "claim_conflict", "Reconcile Issue/state/ledger claim generations"
        )
    if (
        active.core.issue_number != c.issue_number
        or active.claim.repository_id != c.repository_identity
        or Path(active.core.worktree_path).resolve() != c.root.resolve()
        or active.core.branch != c.branch
    ):
        return _unmet(
            "claim_conflict", "Claim repository/Issue/branch/worktree identity differs"
        )
    if active.claim.claim_stage != "completed":
        return _unmet("claim_conflict", "Claim preparation is not complete")
    task = parse_task_from_issue(c.issue)
    if active.claim.reservation_kind == "footprint" and not set(
        task.footprint
    ).issubset(active.core.declared_footprint):
        return _unmet(
            "footprint_expansion_required", "Issue declaration exceeds held reservation"
        )
    return _Observation(source="active claim", value=active.claim.claim_id)


def _impact_table(text: str) -> None:
    lines = [line.strip() for line in text.strip().splitlines()]
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]
    if len(rows) < 3 or rows[0] != ["Reference", "Decision", "Rationale"]:
        raise ValueError(
            "Impact table requires Reference, Decision and Rationale columns and rows"
        )
    if len(rows[1]) != 3 or not all(re.fullmatch(r":?-{3,}:?", x) for x in rows[1]):
        raise ValueError("Invalid impact table separator")
    for row in rows[2:]:
        if len(row) != 3 or not all(row) or row[1] not in {"in scope", "out of scope"}:
            raise ValueError("Every reference needs a decision and nonempty rationale")


def _impact(c: WorkflowContext) -> _Observation:
    value = c.state.get("impact_initial")
    if not isinstance(value, dict) or set(value) != {"relative_path", "digest"}:
        return _unmet(
            "impact_missing", "Record the immutable initial impact table and digest"
        )
    try:
        path = session_path(c.session_dir, value["relative_path"])
        table = (
            path.read_text(encoding="utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .rstrip("\n")
            + "\n"
        )
        _impact_table(table)
        digest = hashlib.sha256(table.encode("utf-8")).hexdigest()
        if value["digest"] != digest:
            raise ValueError(
                "Initial impact-table digest differs; preserve initial snapshot"
            )
    except (OSError, UnicodeError, ValueError) as error:
        return _unmet("impact_invalid", str(error), "impact_initial")
    return _Observation(source="impact_initial", value=value)


def _request(c: WorkflowContext) -> CompleteRequest:
    return CompleteRequest.done(
        c.issue_number,
        c.pr_number or 1,
        claim_id=c.state.get("claim_id"),
        reviewer=c.state.get("reviewer"),
        review_reply=c.review_reply,
        worktree_root=c.root,
        state_path=c.state_path,
    )


def _ci(c: WorkflowContext) -> _Observation:
    path = resolve_evidence_path(c.root)
    if not path.resolve().is_relative_to(c.root.resolve()):
        return _unmet("ci_stale", "CI evidence path belongs to another worktree")
    try:
        evidence = validate_ci_evidence(_request(c))
        if not evidence.tree_sha or not evidence.base_sha:
            return _unmet("ci_stale", "CI evidence lacks tree/base binding")
    except CiEvidenceMissingError as error:
        return _unmet("ci_missing", str(error), "ci_evidence.json")
    except (CiEvidenceError, OSError, UnicodeError) as error:
        return _unmet("ci_stale", str(error), "ci_evidence.json")
    return _Observation(source="ci_evidence.json", value=evidence.to_dict())


def _clean(c: WorkflowContext) -> _Observation:
    if c.worktree_status == "unknown":
        return _unknown("Worktree status is unknown", "Git status")
    if c.worktree_status != "clean":
        return _unmet(
            "worktree_dirty",
            "Commit changes, then refresh clean-HEAD local CI",
            "Git status",
        )
    return _Observation(source="Git status", value="clean")


_PR_SECTIONS = (
    ("Acceptance Criteria", "受け入れ条件"),
    ("Scope Guard", "スコープガード"),
    ("Walkthrough", "変更内容"),
    ("Verification Evidence", "ローカルCI実行結果"),
    ("Reproducer", "再現手順と修正確認"),
    ("Baseline", "ベースライン差分"),
)


def _pr_headings(text: str) -> list[str]:
    """Read ATX headings outside fenced examples in the PR description."""
    headings = []
    fence = ""
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            token, suffix = marker.groups()
            if not fence:
                fence = token
            elif (
                token[0] == fence[0] and len(token) >= len(fence) and not suffix.strip()
            ):
                fence = ""
            continue
        heading = re.match(r"^ {0,3}#{1,6}[ \t]+(.+)$", line)
        if not fence and heading:
            headings.append(heading[1])
    return headings


def _body(c: WorkflowContext) -> _Observation:
    if not c.pr_body or "pr_body" in c.errors:
        return _unmet(
            "pr_body_missing",
            c.errors.get("pr_body", "Supply --pr-body or an existing PR description"),
        )
    text = re.sub(r"<!--.*?-->", "", c.pr_body, flags=re.S)
    headings = _pr_headings(text)
    missing = [
        names[0]
        for names in _PR_SECTIONS
        if not any(
            any(name.casefold() in h.casefold() for name in names) for h in headings
        )
    ]
    if missing:
        return _unmet("pr_body_invalid", "Missing PR sections: " + ", ".join(missing))
    return _Observation(source="PR description", value="required sections present")


def _base(c: WorkflowContext) -> _Observation:
    if c.active is None or not c.active.claim.base_ref:
        return _unmet("base_mismatch", "Claim base reference unavailable")
    expected = (
        c.active.claim.base_ref.removeprefix("refs/remotes/")
        .removeprefix("origin/")
        .removeprefix("refs/heads/")
    )
    if c.pr is not None and c.pr.base_ref != expected:
        return _unmet("base_mismatch", f"PR base must be {expected}")
    return _Observation(source="claim/PR base", value=expected)


def _pr(c: WorkflowContext) -> _Observation:
    pr = c.pr
    if pr is None:
        return _unmet(
            "pr_missing", c.errors.get("forge", "Supply --pr for a current PR")
        )
    if (
        pr.number != c.pr_number
        or not c.head_sha
        or pr.head_sha != c.head_sha
        or pr.head_ref != c.branch
    ):
        return _unmet(
            "pr_head_mismatch", "PR identity/head/branch differs from task worktree"
        )
    if (
        pr.state.upper() not in {"OPEN", "MERGED"}
        or pr.is_cross_repository is not False
        or pr.is_files_truncated
        or not pr.changed_files
    ):
        return _unmet(
            "pr_stale",
            "PR must have verified repository ownership, complete nonempty diff and open/merged state",
        )
    return _base(c)


def _completion(c: WorkflowContext) -> _Observation:
    if c.pr is None or c.forge is None or c.ledger is None:
        return _unmet(
            "completion_unavailable",
            "Current PR, claim ledger and Forge evidence are required",
        )
    result = evaluate_complete_preflight(
        _request(c), worktree_path=c.root, forge=c.forge, run_state=c.ledger
    )
    if not result.accepted:
        return _unmet(
            "completion_unavailable",
            result.reason or "Complete preflight rejected evidence",
        )
    return _Observation(source="complete preflight", value="accepted")


def _reply_posted(c: WorkflowContext, comments: list[dict[str, Any]]) -> _Observation:
    if c.state.get("reviewer") == "skip":
        return _Observation(
            source="review selection",
            value="skipped; existing human integration gate still applies",
        )
    if c.review_reply is None:
        return _unmet("reply_missing", "Supply --review-reply PATH")
    table = parse_judgments(c.review_reply.read_text(encoding="utf-8"))
    if not table["findings"]:
        return _Observation(
            source="review reply", value="zero sources; reply not required"
        )
    digest = judgment_digest(table)[0]
    matches = []
    for comment in comments:
        try:
            if judgment_digest(parse_judgments(comment.get("body") or ""))[0] == digest:
                matches.append(comment)
        except ValueError:
            continue
    if len(matches) != 1:
        return _unmet(
            "reply_duplicate" if matches else "reply_missing",
            "Expected exactly one posted current-round judgment reply; inspect comments before any repost",
        )
    return _Observation(
        source="PR reply", value={"id": matches[0].get("id"), "round": table["round"]}
    )


def _review(c: WorkflowContext) -> tuple[_Observation, _Observation]:
    if c.pr is None or c.forge is None:
        missing = _unmet(
            "review_unavailable", "Current PR and complete review evidence are required"
        )
        return missing, missing
    try:
        summary = verify_review_evidence(_request(c), c.forge, c.pr, c.head_sha)
        reply = _reply_posted(c, c.forge.list_all_issue_comments(c.pr_number))
        after = c.forge.get_pull_request(c.pr_number)
        if after.head_sha != c.head_sha or after.base_ref != c.pr.base_ref:
            return _unmet(
                "review_head_mismatch", "PR changed while reading review evidence"
            ), reply
        return _Observation(
            source="complete review validator", value=summary.to_dict()
        ), reply
    except CompletionJournalError as error:
        return _unmet(error.reason.value, str(error), "review evidence"), _unmet(
            "reply_missing", "Verify the current-round reply after acquiring review"
        )
    except Exception as error:
        missing = _unknown(str(error), "PR review snapshot/reply")
        return missing, missing


_VALIDATORS: dict[str, Callable[[WorkflowContext], _Observation]] = {
    "context_recorded": _context,
    "footprint_valid": _footprint,
    "reviewer_selected": _reviewer,
    "plan_approved": _approval,
    "claim_current": _claim,
    "impact_initial": _impact,
    "implementation_ready": _ci,
    "ci_current": _ci,
    "worktree_clean": _clean,
    "pr_body_ready": _body,
    "base_correct": _base,
    "pr_current": _pr,
    "completion_ready": _completion,
}
_REVIEW_CONDITIONS = {"review_acquired", "findings_judged", "review_current"}
_TERMINALS = {"blocked", "handed-off"}


def _matches(stage: StageDefinition, c: WorkflowContext) -> bool:
    return all(
        not getattr(stage.branches, name)
        or c.state.get(name) in getattr(stage.branches, name)
        for name in ("mode", "change_kind", "backend")
    )


def _terminal_records(c: WorkflowContext) -> list[CompletionJournalRecord]:
    if c.ledger is None:
        return []
    generation = c.active.claim.claim_id if c.active else c.state.get("claim_id")
    records = []
    raw = [
        *c.ledger.completion_journal.values(),
        *c.ledger.completion_replay_receipts.values(),
    ]
    for value in raw:
        if (
            value.get("issue_number") != c.issue_number
            or value.get("generation_id") != generation
        ):
            continue
        record = CompletionJournalRecord.from_dict(value)
        if record.repository_id != c.repository_identity:
            raise ValueError(
                "terminal receipt repository conflicts with current repository"
            )
        if record.posting_evidence and record.stage in {
            CompleteStage.OUTCOME_POSTED,
            CompleteStage.LABEL_CONFIRMED,
            CompleteStage.HANDED_OFF,
        }:
            records.append(record)
    return records


def _terminal(c: WorkflowContext, gate: str) -> WorkflowResult | None:
    if _context(c).status != "ready":
        return None
    try:
        records = _terminal_records(c)
    except (ValueError, TypeError, KeyError) as error:
        return _result(
            c,
            gate,
            "blocked",
            [(_unmet("terminal_receipt_conflict", str(error)), "terminal_receipt")],
        )
    blocked = [r for r in records if r.result == "blocked"]
    handed = [
        r
        for r in records
        if r.result != "blocked" and r.stage is CompleteStage.HANDED_OFF
    ]
    if blocked and handed:
        return _result(
            c,
            gate,
            "blocked",
            [
                (
                    _unmet(
                        "terminal_receipt_conflict",
                        "Conflicting blocked and handed_off receipts",
                    ),
                    "terminal_receipt",
                )
            ],
        )
    if not blocked and not handed:
        return None
    return _terminal_result(c, gate, (blocked or handed)[0], bool(blocked))


def _terminal_result(
    c: WorkflowContext, gate: str, record: CompletionJournalRecord, blocked: bool
) -> WorkflowResult:
    stage = "blocked" if blocked else "handed-off"
    observation = _Observation(
        source="completion receipt",
        value={
            "claim_id": record.generation_id,
            "comment_url": (record.posting_evidence or {})["comment_url"],
        },
    )
    result = _result(c, gate, stage, [(observation, "terminal_receipt")])
    return replace(
        result,
        claim_id=record.generation_id,
        status="terminal_blocked" if blocked else "ready",
        reason_codes=("blocked" if blocked else "handed_off",),
        actions=(
            {
                "description": record.outcome_payload.get(
                    "reason",
                    "Inspect canonical blocked Outcome and follow recovery procedure",
                )
            },
        )
        if blocked
        else (),
        references=(
            "skills/local-ci-developer/references/worktree.md#restart-a-completed-blocked-local-task",
        ),
        artifacts={**c.artifacts, "outcome": observation.value},
    )


def _result(
    c: WorkflowContext,
    gate: str,
    stage: str,
    observations: list[tuple[_Observation, str]],
    references: tuple[str, ...] = (),
) -> WorkflowResult:
    statuses = {o.status for o, _ in observations}
    status = (
        "unmet"
        if statuses & {"unmet", "unknown"}
        else "waiting_human"
        if "waiting_human" in statuses
        else "ready"
    )
    return WorkflowResult(
        c.issue_number,
        c.active.claim.claim_id if c.active else None,
        stage,
        gate,
        status,
        tuple(dict.fromkeys(o.code for o, _ in observations if o.code)),
        tuple(o.check(condition) for o, condition in observations),
        tuple({"description": o.message} for o, _ in observations if o.message),
        references,
        c.artifacts,
        c.head_sha,
    )


def _observe(
    c: WorkflowContext, condition: str, cache: dict[str, _Observation]
) -> _Observation:
    if condition not in cache:
        if condition in _REVIEW_CONDITIONS | {"reply_posted"}:
            review, reply = _review(c)
            cache.update(dict.fromkeys(_REVIEW_CONDITIONS, review))
            cache["reply_posted"] = reply
        elif condition in _VALIDATORS:
            cache[condition] = _VALIDATORS[condition](c)
        else:
            cache[condition] = (
                _unmet("terminal_receipt_unavailable", "No terminal completion receipt")
                if condition == "terminal_receipt"
                else _unknown(f"No validator for {condition}", "stage definitions")
            )
    return cache[condition]


def _derived_stage(
    c: WorkflowContext,
    definitions: tuple[StageDefinition, ...],
    cache: dict[str, _Observation],
) -> str:
    if _context(c).status != "ready":
        return "prepare"
    for stage in definitions:
        if stage.id not in _TERMINALS and _matches(stage, c):
            if any(
                _observe(c, condition, cache).status != "ready"
                for condition in stage.exit_conditions
            ):
                return stage.id
    return "complete"


def _included_stages(
    c: WorkflowContext, definitions: tuple[StageDefinition, ...], owner: StageDefinition
) -> tuple[StageDefinition, ...]:
    return tuple(
        stage
        for stage in definitions
        if (
            stage.order < owner.order
            and stage.id not in _TERMINALS
            or stage.id == owner.id
        )
        and _matches(stage, c)
    )


def _check_gate(
    context: WorkflowContext,
    gate: str,
    *,
    stages: tuple[StageDefinition, ...] | None = None,
) -> WorkflowResult:
    """Evaluate the requested and earlier applicable conditions; never execute actions."""
    if gate not in GATES:
        raise ValueError(f"Unknown workflow gate: {gate}")
    definitions = stages if stages is not None else load_stage_definitions()
    terminal = _terminal(context, gate)
    if terminal is not None:
        return terminal
    cache: dict[str, _Observation] = {}
    stage_id = _derived_stage(context, definitions, cache)
    owner = next((stage for stage in definitions if stage.gate == gate), None)
    if owner is None:
        return replace(
            _result(context, gate, stage_id, []),
            status="unmet",
            reason_codes=("gate_not_applicable",),
        )
    included = _included_stages(context, definitions, owner)
    checks = [
        (_observe(context, condition, cache), condition)
        for stage in included
        for condition in stage.exit_conditions
    ]
    result = _result(
        context,
        gate,
        stage_id,
        checks,
        tuple(dict.fromkeys(ref for stage in included for ref in stage.references)),
    )
    if not _matches(owner, context):
        return replace(
            result,
            status="unmet",
            reason_codes=(*result.reason_codes, "gate_not_applicable"),
        )
    return result


def check_gate(
    context: WorkflowContext,
    gate: str,
    *,
    stages: tuple[StageDefinition, ...] | None = None,
) -> WorkflowResult:
    """Check evidence serially with Git optional locks disabled and restored."""
    with readonly_git():
        return _check_gate(context, gate, stages=stages)
