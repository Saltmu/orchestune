import sys

from orchestune.infra.cli_output import Block, make_output
from orchestune.version import get_version


def _dispatch_command(cmd: str) -> None:
    if cmd == "dag":
        from orchestune.dag.cli import main as dag_main

        dag_main()
    elif cmd == "dispatch":
        from orchestune.dispatch.dispatcher import main as dispatcher_main

        dispatcher_main()
    elif cmd == "status":
        from orchestune.monitor import main as monitor_main

        monitor_main()
    elif cmd == "setup":
        from orchestune.setup_skills import setup_skills

        with_workflow_skill = "--with-workflow-skill" in sys.argv[1:]
        sys.exit(setup_skills(with_workflow_skill=with_workflow_skill))
    elif cmd == "bootstrap":
        from orchestune.bootstrap import main as bootstrap_main

        bootstrap_main()
    elif cmd == "provision":
        from orchestune.provisioning.cli import main as provision_main

        provision_main()
    elif cmd == "replan":
        from orchestune.replan.cli import main as replan_main

        sys.exit(replan_main())
    elif cmd == "remediate":
        from orchestune.remediation.cli import main as remediate_main

        sys.exit(remediate_main())
    elif cmd == "claim":
        from orchestune.claim.cli import main as claim_main

        if any(
            arg in {"--adopt-pr", "--resume"}
            or arg.startswith(("--adopt-pr=", "--resume="))
            for arg in sys.argv
        ):
            from orchestune.dispatch.runtime_reader import read_runtime

            sys.exit(claim_main(observe_external=read_runtime))
        sys.exit(claim_main())
    elif cmd == "complete":
        from orchestune.complete.cli import main as complete_main

        sys.exit(complete_main())
    elif cmd == "recover":
        from orchestune.recovery.cli import main as recover_main

        sys.exit(recover_main())
    elif not _dispatch_tool_command(cmd):
        print(f"Unknown command: {cmd}")
        sys.exit(1)


def _dispatch_tool_command(cmd: str) -> bool:
    if cmd == "gc":
        from orchestune.dispatch.gc_cli import main as gc_main

        sys.exit(gc_main())
    elif cmd == "doctor":
        from orchestune.dispatch.doctor_cli import main as doctor_main

        sys.exit(doctor_main())
    elif cmd == "review":
        from orchestune.dispatch.review_cli import main as review_main

        sys.exit(review_main())
    elif cmd == "config":
        from orchestune.config_wizard.cli import main as config_main

        sys.exit(config_main())
    elif cmd == "skills":
        from orchestune.installer.cli import main as skills_main

        sys.exit(skills_main())
    elif cmd == "scratch":
        from orchestune.scratch.cli import main as scratch_main

        sys.exit(scratch_main())
    elif cmd == "workflow":
        from orchestune.workflow.cli import main as workflow_main

        sys.exit(workflow_main())
    else:
        return False
    return True


def main() -> None:
    if len(sys.argv) < 2:
        output = make_output()
        commands = (
            ("dag", "DAG validation tool"),
            ("dispatch", "Dispatcher/scheduler tool"),
            ("status", "Monitor dispatched agent sessions (--watch for live view)"),
            ("setup", "Setup skills symlinks for AI assistants"),
            ("bootstrap", "Verify gh auth and ensure required GitHub labels exist"),
            ("provision", "Provision GitHub Issues from decomposition_plan.md"),
            ("replan", "Preview or replace an unstarted decomposition generation"),
            ("remediate", "Preview parent-scope remediation tasks"),
            ("claim", "Claim a task issue (--no-apply for a read-only preview)"),
            ("complete", "Complete a claimed task issue"),
            ("recover", "Inspect or recover one stopped claim (preview by default)"),
            ("gc", "Release handoff-ready task reservations (--no-apply to preview)"),
            ("doctor", "Diagnose dispatch single-executor setup (offline, read-only)"),
            ("review", "Inspect, retry or override a Layer-2 semantic review"),
            ("config", "Create or edit orchestune.toml interactively"),
            ("skills", "Install and manage Orchestune skills for AI assistants"),
            ("scratch", "Create scratch session directories for task artifacts"),
            ("workflow", "Check read-only advisory workflow gates"),
        )
        plain_commands = "\n".join(
            f"  {command:<10}{description}" for command, description in commands
        )

        output.emit(output.text_block("Usage: orchestune <command> [<args>]"))
        table = output.table_block(
            ("Command", "Description"), commands, title="Available commands"
        )
        output.emit(
            Block(f"Available commands:\n{plain_commands}", table.rich, soft_wrap=False)
        )
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd in {"--version", "-V"}:
        print(f"orchestune {get_version()}")
        return

    sys.argv = [sys.argv[0]] + sys.argv[2:]
    _dispatch_command(cmd)
