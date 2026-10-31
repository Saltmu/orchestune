"""CLI facade for decomposition-plan Issue provisioning."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from contextlib import ExitStack, suppress

from rich.text import Text

from orchestune.config_contracts import ConfigError
from orchestune.forge import GitHubForge
from orchestune.infra.cli_output import Block, CliOutput, Level, make_output
from orchestune.infra.cli_progress import ProgressHandle, progress
from orchestune.infra.repository_config import resolve_repo_root
from orchestune.provisioning.flow import (
    IssuePreview,
    ProvisionProgress,
    ProvisionProgressHook,
    ProvisionResult,
    _build_provisioning_dag,
)
from orchestune.provisioning.flow import provision_issues as _provision_issues
from orchestune.provisioning.parent import _resolve_parent_issue
from orchestune.provisioning.plan import (
    PlanMetadata,
    _load_plan,
    _parent_body,
    restore_plan_file_from_parent,
)
from orchestune.provisioning.rendering import (
    _build_subtask_issue_body,
    _derive_labels,
    _render_issue_body,
    _subtask_id_from_body,
    validate_template_identity_marker,
)
from orchestune.provisioning.subtasks import (
    _link_subtask_relationships,
    _provision_subtask,
)

__all__ = (
    "IssuePreview",
    "PlanMetadata",
    "ProvisionResult",
    "_build_provisioning_dag",
    "_build_subtask_issue_body",
    "_derive_labels",
    "_link_subtask_relationships",
    "_load_plan",
    "_parent_body",
    "_print_result",
    "_provision_subtask",
    "_render_issue_body",
    "_resolve_parent_issue",
    "_subtask_id_from_body",
    "validate_template_identity_marker",
    "main",
    "provision_issues",
)


def provision_issues(*args: object, **kwargs: object) -> ProvisionResult:
    """Compatibility entry point for callers of ``orchestune.provisioning``."""
    return _provision_issues(*args, **kwargs)  # type: ignore[arg-type]


def _say(output: CliOutput, text: str = "", *, multiline: bool = False) -> None:
    """Write one plain line (or block) through the shared sanitizing layer."""
    output.emit(output.text_block(text, multiline=multiline))


def _print_resume_hint(plan_path: str) -> None:
    try:
        subtasks, metadata = _load_plan(plan_path)
    except Exception:
        return
    known = {task.id: task.issue_number for task in subtasks if task.issue_number}
    _say(
        make_output(sys.stderr),
        f"Saved Issue numbers: parent={metadata.parent_issue_number}, "
        f"subtasks={known}; unfinished={len(subtasks) - len(known)}. "
        "Rerun the same provision command after resolving the error.",
    )


def _print_error(error: Exception) -> None:
    _say(make_output(sys.stderr), f"Error: {error}", multiline=True)


_PROGRESS_TEXT = "Provisioning Issues..."


class ProvisionProgressDisplay:
    """Own one best-effort Live; display failures never retry provisioning."""

    def __init__(self, output: CliOutput) -> None:
        self._output = output
        self._stack = ExitStack()
        self._handle: ProgressHandle | None = None
        self._description = _PROGRESS_TEXT
        self._closed = False
        self._disabled = False

    def __enter__(self) -> ProvisionProgressHook | None:
        if not self._output.policy.rich:
            return None
        try:
            self._handle = self._stack.enter_context(progress(self._output))
            self._update(self._description)
        except Exception:
            self._disable()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __call__(self, event: ProvisionProgress) -> None:
        if self._closed:
            return
        try:
            if event.step == "stopped":
                self.close()
                return
            if event.step == "retry-wait":
                description = (
                    f"{self._description} (retrying in {event.wait_seconds:g}s)"
                )
            else:
                phase = (
                    f"[{event.index}/{event.total}] {event.subtask_id}"
                    if event.step == "subtask"
                    else "syncing plan"
                    if event.step == "sync"
                    else event.step
                )
                self._description = f"{_PROGRESS_TEXT} {phase}"
                description = self._description
            self._update(description)
        except Exception:
            self._disable()

    def _update(self, description: str) -> None:
        block = self._output.text_block(description, multiline=False)
        text = Text(block.plain, no_wrap=True, overflow="ellipsis")
        if self._handle is not None:
            self._handle.update(Block(block.plain, text))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stack.close()
        except Exception:
            self._disable()

    def _disable(self) -> None:
        if self._disabled:
            return
        self._disabled = self._closed = True
        with suppress(Exception):
            self._stack.close()
        with suppress(Exception):
            _say(
                make_output(sys.stderr),
                "Warning: progress unavailable; continuing provision",
            )


def _print_result(result: ProvisionResult, output: CliOutput | None = None) -> None:
    """Print the outcome; decorated only on a TTY, otherwise the legacy plain text."""
    output = make_output() if output is None else output
    if output.policy.rich:
        _print_result_rich(result, output)
    else:
        _print_result_plain(result, output)


def _print_preview_plain(result: ProvisionResult, output: CliOutput) -> None:
    _say(output, "Dry run (--no-apply): no Issues were created.")
    if result.parent_issue_number is not None:
        _say(output, f"Target parent issue: #{result.parent_issue_number}")
    for preview in result.previews:
        status = "reuse expected" if preview.already_has_issue else "would create"
        _say(output)
        _say(output, f"=== {preview.subtask_id} ({status}) ===")
        _say(output, f"Title: {preview.title}")
        _say(output, f"Labels: {', '.join(preview.labels)}")
        _say(output, preview.body, multiline=True)


def _print_result_plain(result: ProvisionResult, output: CliOutput) -> None:
    if not result.applied:
        _print_preview_plain(result, output)
        return
    _say(output, f"Parent issue: #{result.parent_issue_number}")
    _say(output, f"Created: {len(result.created)}")
    for subtask_id, number in result.created.items():
        _say(output, f"  + {subtask_id} -> #{number}")
    _say(output, f"Reused: {len(result.reused)}")
    for subtask_id, number in result.reused.items():
        _say(output, f"  = {subtask_id} -> #{number}")
    _say(output)
    if result.degraded_subtask_ids:
        _say(
            output,
            "Operating mode: degraded/parent-metadata for "
            f"{len(result.degraded_subtask_ids)} subtask(s)",
        )
        for subtask_id in result.degraded_subtask_ids:
            _say(output, f"  ! {subtask_id}")
    else:
        _say(
            output,
            "Operating mode: full (native sub-issue/blocked_by relationships linked)",
        )
    if result.parent_only_subtask_ids:
        _say(output)
        _say(
            output,
            f"Error: the plan of #{result.parent_issue_number} has registered "
            f"subtasks missing from the local plan: "
            f"{', '.join(result.parent_only_subtask_ids)}",
        )
        _say(
            output,
            "No Issues were created. Restore the plan with "
            f"`orchestune provision --restore-plan {result.parent_issue_number}` "
            "and rerun provision.",
        )
        return
    if not result.plan_synced:
        _say(output)
        _say(
            output,
            f"Warning: could not sync decomposition plan into #{result.parent_issue_number}'s body.",
        )
        _say(
            output,
            "The local plan file was updated, but the parent Issue body does not reflect the latest state.",
        )


def _print_preview_rich(result: ProvisionResult, output: CliOutput) -> None:
    items = []
    if result.parent_issue_number is not None:
        items.append(("Target parent issue", f"#{result.parent_issue_number}"))
    output.summary(Level.DRY_RUN, "Dry run (--no-apply): no Issues were created", items)
    for preview in result.previews:
        status = "reuse expected" if preview.already_has_issue else "would create"
        output.summary(
            Level.DRY_RUN,
            f"{preview.subtask_id} ({status})",
            [("Title", preview.title), ("Labels", ", ".join(preview.labels))],
        )
        output.emit(output.text_block(preview.body))


def _print_outcome_rich(result: ProvisionResult, output: CliOutput) -> None:
    number = result.parent_issue_number
    if result.parent_only_subtask_ids:
        level = Level.FAILURE
    elif result.plan_synced:
        level = Level.SUCCESS
    else:
        level = Level.WARNING
    output.summary(
        level,
        f"Parent issue: #{number}",
        [("Created", str(len(result.created))), ("Reused", str(len(result.reused)))],
    )
    rows = [("created", sid, f"#{n}") for sid, n in result.created.items()]
    rows += [("reused", sid, f"#{n}") for sid, n in result.reused.items()]
    if rows:
        output.table(("Result", "Subtask", "Issue"), rows, title="Issues")


def _print_result_rich(result: ProvisionResult, output: CliOutput) -> None:
    if not result.applied:
        _print_preview_rich(result, output)
        return
    _print_outcome_rich(result, output)
    if result.degraded_subtask_ids:
        output.message(
            Level.WARNING,
            "Operating mode: degraded/parent-metadata for "
            f"{len(result.degraded_subtask_ids)} subtask(s)",
            detail="\n".join(result.degraded_subtask_ids),
        )
    else:
        output.message(
            Level.SUCCESS,
            "Operating mode: full (native sub-issue/blocked_by relationships linked)",
        )
    number = result.parent_issue_number
    if result.parent_only_subtask_ids:
        output.message(
            Level.FAILURE,
            f"the plan of #{number} has registered subtasks missing from the local plan: "
            f"{', '.join(result.parent_only_subtask_ids)}",
            detail="No Issues were created. Restore the plan with "
            f"`orchestune provision --restore-plan {number}` and rerun provision.",
        )
        return
    if not result.plan_synced:
        output.message(
            Level.WARNING,
            f"could not sync decomposition plan into #{number}'s body.",
            detail="The local plan file was updated, but the parent Issue body "
            "does not reflect the latest state.",
        )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="orchestune provision: decomposition_plan.mdからIssueを起票する"
    )
    parser.add_argument("--plan", default="decomposition_plan.md")
    parser.add_argument("--template", default=".github/issue_template.md")
    parser.add_argument("--apply", dest="apply", action="store_true", default=True)
    parser.add_argument("--no-apply", dest="apply", action="store_false")
    parser.add_argument("--parent-issue", type=int, default=None)
    parser.add_argument(
        "--restore-plan", type=int, metavar="PARENT_ISSUE", default=None
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    output = make_output()
    try:
        if args.restore_plan is not None:
            out = restore_plan_file_from_parent(
                GitHubForge(),
                args.restore_plan,
                args.plan,
                template_path=args.template,
            )
            _say(
                output,
                f"Successfully restored decomposition plan from #{args.restore_plan} to {out}",
            )
            raise SystemExit(0)
        with ProvisionProgressDisplay(output) as hook:
            result = _provision_issues(
                args.plan,
                forge=GitHubForge(),
                apply=args.apply,
                template_path=args.template,
                repo_root=resolve_repo_root(args.plan),
                parent_issue=args.parent_issue,
                on_progress=hook,
            )
    except ConfigError as error:
        _print_error(error)
        raise SystemExit(2) from error
    except Exception as error:
        _print_error(error)
        _print_resume_hint(args.plan)
        raise SystemExit(1) from error
    _print_result(result, output)
    # #664: 同期に失敗した実行を成功終了させると、「provisionは通ったのに親Issue
    # だけ古い」状態が検知されないまま残る（本文サイズ超過や書き込み拒否など）。
    raise SystemExit(3 if result.applied and not result.plan_synced else 0)
