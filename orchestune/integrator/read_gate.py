"""Bidirectional read checks before review shortcuts and parent publication.

Reports use immutable child sources, original checkpoints and closed parent
children. Rebase never rewrites reads. Unknown inputs fail closed when required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

from orchestune.claim.read_checkpoint import restore_read_checkpoint
from orchestune.infra.git_cli import run_git
from orchestune.integrator.finalization import (
    ReceiptLookupStatus,
    lookup_integration_receipt,
)
from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.integrator.read_context import (
    ReadContextRequest,
    ReaderHistory,
    ReaderSourceProof,
    build_read_context,
)
from orchestune.integrator.read_reports import restore_read_report, save_read_report
from orchestune.issue_parsing import effective_parent_number, parse_task_from_issue
from orchestune.labels import StatusLabel
from orchestune.outcome_record import parse_from_comments
from orchestune.read_dependencies.contracts import (
    ChangedResource,
    ReadCoverage,
    ReaderSnapshotEntry,
    ReadEvidenceV1,
    ReadGateDecision,
    ReadGateInputs,
    ReadValidationV1,
    StaleReadGate,
    StaleReadReportV1,
    StaleVerdict,
    WriterChanges,
    decide_read_gate,
)
from orchestune.read_dependencies.detection import detect_stale_reads
from orchestune.read_dependencies.git import observe_changed_resources
from orchestune.read_dependencies.report_codec import normalize_report
from orchestune.read_dependencies.validation import (
    parse_read_validation,
    validate_read_validation,
)
from orchestune.review.semantic_gate import (
    TERMINAL_PASSED_PHASES,
    SemanticReviewPhase,
    restore_latest_fix_result,
)


def evaluate_report_gate(
    report: StaleReadReportV1 | None,
    mode: StaleReadGate | str,
    *,
    head_sha: str,
    validation: ReadValidationV1 | None = None,
) -> ReadGateDecision:
    """Apply the shared mode contract only to an intact report for this H."""
    if report is not None:
        try:
            report = normalize_report(report)
            if report.candidate_head_sha != head_sha:
                report = None
        except (TypeError, ValueError):
            report = None
    return decide_read_gate(
        ReadGateInputs(
            mode=StaleReadGate(mode),
            verdict=StaleVerdict.UNKNOWN if report is None else report.verdict,
            coverage=ReadCoverage.UNAVAILABLE if report is None else report.coverage,
            candidate_head_sha=head_sha,
            report_digest=None if report is None else report.digest,
            candidate_ids=()
            if report is None
            else tuple(c.candidate_id for c in report.candidates),
            validation=validation,
        )
    )


@dataclass(frozen=True)
class _HistorySource:
    history: ReaderHistory

    def list_integrated_readers(self, **_kwargs: Any) -> ReaderHistory:
        return self.history

    def load_snapshot_evidence(
        self, _entry: ReaderSnapshotEntry
    ) -> ReadEvidenceV1 | None:
        return None  # Bootstrap uses immutable receipt/Outcome history.


def _trusted_comments(ctx: Any, number: int) -> list[dict[str, Any]]:
    author = ctx.forge.get_authenticated_user()
    if not isinstance(author, str) or not author:
        raise ValueError("read evidence author is unavailable")
    return [
        c
        for c in ctx.forge.list_all_issue_comments(number)
        if (c.get("user") or c.get("author") or {}).get("login", "").casefold()
        == author.casefold()
    ]


def _source_proof(
    ctx: Any, proof: TaskIntegrationProof, repository: str
) -> ReaderSourceProof:
    comments = _trusted_comments(ctx, proof.issue_number)
    records = [parse_from_comments([c]) for c in comments]
    matches = [
        r
        for r in records
        if r is not None and r.result == "done" and r.head_sha == proof.source_sha
    ]
    if len({(r.claim_id, r.completion_id) for r in matches}) != 1:
        raise ValueError("read completion generation is missing or ambiguous")
    outcome = matches[-1]
    if not outcome.claim_id or not outcome.completion_id:
        raise ValueError("read completion identity is missing")
    if any(r != outcome for r in matches):
        raise ValueError("read completion payloads conflict")
    return ReaderSourceProof(
        proof=proof,
        claim_id=outcome.claim_id,
        completion_id=outcome.completion_id,
        outcome=outcome,
        checkpoint=restore_read_checkpoint(
            comments, repository, proof.issue_number, outcome.claim_id
        ),
    )


def _historical_sources(ctx: Any) -> list[TaskIntegrationProof]:
    parent = ctx.attempt_identity.parent_issue_number
    candidates = ctx.forge.find_issues_by_parent_metadata(parent)
    if len(candidates) >= 1000:
        raise ValueError("historical reader search may be truncated")
    issues = {i.number: i for i in candidates if effective_parent_number(i) == parent}
    issues.update({i.number: i for i in ctx.forge.list_sub_issues(parent)})
    incoming = {c.issue_number for c in ctx.attempt_identity.child_sources}
    proofs = []
    for issue in issues.values():
        if issue.number in incoming:
            continue
        task = parse_task_from_issue(issue)
        if not task.subtask_id:
            raise ValueError("parent reader identity is unavailable")
        lookup = lookup_integration_receipt(
            ctx.forge, issue.number, task.subtask_id, f"parent/issue-{parent}"
        )
        if lookup.status is ReceiptLookupStatus.UNAVAILABLE:
            raise ValueError("historical reader receipt is unavailable")
        if lookup.receipt is None:
            if _missing_receipt_is_unknown(ctx, issue):
                raise ValueError("included reader lacks a receipt")
            continue
        proof = lookup.receipt.proof
        ancestor = run_git(
            [
                "merge-base",
                "--is-ancestor",
                proof.source_sha,
                ctx.attempt_identity.base_sha,
            ],
            cwd=ctx.repository_root,
            check=False,
        )
        if ancestor.returncode:
            raise ValueError("historical reader is not integrated at P")
        proofs.append(proof)
    return proofs


def _missing_receipt_is_unknown(ctx: Any, issue: Any) -> bool:
    if "integration:included" in issue.labels:
        return True
    record = parse_from_comments(_trusted_comments(ctx, issue.number))
    if record is not None and record.result == "not-needed":
        return False
    if issue.state.upper() == "CLOSED":
        return True
    if record is None or record.result != "done":
        return StatusLabel.DONE in issue.labels
    if record.head_sha is None:
        return True
    ancestry = run_git(
        ["merge-base", "--is-ancestor", record.head_sha, ctx.attempt_identity.base_sha],
        cwd=ctx.repository_root,
        check=False,
    )
    # Parent push may have succeeded before receipt/label/close delivery. An
    # open Issue must not erase that reader after such a crash window.
    return ancestry.returncode != 1


def _read_paths(sources: list[ReaderSourceProof]) -> tuple[str, ...]:
    paths: set[str] = set()
    for source in sources:
        evidence = None if source.outcome is None else source.outcome.read_evidence
        if evidence is None:
            raise ValueError("reader evidence is unavailable")
        paths.update(evidence.declared_reads or ())
        paths.update(o.path for o in evidence.observations)
    return tuple(sorted(paths))


def _writers(
    ctx: Any, sources: list[ReaderSourceProof], head: str
) -> tuple[WriterChanges, ...]:
    paths = _read_paths(sources)
    known = _known_writers(ctx, sources, paths)
    writers: list[WriterChanges] = []
    bases = {ctx.attempt_identity.base_sha}
    bases.update(
        s.outcome.read_evidence.input_base_sha
        for s in sources
        if s.outcome and s.outcome.read_evidence
    )
    for base in sorted(bases):
        for change in observe_changed_resources(
            base, head, paths, cwd=ctx.repository_root
        ):
            matches = [
                w for w in known if any(_same_target(change, c) for c in w.changes)
            ]
            if not matches:
                writers.append(
                    WriterChanges(issue_number=None, claim_id=None, changes=(change,))
                )
            for writer in matches:
                writers.append(
                    WriterChanges(
                        issue_number=writer.issue_number,
                        claim_id=writer.claim_id,
                        changes=(change,),
                    )
                )
    return tuple(writers)


def _same_target(left: ChangedResource, right: ChangedResource) -> bool:
    return (left.path, left.new_blob_oid, left.new_file_mode, left.availability) == (
        right.path,
        right.new_blob_oid,
        right.new_file_mode,
        right.availability,
    )


def _known_writers(
    ctx: Any, sources: list[ReaderSourceProof], paths: tuple[str, ...]
) -> list[WriterChanges]:
    incoming = {c.issue_number for c in ctx.attempt_identity.child_sources}
    writers = []
    for source in sources:
        assert source.proof and source.outcome and source.outcome.read_evidence
        evidence = source.outcome.read_evidence
        # Current sources are compared to P so a rebase cannot claim upstream
        # writes as the reader's own. Historical receipts retain original bases.
        base = (
            ctx.attempt_identity.base_sha
            if source.proof.issue_number in incoming
            else evidence.input_base_sha
        )
        changes = observe_changed_resources(
            base, source.proof.source_sha, paths, cwd=ctx.repository_root
        )
        if changes:
            writers.append(
                WriterChanges(
                    issue_number=source.proof.issue_number,
                    claim_id=source.claim_id,
                    changes=changes,
                )
            )
    return writers


def build_integration_read_report(ctx: Any, head_sha: str) -> StaleReadReportV1:
    """Recompute both directions from frozen sources, including closed readers."""
    identity = ctx.attempt_identity
    if identity is None:
        raise ValueError("read attempt identity is missing")
    repository = ctx.forge.get_repository_slug()
    current = [
        TaskIntegrationProof(c.issue_number, c.subtask_id, "", c.source_sha)
        for c in identity.child_sources
    ]
    historical = _historical_sources(ctx)
    sources = [_source_proof(ctx, p, repository) for p in historical + current]
    history = ReaderHistory(
        readers=tuple(sources[: len(historical)]),
        complete=True,
        empty_verified=not historical,
    )
    built = build_read_context(
        ReadContextRequest(
            repository=repository,
            parent_issue_number=identity.parent_issue_number,
            parent_sha=identity.base_sha,
            candidate_head_sha=head_sha,
            current_sources=tuple(sources[len(historical) :]),
            writers=_writers(ctx, sources, head_sha),
        ),
        _HistorySource(history),
    )
    if built.inputs is None or built.unknowns:
        raise ValueError("read context is incomplete")
    return detect_stale_reads(built.inputs)


def prepare_read_gate(ctx: Any, head_sha: str) -> ReadGateDecision:
    """Recompute and durably save the report before state/Runner handoff."""
    mode = StaleReadGate(getattr(ctx.config, "stale_read_gate", "advisory"))
    ctx.read_report, ctx.read_gate_error = None, None
    if mode is not StaleReadGate.OFF:
        try:
            report = normalize_report(build_integration_read_report(ctx, head_sha))
            identity = ctx.attempt_identity
            if (
                report.repository != ctx.forge.get_repository_slug()
                or identity is None
                or report.parent_issue_number != identity.parent_issue_number
                or report.parent_sha != identity.base_sha
                or report.candidate_head_sha != head_sha
            ):
                raise ValueError("read report repository/P/H mismatch")
            saved = save_read_report(
                ctx.forge, ctx.integration_pr_number, report, target_head_sha=head_sha
            )
            if saved.report is None:
                raise ValueError(saved.reason)
            ctx.read_report = report
        except Exception as error:  # noqa: BLE001 - acquisition failures are unknown, never fresh.
            ctx.read_gate_error = str(error)
    target = head_sha if re.fullmatch(r"[0-9a-f]{40}", head_sha) else "0" * 40
    ctx.read_gate_decision = evaluate_report_gate(
        ctx.read_report, mode, head_sha=target
    )
    return cast(ReadGateDecision, ctx.read_gate_decision)


def _validation(ctx: Any, report: StaleReadReportV1) -> ReadValidationV1:
    state = ctx.semantic_review_state
    comments = _trusted_comments(ctx, state.pr_number)
    result = restore_latest_fix_result(
        [c["body"] for c in comments],
        attempt_id=state.attempt_id,
        operation_id=str(state.operation_id),
    )
    if (
        result is None
        or result.status != "completed"
        or result.judgment_comment_id is None
        or result.input_sha != (state.fix_input_sha or state.head_sha)
    ):
        raise ValueError("read candidate judgment result is unavailable")
    matches = [c for c in comments if c.get("id") == result.judgment_comment_id]
    if len(matches) != 1:
        raise ValueError("read candidate judgment comment is unavailable")
    validation = parse_read_validation(matches[0]["body"])
    validate_read_validation(
        validation,
        round_number=state.round,
        head_sha=state.head_sha,
        report_digest=report.digest or "",
        candidate_ids=tuple(c.candidate_id for c in report.candidates),
    )
    return validation


def _require_durable_report(ctx: Any, report: StaleReadReportV1, head: str) -> None:
    state = ctx.semantic_review_state
    durable = restore_read_report(
        ctx.forge,
        state.pr_number,
        report_digest=state.read_report_digest,
        target_head_sha=head,
        expected_author=ctx.forge.get_authenticated_user(),
    )
    if durable.report != report:
        raise ValueError("read report could not be restored")


def validate_read_finalization(ctx: Any) -> str | None:
    """Recheck P/H/report/CI and candidate judgments, also for restored passes."""
    state = ctx.semantic_review_state
    if getattr(ctx.config, "stale_read_gate", "advisory") != "required":
        prepare_read_gate(ctx, getattr(state, "head_sha", "0" * 40))
        return None
    try:
        head = run_git(["rev-parse", "HEAD"], cwd=ctx.repository_root).stdout.strip()
    except Exception as error:  # noqa: BLE001 - an unreadable checkout cannot authorize publication.
        ctx.read_gate_error = str(error)
        return str(error)
    decision = prepare_read_gate(ctx, head)
    try:
        report = ctx.read_report
        if state is None or report is None:
            raise ValueError(
                ctx.read_gate_error or "required read report/state is missing"
            )
        if state.phase not in TERMINAL_PASSED_PHASES:
            raise ValueError("read review phase is not passed")
        if (
            state.head_sha != head
            or state.base_sha != report.parent_sha
            or state.ci_verified_sha != head
            or state.read_report_digest != report.digest
            or state.reviewed_read_report_digest != report.digest
        ):
            raise ValueError("read report/P/H/CI differs from the passed review")
        _require_durable_report(ctx, report, head)
        validation = (
            None
            if state.phase is SemanticReviewPhase.FAST_PATH_PASSED
            else _validation(ctx, report)
        )
        decision = evaluate_report_gate(
            report, decision.mode, head_sha=head, validation=validation
        )
        ctx.read_gate_decision = decision
        if not decision.allow_parent_update:
            raise ValueError(f"read gate: {decision.reason.value}")
    except Exception as error:  # noqa: BLE001 - final publication must fail closed.
        ctx.read_gate_error = str(error)
        return str(error)
    return None


def read_gate_diagnostics(ctx: Any) -> dict[str, Any]:
    """Expose all three modes, including an explicit disabled diagnostic."""
    decision = getattr(ctx, "read_gate_decision", None)
    return (
        {}
        if decision is None
        else {
            "mode": decision.mode.value,
            "reason": decision.reason.value,
            "verdict": decision.verdict.value,
            "report_digest": decision.report_digest,
            "error": ctx.read_gate_error,
        }
    )
