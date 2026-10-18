"""Retry finalization and close without re-integrating already included children."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import StrEnum

from orchestune.infra.git_cli import (
    ConditionalBranchDeletionResult,
    delete_remote_branch_if_matches,
    read_remote_branch_tip,
)
from orchestune.integrator.finalization import (
    ReceiptLookupResult,
    ReceiptLookupStatus,
    lookup_integration_receipt,
    resolve_retry_close_delivery,
)
from orchestune.integrator.finalization_retry import (
    BLOCKED_LABEL,
    BlockedOutcome,
    DenialVerdict,
    handle_denied_deletion,
    latest_blocked_proof,
    settle_blocked_child,
)
from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.integrator.types import (
    IntegrationComponent,
    IntegrationContext,
    IntegrationReport,
    IntegrationStatus,
)
from orchestune.models import Task
from orchestune.task_branch_resolution import BranchCapability


@dataclass
class _HeldFinalizations:
    """Children kept out of this cycle because their branch deletion is held (#827)."""

    deferred: list[int] = field(default_factory=list)
    escalated: list[int] = field(default_factory=list)
    reasons: dict[int, str] = field(default_factory=dict)

    def as_report(self) -> IntegrationReport:
        report: IntegrationReport = {}
        if self.deferred:
            report["finalization_deferred"] = self.deferred
        if self.escalated:
            report["finalization_escalated"] = self.escalated
        if self.reasons:
            report["finalization_deferred_reasons"] = self.reasons
        return report


class RetryChildIssueCloseStep(IntegrationComponent):
    """Retry closing child issues whose integration was already merged."""

    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        if not ctx.config.apply:
            return {"status": IntegrationStatus.SUCCESS}

        remaining_tasks = []
        retried_closed: list[int] = []
        held = _HeldFinalizations()
        for task in ctx.active_done_tasks:
            result = lookup_integration_receipt(
                ctx.forge, task.issue_number, task.subtask_id, ctx.base_branch
            )
            if self._hold_unavailable(task, result, held):
                continue
            if "integration:included" not in task.status_labels:
                proof = result.receipt.proof if result.receipt else None
                recovery = self._restore_label_from_receipt(ctx, task, held, proof)
                if recovery is _ReceiptRecovery.UNRECOVERED:
                    remaining_tasks.append(task)
                    continue
                if recovery is not _ReceiptRecovery.READY_TO_CLOSE:
                    continue
            if self._close_child(ctx, task, result, held):
                retried_closed.append(task.issue_number)

        ctx.active_done_tasks = remaining_tasks
        report = held.as_report()
        report["retried_closed_issues"] = retried_closed
        report["status"] = (
            IntegrationStatus.SUCCESS
            if ctx.active_done_tasks
            else IntegrationStatus.NO_DONE_TASKS
        )
        return report

    @staticmethod
    def _hold_unavailable(
        task: Task,
        result: ReceiptLookupResult,
        held: _HeldFinalizations,
    ) -> bool:
        if result.status is not ReceiptLookupStatus.UNAVAILABLE:
            return False
        if (
            "integration:included" not in task.status_labels
            and result.reason == "receipt_lookup_failed"
        ):
            return False
        reason = result.reason or "receipt_lookup_failed"
        held.deferred.append(task.issue_number)
        held.reasons[task.issue_number] = reason
        print(
            f"Warning: Deferred retry closing issue #{task.issue_number}: {reason}",
            file=sys.stderr,
        )
        return True

    @staticmethod
    def _close_child(
        ctx: IntegrationContext,
        task: Task,
        result: ReceiptLookupResult,
        held: _HeldFinalizations,
    ) -> bool:
        delivery, comment = resolve_retry_close_delivery(
            ctx.forge,
            task.issue_number,
            task.subtask_id,
            ctx.base_branch,
            lookup=result,
        )
        if not delivery.can_close:
            held.deferred.append(task.issue_number)
            held.reasons[task.issue_number] = delivery.value
            print(
                f"Warning: Deferred retry closing issue #{task.issue_number}: "
                f"notice delivery deferred ({delivery.value})",
                file=sys.stderr,
            )
            return False
        try:
            ctx.forge.close_issue(task.issue_number, "completed", comment=comment)
        except Exception as error:
            print(
                f"Warning: Failed to retry closing issue #{task.issue_number}: {error}",
                file=sys.stderr,
            )
            return False
        return True

    def _restore_label_from_receipt(
        self,
        ctx: IntegrationContext,
        task: Task,
        held: _HeldFinalizations,
        proof: TaskIntegrationProof | None,
    ) -> _ReceiptRecovery:
        if BLOCKED_LABEL in task.status_labels:
            return self._settle_blocked(ctx, task, held)
        if proof is None or not self._proof_reaches_parent(ctx, proof):
            return _ReceiptRecovery.UNRECOVERED
        if proof.merge_receipt.allows(BranchCapability.DELETE):
            deletion = delete_remote_branch_if_matches(
                ctx.original_root, proof.branch_name, proof.source_sha
            )
            if deletion is ConditionalBranchDeletionResult.TIP_MISMATCH:
                return _ReceiptRecovery.UNRECOVERED
            if deletion not in {
                ConditionalBranchDeletionResult.DELETED,
                ConditionalBranchDeletionResult.ALREADY_ABSENT,
            }:
                return self._defer_held_deletion(ctx, task, proof, deletion, held)
        return self._label_recovered(ctx, task)

    @staticmethod
    def _defer_held_deletion(
        ctx: IntegrationContext,
        task: Task,
        proof: TaskIntegrationProof,
        deletion: ConditionalBranchDeletionResult,
        held: _HeldFinalizations,
    ) -> _ReceiptRecovery:
        """#827: the proof already reached the parent, so a held deletion must not
        send the task through worktree, push and PR again; only a deletion the
        remote refused by policy is counted toward escalation."""
        held.deferred.append(task.issue_number)
        if deletion is ConditionalBranchDeletionResult.DENIED:
            verdict = handle_denied_deletion(
                ctx.forge,
                proof,
                ctx.base_branch,
                ctx.config.integration_run_id,
                ctx.config.parent_issue_number,
            )
            if verdict is DenialVerdict.ESCALATED:
                held.escalated.append(task.issue_number)
        return _ReceiptRecovery.DEFERRED

    def _settle_blocked(
        self, ctx: IntegrationContext, task: Task, held: _HeldFinalizations
    ) -> _ReceiptRecovery:
        """A labeled child is read-only, so an unverifiable proof holds it rather
        than returning it to merge and finalization, which would retry the
        refused deletion. The proof comes from the newest finalization record,
        not the receipt (several receipts exist once a tip was re-integrated)."""
        proof = latest_blocked_proof(
            ctx.forge, task.issue_number, task.subtask_id, ctx.base_branch
        )
        if proof is None or not self._proof_reaches_parent(ctx, proof):
            held.deferred.append(task.issue_number)
            return _ReceiptRecovery.DEFERRED
        outcome = settle_blocked_child(
            ctx.forge,
            proof,
            ctx.base_branch,
            ctx.config.parent_issue_number,
            lambda: read_remote_branch_tip(ctx.original_root, proof.branch_name),
        )
        if outcome is BlockedOutcome.REINTEGRATE:
            return _ReceiptRecovery.UNRECOVERED
        if outcome is BlockedOutcome.HOLD:
            held.deferred.append(task.issue_number)
            return _ReceiptRecovery.DEFERRED
        return self._label_recovered(ctx, task)

    @staticmethod
    def _label_recovered(ctx: IntegrationContext, task: Task) -> _ReceiptRecovery:
        try:
            ctx.forge.add_label(task.issue_number, "integration:included")
        except Exception as error:
            print(
                "Warning: Failed to label receipt-recovered issue "
                f"#{task.issue_number}: {error}",
                file=sys.stderr,
            )
            return _ReceiptRecovery.RETRY_FINALIZATION
        return _ReceiptRecovery.READY_TO_CLOSE

    @staticmethod
    def _proof_reaches_parent(
        ctx: IntegrationContext, proof: TaskIntegrationProof
    ) -> bool:
        try:
            return ctx.forge.is_merge_commit_reachable_from(
                proof.source_sha, ctx.base_branch.removeprefix("origin/")
            )
        except Exception as error:
            print(
                "Warning: Failed to validate receipt proof for "
                f"#{proof.issue_number}: {error}",
                file=sys.stderr,
            )
            return False


class _ReceiptRecovery(StrEnum):
    UNRECOVERED = "unrecovered"
    RETRY_FINALIZATION = "retry_finalization"
    DEFERRED = "deferred"
    READY_TO_CLOSE = "ready_to_close"
