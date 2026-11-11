import argparse
import importlib
import sys
from collections.abc import Callable
from dataclasses import dataclass

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
    module: str | None = None
    function: str = "main"
    exit_with_return: bool = False
    handler: Callable[[], None] | None = None
    kwargs: Callable[[list[str]], dict[str, object]] | None = None


_COMMAND_SPECS: tuple[_Command, ...] = (
    _Command("dag", "DAG validation tool", "orchestune.dag.cli"),
    _Command("dispatch", "Dispatcher/scheduler tool", "orchestune.dispatch.dispatcher"),
    _Command(
        "status",
        "Monitor dispatched agent sessions (--watch for live view)",
        "orchestune.monitor",
    ),
    _Command(
        "setup",
        "Setup skills symlinks for AI assistants",
        handler=lambda: _run_setup(sys.argv[1:]),
    ),
    _Command(
        "bootstrap",
        "Verify gh auth and ensure required GitHub labels exist",
        "orchestune.bootstrap",
    ),
    _Command(
        "provision",
        "Provision GitHub Issues from decomposition_plan.md",
        "orchestune.provisioning.cli",
    ),
    _Command(
        "replan",
        "Preview or replace an unstarted decomposition generation",
        "orchestune.replan.cli",
        exit_with_return=True,
    ),
    _Command(
        "remediate",
        "Preview parent-scope remediation tasks",
        "orchestune.remediation.cli",
        exit_with_return=True,
    ),
    _Command(
        "claim",
        "Claim a task issue (--apply to claim)",
        "orchestune.claim.cli",
        exit_with_return=True,
        kwargs=_claim_kwargs,
    ),
    _Command(
        "complete",
        "Complete a claimed task issue",
        "orchestune.complete.cli",
        exit_with_return=True,
    ),
    _Command(
        "recover",
        "Inspect or recover one stopped claim (preview by default)",
        "orchestune.recovery.cli",
        exit_with_return=True,
    ),
    _Command(
        "gc",
        "Release handoff-ready task reservations (--apply to release)",
        "orchestune.dispatch.gc_cli",
        exit_with_return=True,
    ),
    _Command(
        "doctor",
        "Diagnose dispatch single-executor setup (offline, read-only)",
        "orchestune.dispatch.doctor.cli",
        exit_with_return=True,
    ),
    _Command(
        "blocked",
        "Count blocked-human-review markers (read-only)",
        "orchestune.blocked_cli",
        exit_with_return=True,
    ),
    _Command(
        "review",
        "Inspect, retry or override a Layer-2 semantic review",
        "orchestune.dispatch.review_cli",
        exit_with_return=True,
    ),
    _Command(
        "reviewer",
        "Resolve the interactive review bot (read-only)",
        "orchestune.dispatch.reviewer_cli",
        exit_with_return=True,
    ),
    _Command(
        "config",
        "Create or edit orchestune.toml interactively",
        "orchestune.config_wizard.cli",
        exit_with_return=True,
    ),
    _Command(
        "skills",
        "Install and manage Orchestune skills for AI assistants",
        "orchestune.installer.cli",
        exit_with_return=True,
    ),
    _Command(
        "scratch",
        "Create scratch session directories for task artifacts",
        "orchestune.scratch.cli",
        exit_with_return=True,
    ),
    _Command(
        "workflow",
        "Check read-only advisory workflow gates",
        "orchestune.workflow.cli",
        exit_with_return=True,
    ),
    _Command(
        "trial",
        "Local autonomous trial without GitHub setup",
        "orchestune.local_trial.cli",
        exit_with_return=True,
    ),
)

_COMMAND_SPEC_BY_NAME = {spec.name: spec for spec in _COMMAND_SPECS}
COMMANDS: tuple[tuple[str, str], ...] = tuple(
    (spec.name, spec.description) for spec in _COMMAND_SPECS if spec.name != "trial"
)


def _print_command_list() -> None:
    output = make_output()
    plain_commands = "\n".join(
        f"  {command:<10}{description}" for command, description in COMMANDS
    )

    output.emit(output.text_block("Usage: orchestune <command> [<args>]"))
    table = output.table_block(
        ("Command", "Description"), COMMANDS, title="Available commands"
    )
    output.emit(
        Block(f"Available commands:\n{plain_commands}", table.rich, soft_wrap=False)
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
