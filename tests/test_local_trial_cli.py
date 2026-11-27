"""Unit tests for orchestune local trial CLI (#1734, #1738)."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from orchestune.local_trial import cli as trial_cli
from orchestune.local_trial.cli import main
from orchestune.local_trial.contracts import (
    AgentRunner,
    TrialError,
    TrialStore,
    TrialWorkspace,
    plan_digest,
    request_digest,
)
from orchestune.local_trial.models import (
    AgentInfo,
    Budget,
    Checkpoint,
    Decision,
    DecisionValue,
    DisciplineStatus,
    Observation,
    RepositoryIdentity,
    RequestSnapshot,
    TrialPhase,
    TrialPlan,
    TrialState,
    TrialView,
    WorkspaceIdentity,
)
from orchestune.local_trial.service import TrialDependencies


def _dummy_state(trial_id: str, phase: TrialPhase = TrialPhase.PLANNED) -> TrialState:
    req_text = "Implement feature XYZ"
    req_snap = RequestSnapshot(
        text=req_text,
        digest=request_digest(req_text),
        ci_argv_override=None,
    )
    repo = RepositoryIdentity(
        source_root="/tmp/test",
        primary_root="/tmp/test",
        common_dir="/tmp/test/.git",
        base_ref="main",
        base_sha="a" * 40,
    )
    ws = WorkspaceIdentity(
        implementation_path="/tmp/test/impl",
        review_path="/tmp/test/rev",
        branch="trial/1",
    )
    plan = TrialPlan(
        goal="Implement feature XYZ",
        acceptance_criteria=("criterion 1",),
        write_paths=("foo.py",),
        discipline_paths=(),
        discipline_status=DisciplineStatus.COMPATIBLE,
        discipline_notes=(),
        ci_argv=("pytest", "-v"),
    )
    return TrialState(
        schema_version=1,
        trial_id=trial_id,
        repository=repo,
        workspace=ws,
        request=req_snap,
        agent=AgentInfo(name="codex", executable="codex", version="1.0.0"),
        plan=plan,
        approved_plan_digest=None,
        approved_worktree_digest=None,
        approved_index_digest=None,
        phase=phase,
        checkpoint=Checkpoint.AWAIT_APPROVAL,
        reason="approval_required" if phase == TrialPhase.PLANNED else None,
        revision=1,
        budget=Budget(plan_revision=0, cycles_started=0, reviews_started=0),
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
            started_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            elapsed_seconds=10.0,
        ),
        decision=None,
    )


def _dummy_view(
    state: TrialState,
    *,
    effective_phase: TrialPhase | None = None,
    reason: str | None = None,
) -> TrialView:
    eff = effective_phase or state.phase
    reas = reason if reason is not None else state.reason
    return TrialView(
        state=state,
        effective_phase=eff,
        reason=reas,
        diff="diff --git a/foo.py b/foo.py\n+hello",
        attempts=(),
        next_actions=(),
        worktree_digest="c" * 64,
        ci_candidates=(("pytest",),),
        ready_is_current=(eff == TrialPhase.READY),
    )


def _make_fake_deps(state: TrialState | None = None) -> TrialDependencies:
    store = MagicMock(spec=TrialStore)
    if state is not None:
        store.active_trial_id.return_value = state.trial_id
        store.load.return_value = state
    else:
        store.active_trial_id.return_value = None
        store.load.side_effect = TrialError("not_found", "Trial not found")

    ws = MagicMock(spec=TrialWorkspace)
    agents = MagicMock(spec=AgentRunner)
    agents.detect.return_value = [
        AgentInfo(name="codex", executable="codex", version="1.0.0")
    ]
    proc = MagicMock()
    return TrialDependencies(
        store=store,
        workspace=ws,
        agents=agents,
        process_runner=proc,
        clock=lambda: "2026-01-01T00:00:00Z",
        new_id=lambda: "d" * 32,
        controller_alive=lambda pid: True,
    )


class TestArgumentParsingAndValidation:
    def test_help_returns_zero(self, capsys):
        assert main(["--help"]) == 0
        assert main(["start", "--help"]) == 0
        assert main(["resume", "--help"]) == 0
        assert main(["status", "--help"]) == 0
        assert main(["result", "--help"]) == 0

    def test_unknown_subcommand_or_arg_returns_two(self):
        # #1901: `start`/`resume`/`result` no longer fail on missing
        # request/id alone; those cases are TTY/active-controlled and are
        # covered by TestRequestInputAndActiveId.
        assert main(["unknown"]) == 2
        assert main(["start", "--unknown"]) == 2

    @pytest.mark.parametrize(
        "bad_id",
        ["not-hex", "short", "a" * 31, "a" * 33, ""],
    )
    def test_invalid_id_returns_two(self, bad_id):
        assert main(["status", bad_id]) == 2
        assert main(["resume", bad_id]) == 2
        assert main(["result", bad_id]) == 2

    @pytest.mark.parametrize(
        "bad_digest",
        ["not-hex", "short", "a" * 63, "a" * 65],
    )
    def test_invalid_digest_returns_two(self, bad_digest):
        trial_id = "a" * 32
        assert main(["resume", trial_id, "--approve-plan", bad_digest]) == 2
        assert main(["resume", trial_id, "--confirm-worktree", bad_digest]) == 2

    def test_forbidden_flag_combinations_returns_two(self):
        trial_id = "a" * 32
        # confirm-stopped with others
        assert (
            main(
                [
                    "resume",
                    trial_id,
                    "--confirm-stopped",
                    "b" * 32,
                    "--approve-plan",
                    "c" * 64,
                ]
            )
            == 2
        )
        assert (
            main(
                [
                    "resume",
                    trial_id,
                    "--confirm-stopped",
                    "b" * 32,
                    "--request-file",
                    "x",
                ]
            )
            == 2
        )
        assert (
            main(
                [
                    "resume",
                    trial_id,
                    "--confirm-stopped",
                    "b" * 32,
                    "--ci-command",
                    "pytest",
                ]
            )
            == 2
        )
        assert (
            main(
                [
                    "resume",
                    trial_id,
                    "--confirm-stopped",
                    "b" * 32,
                    "--confirm-worktree",
                    "c" * 64,
                ]
            )
            == 2
        )
        # approve-plan with request-file or ci-command
        assert (
            main(
                ["resume", trial_id, "--approve-plan", "c" * 64, "--request-file", "x"]
            )
            == 2
        )
        assert (
            main(
                [
                    "resume",
                    trial_id,
                    "--approve-plan",
                    "c" * 64,
                    "--ci-command",
                    "pytest",
                ]
            )
            == 2
        )

    def test_request_file_validation(self, tmp_path):
        non_existent = tmp_path / "nope.txt"
        assert main(["start", "--request-file", str(non_existent)]) == 2

        dir_path = tmp_path / "somedir"
        dir_path.mkdir()
        assert main(["start", "--request-file", str(dir_path)]) == 2

        empty_file = tmp_path / "empty.txt"
        empty_file.write_text("   \n\t")
        assert main(["start", "--request-file", str(empty_file)]) == 2

        invalid_utf8 = tmp_path / "bad.txt"
        invalid_utf8.write_bytes(b"\xff\xfe\x00")
        assert main(["start", "--request-file", str(invalid_utf8)]) == 2

        large_file = tmp_path / "large.txt"
        large_file.write_bytes(b"a" * (1024 * 1024 + 1))
        assert main(["start", "--request-file", str(large_file)]) == 2

    def test_ci_command_syntax_error(self, tmp_path):
        req = tmp_path / "req.txt"
        req.write_text("valid request")
        assert (
            main(
                [
                    "start",
                    "--request-file",
                    str(req),
                    "--ci-command",
                    "pytest 'unclosed",
                ]
            )
            == 2
        )


class TestRequestInputAndActiveId:
    """#1901: --request / interactive prompt / omitted trial id."""

    def test_start_inline_request_uses_same_start_path(self):
        state = _dummy_state("1" * 32, TrialPhase.PLANNED)
        view = _dummy_view(state)
        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "start_trial", return_value=view) as mock_start,
            patch("sys.stdin.isatty", return_value=False),
        ):
            assert main(["start", "--request", "  padded request text  "]) == 0
        req = mock_start.call_args[0][0]
        assert req.request_text == "  padded request text  "

    @pytest.mark.parametrize(
        "request_text", ["task description\n", "task description\r\n"]
    )
    def test_start_request_file_preserves_trailing_newline(
        self, tmp_path, request_text
    ):
        req_file = tmp_path / "req.txt"
        req_file.write_bytes(request_text.encode("utf-8"))
        state = _dummy_state("1" * 32, TrialPhase.PLANNED)
        view = _dummy_view(state)
        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "start_trial", return_value=view) as mock_start,
            patch("sys.stdin.isatty", return_value=False),
        ):
            assert main(["start", "--request-file", str(req_file)]) == 0
        assert mock_start.call_args[0][0].request_text == request_text

    def test_start_request_and_request_file_mutually_exclusive(self, tmp_path):
        req = tmp_path / "req.txt"
        req.write_text("task description")
        assert main(["start", "--request", "inline", "--request-file", str(req)]) == 2

    def test_start_no_request_non_interactive_returns_two(self):
        with (
            patch.object(trial_cli, "make_dependencies") as mock_deps,
            patch("sys.stdin.isatty", return_value=False),
            patch("sys.stdout.isatty", return_value=False),
        ):
            assert main(["start"]) == 2
        mock_deps.assert_not_called()

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
    def test_start_blank_inline_request_returns_two(self, blank):
        with patch.object(trial_cli, "make_dependencies") as mock_deps:
            assert main(["start", "--request", blank]) == 2
        mock_deps.assert_not_called()

    def test_start_over_limit_inline_request_returns_two(self):
        with patch.object(trial_cli, "make_dependencies") as mock_deps:
            assert main(["start", "--request", "a" * (1024 * 1024 + 1)]) == 2
        mock_deps.assert_not_called()

    def test_start_non_utf8_encodable_inline_request_returns_two(self):
        with patch.object(trial_cli, "make_dependencies") as mock_deps:
            assert main(["start", "--request", "\ud800"]) == 2
        mock_deps.assert_not_called()

    def test_start_interactive_prompt_reads_one_line(self):
        state = _dummy_state("1" * 32, TrialPhase.PLANNED)
        view = _dummy_view(state)
        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "start_trial", return_value=view) as mock_start,
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch(
                "builtins.input", side_effect=["prompted request line", "no"]
            ) as mock_input,
        ):
            assert main(["start"]) == 0
        assert mock_start.call_args[0][0].request_text == "prompted request line"
        # request prompt + plan approval prompt, exactly one line each
        assert mock_input.call_count == 2

    def test_start_interactive_prompt_eof_returns_two(self):
        with (
            patch.object(trial_cli, "make_dependencies") as mock_deps,
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("builtins.input", side_effect=EOFError),
        ):
            assert main(["start"]) == 2
        mock_deps.assert_not_called()

    def test_start_interactive_prompt_ctrl_c_returns_130(self):
        with (
            patch.object(trial_cli, "make_dependencies") as mock_deps,
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("builtins.input", side_effect=KeyboardInterrupt),
        ):
            assert main(["start"]) == 130
        mock_deps.assert_not_called()

    def test_resume_omitted_id_uses_active_once(self):
        trial_id = "5" * 32
        state = _dummy_state(trial_id)
        view = _dummy_view(state)
        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "resume_trial", return_value=view) as mock_resume,
            patch("sys.stdin.isatty", return_value=False),
        ):
            assert main(["resume"]) == 0
        assert mock_resume.call_args[0][0].trial_id == trial_id
        assert fake_deps.store.active_trial_id.call_count == 1

    def test_resume_explicit_id_does_not_call_active(self):
        trial_id = "5" * 32
        state = _dummy_state(trial_id)
        view = _dummy_view(state)
        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "resume_trial", return_value=view),
            patch("sys.stdin.isatty", return_value=False),
        ):
            assert main(["resume", trial_id]) == 0
        assert fake_deps.store.active_trial_id.call_count == 0

    def test_resume_explicit_uppercase_id_lowercased(self):
        state = _dummy_state("a" * 32)
        view = _dummy_view(state)
        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "resume_trial", return_value=view) as mock_resume,
            patch("sys.stdin.isatty", return_value=False),
        ):
            assert main(["resume", "A" * 32]) == 0
        assert mock_resume.call_args[0][0].trial_id == "a" * 32

    @pytest.mark.parametrize("command", [["resume"], ["result"], ["status"]])
    def test_omitted_id_no_active_returns_two(self, command):
        fake_deps = _make_fake_deps(None)
        with patch.object(trial_cli, "make_dependencies", return_value=fake_deps):
            assert main(command) == 2
        assert fake_deps.store.active_trial_id.call_count == 1

    @pytest.mark.parametrize("err_code", ["state_invalid", "schema_unsupported"])
    def test_omitted_id_corrupt_pointer_returns_store_error(self, err_code):
        fake_deps = _make_fake_deps(None)
        fake_deps.store.active_trial_id.side_effect = TrialError(
            err_code, "corrupt pointer"
        )
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "resume_trial") as mock_resume,
        ):
            assert main(["resume"]) == 2
        mock_resume.assert_not_called()

    def test_omitted_id_uses_repo_store(self, tmp_path):
        trial_id = "7" * 32
        state = _dummy_state(trial_id)
        view = _dummy_view(state)
        fake_deps = _make_fake_deps(state)
        with (
            patch.object(
                trial_cli, "make_dependencies", return_value=fake_deps
            ) as mock_deps,
            patch.object(trial_cli, "resume_trial", return_value=view),
            patch("sys.stdin.isatty", return_value=False),
        ):
            assert main(["resume", "--repo", str(tmp_path)]) == 0
        mock_deps.assert_called_once_with(str(tmp_path))
        assert fake_deps.store.active_trial_id.call_count == 1

    def test_status_omitted_id_uses_active_once(self):
        trial_id = "2" * 32
        state = _dummy_state(trial_id, TrialPhase.NEEDS_ATTENTION)
        view = _dummy_view(
            state,
            effective_phase=TrialPhase.NEEDS_ATTENTION,
            reason="budget_exhausted",
        )
        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "inspect_trial", return_value=view) as mock_inspect,
        ):
            assert main(["status"]) == 0
        mock_inspect.assert_called_once_with(trial_id, fake_deps)
        assert fake_deps.store.active_trial_id.call_count == 1

    def test_result_omitted_id_inspect_uses_active_once(self, capsys):
        trial_id = "6" * 32
        state = _dummy_state(trial_id, TrialPhase.READY)
        view = _dummy_view(state, effective_phase=TrialPhase.READY)
        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "inspect_trial", return_value=view) as mock_inspect,
        ):
            assert main(["result", "--json"]) == 0
        mock_inspect.assert_called_once_with(trial_id, fake_deps)
        assert fake_deps.store.active_trial_id.call_count == 1
        data = json.loads(capsys.readouterr().out)
        assert data["trial_id"] == trial_id

    def test_result_omitted_id_decision_keep_uses_active_once(self):
        trial_id = "6" * 32
        state = _dummy_state(trial_id, TrialPhase.READY)
        view = _dummy_view(state)
        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "record_decision", return_value=view) as mock_dec,
        ):
            assert main(["result", "--decision", "keep"]) == 0
        mock_dec.assert_called_once_with(trial_id, "keep", fake_deps)
        assert fake_deps.store.active_trial_id.call_count == 1

    def test_result_explicit_id_redisplays_closed_trial(self, capsys):
        trial_id = "8" * 32
        state = _dummy_state(trial_id, TrialPhase.CLOSED)
        state = replace(
            state,
            decision=Decision(
                value=DecisionValue.KEEP, recorded_at="2026-01-01T00:00:00Z"
            ),
        )
        view = _dummy_view(state, effective_phase=TrialPhase.CLOSED)
        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "inspect_trial", return_value=view) as mock_inspect,
        ):
            assert main(["result", trial_id]) == 0
        mock_inspect.assert_called_once_with(trial_id, fake_deps)
        out = capsys.readouterr().out
        assert trial_id in out
        assert "decision: keep" in out


