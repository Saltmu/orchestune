"""Tests for orchestune.local_trial models, contracts, codecs, and digests."""

import pytest

from orchestune.local_trial.contracts import (
    KNOWN_TRIAL_ERROR_CODES,
    AgentRunner,
    TrialError,
    agent_payload_schema,
    canonical_digest,
    decode_agent_payload,
    decode_attempt,
    decode_state,
    encode_attempt,
    encode_state,
    plan_digest,
    request_digest,
    workspace_digest,
)
from orchestune.local_trial.models import (
    MAX_ARTIFACT_BYTES,
    MAX_CONTEXT_BYTES,
    MAX_CYCLES,
    MAX_REVIEWS,
    PROCESS_TAIL_BYTES,
    SCHEMA_VERSION,
    AgentInfo,
    AgentPayload,
    AgentRequest,
    AttemptInput,
    AttemptKind,
    AttemptResult,
    Budget,
    Change,
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
    TrialView,
    WorkspaceIdentity,
    WorkspaceSnapshot,
)


def _valid_plan() -> TrialPlan:
    return TrialPlan(
        goal="Greet the world",
        acceptance_criteria=("Tests pass",),
        write_paths=("greeting.py",),
        discipline_paths=("AGENTS.md",),
        discipline_notes=(),
        ci_argv=("python", "-m", "pytest"),
        discipline_status=DisciplineStatus.COMPATIBLE,
    )


def _valid_attempt_input(kind: AttemptKind = AttemptKind.PLAN) -> AttemptInput:
    return AttemptInput(
        trial_id="1" * 32,
        attempt_id="2" * 32,
        kind=kind,
        input_head_sha="a" * 40,
        base_sha="b" * 40,
        plan_digest="c" * 64 if kind != AttemptKind.PLAN else None,
        plan_revision=1,
        cwd="/repo/worktree",
        artifact_dir="/repo/artifacts",
        started_at="2026-11-07T12:00:00Z",
    )


def _valid_process_record() -> ProcessRecord:
    return ProcessRecord(
        outcome=ProcessStatus.SUCCESS,
        returncode=0,
        stop_confirmed=True,
        elapsed_seconds=12.5,
        timeout_seconds=1800.0,
        stdout_tail="done",
        stderr_tail="",
        detail="",
    )


def _valid_agent_info() -> AgentInfo:
    return AgentInfo(
        name="codex",
        executable="/usr/bin/codex",
        version="1.0.0",
    )


def _valid_state() -> TrialState:
    repo = RepositoryIdentity(
        source_root="/repo",
        primary_root="/repo",
        common_dir="/repo/.git",
        base_ref="main",
        base_sha="b" * 40,
    )
    ws = WorkspaceIdentity(
        implementation_path="/repo/worktree",
        review_path="/repo/review_worktree",
        branch="claude/issue-1734-trial-contract",
    )
    req = RequestSnapshot(
        text="Fix something",
        digest=request_digest("Fix something"),
        ci_argv_override=None,
    )
    return TrialState(
        schema_version=SCHEMA_VERSION,
        trial_id="1" * 32,
        repository=repo,
        workspace=ws,
        request=req,
        agent=_valid_agent_info(),
        plan=None,
        approved_plan_digest=None,
        approved_worktree_digest=None,
        approved_index_digest=None,
        phase=TrialPhase.PREPARING,
        checkpoint=Checkpoint.PREPARE,
        reason=None,
        revision=0,
        budget=Budget(plan_revision=1, cycles_started=0, reviews_started=0),
        head_sha="b" * 40,
        pending_attempt=None,
        pending_git=None,
        attempt_ids=(),
        ci_evidence=None,
        review_evidence=None,
        findings=(),
        responses=(),
        stop_confirmations=(),
        controller=None,
        observation=Observation(
            started_at="2026-11-07T12:00:00Z",
            updated_at="2026-11-07T12:00:00Z",
            elapsed_seconds=0.0,
            setup_steps=(),
            notes=(),
        ),
        decision=None,
    )


