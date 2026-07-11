from __future__ import annotations

import argparse
import sys
from importlib import resources
from pathlib import Path

from orchestune.forge import REQUIRED_LABELS, Forge, ForgeAuthError, GitHubForge

CLAUDE_SETTINGS_TEMPLATE_PACKAGE = "orchestune.templates"
CLAUDE_SETTINGS_TEMPLATE_NAME = "claude_settings.default.json"


def ensure_claude_settings(repo_root: Path) -> bool:
    """動作リポジトリに`.claude/settings.json`が無ければデフォルトの許可リストを作成する。

    既にファイルが存在する場合は、ユーザーが既にカスタマイズ済みの可能性があるため
    上書き・マージは行わず何もしない。
    """
    settings_path = repo_root / ".claude" / "settings.json"
    if settings_path.exists():
        return False

    template = (
        resources.files(CLAUDE_SETTINGS_TEMPLATE_PACKAGE)
        .joinpath(CLAUDE_SETTINGS_TEMPLATE_NAME)
        .read_text(encoding="utf-8")
    )
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(template, encoding="utf-8")
    return True


def run_bootstrap(forge: Forge | None = None, repo_root: Path | None = None) -> int:
    forge = forge or GitHubForge()
    repo_root = repo_root or Path.cwd()

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

    settings_path = repo_root / ".claude" / "settings.json"
    if ensure_claude_settings(repo_root):
        print(f"Created default .claude/settings.json at {settings_path}")
    else:
        print(f".claude/settings.json already exists at {settings_path}, skipping.")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="orchestune bootstrap: gh認証確認と必須ラベルの起票を行う"
    )
    parser.parse_args(argv)
    sys.exit(run_bootstrap())