class TestAgentSelection:
    def test_zero_agents_detected(self, tmp_path):
        req = tmp_path / "req.txt"
        req.write_text("valid request")
        fake_deps = _make_fake_deps()
        fake_deps.agents.detect.return_value = []
        with patch.object(trial_cli, "make_dependencies", return_value=fake_deps):
            assert main(["start", "--request-file", str(req)]) == 2

    def test_multiple_agents_non_interactive(self, tmp_path):
        req = tmp_path / "req.txt"
        req.write_text("valid request")
        fake_deps = _make_fake_deps()
        fake_deps.agents.detect.return_value = [
            AgentInfo(name="claude", executable="claude", version="1"),
            AgentInfo(name="codex", executable="codex", version="1"),
        ]
        with patch.object(trial_cli, "make_dependencies", return_value=fake_deps):
            with patch("sys.stdin.isatty", return_value=False):
                assert main(["start", "--request-file", str(req)]) == 2

    def test_explicit_unavailable_agent(self, tmp_path):
        req = tmp_path / "req.txt"
        req.write_text("valid request")
        fake_deps = _make_fake_deps()
        fake_deps.agents.detect.return_value = [
            AgentInfo(name="claude", executable="claude", version="1")
        ]
        with patch.object(trial_cli, "make_dependencies", return_value=fake_deps):
            assert main(["start", "--request-file", str(req), "--agent", "codex"]) == 2