class TestModelsImmutability:
    def test_frozen_dataclass(self) -> None:
        plan = _valid_plan()
        with pytest.raises(AttributeError):
            plan.goal = "Changed"  # type: ignore[misc]

    def test_constants(self) -> None:
        assert SCHEMA_VERSION == 1
        assert MAX_CYCLES == 3
        assert MAX_REVIEWS == 3
        assert MAX_ARTIFACT_BYTES == 262144
        assert PROCESS_TAIL_BYTES == 1048576
        assert MAX_CONTEXT_BYTES == 1048576

    def test_auxiliary_dtos(self) -> None:
        ctrl = ControllerInfo(pid=1234, started_at="2026-11-07T12:00:00Z", token="tok")
        assert ctrl.pid == 1234
        dec = Decision(value=DecisionValue.ADOPTED, recorded_at="2026-11-07T12:00:00Z")
        assert dec.value == DecisionValue.ADOPTED
        stop = StopConfirmation(attempt_id="2" * 32, recorded_at="2026-11-07T12:00:00Z")
        assert stop.attempt_id == "2" * 32
        intent = GitIntent(
            operation=GitOperation.COMMIT,
            pre_head_sha="0" * 40,
            expected_tree_sha="1" * 40,
            target_sha="2" * 40,
            allowed_paths=("file.py",),
        )
        assert intent.operation == GitOperation.COMMIT
        ev = Evidence(
            attempt_id="2" * 32,
            base_sha="0" * 40,
            head_sha="1" * 40,
            plan_digest="d" * 64,
            ci_argv_digest="a" * 64,
            passed=True,
        )
        assert ev.passed is True
        view = TrialView(
            state=_valid_state(),
            effective_phase=TrialPhase.PREPARING,
            reason=None,
            diff="",
            attempts=(),
            next_actions=("start",),
            worktree_digest=None,
            ci_candidates=(),
            ready_is_current=False,
        )
        assert view.effective_phase == TrialPhase.PREPARING
        req = AgentRequest(
            input=_valid_attempt_input(),
            prompt="Do something",
            schema={},
            context={},
        )
        assert req.prompt == "Do something"


class TestAgentPayloadCodec:
    def test_plan_roundtrip(self) -> None:
        payload = AgentPayload(
            schema_version=SCHEMA_VERSION,
            trial_id="1" * 32,
            attempt_id="2" * 32,
            kind=AttemptKind.PLAN,
            input_head_sha="a" * 40,
            plan_digest=None,
            payload=PlanPayload(plan=_valid_plan()),
        )
        data = {
            "schema_version": 1,
            "trial_id": "1" * 32,
            "attempt_id": "2" * 32,
            "kind": "plan",
            "input_head_sha": "a" * 40,
            "plan_digest": None,
            "payload": {
                "plan": {
                    "goal": "Greet the world",
                    "acceptance_criteria": ["Tests pass"],
                    "write_paths": ["greeting.py"],
                    "discipline_paths": ["AGENTS.md"],
                    "discipline_notes": [],
                    "ci_argv": ["python", "-m", "pytest"],
                    "discipline_status": "compatible",
                }
            },
        }
        decoded = decode_agent_payload(data)
        assert decoded == payload

    def test_implementation_roundtrip(self) -> None:
        payload = AgentPayload(
            schema_version=SCHEMA_VERSION,
            trial_id="1" * 32,
            attempt_id="2" * 32,
            kind=AttemptKind.IMPLEMENT,
            input_head_sha="a" * 40,
            plan_digest="d" * 64,
            payload=ImplementationPayload(
                summary="Changes done",
                responses=(
                    FindingResponse(
                        finding_id="f1",
                        action=FindingAction.FIXED,
                        explanation="Fixed bug",
                    ),
                ),
            ),
        )
        data = {
            "schema_version": 1,
            "trial_id": "1" * 32,
            "attempt_id": "2" * 32,
            "kind": "implement",
            "input_head_sha": "a" * 40,
            "plan_digest": "d" * 64,
            "payload": {
                "summary": "Changes done",
                "responses": [
                    {
                        "finding_id": "f1",
                        "action": "fixed",
                        "explanation": "Fixed bug",
                    }
                ],
            },
        }
        decoded = decode_agent_payload(data)
        assert decoded == payload

    def test_review_roundtrip(self) -> None:
        payload = AgentPayload(
            schema_version=SCHEMA_VERSION,
            trial_id="1" * 32,
            attempt_id="2" * 32,
            kind=AttemptKind.REVIEW,
            input_head_sha="a" * 40,
            plan_digest="d" * 64,
            payload=ReviewPayload(
                verdict=ReviewVerdict.CHANGES_REQUESTED,
                findings=(
                    Finding(
                        id="f1",
                        severity=FindingSeverity.HIGH,
                        path="greeting.py",
                        line=10,
                        reason="Bug",
                        requested_change="Fix it",
                    ),
                ),
            ),
        )
        data = {
            "schema_version": 1,
            "trial_id": "1" * 32,
            "attempt_id": "2" * 32,
            "kind": "review",
            "input_head_sha": "a" * 40,
            "plan_digest": "d" * 64,
            "payload": {
                "verdict": "changes_requested",
                "findings": [
                    {
                        "id": "f1",
                        "severity": "high",
                        "path": "greeting.py",
                        "line": 10,
                        "reason": "Bug",
                        "requested_change": "Fix it",
                    }
                ],
            },
        }
        decoded = decode_agent_payload(data)
        assert decoded == payload

    def test_ci_kind_rejected(self) -> None:
        data = {
            "schema_version": 1,
            "trial_id": "1" * 32,
            "attempt_id": "2" * 32,
            "kind": "ci",
            "input_head_sha": "a" * 40,
            "plan_digest": None,
            "payload": {},
        }
        with pytest.raises(TrialError) as exc_info:
            decode_agent_payload(data)
        assert exc_info.value.code == "invalid_input"


