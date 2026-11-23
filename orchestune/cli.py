import argparse
import importlib
import sys
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Group
from rich.text import Text

from orchestune.infra.cli_output import Block, make_output
from orchestune.version import get_version


def _run_setup(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="orchestune setup",
        description=(
            "Link project skills into assistant skill directories. "
            "Deprecated: use 'orchestune skills install' instead."
        ),
    )
    parser.add_argument(
        "--with-workflow-skill",
        action="store_true",
        help=(
            "Copy the workflow template skill into the current project "
            "(not the user skill directory)."
        ),
    )
    args = parser.parse_args(argv)
    setup_skills = importlib.import_module("orchestune.setup_skills").setup_skills
    sys.exit(setup_skills(with_workflow_skill=args.with_workflow_skill))


def _dispatch_command(cmd: str) -> None:
    spec = _COMMAND_SPEC_BY_NAME.get(cmd)
    if spec is None:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
    if spec.handler is not None:
        spec.handler()
        return
    assert spec.module is not None
    function = getattr(importlib.import_module(spec.module), spec.function)
    kwargs = spec.kwargs(sys.argv) if spec.kwargs is not None else {}
    result = function(**kwargs)
    if spec.exit_with_return:
        sys.exit(result)


def _claim_kwargs(argv: list[str]) -> dict[str, object]:
    if any(
        arg in {"--adopt-pr", "--resume"}
        or arg.startswith(("--adopt-pr=", "--resume="))
        for arg in argv
    ):
        reader = importlib.import_module(
            "orchestune.dispatch.runtime_reader"
        ).read_runtime
        return {"observe_external": reader}
    return {}


@dataclass(frozen=True)
class _Command:
    name: str
    description: str
    group: str
    module: str | None = None
    function: str = "main"
    exit_with_return: bool = False
    handler: Callable[[], None] | None = None
    kwargs: Callable[[list[str]], dict[str, object]] | None = None


_GROUP_ORDER = (
    "Getting started",
    "Run and monitor",
    "Recovery and maintenance",
    "Agent and advanced commands",
    "Deprecated",
)

