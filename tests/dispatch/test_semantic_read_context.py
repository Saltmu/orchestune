"""Read report binding and fail-closed transport in the semantic review loop."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from orchestune.dispatch.semantic_review.evidence import SemanticReviewEvidence, _Stop
from orchestune.dispatch.semantic_review.read_context import (
    ReadContextError,
    SemanticReadContext,
)
from orchestune.integrator.read_context import ReadContextResult
from orchestune.integrator.read_reports import save_read_report
from orchestune.read_dependencies.contracts import (
    ReadContextInputs,
    ReadCoverage,
    ReaderInput,
    StaleReadGate,
    WriterChanges,
    seal_report,
)
from orchestune.read_dependencies.detection import detect_stale_reads
from orchestune.review.semantic_gate import (
    SemanticReviewPhase,
    SemanticReviewRetryV1,
    SemanticReviewStateV1,
    format_semantic_fix_result_marker,
    format_semantic_review_retry_marker,
    parse_semantic_fix_result_marker,
)
from tests.dispatch.test_postcycle_semantic_review import (
    FIXED,
    HEAD,
    PR,
    SELF,
    World,
)
from tests.read_dependency_test_support import MODIFY, READER, TWO_VERSIONS
from tests.test_read_report_codec import sample_report
from tests.test_read_report_transport import FakeForge


def report():
    return seal_report(
        replace(
            sample_report(), coverage=ReadCoverage.COMPLETE, unknowns=(), digest=None
        )
    )


def state(**changes):
    current = report()
    return replace(
        SemanticReviewStateV1(
            "a" * 64,
            current.parent_issue_number,
            42,
            SemanticReviewPhase.REVIEW_PENDING,
            1,
            current.candidate_head_sha,
            current.parent_sha,
            (),
            read_report_digest=current.digest,
        ),
        **changes,
    )


class Forge(FakeForge):
    def get_repository_slug(self):
        return report().repository


def stored():
    forge = Forge()
    current = report()
    assert save_read_report(
        forge, 42, current, target_head_sha=current.candidate_head_sha
    ).report
    return forge, SemanticReadContext(forge, StaleReadGate.REQUIRED)


def validation(current, **changes):
    payload = {
        "version": 1,
        "round_number": 1,
        "head_sha": current.candidate_head_sha,
        "report_digest": current.digest,
        "decisions": [
            {
                "candidate_id": c.candidate_id,
                "conclusion": "compatible",
                "rationale": "call site checked",
            }
            for c in current.candidates
        ],
    } | changes
    return "```orchestune-read-validation\n" + json.dumps(payload) + "\n```"


def test_restore_verified_report_and_require_exact_candidate_decisions():
    _, reader = stored()
    context = reader.restore(state())
    assert context.report == report()
    assert context.digest == report().digest
    assert context.validation(validation(report()), state()) is not None
    assert context.validation(validation(report(), decisions=[]), state()) is None
    assert context.validation(validation(report(), head_sha="f" * 40), state()) is None
    marker = context.marker(state())
    assert marker in context.render(state())
    assert context.matches_trigger(context.render(state()), state())
    assert not context.matches_trigger(
        context.render(state()).replace("src/registry.py", "src/tampered.py"), state()
    )
    assert not context.matches_trigger("```\n" + marker + "\n```", state())


@pytest.mark.parametrize(
    "damage", ["missing", "tampered", "foreign", "parent", "repository"]
)
def test_required_corrupt_or_wrong_target_report_never_supplies_pass_digest(damage):
    forge, reader = stored()
    current = state()
    if damage == "missing":
        forge.comments.clear()
    elif damage == "tampered":
        forge.comments[0]["body"] += "broken"
    elif damage == "foreign":
        forge.comments[0]["author"] = {"login": "stranger"}
    elif damage == "parent":
        current = replace(current, base_sha="e" * 40)
    else:
        forge.get_repository_slug = lambda: "other/repository"
    context = reader.restore(current)
    assert context.digest is None
    assert context.error


def test_recompute_after_head_change_never_uses_old_report_or_validation():
    forge, _ = stored()
    calls = []

    def build(current, head):
        calls.append(head)
        return ReadContextResult(
            inputs=ReadContextInputs(
                repository=report().repository,
                parent_issue_number=current.parent_issue_number,
                parent_sha=current.base_sha,
                candidate_head_sha=head,
                readers=(),
            ),
            snapshot=None,
            bootstrap=False,
            coverage=ReadCoverage.COMPLETE,
        )

    reader = SemanticReadContext(forge, StaleReadGate.REQUIRED, build)
    context = reader.restore(state(), "f" * 40)
    assert calls == ["f" * 40]
    assert context.report.candidate_head_sha == "f" * 40
    assert context.digest != report().digest
    assert context.validation(validation(report()), state(head_sha="f" * 40)) is None
    assert reader.persist(context, 42).durable


def test_same_head_recompute_exposes_changed_digest_instead_of_reusing_pass():
    forge, _ = stored()
    inputs = ReadContextInputs(
        repository=report().repository,
        parent_issue_number=report().parent_issue_number,
        parent_sha=report().parent_sha,
        candidate_head_sha=report().candidate_head_sha,
        readers=(),
    )
    reader = SemanticReadContext(
        forge,
        StaleReadGate.REQUIRED,
        lambda *_: ReadContextResult(
            inputs=inputs,
            snapshot=None,
            bootstrap=False,
            coverage=ReadCoverage.COMPLETE,
        ),
    )
    assert reader.restore(state()).digest != state().read_report_digest


def test_off_skips_detection_and_advisory_retains_legacy_behavior():
    forge = Forge()

    def forbidden(*_):
        pytest.fail("off must not recompute")

    assert (
        SemanticReadContext(forge, StaleReadGate.OFF, forbidden).restore(state()).report
        is None
    )
    context = SemanticReadContext(forge, StaleReadGate.ADVISORY).restore(state())
    assert not context.required
    assert context.validation("", state()) is None


class ReadWorld(World):
    """Use an authenticated service account for the durable report transport."""

    def forge(self):
        forge = super().forge()
        forge.get_authenticated_user.return_value = "orchestune"
        forge.list_notice_trusted_authors.return_value = frozenset({"orchestune", SELF})
        forge.get_repository_slug.return_value = READER.repository
        return forge

    def post(self, number, body, author="orchestune"):
        return super().post(number, body, author)


def build_inputs(current, head):
    return ReadContextResult(
        inputs=ReadContextInputs(
            repository=READER.repository,
            parent_issue_number=current.parent_issue_number,
            parent_sha=current.base_sha,
            candidate_head_sha=head,
            readers=(ReaderInput(identity=READER, evidence=TWO_VERSIONS),),
            writers=(
                WriterChanges(
                    issue_number=11, claim_id="claim-writer", changes=(MODIFY,)
                ),
            ),
        ),
        snapshot=None,
        bootstrap=False,
        coverage=ReadCoverage.COMPLETE,
    )


def read_world(tmp_path, **config):
    world = ReadWorld(tmp_path, stale_read_gate="required", **config)
    world.read_context_builder = build_inputs
    return world


def judged_report(world):
    return detect_stale_reads(build_inputs(world.state, world.state.head_sha).inputs)


def finish_valid(world, output=None, **changes):
    world.finish_job(output=output)
    table = next(
        c
        for c in reversed(world.comments[PR])
        if "```orchestune-review-judgments" in c["body"]
    )
    table["body"] += "\n\n" + validation(
        judged_report(world), **({"round_number": world.state.round} | changes)
    )
    return table


def start_job(world):
    world.seed()
    world.cycle()
    world.review()
    world.cycle()


def test_required_fix_new_head_recomputes_report_runs_ci_and_re_reviews(tmp_path):
    world = read_world(tmp_path)
    start_job(world)
    old_digest = world.state.read_report_digest
    finish_valid(world, FIXED)
    world.cycle()
    assert world.state.head_sha == FIXED
    assert world.state.phase is SemanticReviewPhase.CI_VERIFICATION
    assert world.state.read_report_digest != old_digest
    assert world.state.reviewed_read_report_digest is None
    assert world.state.ci_verified_sha is None
    world.cycle()
    assert world.state.round == 2
    assert world.state.read_report_digest in world.triggers()[-1]
    assert FIXED in world.triggers()[-1]
    world.review("No remaining findings")
    world.cycle()
    finish_valid(world)
    world.cycle()
    assert world.state.phase is SemanticReviewPhase.PASSED
    assert world.state.reviewed_read_report_digest == world.state.read_report_digest
    assert len(world.ci) == 1 and world.ci[0][1] == FIXED
    assert world.labels[1101] == {"status:done"}


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "omitted",
        "unknown",
        "duplicate",
        "head",
        "round",
        "digest",
        "foreign",
        "result_head",
        "result_status",
        "unknown_id",
    ],
)
def test_required_candidate_validation_is_bound_to_signed_job_result(tmp_path, damage):
    world = read_world(tmp_path)
    start_job(world)
    current = judged_report(world)
    decisions = [
        {
            "candidate_id": c.candidate_id,
            "conclusion": "compatible",
            "rationale": "checked",
        }
        for c in current.candidates
    ]
    changes = {}
    if damage == "omitted":
        changes = {"decisions": []}
    elif damage == "unknown":
        changes = {"decisions": [decisions[0] | {"conclusion": "unknown"}]}
    elif damage == "duplicate":
        changes = {"decisions": decisions + decisions}
    elif damage == "head":
        changes = {"head_sha": FIXED}
    elif damage == "round":
        changes = {"round_number": 2}
    elif damage == "digest":
        changes = {"report_digest": "e" * 64}
    elif damage == "unknown_id":
        changes = {"decisions": [decisions[0] | {"candidate_id": "sr1-" + "f" * 64}]}
    if damage == "missing":
        world.finish_job(output=None)
    else:
        table = finish_valid(world, **changes)
        if damage == "foreign":
            table["user"]["login"] = "stranger"
        if damage in ("result_head", "result_status"):
            comment = world.comments[PR][-1]
            result = parse_semantic_fix_result_marker(comment["body"])
            changes = (
                {"input_sha": FIXED}
                if damage == "result_head"
                else {"status": "failed"}
            )
            comment["body"] = format_semantic_fix_result_marker(
                replace(result, **changes)
            )
    world.cycle()
    assert world.state.phase is SemanticReviewPhase.ESCALATED
    assert world.state.reviewed_read_report_digest is None


def test_required_unknown_context_stops_before_trigger_or_dispatch(tmp_path):
    world = read_world(tmp_path)
    world.read_context_builder = None
    world.seed()
    world.cycle()
    assert world.state.phase is SemanticReviewPhase.ESCALATED
    assert not world.triggers()
    assert not world.dispatched


def test_even_zero_candidates_and_findings_require_explicit_read_validation(tmp_path):
    world = read_world(tmp_path)

    def empty(s, h):
        return replace(
            build_inputs(s, h),
            inputs=replace(build_inputs(s, h).inputs, readers=(), writers=()),
        )

    world.read_context_builder = empty
    start_job(world)
    world.finish_job(output=None)
    world.cycle()
    assert world.state.phase is SemanticReviewPhase.ESCALATED


def test_report_change_does_not_invalidate_unstopped_job(tmp_path):
    world = read_world(tmp_path)
    start_job(world)
    old_digest = world.state.read_report_digest
    world.read_context_builder = lambda s, h: replace(
        build_inputs(s, h), inputs=replace(build_inputs(s, h).inputs, writers=())
    )
    world.cycle()
    assert world.state.escalation_reason == "invalidation_unconfirmed"
    assert world.state.read_report_digest == old_digest
    world.cycle()
    assert len(world.dispatched) == 1


def test_required_restart_and_ack_retry_keep_context_and_do_not_duplicate_fix(tmp_path):
    world = read_world(tmp_path, semantic_review_auto_retry=True)
    world.seed()
    world.cycle()
    digest = world.state.read_report_digest
    world.cycle(minutes=16)
    assert len(world.triggers()) == 2
    assert all(digest in t and HEAD in t for t in world.triggers())
    world.review()
    world.cycle()
    world.cycle()
    assert len(world.dispatched) == 1


def test_explicit_restart_attaches_same_report_and_respects_round_budget(tmp_path):
    world = read_world(tmp_path, max_semantic_review_rounds=2)
    start_job(world)
    world.finish_job(output=None)
    world.cycle()  # missing candidate judgments escalate
    for number in (1, 2):
        request = SemanticReviewRetryV1(
            world.state.attempt_id,
            HEAD,
            f"restart-{number}",
            "restart",
            "context corrected",
        )
        world.post(PR, format_semantic_review_retry_marker(request), "alice")
        world.cycle()
        if number == 1:
            assert world.state.round == 2
            assert world.state.read_report_digest in world.triggers()[-1]
            world.review("No defects")
            world.cycle()
            world.finish_job(output=None)
            world.cycle()
    assert world.state.phase is SemanticReviewPhase.ESCALATED
    assert len(world.triggers()) == 2


def test_lost_trigger_response_and_reserved_launch_are_not_repeated(tmp_path):
    world = read_world(tmp_path)
    forge = world.config.resolved_forge
    original = forge.create_issue_comment.side_effect
    lost = False

    def post(number, body):
        nonlocal lost
        result = original(number, body)
        if "orchestune:review-trigger" in body and not lost:
            lost = True
            raise RuntimeError("response lost")
        return result

    forge.create_issue_comment.side_effect = post
    world.seed()
    world.cycle()
    world.cycle()
    assert len(world.triggers()) == 1
    world.review()
    launches = []

    def launch(*_, **kwargs):
        launches.append(kwargs)
        raise RuntimeError("launch response lost")

    world.ops_overrides["dispatch_fix"] = launch
    world.cycle()
    world.cycle()
    assert len(launches) == 1
    assert "orchestune-read-validation" in launches[0]["read_context"]
    assert world.state.read_report_digest in launches[0]["read_context"]


def test_safe_reply_fallback_preserves_required_durable_context(monkeypatch):
    forge, reader = stored()
    context = reader.persist(reader.restore(state()), 42)
    evidence = SemanticReviewEvidence(forge)
    ctx = SimpleNamespace(read_context=context)
    monkeypatch.setattr(evidence, "trigger_reply", lambda *_: "Review\n```unclosed")
    reply = evidence.safe_reply(ctx, state(), "codex")
    assert context.marker(state()) in reply
    assert "orchestune-read-report:v1" in reply
    ctx.read_context = replace(context, durable=False)
    with pytest.raises(ReadContextError, match="durable"):
        evidence.safe_reply(ctx, state(), "codex")
    monkeypatch.setattr(evidence, "trigger_reply", lambda *_: "Review without context")
    with pytest.raises(_Stop, match="read_context_invalid"):
        evidence.safe_reply(ctx, state(), "codex")


def test_unavailable_coverage_builder_or_transport_is_never_a_required_pass():
    forge, reader = stored()
    unknown = seal_report(replace(sample_report(), digest=None))
    forge.comments.clear()
    save_read_report(forge, 42, unknown, target_head_sha=unknown.candidate_head_sha)
    context = reader.restore(state(read_report_digest=unknown.digest))
    assert context.digest is None and context.error
    with pytest.raises(ReadContextError):
        context.render(state())
    with pytest.raises(ReadContextError):
        reader.persist(context, 42)
    reader = SemanticReadContext(
        forge,
        "required",
        lambda *_: ReadContextResult(
            inputs=None,
            snapshot=None,
            bootstrap=False,
            coverage=ReadCoverage.UNAVAILABLE,
        ),
    )
    assert reader.restore(state(read_report_digest=None)).digest is None
    forge, reader = stored()
    context = reader.restore(state())
    forge.comments[0]["body"] += "broken"
    with pytest.raises(ReadContextError):
        reader.persist(context, 42)


def test_advisory_rendering_and_transport_failure_do_not_enforce_validation():
    forge, _ = stored()
    reader = SemanticReadContext(forge, "advisory")
    context = reader.restore(state())
    forge.comments[0]["body"] += "broken"
    failed = reader.persist(context, 42)
    assert not failed.required and failed.error
    assert "advisory" in failed.fix_instructions(state())
    assert reader.persist(failed, 42) == failed
    off = SemanticReadContext(forge, "off").restore(state())
    assert off.render(state()) == ""
    with pytest.raises(ReadContextError, match="heads differ"):
        context.render(state(head_sha=FIXED))
