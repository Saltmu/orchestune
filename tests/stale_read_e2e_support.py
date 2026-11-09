"""Real Git/Forge scenarios; only review and external job execution are simulated."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from orchestune.claim.read_checkpoint import checkpoint_marker
from orchestune.infra.git_cli import run_git
from orchestune.integrator.attempt import start_required_review
from orchestune.integrator.finalization import render_integration_receipt
from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.integrator.read_gate import build_integration_read_report
from orchestune.models import IssueRecord, PrRecord
from orchestune.outcome_record import OutcomeRecord, ReviewSummary
from orchestune.read_dependencies.contracts import (
    CheckpointAvailability,
    ReadCheckpointV1,
    ReadCoverage,
    ReadEvidenceV1,
    ReadSource,
    parent_read_snapshot_ref,
    seal_evidence,
)
from orchestune.read_dependencies.git import lookup_resource
from orchestune.review.semantic_gate import (
    ChildSourceRef,
    IntegrationAttemptIdentity,
    SemanticFixResultV1,
    SemanticReviewPhase,
    format_semantic_fix_result_marker,
)
from tests.dispatch.test_postcycle_semantic_review import ATTEMPT, PR, World

PARENT = 1033
PARENT_REF = f"refs/heads/parent/issue-{PARENT}"
SNAPSHOT_REF = parent_read_snapshot_ref(PARENT)


def git(root: Path, *args: str) -> str:
    return run_git(list(args), cwd=root).stdout.strip()


def validation_body(report, round_number=1, **changes):
    payload = {
        "version": 1,
        "round_number": round_number,
        "head_sha": report.candidate_head_sha,
        "report_digest": report.digest,
        "decisions": [
            {
                "candidate_id": c.candidate_id,
                "conclusion": "compatible",
                "rationale": "Reviewed the recorded input against the consumer",
            }
            for c in report.candidates
        ],
    } | changes
    return "```orchestune-read-validation\n" + json.dumps(payload) + "\n```"


class StaleReadScenario:
    """Preserve source evidence while advancing a real parent and snapshot pair."""

    def __init__(self, root, forge, monkeypatch):
        self.root, self.forge = root / "local", forge
        self.remote = root / "origin.git"
        git(root, "init", "--bare", str(self.remote))
        git(root, "init", "-b", "main", str(self.root))
        git(self.root, "config", "user.name", "E2E")
        git(self.root, "config", "user.email", "e2e@example.invalid")
        git(self.root, "remote", "add", "origin", str(self.remote))
        self.base = self.commit("registry.py", "v1\n")
        git(self.root, "push", "origin", f"HEAD:{PARENT_REF}")
        monkeypatch.setattr(
            forge, "get_repository_slug", lambda: "owner/repo", raising=False
        )
        monkeypatch.setattr(forge, "add_comment", forge.create_issue_comment)
        self.ctx = SimpleNamespace(
            repository_root=self.root,
            forge=forge,
            integration_pr_number=PR,
            config=SimpleNamespace(
                stale_read_gate="required",
                semantic_review_mode="required",
                force_integration_review=False,
            ),
            semantic_review_state=None,
            semantic_review_override=None,
            read_report=None,
            read_gate_error=None,
            read_gate_decision=None,
        )

    def commit(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        git(self.root, "add", "--", path)
        git(self.root, "commit", "-m", f"update {path}")
        return git(self.root, "rev-parse", "HEAD")

    def record(self, number, base, head, reads=(), *, integrated=False):
        claim = f"claim-{number}"
        evidence = seal_evidence(
            ReadEvidenceV1(
                repository="owner/repo",
                issue_number=number,
                claim_id=claim,
                input_base_sha=base,
                head_sha=head,
                declared_reads=reads,
                observations=tuple(
                    lookup_resource(base, p, cwd=self.root) for p in reads
                ),
                source=ReadSource.ADAPTER_OBSERVED,
                coverage=ReadCoverage.COMPLETE,
            )
        )
        outcome = OutcomeRecord(
            issue=number,
            result="done",
            claim_id=claim,
            completion_id=f"completion-{number}",
            head_sha=head,
            read_evidence=evidence,
            review=ReviewSummary(bot="codex", verdict="pass", reviewed_head_sha=head),
        )
        self.forge.seed_issue(
            IssueRecord(
                number,
                "child",
                f"```yaml\nsubtask_id: child-{number}\nparent_issue_number: {PARENT}\n```",
                ("status:done",),
                "2026-01-01",
                parent={"number": PARENT},
            )
        )
        self.forge.create_issue_comment(number, outcome.render())
        self.forge.create_issue_comment(
            number,
            checkpoint_marker(
                ReadCheckpointV1(
                    repository="owner/repo",
                    issue_number=number,
                    claim_id=claim,
                    input_base_sha=base,
                    availability=CheckpointAvailability.RECORDED,
                )
            ),
        )
        if integrated:
            self.include(number, head)
        return ChildSourceRef(number, f"child-{number}", head)

    def include(self, number, head):
        proof = TaskIntegrationProof(number, f"child-{number}", f"child-{number}", head)
        self.forge.create_issue_comment(
            number, render_integration_receipt(proof, f"parent/issue-{PARENT}")
        )
        self.forge.add_label(number, "integration:included")
        self.forge.close_issue(number, "integrated")

    def attempt(self, parent, *children):
        self.ctx.attempt_identity = IntegrationAttemptIdentity.create(
            PARENT, parent, children
        )
        self.sync_pr(git(self.root, "rev-parse", "HEAD"))
        return start_required_review(
            self.ctx, lambda forge, number: SimpleNamespace(record=self.outcome(number))
        )

    def outcome(self, number):
        from orchestune.outcome_record import parse_from_comments

        return parse_from_comments(self.forge.list_all_issue_comments(number))

    def sync_pr(self, head):
        self.forge.seed_pr(
            PrRecord(
                PR,
                self.ctx.attempt_identity.temp_branch,
                (),
                base_ref=f"parent/issue-{PARENT}",
                is_cross_repository=False,
                head_sha=head,
            ),
            "open",
        )

    def report(self):
        return build_integration_read_report(
            self.ctx, git(self.root, "rev-parse", "HEAD")
        )

    def approve(self, **validation_changes):
        """Simulate only the trusted review job's judgment, never gate evaluation."""
        report = self.report()
        state = self.ctx.semantic_review_state
        operation = f"{state.attempt_id[:12]}-g0-r1-fix-0"
        table = self.forge.create_issue_comment(
            PR, validation_body(report, **validation_changes)
        )
        self.forge.create_issue_comment(
            PR,
            format_semantic_fix_result_marker(
                SemanticFixResultV1(
                    state.attempt_id,
                    operation,
                    state.head_sha,
                    None,
                    table["id"],
                    "completed",
                )
            ),
        )
        self.ctx.semantic_review_state = replace(
            state,
            phase=SemanticReviewPhase.PASSED,
            round=1,
            operation_id=operation,
            read_report_digest=report.digest,
            reviewed_read_report_digest=report.digest,
        )
        return report

    def refs(self):
        return tuple(
            git(self.remote, "for-each-ref", "--format=%(objectname)", ref)
            for ref in (PARENT_REF, SNAPSHOT_REF)
        )