_COMMAND_SPECS: tuple[_Command, ...] = (
    _Command(
        "trial",
        "Local autonomous trial without GitHub setup",
        "Getting started",
        "orchestune.local_trial.cli",
        exit_with_return=True,
    ),
    _Command(
        "config",
        "Create or edit orchestune.toml interactively",
        "Getting started",
        "orchestune.config_wizard.cli",
        exit_with_return=True,
    ),
    _Command(
        "skills",
        "Install and manage Orchestune skills for AI assistants",
        "Getting started",
        "orchestune.installer.cli",
        exit_with_return=True,
    ),
    _Command(
        "bootstrap",
        "Verify gh auth and ensure required GitHub labels exist",
        "Getting started",
        "orchestune.bootstrap",
    ),
    _Command(
        "doctor",
        "Diagnose dispatch single-executor setup (offline, read-only)",
        "Getting started",
        "orchestune.dispatch.doctor.cli",
        exit_with_return=True,
    ),
    _Command(
        "dag",
        "DAG validation tool",
        "Run and monitor",
        "orchestune.dag.cli",
    ),
    _Command(
        "provision",
        "Provision GitHub Issues from decomposition_plan.md",
        "Run and monitor",
        "orchestune.provisioning.cli",
    ),
    _Command(
        "dispatch",
        "Dispatcher/scheduler tool",
        "Run and monitor",
        "orchestune.dispatch.dispatcher",
    ),
    _Command(
        "status",
        "Monitor dispatched agent sessions (--watch for live view)",
        "Run and monitor",
        "orchestune.monitor",
    ),
    _Command(
        "blocked",
        "Count blocked-human-review markers (read-only)",
        "Run and monitor",
        "orchestune.blocked_cli",
        exit_with_return=True,
    ),
    _Command(
        "review",
        "Inspect, retry or override a Layer-2 semantic review",
        "Run and monitor",
        "orchestune.dispatch.review_cli",
        exit_with_return=True,
    ),
    _Command(
        "replan",
        "Preview or replace an unstarted decomposition generation",
        "Recovery and maintenance",
        "orchestune.replan.cli",
        exit_with_return=True,
    ),
    _Command(
        "remediate",
        "Preview parent-scope remediation tasks",
        "Recovery and maintenance",
        "orchestune.remediation.cli",
        exit_with_return=True,
    ),
    _Command(
        "recover",
        "Inspect or recover one stopped claim (preview by default)",
        "Recovery and maintenance",
        "orchestune.recovery.cli",
        exit_with_return=True,
    ),
    _Command(
        "gc",
        "Release handoff-ready task reservations (--apply to release)",
        "Recovery and maintenance",
        "orchestune.dispatch.gc_cli",
        exit_with_return=True,
    ),
    _Command(
        "claim",
        "Claim a task issue (--apply to claim)",
        "Agent and advanced commands",
        "orchestune.claim.cli",
        exit_with_return=True,
        kwargs=_claim_kwargs,
    ),
    _Command(
        "complete",
        "Complete a claimed task issue",
        "Agent and advanced commands",
        "orchestune.complete.cli",
        exit_with_return=True,
    ),
    _Command(
        "scratch",
        "Create scratch session directories for task artifacts",
        "Agent and advanced commands",
        "orchestune.scratch.cli",
        exit_with_return=True,
    ),
    _Command(
        "workflow",
        "Check read-only advisory workflow gates",
        "Agent and advanced commands",
        "orchestune.workflow.cli",
        exit_with_return=True,
    ),
    _Command(
        "context",
        "Rebuild and export the local context index",
        "Agent and advanced commands",
        "orchestune.context.cli",
        exit_with_return=True,
    ),
    _Command(
        "reviewer",
        "Resolve the interactive review bot (read-only)",
        "Agent and advanced commands",
        "orchestune.dispatch.reviewer_cli",
        exit_with_return=True,
    ),
    _Command(
        "setup",
        "Deprecated: use 'orchestune skills install' instead.",
        "Deprecated",
        handler=lambda: _run_setup(sys.argv[1:]),
    ),
)

_COMMAND_SPEC_BY_NAME = {spec.name: spec for spec in _COMMAND_SPECS}


def _grouped_commands() -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    grouped: dict[str, list[tuple[str, str]]] = {name: [] for name in _GROUP_ORDER}
    for spec in _COMMAND_SPECS:
        grouped[spec.group].append((spec.name, spec.description))
    return tuple((name, tuple(grouped[name])) for name in _GROUP_ORDER)


COMMANDS: tuple[tuple[str, str], ...] = tuple(
    row for _heading, rows in _grouped_commands() for row in rows
)
_NAME_WIDTH = max(len(name) for name, _description in COMMANDS) + 2


def _print_command_list() -> None:
    output = make_output()
    output.emit(output.text_block("Usage: orchestune <command> [<args>]"))
    lines = ["Available commands:"]
    tables = []
    for heading, rows in _grouped_commands():
        lines.append(heading)
        lines.extend(
            f"  {name:<{_NAME_WIDTH}}{description}" for name, description in rows
        )
        tables.append(
            output.table_block(("Command", "Description"), rows, title=heading).rich
        )
    plain = "\n".join(lines)
    output.emit(
        Block(plain, Group(Text("Available commands"), *tables), soft_wrap=False)
    )


def main() -> None:
    if len(sys.argv) < 2:
        _print_command_list()
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd in {"--help", "-h"}:
        _print_command_list()
        return

    if cmd in {"--version", "-V"}:
        print(f"orchestune {get_version()}")
        return

    sys.argv = [sys.argv[0]] + sys.argv[2:]
    _dispatch_command(cmd)


if __name__ == "__main__":
    main()