class TestAttemptResultCodec:
    def test_attempt_roundtrip_with_payload(self) -> None:
        inp = _valid_attempt_input(AttemptKind.PLAN)
        proc = _valid_process_record()
        payload = AgentPayload(
            schema_version=SCHEMA_VERSION,
            trial_id="1" * 32,
            attempt_id="2" * 32,
            kind=AttemptKind.PLAN,
            input_head_sha="a" * 40,
            plan_digest=None,
            payload=PlanPayload(plan=_valid_plan()),
        )
        result = AttemptResult(
            schema_version=SCHEMA_VERSION,
            input=inp,
            process=proc,
            agent_payload=payload,
            ended_at="2026-11-07T12:00:15Z",
        )
        encoded = encode_attempt(result)
        decoded = decode_attempt(encoded)
        assert decoded == result

    def test_attempt_ci_roundtrip(self) -> None:
        inp = _valid_attempt_input(AttemptKind.CI)
        proc = _valid_process_record()
        result = AttemptResult(
            schema_version=SCHEMA_VERSION,
            input=inp,
            process=proc,
            agent_payload=None,
            ended_at="2026-11-07T12:00:15Z",
        )
        encoded = encode_attempt(result)
        decoded = decode_attempt(encoded)
        assert decoded == result

    def test_ci_with_non_null_agent_payload_rejected(self) -> None:
        inp = _valid_attempt_input(AttemptKind.CI)
        proc = _valid_process_record()
        payload = AgentPayload(
            schema_version=SCHEMA_VERSION,
            trial_id="1" * 32,
            attempt_id="2" * 32,
            kind=AttemptKind.PLAN,
            input_head_sha="a" * 40,
            plan_digest=None,
            payload=PlanPayload(plan=_valid_plan()),
        )
        result = AttemptResult(
            schema_version=SCHEMA_VERSION,
            input=inp,
            process=proc,
            agent_payload=payload,
            ended_at="2026-11-07T12:00:15Z",
        )
        encoded = encode_attempt(result)
        with pytest.raises(TrialError) as exc_info:
            decode_attempt(encoded)
        assert exc_info.value.code == "invalid_input"


class TestStateCodec:
    def test_state_roundtrip(self) -> None:
        state = _valid_state()
        encoded = encode_state(state)
        decoded = decode_state(encoded)
        assert decoded == state


class TestStrictDecoderRejections:
    @pytest.mark.parametrize(
        ("mutation", "expected_code"),
        [
            ({"schema_version": 2}, "schema_unsupported"),
            ({"extra_key": "val"}, "invalid_input"),
            ({"trial_id": "not-32-hex"}, "invalid_input"),
            ({"revision": -1}, "invalid_input"),
            ({"revision": True}, "invalid_input"),
            ({"revision": "not_an_int"}, "invalid_input"),
            ({"phase": "not_a_valid_phase"}, "invalid_input"),
        ],
    )
    def test_state_decoder_rejections(self, mutation: dict, expected_code: str) -> None:
        state = _valid_state()
        data = encode_state(state)
        data.update(mutation)
        with pytest.raises(TrialError) as exc_info:
            decode_state(data)
        assert exc_info.value.code == expected_code

    def test_state_decoder_missing_required_key(self) -> None:
        state = _valid_state()
        data = encode_state(state)
        del data["trial_id"]
        with pytest.raises(TrialError) as exc_info:
            decode_state(data)
        assert exc_info.value.code == "invalid_input"

    def test_review_payload_duplicate_finding_ids(self) -> None:
        data = {
            "verdict": "changes_requested",
            "findings": [
                {
                    "id": "f1",
                    "severity": "high",
                    "path": "a.py",
                    "line": 1,
                    "reason": "r1",
                    "requested_change": "c1",
                },
                {
                    "id": "f1",
                    "severity": "low",
                    "path": "b.py",
                    "line": 2,
                    "reason": "r2",
                    "requested_change": "c2",
                },
            ],
        }
        with pytest.raises(TrialError) as exc_info:
            decode_agent_payload(
                {
                    "schema_version": 1,
                    "trial_id": "1" * 32,
                    "attempt_id": "2" * 32,
                    "kind": "review",
                    "input_head_sha": "a" * 40,
                    "plan_digest": "d" * 64,
                    "payload": data,
                }
            )
        assert exc_info.value.code == "invalid_input"

    def test_review_payload_pass_with_findings(self) -> None:
        data = {
            "verdict": "pass",
            "findings": [
                {
                    "id": "f1",
                    "severity": "high",
                    "path": "a.py",
                    "line": 1,
                    "reason": "r1",
                    "requested_change": "c1",
                }
            ],
        }
        with pytest.raises(TrialError) as exc_info:
            decode_agent_payload(
                {
                    "schema_version": 1,
                    "trial_id": "1" * 32,
                    "attempt_id": "2" * 32,
                    "kind": "review",
                    "input_head_sha": "a" * 40,
                    "plan_digest": "d" * 64,
                    "payload": data,
                }
            )
        assert exc_info.value.code == "invalid_input"

    def test_process_record_non_finite_float(self) -> None:
        inp = _valid_attempt_input(AttemptKind.CI)
        proc_data = {
            "outcome": "success",
            "returncode": 0,
            "stop_confirmed": True,
            "elapsed_seconds": float("nan"),
            "timeout_seconds": 900.0,
            "stdout_tail": "",
            "stderr_tail": "",
            "detail": "",
        }
        data = {
            "schema_version": 1,
            "input": encode_attempt(
                AttemptResult(
                    schema_version=1,
                    input=inp,
                    process=_valid_process_record(),
                    agent_payload=None,
                    ended_at="2026-11-07T12:00:15Z",
                )
            )["input"],
            "process": proc_data,
            "agent_payload": None,
            "ended_at": "2026-11-07T12:00:15Z",
        }
        with pytest.raises(TrialError) as exc_info:
            decode_attempt(data)
        assert exc_info.value.code == "invalid_input"