class TestInteractiveFlowAndExitCodes:
    def test_start_non_interactive_planned_approval_required(self, tmp_path):
        req = tmp_path / "req.txt"
        req.write_text("task description")
        state = _dummy_state("1" * 32, TrialPhase.PLANNED)
        view = _dummy_view(state)

        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "start_trial", return_value=view),
            patch("sys.stdin.isatty", return_value=False),
        ):
            code = main(["start", "--request-file", str(req)])
            assert code == 0

    def test_start_interactive_approved_with_yes(self, tmp_path):
        req = tmp_path / "req.txt"
        req.write_text("task description")
        state = _dummy_state("1" * 32, TrialPhase.PLANNED)
        planned_view = _dummy_view(state)

        ready_state = _dummy_state("1" * 32, TrialPhase.READY)
        ready_view = _dummy_view(ready_state)

        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "start_trial", return_value=planned_view),
            patch.object(
                trial_cli, "resume_trial", return_value=ready_view
            ) as mock_resume,
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("builtins.input", return_value="yes"),
        ):
            code = main(["start", "--request-file", str(req)])
            assert code == 0
            # Spy: verify same plan digest passed to resume_trial
            expected_digest = plan_digest(
                state.plan, state.request.digest, state.repository.base_sha
            )
            mock_resume.assert_called_once()
            call_req = mock_resume.call_args[0][0]
            assert call_req.approve_plan == expected_digest

    def test_start_interactive_declined_with_no(self, tmp_path):
        req = tmp_path / "req.txt"
        req.write_text("task description")
        state = _dummy_state("1" * 32, TrialPhase.PLANNED)
        planned_view = _dummy_view(state)

        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "start_trial", return_value=planned_view),
            patch.object(trial_cli, "resume_trial") as mock_resume,
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("builtins.input", return_value="no"),
        ):
            code = main(["start", "--request-file", str(req)])
            assert code == 0
            mock_resume.assert_not_called()

    def test_start_interactive_eof_preserves_planned(self, tmp_path):
        req = tmp_path / "req.txt"
        req.write_text("task description")
        state = _dummy_state("1" * 32, TrialPhase.PLANNED)
        planned_view = _dummy_view(state)

        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "start_trial", return_value=planned_view),
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("builtins.input", side_effect=EOFError),
        ):
            code = main(["start", "--request-file", str(req)])
            assert code == 0

    def test_start_keyboard_interrupt_returns_130(self, tmp_path):
        req = tmp_path / "req.txt"
        req.write_text("task description")
        state = _dummy_state("1" * 32, TrialPhase.PLANNED)
        planned_view = _dummy_view(state)

        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "start_trial", return_value=planned_view),
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("builtins.input", side_effect=KeyboardInterrupt),
        ):
            code = main(["start", "--request-file", str(req)])
            assert code == 130

    def test_start_ci_selection_keyboard_interrupt_returns_130(self, tmp_path):
        req = tmp_path / "req.txt"
        req.write_text("task description")
        state = _dummy_state("1" * 32, TrialPhase.PLANNED)
        ci_missing_view = _dummy_view(
            state,
            effective_phase=TrialPhase.PLANNED,
            reason="ci_missing",
        )

        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "start_trial", return_value=ci_missing_view),
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("builtins.input", side_effect=KeyboardInterrupt),
        ):
            code = main(["start", "--request-file", str(req)])
            assert code == 130

    @pytest.mark.parametrize(
        "phase,reason,expected_code",
        [
            (TrialPhase.READY, None, 0),
            (TrialPhase.PLANNED, "approval_required", 0),
            (TrialPhase.PLANNED, "ci_missing", 1),
            (TrialPhase.PLANNED, "discipline_incompatible", 1),
            (TrialPhase.NEEDS_ATTENTION, "budget_exhausted", 1),
            (TrialPhase.NEEDS_ATTENTION, "interrupted", 130),
            (TrialPhase.NEEDS_ATTENTION, "stop_unconfirmed", 1),
        ],
    )
    def test_exit_code_table_for_resume(self, phase, reason, expected_code):
        trial_id = "1" * 32
        state = _dummy_state(trial_id, phase)
        view = _dummy_view(state, effective_phase=phase, reason=reason)

        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "resume_trial", return_value=view),
            patch("sys.stdin.isatty", return_value=False),
        ):
            code = main(["resume", trial_id])
            assert code == expected_code

    @pytest.mark.parametrize(
        "command,expected",
        [
            (
                '"C:/Program Files/Python/python.exe" "scripts/new test.py"',
                ("C:/Program Files/Python/python.exe", "scripts/new test.py"),
            ),
            (
                '"/opt/python env/bin/python" "scripts/new test.py"',
                ("/opt/python env/bin/python", "scripts/new test.py"),
            ),
        ],
    )
    def test_resume_forwards_quoted_ci_command(self, command, expected):
        state = _dummy_state("1" * 32)
        with (
            patch.object(
                trial_cli, "make_dependencies", return_value=_make_fake_deps(state)
            ),
            patch.object(
                trial_cli, "resume_trial", return_value=_dummy_view(state)
            ) as resume,
            patch("sys.stdin.isatty", return_value=False),
        ):
            assert main(["resume", state.trial_id, "--ci-command", command]) == 0
        assert resume.call_args.args[0].ci_argv_override == expected

    def test_resume_trial_error_interrupted_returns_130(self):
        trial_id = "1" * 32
        fake_deps = _make_fake_deps()
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(
                trial_cli,
                "resume_trial",
                side_effect=TrialError("interrupted", "Execution interrupted"),
            ),
            patch("sys.stdin.isatty", return_value=False),
        ):
            code = main(["resume", trial_id])
            assert code == 130

    @pytest.mark.parametrize("err_code", ["agent_failed", "ci_failed", "review_failed"])
    def test_resume_trial_error_execution_failure_returns_one(self, err_code):
        trial_id = "1" * 32
        fake_deps = _make_fake_deps()
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(
                trial_cli,
                "resume_trial",
                side_effect=TrialError(err_code, f"Execution failed: {err_code}"),
            ),
            patch("sys.stdin.isatty", return_value=False),
        ):
            code = main(["resume", trial_id])
            assert code == 1