class GitReviewWorld(World):
    """Existing external-job fixture with a real Git read-context builder."""

    def __init__(self, scenario):
        self.scenario = scenario
        super().__init__(scenario.root, stale_read_gate="required")
        for comment in scenario.forge.list_all_issue_comments(PR):
            if "orchestune-read-report" in comment["body"]:
                self.post(PR, comment["body"])
        from orchestune.integrator.read_gate import make_default_read_context_builder

        self.read_context_builder = make_default_read_context_builder(
            scenario.forge, scenario.root
        )
        self.pr_head = scenario.ctx.semantic_review_state.head_sha
        self.refs = {f"parent/issue-{PARENT}": scenario.ctx.attempt_identity.base_sha}
        from orchestune.branch_naming import build_task_branch_name

        self.refs.update(
            {
                build_task_branch_name(c.issue_number, c.subtask_id): c.source_sha
                for c in scenario.ctx.attempt_identity.child_sources
            }
        )

    def forge(self):
        forge = super().forge()
        forge.get_authenticated_user.return_value = "orchestune"
        forge.list_notice_trusted_authors.return_value = frozenset({"orchestune"})
        forge.get_repository_slug.return_value = "owner/repo"
        forge.list_open_prs.side_effect = lambda *a, **k: [
            PrRecord(
                PR,
                self.scenario.ctx.attempt_identity.temp_branch,
                (),
                base_ref=f"parent/issue-{PARENT}",
                is_cross_repository=False,
                head_sha=self.pr_head,
            )
        ]
        return forge

    def post(self, number, body, author="orchestune"):
        return super().post(number, body, author)

    def seed(self, **changes):
        state = self.scenario.ctx.semantic_review_state
        return super().seed(
            **(
                dict(
                    attempt_id=state.attempt_id,
                    head_sha=state.head_sha,
                    base_sha=state.base_sha,
                    child_sources=state.child_sources,
                    ci_verified_sha=state.head_sha,
                    read_report_digest=state.read_report_digest,
                )
                | changes
            )
        )

    def finish_job(self, *, output, **changes):
        super().finish_job(output=output)
        # World models LLM output; bind that output to this real integration identity.
        from orchestune.read_dependencies.detection import detect_stale_reads

        for comment in reversed(self.comments[PR]):
            if "orchestune:semantic-fix-result" in comment["body"]:
                comment["body"] = comment["body"].replace(
                    ATTEMPT, self.state.attempt_id
                )
                break
        table = next(
            c
            for c in reversed(self.comments[PR])
            if "```orchestune-review-judgments" in c["body"]
        )
        built = self.read_context_builder(self.state, self.state.head_sha)
        report = detect_stale_reads(built.inputs)
        table["body"] += "\n" + validation_body(report, self.state.round, **changes)

    def handoff(self):
        """Deliver the job/runner fixture's PR evidence to the integrator Forge."""
        scenario = self.scenario
        for comment in self.comments[PR]:
            copied = dict(comment, issue_number=PR)
            if copied["user"]["login"] == "orchestune":
                copied["user"] = {"login": scenario.forge.get_authenticated_user()}
            # Preserve result->judgment IDs when the API fixture changes hands.
            scenario.forge.event_comments.append(copied)
        scenario.ctx.semantic_review_state = self.state
        scenario.sync_pr(self.state.head_sha)
