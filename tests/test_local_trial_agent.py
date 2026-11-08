"""Unit and integration tests for LocalAgentRunner and prompt generation (#1735)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestune.infra.local_trial_agent import LocalAgentRunner, build_command
from orchestune.infra.managed_process import (
    ManagedProcessResult,
    ManagedProcessSpec,
    ProcessOutcome,
)
from orchestune.local_trial.contracts import TrialError
from orchestune.local_trial.models import (
    MAX_ARTIFACT_BYTES,
    MAX_CONTEXT_BYTES,
    SCHEMA_VERSION,
    AgentInfo,
    AgentRequest,
    AttemptInput,
    AttemptKind,
    AttemptResult,
    DisciplineStatus,
    Finding,
    FindingAction,
    FindingResponse,
    FindingSeverity,
    Observation,
    PlanPayload,
    ProcessRecord,
    ProcessStatus,
    RepositoryIdentity,
    RequestSnapshot,
    TrialPlan,
    TrialState,
    WorkspaceIdentity,
)
from orchestune.local_trial.prompts import build_agent_request


class FakeRunner:
    """Fake ProcessRunner recording calls and returning configured results."""

    def __init__(self, results: list[ManagedProcessResult] | None = None) -> None:
        self.calls: list[ManagedProcessSpec] = []
        self._results = list(results or [])

    def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
        self.calls.append(spec)
        if self._results:
            return self._results.pop(0)
        return ManagedProcessResult(
            outcome=ProcessOutcome.SUCCESS,
            stage=spec.stage,
            returncode=0,
            elapsed_seconds=0.1,
            timeout_seconds=spec.timeout_seconds,
            stdout_tail="v1.0.0\n",
            stderr_tail="",
            stop_confirmed=True,
            detail="",
        )


def _make_state(
    *,
    trial_id: str = "1" * 32,
    plan: TrialPlan | None = None,
    findings: tuple[Finding, ...] = (),
    responses: tuple[FindingResponse, ...] = (),
) -> TrialState:
    return TrialState(
        schema_version=SCHEMA_VERSION,
        trial_id=trial_id,
        repository=RepositoryIdentity(
            source_root="/repo",
            primary_root="/repo",
            common_dir="/repo/.git",
            base_ref="main",
            base_sha="0" * 40,
        ),
        workspace=WorkspaceIdentity(
            implementation_path="/repo",
            review_path="/repo-review",
            branch="trial-1",
        ),
        request=RequestSnapshot(text="Implement feature", digest="f" * 64),
        agent=AgentInfo(name="codex", executable="/bin/codex", version="0.167.0"),
        plan=plan,
        approved_plan_digest="p" * 64 if plan else None,
        approved_worktree_digest=None,
        approved_index_digest=None,
        phase="implementing",  # type: ignore[arg-type]
        checkpoint="implement",  # type: ignore[arg-type]
        reason=None,
        revision=1,
        budget=None,  # type: ignore[arg-type]
        head_sha="a" * 40,
        pending_attempt=None,
        pending_git=None,
        attempt_ids=(),
        ci_evidence=None,
        review_evidence=None,
        findings=findings,
        responses=responses,
        stop_confirmations=(),
        controller=None,
        observation=Observation(
            started_at="2026-11-07T12:00:00Z",
            updated_at="2026-11-07T12:00:00Z",
            elapsed_seconds=0.0,
        ),
        decision=None,
    )


def _make_attempt_input(
    tmp_path: Path,
    *,
    trial_id: str = "1" * 32,
    attempt_id: str = "2" * 32,
    kind: AttemptKind = AttemptKind.PLAN,
) -> AttemptInput:
    artifact_dir = tmp_path / ".orchestune" / "trial-artifacts" / attempt_id
    return AttemptInput(
        trial_id=trial_id,
        attempt_id=attempt_id,
        kind=kind,
        input_head_sha="a" * 40,
        base_sha="0" * 40,
        plan_digest="d" * 64 if kind != AttemptKind.PLAN else None,
        plan_revision=1,
        cwd=str(tmp_path),
        artifact_dir=str(artifact_dir),
        started_at="2026-11-07T12:00:00Z",
    )


# ==============================================================================
# 1. detect tests
# ==============================================================================


class TestDetect:
    def test_detect_neither_found(self) -> None:
        runner = FakeRunner()
        agent_runner = LocalAgentRunner(runner=runner, which_fn=lambda name: None)
        assert agent_runner.detect() == ()
        assert len(runner.calls) == 0

    def test_detect_claude_only(self) -> None:
        def fake_which(name: str) -> str | None:
            return "/usr/local/bin/claude" if name == "claude" else None

        runner = FakeRunner(
            [
                ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage="trial:detect:version",
                    returncode=0,
                    elapsed_seconds=0.05,
                    timeout_seconds=10.0,
                    stdout_tail="2.1.318\nExtra info\n",
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )
            ]
        )
        agent_runner = LocalAgentRunner(runner=runner, which_fn=fake_which)
        agents = agent_runner.detect()
        assert len(agents) == 1
        assert agents[0] == AgentInfo(
            name="claude",
            executable="/usr/local/bin/claude",
            version="2.1.318",
        )
        assert len(runner.calls) == 1
        assert runner.calls[0].args == ("/usr/local/bin/claude", "--version")
        assert runner.calls[0].timeout_seconds == 10.0

    def test_detect_both_order(self) -> None:
        def fake_which(name: str) -> str | None:
            return f"/bin/{name}"

        runner = FakeRunner(
            [
                ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage="trial:detect:version",
                    returncode=0,
                    elapsed_seconds=0.05,
                    timeout_seconds=10.0,
                    stdout_tail="claude-code 2.1.318\n",
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                ),
                ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage="trial:detect:version",
                    returncode=0,
                    elapsed_seconds=0.05,
                    timeout_seconds=10.0,
                    stdout_tail="codex 0.167.0\n",
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                ),
            ]
        )
        agent_runner = LocalAgentRunner(runner=runner, which_fn=fake_which)
        agents = agent_runner.detect()
        assert len(agents) == 2
        assert agents[0].name == "claude"
        assert agents[0].version == "claude-code 2.1.318"
        assert agents[1].name == "codex"
        assert agents[1].version == "codex 0.167.0"

    def test_detect_version_failure_raises_unavailable(self) -> None:
        def fake_which(name: str) -> str | None:
            return "/bin/claude" if name == "claude" else None

        runner = FakeRunner(
            [
                ManagedProcessResult(
                    outcome=ProcessOutcome.NONZERO_EXIT,
                    stage="trial:detect:version",
                    returncode=1,
                    elapsed_seconds=0.05,
                    timeout_seconds=10.0,
                    stdout_tail="",
                    stderr_tail="Crash",
                    stop_confirmed=True,
                    detail="Process failed",
                )
            ]
        )
        agent_runner = LocalAgentRunner(runner=runner, which_fn=fake_which)
        with pytest.raises(TrialError) as exc_info:
            agent_runner.detect()
        assert exc_info.value.code == "agent_unavailable"

    def test_detect_version_timeout_raises_unavailable(self) -> None:
        def fake_which(name: str) -> str | None:
            return "/bin/codex" if name == "codex" else None

        runner = FakeRunner(
            [
                ManagedProcessResult(
                    outcome=ProcessOutcome.TIMED_OUT,
                    stage="trial:detect:version",
                    returncode=None,
                    elapsed_seconds=10.0,
                    timeout_seconds=10.0,
                    stdout_tail="",
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="Timeout",
                )
            ]
        )
        agent_runner = LocalAgentRunner(runner=runner, which_fn=fake_which)
        with pytest.raises(TrialError) as exc_info:
            agent_runner.detect()
        assert exc_info.value.code == "agent_unavailable"


# ==============================================================================
# 2. build_command tests
# ==============================================================================


class TestBuildCommand:
    @pytest.mark.parametrize(
        ("kind", "expected_sandbox"),
        [
            (AttemptKind.PLAN, "read-only"),
            (AttemptKind.REVIEW, "read-only"),
            (AttemptKind.IMPLEMENT, "workspace-write"),
            (AttemptKind.FIX, "workspace-write"),
        ],
    )
    def test_codex_command_structure(
        self, tmp_path: Path, kind: AttemptKind, expected_sandbox: str
    ) -> None:
        inp = _make_attempt_input(tmp_path, kind=kind)
        req = AgentRequest(
            input=inp,
            prompt="Follow instructions in context",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        cmd = build_command(req, agent)
        assert isinstance(cmd, tuple)
        assert cmd[0] == "/bin/codex"
        assert cmd[1] == "exec"
        assert "--cd" in cmd
        assert cmd[cmd.index("--cd") + 1] == str(tmp_path)
        assert "--sandbox" in cmd
        assert cmd[cmd.index("--sandbox") + 1] == expected_sandbox
        assert "-c" in cmd
        assert cmd[cmd.index("-c") + 1] == 'approval_policy="never"'
        assert "--output-schema" in cmd
        schema_path = cmd[cmd.index("--output-schema") + 1]
        assert schema_path.endswith("schema.json")
        assert "--output-last-message" in cmd
        result_path = cmd[cmd.index("--output-last-message") + 1]
        assert str(inp.artifact_dir) in result_path
        assert "--color" in cmd
        assert cmd[cmd.index("--color") + 1] == "never"
        assert cmd[-1] == "Follow instructions in context"

        # Check absence of unsafe flags
        joined = " ".join(cmd)
        for forbidden in [
            "--model",
            "--dangerously-skip",
            "resume",
            "fork",
            "--worktree",
        ]:
            assert forbidden not in joined

    @pytest.mark.parametrize(
        "kind",
        [AttemptKind.PLAN, AttemptKind.REVIEW, AttemptKind.IMPLEMENT, AttemptKind.FIX],
    )
    def test_claude_command_structure(self, tmp_path: Path, kind: AttemptKind) -> None:
        inp = _make_attempt_input(tmp_path, kind=kind)
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"v": {"type": "string"}},
        }
        req = AgentRequest(
            input=inp,
            prompt="Follow instructions in context",
            schema=schema,
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="claude", executable="/bin/claude", version="2.1.318")
        cmd = build_command(req, agent)
        assert isinstance(cmd, tuple)
        assert cmd[0] == "/bin/claude"
        assert cmd[1] == "-p"
        assert cmd[2] == "Follow instructions in context"
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert "--json-schema" in cmd
        assert json.loads(cmd[cmd.index("--json-schema") + 1]) == schema
        assert "--permission-prompts" in cmd
        assert cmd[cmd.index("--permission-prompts") + 1] == "none"
        assert "--no-session-persistence" in cmd

        # Check absence of unsafe flags
        joined = " ".join(cmd)
        for forbidden in ["--bare", "bypass", "allow-all"]:
            assert forbidden not in joined


# ==============================================================================
# 3. ProcessOutcome & KeyboardInterrupt tests
# ==============================================================================


class TestProcessOutcomeAndInterrupt:
    def test_keyboard_interrupt_returns_interrupted_result(
        self, tmp_path: Path
    ) -> None:
        class InterruptRunner:
            def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
                raise KeyboardInterrupt

        inp = _make_attempt_input(tmp_path)
        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        agent_runner = LocalAgentRunner(runner=InterruptRunner())  # type: ignore[arg-type]

        result = agent_runner.run(req, agent)
        assert result.process.outcome == ProcessStatus.INTERRUPTED
        assert result.process.returncode is None
        assert result.process.stop_confirmed is False
        assert result.agent_payload is None

    @pytest.mark.parametrize(
        ("outcome", "returncode", "stop_confirmed"),
        [
            (ProcessOutcome.NONZERO_EXIT, 2, True),
            (ProcessOutcome.TIMED_OUT, None, True),
            (ProcessOutcome.START_FAILED, None, None),
            (ProcessOutcome.STOP_UNCONFIRMED, 0, False),
        ],
    )
    def test_process_failures_prevent_pass(
        self,
        tmp_path: Path,
        outcome: ProcessOutcome,
        returncode: int | None,
        stop_confirmed: bool | None,
    ) -> None:
        inp = _make_attempt_input(tmp_path)
        # Even if a result file is written on disk
        artifact_dir = Path(inp.artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        runner = FakeRunner(
            [
                ManagedProcessResult(
                    outcome=outcome,
                    stage="trial:plan:" + inp.attempt_id,
                    returncode=returncode,
                    elapsed_seconds=1.0,
                    timeout_seconds=1800.0,
                    stdout_tail="",
                    stderr_tail="error",
                    stop_confirmed=stop_confirmed,
                    detail="failed",
                )
            ]
        )
        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        agent_runner = LocalAgentRunner(runner=runner)

        result = agent_runner.run(req, agent)
        assert result.process.outcome == ProcessStatus(outcome.value)
        assert result.process.returncode == returncode
        assert result.process.stop_confirmed == stop_confirmed
        assert result.agent_payload is None


# ==============================================================================
# 4. Artifact extraction tests (Codex & Claude)
# ==============================================================================


class TestArtifactExtraction:
    def test_codex_successful_extraction(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path, kind=AttemptKind.PLAN)
        payload_data = {
            "schema_version": 1,
            "trial_id": inp.trial_id,
            "attempt_id": inp.attempt_id,
            "kind": "plan",
            "input_head_sha": inp.input_head_sha,
            "plan_digest": None,
            "payload": {
                "plan": {
                    "goal": "Write tests",
                    "acceptance_criteria": ["All tests pass"],
                    "write_paths": ["tests/test.py"],
                    "discipline_paths": ["AGENTS.md"],
                    "discipline_notes": ["Note"],
                    "ci_argv": ["pytest"],
                    "discipline_status": "compatible",
                }
            },
        }

        class FakeCodexRunner:
            def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
                args = list(spec.args)
                idx = args.index("--output-last-message")
                out_path = Path(args[idx + 1])
                out_path.write_text(json.dumps(payload_data), encoding="utf-8")
                return ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage=spec.stage,
                    returncode=0,
                    elapsed_seconds=2.0,
                    timeout_seconds=spec.timeout_seconds,
                    stdout_tail="done",
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )

        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        runner = LocalAgentRunner(runner=FakeCodexRunner())  # type: ignore[arg-type]
        result = runner.run(req, agent)

        assert result.process.outcome == ProcessStatus.SUCCESS
        assert result.agent_payload is not None
        assert isinstance(result.agent_payload.payload, PlanPayload)
        assert result.agent_payload.payload.plan.goal == "Write tests"

        # Verify result.json is written
        result_file = Path(inp.artifact_dir) / "result.json"
        assert result_file.exists()
        saved = json.loads(result_file.read_text(encoding="utf-8"))
        assert saved["trial_id"] == inp.trial_id

    def test_codex_missing_file_rejected(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path)
        runner = FakeRunner()  # Does not create the file
        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        runner_adapter = LocalAgentRunner(runner=runner)
        result = runner_adapter.run(req, agent)
        assert result.agent_payload is None
        assert "artifact_invalid" in result.process.detail

    def test_codex_invalid_json_rejected(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path)

        class BadJsonRunner:
            def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
                args = list(spec.args)
                idx = args.index("--output-last-message")
                out_path = Path(args[idx + 1])
                out_path.write_text("{not valid json", encoding="utf-8")
                return ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage=spec.stage,
                    returncode=0,
                    elapsed_seconds=1.0,
                    timeout_seconds=spec.timeout_seconds,
                    stdout_tail="",
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )

        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        runner = LocalAgentRunner(runner=BadJsonRunner())  # type: ignore[arg-type]
        result = runner.run(req, agent)
        assert result.agent_payload is None
        assert "artifact_invalid" in result.process.detail

    def test_codex_invalid_schema_rejected(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path)

        class BadSchemaRunner:
            def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
                args = list(spec.args)
                idx = args.index("--output-last-message")
                out_path = Path(args[idx + 1])
                out_path.write_text(json.dumps({"unknown_key": 123}), encoding="utf-8")
                return ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage=spec.stage,
                    returncode=0,
                    elapsed_seconds=1.0,
                    timeout_seconds=spec.timeout_seconds,
                    stdout_tail="",
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )

        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        runner = LocalAgentRunner(runner=BadSchemaRunner())  # type: ignore[arg-type]
        result = runner.run(req, agent)
        assert result.agent_payload is None
        assert "artifact_invalid" in result.process.detail

    def test_codex_different_attempt_id_preserved_as_observed(
        self, tmp_path: Path
    ) -> None:
        inp = _make_attempt_input(tmp_path, kind=AttemptKind.PLAN)
        other_attempt_id = "9" * 32
        payload_data = {
            "schema_version": 1,
            "trial_id": inp.trial_id,
            "attempt_id": other_attempt_id,
            "kind": "plan",
            "input_head_sha": inp.input_head_sha,
            "plan_digest": None,
            "payload": {
                "plan": {
                    "goal": "Write tests",
                    "acceptance_criteria": ["All tests pass"],
                    "write_paths": ["tests/test.py"],
                    "discipline_paths": ["AGENTS.md"],
                    "discipline_notes": ["Note"],
                    "ci_argv": ["pytest"],
                    "discipline_status": "compatible",
                }
            },
        }

        class FakeCodexRunner:
            def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
                args = list(spec.args)
                idx = args.index("--output-last-message")
                out_path = Path(args[idx + 1])
                out_path.write_text(json.dumps(payload_data), encoding="utf-8")
                return ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage=spec.stage,
                    returncode=0,
                    elapsed_seconds=2.0,
                    timeout_seconds=spec.timeout_seconds,
                    stdout_tail="done",
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )

        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        runner = LocalAgentRunner(runner=FakeCodexRunner())  # type: ignore[arg-type]
        result = runner.run(req, agent)
        assert result.agent_payload is not None
        assert result.agent_payload.attempt_id == other_attempt_id

    def test_codex_symlink_rejected(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path)

        class SymlinkRunner:
            def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
                args = list(spec.args)
                idx = args.index("--output-last-message")
                out_path = Path(args[idx + 1])
                target = out_path.parent / "real.json"
                target.write_text("{}", encoding="utf-8")
                out_path.symlink_to(target)
                return ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage=spec.stage,
                    returncode=0,
                    elapsed_seconds=1.0,
                    timeout_seconds=spec.timeout_seconds,
                    stdout_tail="",
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )

        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        runner = LocalAgentRunner(runner=SymlinkRunner())  # type: ignore[arg-type]
        result = runner.run(req, agent)
        assert result.agent_payload is None
        assert "artifact_invalid" in result.process.detail

    def test_codex_oversized_artifact_rejected(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path)

        class OversizedRunner:
            def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
                args = list(spec.args)
                idx = args.index("--output-last-message")
                out_path = Path(args[idx + 1])
                out_path.write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
                return ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage=spec.stage,
                    returncode=0,
                    elapsed_seconds=1.0,
                    timeout_seconds=spec.timeout_seconds,
                    stdout_tail="",
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )

        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        runner = LocalAgentRunner(runner=OversizedRunner())  # type: ignore[arg-type]
        result = runner.run(req, agent)
        assert result.agent_payload is None
        assert "artifact_invalid" in result.process.detail

    def test_claude_successful_extraction(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path, kind=AttemptKind.IMPLEMENT)
        payload_data = {
            "schema_version": 1,
            "trial_id": inp.trial_id,
            "attempt_id": inp.attempt_id,
            "kind": "implement",
            "input_head_sha": inp.input_head_sha,
            "plan_digest": "p" * 64,
            "payload": {
                "summary": "Fixes implemented",
                "responses": [
                    {
                        "finding_id": "f1",
                        "action": "fixed",
                        "explanation": "Applied patch",
                    }
                ],
            },
        }
        stdout_envelope = json.dumps(
            {
                "is_error": False,
                "permission_denials": [],
                "structured_output": payload_data,
            }
        )
        runner = FakeRunner(
            [
                ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage="trial:implement:" + inp.attempt_id,
                    returncode=0,
                    elapsed_seconds=1.5,
                    timeout_seconds=1800.0,
                    stdout_tail=stdout_envelope,
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )
            ]
        )
        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="claude", executable="/bin/claude", version="2.1.318")
        runner_adapter = LocalAgentRunner(runner=runner)
        result = runner_adapter.run(req, agent)

        assert result.agent_payload is not None
        assert result.agent_payload.kind == AttemptKind.IMPLEMENT
        assert (
            result.agent_payload.payload.summary == "Fixes implemented"  # type: ignore[union-attr]
        )

        result_file = Path(inp.artifact_dir) / "result.json"
        assert result_file.exists()

    def test_claude_missing_structured_output_rejected(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path)
        stdout_envelope = json.dumps(
            {
                "is_error": False,
                "permission_denials": [],
            }
        )
        runner = FakeRunner(
            [
                ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage="trial:plan:" + inp.attempt_id,
                    returncode=0,
                    elapsed_seconds=1.0,
                    timeout_seconds=1800.0,
                    stdout_tail=stdout_envelope,
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )
            ]
        )
        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="claude", executable="/bin/claude", version="2.1.318")
        runner_adapter = LocalAgentRunner(runner=runner)
        result = runner_adapter.run(req, agent)
        assert result.agent_payload is None
        assert "artifact_invalid" in result.process.detail

    def test_claude_invalid_json_rejected(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path)
        runner = FakeRunner(
            [
                ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage="trial:plan:" + inp.attempt_id,
                    returncode=0,
                    elapsed_seconds=1.0,
                    timeout_seconds=1800.0,
                    stdout_tail="not a valid json string",
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )
            ]
        )
        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="claude", executable="/bin/claude", version="2.1.318")
        runner_adapter = LocalAgentRunner(runner=runner)
        result = runner_adapter.run(req, agent)
        assert result.agent_payload is None
        assert "artifact_invalid" in result.process.detail

    def test_claude_is_error_reported(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path)
        stdout_envelope = json.dumps(
            {
                "is_error": True,
                "permission_denials": [],
                "error": "Model crashed",
            }
        )
        runner = FakeRunner(
            [
                ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage="trial:plan:" + inp.attempt_id,
                    returncode=0,
                    elapsed_seconds=1.0,
                    timeout_seconds=1800.0,
                    stdout_tail=stdout_envelope,
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )
            ]
        )
        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="claude", executable="/bin/claude", version="2.1.318")
        runner_adapter = LocalAgentRunner(runner=runner)
        result = runner_adapter.run(req, agent)
        assert result.agent_payload is None
        assert "agent_failed" in result.process.detail

    def test_claude_permission_denials_reported(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path)
        stdout_envelope = json.dumps(
            {
                "is_error": False,
                "permission_denials": ["write to /etc"],
                "structured_output": None,
            }
        )
        runner = FakeRunner(
            [
                ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage="trial:plan:" + inp.attempt_id,
                    returncode=0,
                    elapsed_seconds=1.0,
                    timeout_seconds=1800.0,
                    stdout_tail=stdout_envelope,
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )
            ]
        )
        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="claude", executable="/bin/claude", version="2.1.318")
        runner_adapter = LocalAgentRunner(runner=runner)
        result = runner_adapter.run(req, agent)
        assert result.agent_payload is None
        assert "agent_failed" in result.process.detail

    def test_claude_truncation_suspicion_rejected(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path)
        # Exactly 1MiB stdout_tail
        stdout_tail = "a" * (1024 * 1024)
        runner = FakeRunner(
            [
                ManagedProcessResult(
                    outcome=ProcessOutcome.SUCCESS,
                    stage="trial:plan:" + inp.attempt_id,
                    returncode=0,
                    elapsed_seconds=1.0,
                    timeout_seconds=1800.0,
                    stdout_tail=stdout_tail,
                    stderr_tail="",
                    stop_confirmed=True,
                    detail="",
                )
            ]
        )
        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="claude", executable="/bin/claude", version="2.1.318")
        runner_adapter = LocalAgentRunner(runner=runner)
        result = runner_adapter.run(req, agent)
        assert result.agent_payload is None
        assert "artifact_invalid" in result.process.detail


# ==============================================================================
# 5. Bounds & limits tests
# ==============================================================================


class TestBoundsAndLimits:
    def test_existing_result_file_refuses_execution(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path)
        artifact_dir = Path(inp.artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "result.json").write_text("{}", encoding="utf-8")

        runner = FakeRunner()
        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        runner_adapter = LocalAgentRunner(runner=runner)
        result = runner_adapter.run(req, agent)

        assert result.process.outcome == ProcessStatus.START_FAILED
        assert result.agent_payload is None
        assert len(runner.calls) == 0

    def test_artifact_dir_outside_cwd_refuses_execution(self, tmp_path: Path) -> None:
        outside_dir = tmp_path.parent / "outside"
        inp = AttemptInput(
            trial_id="1" * 32,
            attempt_id="2" * 32,
            kind=AttemptKind.PLAN,
            input_head_sha="a" * 40,
            base_sha="0" * 40,
            plan_digest=None,
            plan_revision=1,
            cwd=str(tmp_path),
            artifact_dir=str(outside_dir),
            started_at="2026-11-07T12:00:00Z",
        )
        runner = FakeRunner()
        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={"trial_id": inp.trial_id},
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        runner_adapter = LocalAgentRunner(runner=runner)
        result = runner_adapter.run(req, agent)

        assert result.process.outcome == ProcessStatus.START_FAILED
        assert result.agent_payload is None
        assert len(runner.calls) == 0

    def test_context_exceeding_1mib_refuses_execution(self, tmp_path: Path) -> None:
        inp = _make_attempt_input(tmp_path)
        runner = FakeRunner()
        req = AgentRequest(
            input=inp,
            prompt="prompt",
            schema={"type": "object"},
            context={
                "trial_id": inp.trial_id,
                "large_blob": "x" * (MAX_CONTEXT_BYTES + 10),
            },
        )
        agent = AgentInfo(name="codex", executable="/bin/codex", version="0.167.0")
        runner_adapter = LocalAgentRunner(runner=runner)
        result = runner_adapter.run(req, agent)

        assert result.process.outcome == ProcessStatus.START_FAILED
        assert result.agent_payload is None
        assert len(runner.calls) == 0


# ==============================================================================
# 6. build_agent_request tests
# ==============================================================================


class TestBuildAgentRequest:
    @pytest.mark.parametrize(
        "kind",
        [AttemptKind.PLAN, AttemptKind.IMPLEMENT, AttemptKind.FIX, AttemptKind.REVIEW],
    )
    def test_prompt_size_within_bound(self, tmp_path: Path, kind: AttemptKind) -> None:
        plan = TrialPlan(
            goal="Goal",
            acceptance_criteria=("AC1",),
            write_paths=("f.py",),
            discipline_paths=("AGENTS.md",),
            discipline_notes=("Notes",),
            ci_argv=("pytest",),
            discipline_status=DisciplineStatus.COMPATIBLE,
        )
        state = _make_state(
            plan=plan,
            findings=(
                Finding(
                    id="find1",
                    severity=FindingSeverity.HIGH,
                    path="foo.py",
                    line=10,
                    reason="Bug",
                    requested_change="Fix",
                ),
            ),
            responses=(
                FindingResponse(
                    finding_id="find1",
                    action=FindingAction.FIXED,
                    explanation="Done",
                ),
            ),
        )
        attempt = _make_attempt_input(tmp_path, kind=kind)
        req = build_agent_request(
            state=state,
            attempt=attempt,
            diff="diff --git a/foo.py b/foo.py\n+new line\n",
            previous_results=(),
            ci_candidates=(("pytest",),),
        )

        assert len(req.prompt.encode("utf-8")) <= 4096
        assert req.input == attempt
        assert req.context["trial_id"] == attempt.trial_id
        assert req.context["attempt_id"] == attempt.attempt_id
        assert req.context["kind"] == str(kind)
        assert req.context["request"] == state.request.text
        assert req.context["diff"] == "diff --git a/foo.py b/foo.py\n+new line\n"
        assert req.context["ci_candidates"] == [["pytest"]]

    def test_ci_result_extracted_from_previous_attempts(self, tmp_path: Path) -> None:
        state = _make_state()
        ci_input = _make_attempt_input(
            tmp_path, attempt_id="3" * 32, kind=AttemptKind.CI
        )
        ci_result = AttemptResult(
            schema_version=SCHEMA_VERSION,
            input=ci_input,
            process=ProcessRecord(
                outcome=ProcessStatus.SUCCESS,
                returncode=0,
                stop_confirmed=True,
                elapsed_seconds=12.5,
                timeout_seconds=900.0,
                stdout_tail="passed",
                stderr_tail="",
                detail="CI passed",
            ),
            agent_payload=None,
            ended_at="2026-11-07T12:05:00Z",
        )
        attempt = _make_attempt_input(tmp_path, kind=AttemptKind.IMPLEMENT)
        req = build_agent_request(
            state=state,
            attempt=attempt,
            diff="",
            previous_results=(ci_result,),
        )
        ci_res = req.context["ci_result"]
        assert isinstance(ci_res, dict)
        assert ci_res["outcome"] == "success"
        assert ci_res["target_sha"] == ci_input.input_head_sha
