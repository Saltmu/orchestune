"""CLI entrypoints and argument parsing for local trial (#1734, #1738)."""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import re
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orchestune.local_trial.contracts import (
    TrialError,
    encode_attempt,
    encode_state,
    plan_digest,
)
from orchestune.local_trial.models import (
    DisciplineStatus,
    TrialPhase,
    TrialState,
    TrialView,
)
from orchestune.local_trial.service import (
    ResumeRequest,
    StartRequest,
    TrialDependencies,
    inspect_trial,
    make_dependencies,
    record_decision,
    resume_trial,
    start_trial,
)

_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_REQUEST_BYTES = 1024 * 1024


def _validate_hex_id(name: str, val: str) -> str:
    if not _ID_RE.fullmatch(val):
        raise TrialError("invalid_input", f"{name} must be a 32-character hex string")
    return val.lower()


def _validate_hex_digest(name: str, val: str) -> str:
    if not _DIGEST_RE.fullmatch(val):
        raise TrialError("invalid_input", f"{name} must be a 64-character hex string")
    return val.lower()


def _read_request_file(path_str: str) -> str:
    path = Path(path_str)
    if not path.exists():
        raise TrialError("invalid_input", f"Request file does not exist: {path_str}")
    if path.is_dir():
        raise TrialError(
            "invalid_input", f"Request file cannot be a directory: {path_str}"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise TrialError("invalid_input", f"Cannot stat request file: {exc}") from exc
    if size > _MAX_REQUEST_BYTES:
        raise TrialError("invalid_input", f"Request file exceeds 1MiB: {size} bytes")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TrialError("invalid_input", f"Cannot read request file: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TrialError(
            "invalid_input", f"Request file is not valid UTF-8: {exc}"
        ) from exc
    if not text.strip():
        raise TrialError("invalid_input", "Request file is empty")
    return text


def _parse_ci_override(ci_str: str | None) -> tuple[str, ...] | None:
    if ci_str is None:
        return None
    try:
        mod = importlib.import_module("orchestune.config_schema")
        parse_fn = mod.parse_ci_command
        parsed = parse_fn(ci_str)
    except Exception as exc:  # noqa: BLE001 - boundary: wrap parse error
        raise TrialError("invalid_input", f"Invalid CI command: {exc}") from exc
    return tuple(parsed)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestune trial",
        description="Local autonomous trial without GitHub setup",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    start_p = sub.add_parser("start", help="Start a new local trial")
    start_p.add_argument(
        "--request-file", required=True, help="Path to request text file"
    )
    start_p.add_argument("--repo", default=None, help="Repository root path")
    start_p.add_argument(
        "--agent", choices=["claude", "codex"], default=None, help="Trial agent"
    )
    start_p.add_argument(
        "--ci-command", default=None, help="Preferred CI command string"
    )

    status_p = sub.add_parser("status", help="Show status of local trial")
    status_p.add_argument("id", nargs="?", default=None, help="Trial ID (optional)")
    status_p.add_argument("--repo", default=None, help="Repository root path")
    status_p.add_argument("--json", action="store_true", help="Output JSON format")

    res_p = sub.add_parser("resume", help="Resume or confirm a local trial")
    res_p.add_argument("id", help="Trial ID")
    res_p.add_argument("--repo", default=None, help="Repository root path")
    res_p.add_argument(
        "--approve-plan", default=None, help="64-char plan digest to approve"
    )
    res_p.add_argument(
        "--confirm-stopped", default=None, help="Confirm stopped attempt ID"
    )
    res_p.add_argument(
        "--confirm-worktree", default=None, help="Confirm dirty worktree digest"
    )
    res_p.add_argument(
        "--request-file", default=None, help="Path to new request text file"
    )
    res_p.add_argument("--ci-command", default=None, help="New CI command string")

    ret_p = sub.add_parser("result", help="Inspect result or record adoption decision")
    ret_p.add_argument("id", help="Trial ID")
    ret_p.add_argument(
        "--decision", choices=["keep", "adopted"], default=None, help="Decision"
    )
    ret_p.add_argument("--repo", default=None, help="Repository root path")
    ret_p.add_argument("--json", action="store_true", help="Output JSON format")
    return parser


def _exit_code_for_view(view: TrialView) -> int:
    if view.reason == "interrupted":
        return 130
    if view.effective_phase == TrialPhase.READY:
        return 0
    if view.effective_phase == TrialPhase.PLANNED and view.reason in (
        "approval_required",
        None,
    ):
        return 0
    return 1


def _render_json(view: TrialView, trial_id: str) -> None:
    phase_str = (
        view.effective_phase.value
        if hasattr(view.effective_phase, "value")
        else str(view.effective_phase)
    )
    data: dict[str, Any] = {
        "schema_version": 1,
        "trial_id": trial_id,
        "state": encode_state(view.state) if view.state is not None else None,
        "effective_phase": phase_str,
        "reason": view.reason,
        "ready_is_current": view.ready_is_current,
        "worktree_digest": view.worktree_digest,
        "ci_candidates": [list(c) for c in view.ci_candidates],
        "attempts": [encode_attempt(att) for att in view.attempts],
        "diff": view.diff,
        "next_actions": list(view.next_actions),
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _format_path_quote(path: str) -> str:
    norm = path.replace("\\", "/")
    return shlex.quote(norm)


def _render_text_plan(state: TrialState, lines: list[str]) -> str | None:
    if state.plan is None:
        return None
    plan = state.plan
    lines.append(f"goal: {plan.goal}")
    if plan.acceptance_criteria:
        lines.append("acceptance_criteria:")
        for ac in plan.acceptance_criteria:
            lines.append(f"  - {ac}")
    if plan.write_paths:
        lines.append("write_paths:")
        for wp in plan.write_paths:
            lines.append(f"  - {wp}")
    lines.append(f"discipline_status: {plan.discipline_status}")
    if plan.discipline_notes:
        lines.append(f"discipline_notes: {plan.discipline_notes}")
    ci_disp = shlex.join(plan.ci_argv) if plan.ci_argv else "(none)"
    lines.append(f"ci_argv: {ci_disp}")
    p_dig = plan_digest(plan, state.request.digest, state.repository.base_sha)
    lines.append(f"plan_digest: {p_dig}")
    return p_dig


def _render_text_evidence(state: TrialState, lines: list[str]) -> None:
    lines.append(
        f"budget: cycles={state.budget.cycles_started}/3, reviews={state.budget.reviews_started}/3"
    )
    if state.ci_evidence:
        status_word = "passed" if state.ci_evidence.passed else "failed"
        lines.append(f"ci_evidence: {status_word} (head={state.ci_evidence.head_sha})")
    if state.review_evidence:
        status_word = "passed" if state.review_evidence.passed else "failed"
        lines.append(
            f"review_evidence: 同一製品の別セッションレビュー {status_word} "
            f"(head={state.review_evidence.head_sha})"
        )
    if state.findings:
        lines.append(f"findings ({len(state.findings)}):")
        for f in state.findings:
            lines.append(f"  - [{f.severity}] {f.path}:{f.line}: {f.reason}")
    if state.responses:
        lines.append(f"responses ({len(state.responses)}):")
        for r in state.responses:
            lines.append(f"  - [{r.action}] {r.finding_id}: {r.explanation}")


def _render_text_guidance(
    view: TrialView,
    state: TrialState,
    plan_dig: str | None,
    repo_arg: str,
    lines: list[str],
) -> None:
    quoted_repo = _format_path_quote(repo_arg)
    if view.effective_phase == TrialPhase.PLANNED and plan_dig is not None:
        lines.append("\n計画承認コマンド (Next Action):")
        lines.append(
            f"  orchestune trial resume {state.trial_id} --repo {quoted_repo} "
            f"--approve-plan {plan_dig}"
        )
    elif view.effective_phase == TrialPhase.READY and view.ready_is_current:
        src = _format_path_quote(state.repository.source_root)
        br = shlex.quote(state.workspace.branch)
        lines.append("\nローカルの採用候補 - 手動採用手順 (Manual adoption flow):")
        lines.append(
            f"  1. git -C {src} status  (リポジトリがcleanかつbaseであることを確認)"
        )
        lines.append(f"  2. git -C {src} merge --ff-only {br}")
        lines.append(
            f"  3. orchestune trial result {state.trial_id} --decision adopted --repo {src}"
        )
        lines.append(
            f"  (採用せず保持する場合: orchestune trial result {state.trial_id} --decision keep --repo {src})"
        )
    elif view.worktree_digest and view.reason == "worktree_dirty":
        lines.append("\n作業ツリー変更確認 (Next Action):")
        lines.append(
            f"  orchestune trial resume {state.trial_id} --repo {quoted_repo} "
            f"--confirm-worktree {view.worktree_digest}"
        )
    elif state.pending_attempt and view.reason == "execution_unknown":
        lines.append("\nプロセス停止確認 (Next Action):")
        lines.append(
            f"  orchestune trial resume {state.trial_id} --repo {quoted_repo} "
            f"--confirm-stopped {state.pending_attempt.attempt_id}"
        )


def _render_text(view: TrialView, *, is_result: bool, repo_arg: str) -> None:
    lines: list[str] = []
    tid = view.state.trial_id if view.state else "(unknown)"
    lines.append(f"trial_id: {tid}")
    phase_val = (
        view.effective_phase.value
        if hasattr(view.effective_phase, "value")
        else str(view.effective_phase)
    )
    if view.effective_phase == TrialPhase.READY and view.ready_is_current:
        lines.append("effective_phase: ready (ローカルの採用候補)")
    else:
        lines.append(f"effective_phase: {phase_val}")
    if view.reason:
        lines.append(f"reason: {view.reason}")
    if view.state is None:
        print("\n".join(lines))
        return

    st = view.state
    lines.append(f"base_sha: {st.repository.base_sha}")
    lines.append(f"head_sha: {st.head_sha}")
    lines.append(f"implementation_path: {st.workspace.implementation_path}")
    lines.append(f"review_path: {st.workspace.review_path}")
    p_dig = _render_text_plan(st, lines)
    if st.approved_plan_digest:
        lines.append(f"approved_plan_digest: {st.approved_plan_digest}")
    _render_text_evidence(st, lines)
    if is_result and view.diff:
        lines.append("\ndiff:\n" + view.diff)
    if is_result and st.decision:
        lines.append(
            f"decision: {st.decision.value} (recorded_at={st.decision.recorded_at})"
        )
    _render_text_guidance(view, st, p_dig, repo_arg, lines)
    print("\n".join(lines))


def _select_agent(
    deps: TrialDependencies, explicit_agent: str | None, is_interactive: bool
) -> str:
    detected = deps.agents.detect()
    if explicit_agent is not None:
        matches = [a for a in detected if a.name == explicit_agent]
        if not matches:
            raise TrialError(
                "agent_unavailable",
                f"Specified agent '{explicit_agent}' is unavailable",
            )
        return explicit_agent
    if not detected:
        raise TrialError("agent_unavailable", "No trial agent available")
    if len(detected) == 1:
        return detected[0].name
    if not is_interactive:
        raise TrialError(
            "invalid_input",
            f"Multiple agents available ({', '.join(a.name for a in detected)}); specify --agent",
        )
    print("利用可能なエージェントが複数見つかりました:")
    for idx, a in enumerate(detected, 1):
        print(f"  {idx}. {a.name}")
    while True:
        try:
            choice = input(
                f"エージェントを選択してください [1-{len(detected)}]: "
            ).strip()
        except EOFError:
            raise TrialError("invalid_input", "Agent selection aborted") from None
        if choice.isdigit() and 1 <= int(choice) <= len(detected):
            return detected[int(choice) - 1].name
        print("有効な番号を入力してください。")


def _interactive_ci_selection(
    view: TrialView, deps: TrialDependencies
) -> tuple[TrialView, bool]:
    if not (
        view.reason == "ci_missing" and view.ci_candidates and view.state is not None
    ):
        return view, False
    print("\nCIコマンドが未設定です。候補:")
    for idx, cand in enumerate(view.ci_candidates, 1):
        print(f"  {idx}. {' '.join(cand)}")
    try:
        sel = input("CI候補番号を選択するか、CIコマンドを入力してください: ").strip()
    except EOFError:
        return view, False
    if not sel:
        return view, False
    if sel.isdigit() and 1 <= int(sel) <= len(view.ci_candidates):
        ci_override = view.ci_candidates[int(sel) - 1]
    else:
        mod = importlib.import_module("orchestune.config_schema")
        parse_fn = mod.parse_ci_command
        ci_override = tuple(parse_fn(sel))
    res_req = ResumeRequest(trial_id=view.state.trial_id, ci_argv_override=ci_override)
    new_view = resume_trial(res_req, deps)
    return new_view, True


def _interactive_plan_approval(
    view: TrialView, deps: TrialDependencies
) -> tuple[TrialView, bool]:
    if not (
        view.effective_phase == TrialPhase.PLANNED
        and view.reason in ("approval_required", None)
        and view.state is not None
        and view.state.plan is not None
        and view.state.plan.discipline_status == DisciplineStatus.COMPATIBLE
    ):
        return view, False
    p_dig = plan_digest(
        view.state.plan, view.state.request.digest, view.state.repository.base_sha
    )
    try:
        ans = input("\nこの計画で開始しますか? (yes/no) [no]: ").strip().lower()
    except EOFError:
        return view, False
    if ans in ("yes", "y"):
        res_req = ResumeRequest(trial_id=view.state.trial_id, approve_plan=p_dig)
        new_view = resume_trial(res_req, deps)
        return new_view, True
    return view, False


def _handle_interactive_start(
    view: TrialView, deps: TrialDependencies, repo_arg: str
) -> TrialView:
    view, ci_changed = _interactive_ci_selection(view, deps)
    if ci_changed:
        _render_text(view, is_result=False, repo_arg=repo_arg)
    view, approved = _interactive_plan_approval(view, deps)
    if approved:
        _render_text(view, is_result=False, repo_arg=repo_arg)
    return view


def _cmd_start(args: argparse.Namespace, is_interactive: bool) -> int:
    req_text = _read_request_file(args.request_file)
    ci_argv = _parse_ci_override(args.ci_command)
    repo = os.path.abspath(args.repo) if args.repo else os.getcwd()
    deps = make_dependencies(repo)
    agent = _select_agent(deps, args.agent, is_interactive)

    start_req = StartRequest(
        repo=repo,
        request_text=req_text,
        agent_name=agent,
        ci_argv_override=ci_argv,
    )
    view = start_trial(start_req, deps)
    _render_text(view, is_result=False, repo_arg=repo)

    if is_interactive and view.effective_phase == TrialPhase.PLANNED:
        view = _handle_interactive_start(view, deps, repo)
    return _exit_code_for_view(view)


def _validate_resume_flags(args: argparse.Namespace) -> None:
    if args.confirm_stopped is not None:
        if any(
            (
                args.approve_plan,
                args.confirm_worktree,
                args.request_file,
                args.ci_command,
            )
        ):
            raise TrialError(
                "invalid_input",
                "--confirm-stopped cannot be combined with other resume flags",
            )
    if (args.request_file is not None or args.ci_command is not None) and (
        args.approve_plan is not None or args.confirm_worktree is not None
    ):
        raise TrialError(
            "invalid_input",
            "Cannot specify --approve-plan or --confirm-worktree with --request-file or --ci-command",
        )


def _cmd_resume(args: argparse.Namespace, is_interactive: bool) -> int:
    trial_id = _validate_hex_id("trial_id", args.id)
    _validate_resume_flags(args)
    approve_dig = (
        _validate_hex_digest("approve-plan", args.approve_plan)
        if args.approve_plan
        else None
    )
    stopped_id = (
        _validate_hex_id("confirm-stopped", args.confirm_stopped)
        if args.confirm_stopped
        else None
    )
    wt_dig = (
        _validate_hex_digest("confirm-worktree", args.confirm_worktree)
        if args.confirm_worktree
        else None
    )
    req_text = _read_request_file(args.request_file) if args.request_file else None
    ci_argv = _parse_ci_override(args.ci_command)

    repo = os.path.abspath(args.repo) if args.repo else os.getcwd()
    deps = make_dependencies(repo)
    res_req = ResumeRequest(
        trial_id=trial_id,
        approve_plan=approve_dig,
        confirm_stopped=stopped_id,
        confirm_worktree=wt_dig,
        request_text=req_text,
        ci_argv_override=ci_argv,
    )
    view = resume_trial(res_req, deps)
    _render_text(view, is_result=False, repo_arg=repo)

    if is_interactive and view.effective_phase == TrialPhase.PLANNED:
        view = _handle_interactive_start(view, deps, repo)
    return _exit_code_for_view(view)


def _cmd_status(args: argparse.Namespace) -> int:
    repo = os.path.abspath(args.repo) if args.repo else os.getcwd()
    deps = make_dependencies(repo)
    trial_id = args.id or deps.store.active_trial_id()
    if trial_id is None:
        raise TrialError("not_found", "No active trial found")
    trial_id = _validate_hex_id("trial_id", trial_id)

    view = inspect_trial(trial_id, deps)
    if view.state is None:
        raise TrialError("not_found", f"Trial {trial_id} not found")
    if args.json:
        _render_json(view, trial_id)
    else:
        _render_text(view, is_result=False, repo_arg=repo)
    return 0


def _cmd_result(args: argparse.Namespace) -> int:
    trial_id = _validate_hex_id("trial_id", args.id)
    repo = os.path.abspath(args.repo) if args.repo else os.getcwd()
    deps = make_dependencies(repo)

    if args.decision is not None:
        view = record_decision(trial_id, args.decision, deps)
    else:
        view = inspect_trial(trial_id, deps)
        if view.state is None:
            raise TrialError("not_found", f"Trial {trial_id} not found")

    if args.json:
        _render_json(view, trial_id)
    else:
        _render_text(view, is_result=True, repo_arg=repo)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, io.UnsupportedOperation):
                pass
    parser = _build_parser()
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    is_interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        if args.subcommand == "start":
            return _cmd_start(args, is_interactive)
        if args.subcommand == "resume":
            return _cmd_resume(args, is_interactive)
        if args.subcommand == "status":
            return _cmd_status(args)
        if args.subcommand == "result":
            return _cmd_result(args)
        raise TrialError("invalid_input", f"Unknown subcommand: {args.subcommand}")
    except KeyboardInterrupt:
        return 130
    except TrialError as exc:
        print(f"Error ({exc.code}): {exc.message}", file=sys.stderr)
        if exc.code == "interrupted":
            return 130
        if exc.code in ("agent_failed", "ci_failed", "review_failed"):
            return 1
        return 2
    except Exception as exc:  # noqa: BLE001 - boundary: render unexpected trial failures safely
        print(f"Error: {exc}", file=sys.stderr)
        return 2