class TestStatusAndResult:
    def test_status_returns_zero_even_when_needs_attention(self, capsys):
        trial_id = "2" * 32
        state = _dummy_state(trial_id, TrialPhase.NEEDS_ATTENTION)
        view = _dummy_view(
            state, effective_phase=TrialPhase.NEEDS_ATTENTION, reason="budget_exhausted"
        )

        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "inspect_trial", return_value=view),
        ):
            assert main(["status", trial_id]) == 0
            captured = capsys.readouterr().out
            assert trial_id in captured
            assert "needs_attention" in captured

    def test_status_json_format(self, capsys):
        trial_id = "2" * 32
        state = _dummy_state(trial_id, TrialPhase.READY)
        view = _dummy_view(state, effective_phase=TrialPhase.READY)

        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "inspect_trial", return_value=view),
        ):
            assert main(["status", trial_id, "--json"]) == 0
            out = capsys.readouterr().out
            data = json.loads(out)
            assert "案内" not in out
            assert data["schema_version"] == 1
            assert data["trial_id"] == trial_id
            assert data["effective_phase"] == "ready"
            assert data["ready_is_current"] is True
            assert "attempts" in data
            assert "ci_candidates" in data

    def test_status_unknown_id_returns_two(self):
        trial_id = "3" * 32
        fake_deps = _make_fake_deps(None)
        with patch.object(trial_cli, "make_dependencies", return_value=fake_deps):
            assert main(["status", trial_id]) == 2

    def test_result_decision_keep_returns_zero(self):
        trial_id = "4" * 32
        state = _dummy_state(trial_id, TrialPhase.READY)
        view = _dummy_view(state)

        fake_deps = _make_fake_deps(state)
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(trial_cli, "record_decision", return_value=view) as mock_dec,
        ):
            assert main(["result", trial_id, "--decision", "keep"]) == 0
            mock_dec.assert_called_once_with(trial_id, "keep", fake_deps)

    def test_result_decision_rejected_returns_two(self):
        trial_id = "4" * 32
        fake_deps = _make_fake_deps()
        with (
            patch.object(trial_cli, "make_dependencies", return_value=fake_deps),
            patch.object(
                trial_cli,
                "record_decision",
                side_effect=TrialError("state_invalid", "Not ready for adoption"),
            ),
        ):
            assert main(["result", trial_id, "--decision", "adopted"]) == 2


class TestRootCliDelegation:
    def test_orchestune_trial_root_delegation(self, monkeypatch):
        from orchestune.cli import main as root_main

        monkeypatch.setattr("sys.argv", ["orchestune", "trial", "status", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            root_main()
        assert exc_info.value.code == 0