class TestDigests:
    def test_canonical_digest_key_order_and_whitespace(self) -> None:
        d1 = canonical_digest({"b": 2, "a": [1, 2]})
        d2 = canonical_digest({"a": [1, 2], "b": 2})
        assert d1 == d2

    def test_request_digest_exact_bytes(self) -> None:
        text_lf = "hello\nworld"
        text_crlf = "hello\r\nworld"
        assert request_digest(text_lf) != request_digest(text_crlf)

    def test_plan_digest_sensitivity(self) -> None:
        plan = _valid_plan()
        req_d = request_digest("test request")
        base_sha = "0" * 40
        d_orig = plan_digest(plan, req_d, base_sha)

        # change base_sha
        assert plan_digest(plan, req_d, "1" * 40) != d_orig
        # change request_digest
        assert plan_digest(plan, request_digest("other"), base_sha) != d_orig
        # change write_paths
        plan_w = TrialPlan(
            goal=plan.goal,
            acceptance_criteria=plan.acceptance_criteria,
            write_paths=("other.py",),
            discipline_paths=plan.discipline_paths,
            discipline_notes=plan.discipline_notes,
            ci_argv=plan.ci_argv,
            discipline_status=plan.discipline_status,
        )
        assert plan_digest(plan_w, req_d, base_sha) != d_orig

    def test_workspace_digest(self) -> None:
        ws = WorkspaceSnapshot(
            head_sha="a" * 40,
            base_tip_sha="b" * 40,
            index_digest="c" * 64,
            content_digest="d" * 64,
            branch="main",
            changes=(Change(status="M", path="x.py", old_path=None),),
        )
        d = workspace_digest(ws)
        assert isinstance(d, str) and len(d) == 64


class TestSchemasAndProtocols:
    @pytest.mark.parametrize(
        "kind",
        [AttemptKind.PLAN, AttemptKind.IMPLEMENT, AttemptKind.FIX, AttemptKind.REVIEW],
    )
    def test_agent_payload_schema_valid(self, kind: AttemptKind) -> None:
        s = agent_payload_schema(kind)
        assert s["type"] == "object"
        assert s["additionalProperties"] is False
        assert "required" in s

    def test_agent_payload_schema_ci_fails(self) -> None:
        with pytest.raises(TrialError) as exc_info:
            agent_payload_schema(AttemptKind.CI)
        assert exc_info.value.code == "invalid_input"

    def test_trial_error_properties(self) -> None:
        err = TrialError(code="not_found", message="Item not found")
        assert err.code == "not_found"
        assert err.message == "Item not found"
        assert str(err) == "[not_found] Item not found"
        assert "not_found" in KNOWN_TRIAL_ERROR_CODES

    def test_protocols_runtime_checkable(self) -> None:
        class DummyRunner:
            def detect(self) -> tuple[AgentInfo, ...]:
                return ()

            def run(self, request: AgentRequest, agent: AgentInfo) -> AttemptResult:
                raise NotImplementedError

        assert issubclass(DummyRunner, AgentRunner)
