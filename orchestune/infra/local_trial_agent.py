"""Agent adapter running Claude Code and Codex sessions for local trial (#1734, #1735)."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.managed_process import (
    ManagedProcessResult,
    ManagedProcessSpec,
    ProcessOutcome,
    ProcessRunner,
    get_default_runner,
)
from orchestune.local_trial.contracts import (
    TrialError,
    decode_agent_payload,
    encode_attempt,
)
from orchestune.local_trial.models import (
    AGENT_TIMEOUT_SECONDS,
    MAX_ARTIFACT_BYTES,
    MAX_CONTEXT_BYTES,
    PROCESS_TAIL_BYTES,
    SCHEMA_VERSION,
    AgentInfo,
    AgentPayload,
    AgentRequest,
    AttemptInput,
    AttemptKind,
    AttemptResult,
    ProcessRecord,
    ProcessStatus,
)

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CODEX_OUTPUT_FILENAME = "codex_output.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def build_command(request: AgentRequest, agent: AgentInfo) -> tuple[str, ...]:
    """Construct deterministic CLI command for the given agent and request."""
    artifact_dir = Path(request.input.artifact_dir)
    if agent.name == "codex":
        sandbox = (
            "read-only"
            if request.input.kind
            in (AttemptKind.PLAN, AttemptKind.REVIEW, "plan", "review")
            else "workspace-write"
        )
        schema_path = str(artifact_dir / "schema.json")
        result_path = str(artifact_dir / _CODEX_OUTPUT_FILENAME)
        return (
            agent.executable,
            "exec",
            "--cd",
            request.input.cwd,
            "--sandbox",
            sandbox,
            "-c",
            'approval_policy="never"',
            "--output-schema",
            schema_path,
            "--output-last-message",
            result_path,
            "--color",
            "never",
            request.prompt,
        )

    if agent.name == "claude":
        schema_json = json.dumps(
            request.schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            agent.executable,
            "-p",
            request.prompt,
            "--output-format",
            "json",
            "--json-schema",
            schema_json,
            "--permission-prompts",
            "none",
            "--no-session-persistence",
        )

    raise TrialError("invalid_input", f"Unsupported agent: {agent.name}")


class LocalAgentRunner:
    """Adapter executing local Codex and Claude Code processes."""

    def __init__(
        self,
        *,
        runner: ProcessRunner | None = None,
        which_fn: Callable[[str], str | None] | None = None,
    ) -> None:
        self._runner = runner if runner is not None else get_default_runner()
        self._which = which_fn if which_fn is not None else shutil.which

    def detect(self) -> tuple[AgentInfo, ...]:
        """Detect installed agent CLI binaries and verify their versions."""
        detected: list[AgentInfo] = []
        for name in ("claude", "codex"):
            executable = self._which(name)
            if executable is None:
                continue
            spec = ManagedProcessSpec(
                args=(executable, "--version"),
                stage="trial:detect:version",
                timeout_seconds=10.0,
                tail_bytes=PROCESS_TAIL_BYTES,
            )
            res = self._runner.run(spec)
            if (
                res.outcome != ProcessOutcome.SUCCESS
                or res.returncode != 0
                or res.stop_confirmed is not True
            ):
                reason = res.detail or res.stderr_tail or str(res.outcome)
                raise TrialError(
                    "agent_unavailable",
                    f"Agent '{name}' at '{executable}' failed version check: {reason}",
                )
            lines = [
                line.strip() for line in res.stdout_tail.splitlines() if line.strip()
            ]
            if not lines:
                raise TrialError(
                    "agent_unavailable",
                    f"Agent '{name}' at '{executable}' returned empty version",
                )
            detected.append(
                AgentInfo(name=name, executable=executable, version=lines[0])
            )
        return tuple(detected)

    def build_command(self, request: AgentRequest, agent: AgentInfo) -> tuple[str, ...]:
        """Expose build_command via runner instance."""
        return build_command(request, agent)

    def run(self, request: AgentRequest, agent: AgentInfo) -> AttemptResult:
        """Execute an attempt with bounded checks and structured artifact extraction."""
        inp = request.input
        started_at = _now_iso()

        preflight_error = self._validate_preflight(request)
        if preflight_error is not None:
            return self._make_early_failure(inp, preflight_error, started_at)

        artifact_dir = Path(inp.artifact_dir).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)

        write_json_atomic(artifact_dir / "context.json", request.context)
        write_json_atomic(artifact_dir / "schema.json", request.schema)

        cmd = self.build_command(request, agent)
        spec = ManagedProcessSpec(
            args=cmd,
            cwd=inp.cwd,
            stage=f"trial:{inp.kind}:{inp.attempt_id}",
            timeout_seconds=AGENT_TIMEOUT_SECONDS,
            tail_bytes=PROCESS_TAIL_BYTES,
        )

        try:
            res = self._runner.run(spec)
        except KeyboardInterrupt:
            return self._make_interrupted_result(inp, started_at)

        return self._process_execution_result(request, agent, res, artifact_dir)

    def _validate_preflight(self, request: AgentRequest) -> str | None:
        inp = request.input
        if not _ID_RE.match(inp.trial_id) or not _ID_RE.match(inp.attempt_id):
            return "start_failed: invalid trial_id or attempt_id format"

        cwd = Path(inp.cwd).resolve()
        artifact_dir = Path(inp.artifact_dir).resolve()
        try:
            artifact_dir.relative_to(cwd)
        except ValueError:
            return "start_failed: artifact_dir must be within cwd"

        if (artifact_dir / "result.json").exists() or (
            artifact_dir / _CODEX_OUTPUT_FILENAME
        ).exists():
            return "start_failed: result file already exists in artifact_dir"

        context_bytes = json.dumps(
            request.context, ensure_ascii=False, indent=2
        ).encode("utf-8")
        if len(context_bytes) > MAX_CONTEXT_BYTES:
            return f"start_failed: context size {len(context_bytes)} exceeds 1MiB limit"

        return None

    def _make_early_failure(
        self, inp: AttemptInput, detail: str, started_at: str
    ) -> AttemptResult:
        return AttemptResult(
            schema_version=SCHEMA_VERSION,
            input=inp,
            process=ProcessRecord(
                outcome=ProcessStatus.START_FAILED,
                returncode=None,
                stop_confirmed=True,
                elapsed_seconds=0.0,
                timeout_seconds=AGENT_TIMEOUT_SECONDS,
                stdout_tail="",
                stderr_tail="",
                detail=detail,
            ),
            agent_payload=None,
            ended_at=started_at,
        )

    def _make_interrupted_result(
        self, inp: AttemptInput, started_at: str
    ) -> AttemptResult:
        return AttemptResult(
            schema_version=SCHEMA_VERSION,
            input=inp,
            process=ProcessRecord(
                outcome=ProcessStatus.INTERRUPTED,
                returncode=None,
                stop_confirmed=False,
                elapsed_seconds=0.0,
                timeout_seconds=AGENT_TIMEOUT_SECONDS,
                stdout_tail="",
                stderr_tail="",
                detail="Process interrupted by KeyboardInterrupt",
            ),
            agent_payload=None,
            ended_at=started_at,
        )

    def _process_execution_result(
        self,
        request: AgentRequest,
        agent: AgentInfo,
        res: ManagedProcessResult,
        artifact_dir: Path,
    ) -> AttemptResult:
        ended_at = _now_iso()
        process_outcome = ProcessStatus(res.outcome.value)
        process_detail = res.detail

        agent_payload: AgentPayload | None = None
        if process_outcome == ProcessStatus.SUCCESS:
            agent_payload, artifact_detail = self._extract_artifact(
                request, agent, res, artifact_dir
            )
            if artifact_detail:
                process_detail = (
                    f"{process_detail}; {artifact_detail}"
                    if process_detail
                    else artifact_detail
                )

        proc_record = ProcessRecord(
            outcome=process_outcome,
            returncode=res.returncode,
            stop_confirmed=res.stop_confirmed,
            elapsed_seconds=res.elapsed_seconds,
            timeout_seconds=res.timeout_seconds,
            stdout_tail=res.stdout_tail,
            stderr_tail=res.stderr_tail,
            detail=process_detail,
        )

        attempt_res = AttemptResult(
            schema_version=SCHEMA_VERSION,
            input=request.input,
            process=proc_record,
            agent_payload=agent_payload,
            ended_at=ended_at,
        )

        if agent_payload is not None:
            encoded = encode_attempt(attempt_res)
            write_json_atomic(artifact_dir / "result.json", encoded["agent_payload"])

        return attempt_res

    def _extract_artifact(
        self,
        request: AgentRequest,
        agent: AgentInfo,
        res: ManagedProcessResult,
        artifact_dir: Path,
    ) -> tuple[AgentPayload | None, str]:
        if agent.name == "codex":
            return self._extract_codex_artifact(artifact_dir)
        if agent.name == "claude":
            return self._extract_claude_artifact(res)
        return None, f"artifact_invalid: unsupported agent {agent.name}"

    def _extract_codex_artifact(
        self, artifact_dir: Path
    ) -> tuple[AgentPayload | None, str]:
        path = artifact_dir / _CODEX_OUTPUT_FILENAME
        if not path.exists() or not path.is_file() or path.is_symlink():
            return None, "artifact_invalid: missing result_path or is symlink"
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            return (
                None,
                f"artifact_invalid: artifact size {path.stat().st_size} exceeds {MAX_ARTIFACT_BYTES} bytes",
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return decode_agent_payload(data), ""
        except (
            TrialError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
        ) as exc:
            return None, f"artifact_invalid: {exc}"

    def _extract_claude_artifact(
        self, res: ManagedProcessResult
    ) -> tuple[AgentPayload | None, str]:
        stdout_bytes = res.stdout_tail.encode("utf-8")
        if len(stdout_bytes) >= PROCESS_TAIL_BYTES:
            return (
                None,
                "artifact_invalid: stdout_tail reached or exceeded 1MiB (potential truncation)",
            )
        try:
            root = json.loads(res.stdout_tail)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return None, f"artifact_invalid: JSON decode error: {exc}"

        if not isinstance(root, dict):
            return None, "artifact_invalid: stdout root is not an object"
        if root.get("is_error") is True:
            err_msg = root.get("result") or root.get("error") or ""
            return None, f"agent_failed: is_error is True ({err_msg})"
        if bool(root.get("permission_denials")):
            return (
                None,
                f"agent_failed: permission denials: {root.get('permission_denials')}",
            )

        structured = root.get("structured_output")
        if structured is None or not isinstance(structured, dict):
            return None, "artifact_invalid: missing structured_output"

        try:
            return decode_agent_payload(structured), ""
        except (TrialError, ValueError, TypeError, KeyError) as exc:
            return None, f"artifact_invalid: {exc}"
