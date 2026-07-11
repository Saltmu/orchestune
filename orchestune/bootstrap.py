from __future__ import annotations

import argparse
import json
import shutil
import sys
from importlib import resources
from pathlib import Path

from orchestune.forge import REQUIRED_LABELS, Forge, ForgeAuthError, GitHubForge

CLAUDE_SETTINGS_TEMPLATE_PACKAGE = "orchestune.templates"
CLAUDE_SETTINGS_TEMPLATE_NAME = "claude_settings.default.json"


def setup_agy_permissions(repo_root: Path, home: Path) -> None:
    """agy のパーミッション設定を更新する。"""
    has_gemini = shutil.which("agy") is not None or (home / ".gemini").is_dir()
    if not has_gemini:
        return

    projects_dir = home / ".gemini" / "config" / "projects"
    if not projects_dir.is_dir():
        return

    repo_uri = f"file://{repo_root.resolve()}"
    target_cmds = [
        "git",
        "gh",
        "mkdir",
        "cat",
        "poetry",
        "pip",
        "pytest",
        "claude",
        "codex",
        "agy",
    ]
    available_cmds = [cmd for cmd in target_cmds if shutil.which(cmd) is not None]

    for project_file in projects_dir.glob("*.json"):
        try:
            data = json.loads(project_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        # プロジェクトパスの比較
        matched = False
        resources_list = data.get("projectResources", {}).get("resources", [])
        for res in resources_list:
            folder_uri = res.get("gitFolder", {}).get("folderUri", "")
            if folder_uri == repo_uri:
                matched = True
                break

        if matched:
            # 構造の初期化
            if "permissionGrants" not in data:
                data["permissionGrants"] = {}
            if "permissionGrants" not in data["permissionGrants"]:
                data["permissionGrants"]["permissionGrants"] = {}
            if "allow" not in data["permissionGrants"]["permissionGrants"]:
                data["permissionGrants"]["permissionGrants"]["allow"] = []

            allow_list = data["permissionGrants"]["permissionGrants"]["allow"]
            added_cmds = []
            for cmd in available_cmds:
                grant = f"command({cmd})"
                if grant not in allow_list:
                    allow_list.append(grant)
                    added_cmds.append(cmd)

            if added_cmds:
                project_file.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                print(
                    f"Added agy permissions in {project_file.name} for: {', '.join(added_cmds)}"
                )
            else:
                print(f"agy permissions in {project_file.name} are already up to date.")


def setup_claude_permissions(repo_root: Path, home: Path) -> None:
    """claude のパーミッション設定を更新する。"""
    claude_json_path = home / ".claude.json"
    has_claude = shutil.which("claude") is not None or claude_json_path.exists()
    if not has_claude:
        return

    data = {}
    if claude_json_path.exists():
        try:
            data = json.loads(claude_json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    project_key = str(repo_root.resolve())
    project_settings = data.setdefault(project_key, {})
    allowed_tools = project_settings.setdefault("allowedTools", [])

    updated = False
    if "execute_bash" not in allowed_tools:
        allowed_tools.append("execute_bash")
        updated = True
    if project_settings.get("hasTrustDialogAccepted") is not True:
        project_settings["hasTrustDialogAccepted"] = True
        updated = True

    if updated:
        claude_json_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"Updated {claude_json_path} for project {project_key}")
    else:
        print(f"{claude_json_path} is already up to date for project {project_key}")


def setup_agent_permissions(repo_root: Path) -> None:
    """agy や claude が存在する場合に、このリポジトリの実行に必要なパーミッション設定を自動追加する。"""
    home = Path.home()
    setup_agy_permissions(repo_root, home)
    setup_claude_permissions(repo_root, home)


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

    setup_agent_permissions(repo_root)
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="orchestune bootstrap: gh認証確認と必須ラベルの起票を行う"
    )
    parser.parse_args(argv)
    sys.exit(run_bootstrap())
