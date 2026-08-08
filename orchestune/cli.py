import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: orchestune <command> [<args>]")
        print("Available commands:")
        print("  dag       DAG validation tool")
        print("  dispatch  Dispatcher/scheduler tool")
        print("  status    Monitor dispatched agent sessions (--watch for live view)")
        print("  setup     Setup skills symlinks for AI assistants")
        print("  bootstrap Verify gh auth and ensure required GitHub labels exist")
        print("  provision Provision GitHub Issues from decomposition_plan.md")
        sys.exit(1)

    cmd = sys.argv[1]
    sys.argv = [sys.argv[0]] + sys.argv[2:]

    if cmd == "dag":
        from orchestune.dag_cli import main as dag_main

        dag_main()
    elif cmd == "dispatch":
        from orchestune.dispatcher import main as dispatcher_main

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
        from orchestune.provisioning import main as provision_main

        provision_main()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
