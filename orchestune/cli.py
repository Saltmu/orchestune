import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: orchestune <command> [<args>]")
        print("Available commands:")
        print("  dag       DAG validation tool")
        print("  dispatch  Dispatcher/scheduler tool")
        sys.exit(1)

    cmd = sys.argv[1]
    sys.argv = [sys.argv[0]] + sys.argv[2:]

    if cmd == "dag":
        from orchestune.dag import main as dag_main

        dag_main()
    elif cmd == "dispatch":
        from orchestune.dispatcher import main as dispatcher_main

        dispatcher_main()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
