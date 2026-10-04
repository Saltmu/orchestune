from __future__ import annotations

import argparse
import sys
from pathlib import Path

from orchestune.infra.session_dirs import SessionDirError, create_session_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestune scratch",
        description="Create scratch session directories for task artifacts",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    create_cmd = subparsers.add_parser(
        "create", help="Create a unique session directory"
    )
    create_cmd.add_argument(
        "artifact", help="Artifact category (e.g., planning, task, pr)"
    )
    create_cmd.add_argument("issue_or_task", help="Issue number or short task slug")
    create_cmd.add_argument(
        "--project-dir",
        type=Path,
        help="Target project directory (defaults to Git root of cwd)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1

    if args.subcommand == "create":
        try:
            path = create_session_dir(
                artifact=args.artifact,
                issue_or_task=args.issue_or_task,
                project_dir=args.project_dir,
            )
            print(str(path))
            return 0
        except SessionDirError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Unexpected error creating session directory: {e}", file=sys.stderr)
            return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
