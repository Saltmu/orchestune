from __future__ import annotations

import argparse
import sys

from orchestune.forge import REQUIRED_LABELS, Forge, ForgeAuthError, GitHubForge


def run_bootstrap(forge: Forge | None = None) -> int:
    forge = forge or GitHubForge()

    try:
        forge.check_auth()
    except ForgeAuthError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    result = forge.ensure_labels(REQUIRED_LABELS)
    print(f"Labels created: {len(result.created_labels)}")
    for name in result.created_labels:
        print(f"  + {name}")
    print(f"Labels already present: {len(result.existing_labels)}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="orchestune bootstrap: gh認証確認と必須ラベルの起票を行う"
    )
    parser.parse_args(argv)
    sys.exit(run_bootstrap())
