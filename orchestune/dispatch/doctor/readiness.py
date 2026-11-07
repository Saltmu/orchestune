"""Repository readiness diagnostics for ``orchestune doctor`` (issue #1648)."""

from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from orchestune.config_schema import extract_reviewer_by_target, parse_ci_command
from orchestune.dispatch.doctor.models import (
    CODE_READINESS_AUTH,
    CODE_READINESS_CI_COMMAND,
    CODE_READINESS_CI_DEFINITION,
    CODE_READINESS_DISCIPLINE,
    CODE_READINESS_EXECUTOR,
    CODE_READINESS_IGNORE,
    CODE_READINESS_REVIEWER,
    Diagnostic,
)
from orchestune.dispatch.reviewer import reviewer_for_installed_vendors
from orchestune.dispatch.targets import (
    CODEX_CLOUD_ENV_VAR,
    ROUTINE_ID_ENV_VAR,
    ROUTINE_TOKEN_ENV_VAR,
    detect_installed_local_cli,
    resolve_default_dispatch_target_name,
)
from orchestune.forge import ForgeError, GitHubForge
from orchestune.infra.process_utils import platform_ci_script_relative
from orchestune.session_dirs import ORCHESTUNE_GITIGNORE_LINES

_LOCAL_CLI_BINARIES = {
    "claude-cli": "claude",
    "codex-cli": "codex",
    "agy-cli": "agy",
    "grok-cli": "grok",
}


def _suggest_ci_commands(root: Path) -> list[str]:
    ci_mod = importlib.import_module("orchestune.config_wizard.ci_candidates")
    fn: Callable[[Path], list[str]] = ci_mod.suggest_ci_commands
    return fn(root)


def _check_discipline(repo_root: Path) -> Diagnostic:
    for candidate in ("AGENTS.md", "CLAUDE.md", ".agents/AGENTS.md"):
        if (repo_root / candidate).is_file():
            return Diagnostic(
                CODE_READINESS_DISCIPLINE,
                "ok",
                f"エージェント規律ファイルが確認されました ({candidate})。 / Agent discipline file found.",
                evidence=(candidate,),
            )
    return Diagnostic(
        CODE_READINESS_DISCIPLINE,
        "warning",
        "エージェント規律ファイル（AGENTS.md、CLAUDE.md、または.agents/AGENTS.md）が見つかりません。"
        " / Agent discipline file not found.",
        remediation="orchestune skills install --target all --scope project --with-workflow-skill "
        "を実行してワークフロースキルテンプレートを導入し、規律ファイルを配置してください。",
    )


def _has_workflow_files(repo_root: Path) -> str | None:
    wf_dir = repo_root / ".github" / "workflows"
    if wf_dir.is_dir():
        for entry in wf_dir.iterdir():
            if entry.is_file() and entry.suffix in (".yml", ".yaml"):
                return entry.name
    return None


def _has_explicit_ci_executable(repo_root: Path, raw_cmd: Any) -> str | None:
    try:
        argv = parse_ci_command(raw_cmd)
        if argv:
            exe = argv[0]
            if Path(exe).is_absolute() and Path(exe).is_file():
                return exe
            if not re.match(r"^[a-zA-Z]:[^/\\]", exe) and (
                exe.startswith(".") or "/" in exe or "\\" in exe
            ):
                if (repo_root / exe).is_file():
                    return exe
    except (ValueError, TypeError, OSError):
        pass
    return None


def _check_ci_definition(
    repo_root: Path,
    config: Mapping[str, Any],
    config_valid: bool,
    *,
    cached_suggested: list[str],
) -> Diagnostic:
    wf_name = _has_workflow_files(repo_root)
    if wf_name:
        return Diagnostic(
            CODE_READINESS_CI_DEFINITION,
            "ok",
            f"CI ワークフロー定義が確認されました (.github/workflows/{wf_name})。"
            " / CI workflow definition found.",
            evidence=(f".github/workflows/{wf_name}",),
        )

    if cached_suggested:
        return Diagnostic(
            CODE_READINESS_CI_DEFINITION,
            "ok",
            "CI コマンド候補が検出されました。 / CI command candidate detected.",
            evidence=tuple(cached_suggested[:3]),
        )

    if config_valid and "ci_command" in config and config["ci_command"] is not None:
        exe = _has_explicit_ci_executable(repo_root, config["ci_command"])
        if exe:
            return Diagnostic(
                CODE_READINESS_CI_DEFINITION,
                "ok",
                f"明示された ci-command の実行ファイルが確認されました: {exe}"
                " / Explicit ci-command executable found.",
                evidence=(exe,),
            )

    return Diagnostic(
        CODE_READINESS_CI_DEFINITION,
        "warning",
        "CI ワークフロー定義（.github/workflows/*.yml）、CI コマンド候補、または存在する CI 実行ファイルが見つかりません。"
        " / No CI workflow definition, candidate, or existing CI executable found.",
        remediation="CI のワークフローファイル（.github/workflows/ci.yml 等）を追加するか、"
        "設定ファイルで有効な ci-command の入口を設定してください。",
    )


def _check_unconfigured_ci_command(
    repo_root: Path,
    cached_suggested: list[str],
) -> Diagnostic:
    rel_script = platform_ci_script_relative()
    if (repo_root / rel_script).is_file():
        return Diagnostic(
            CODE_READINESS_CI_COMMAND,
            "ok",
            f"ci-command 未設定のため既定スクリプト ({rel_script}) にフォールバックします。"
            f" / Falling back to default CI script: {rel_script}",
            evidence=(str(rel_script),),
        )
    suggestions_str = (
        ", ".join(f'"{c}"' for c in cached_suggested)
        if cached_suggested
        else 'ci-command = "pytest"'
    )
    return Diagnostic(
        CODE_READINESS_CI_COMMAND,
        "warning",
        f"ci-command が未設定で、既定スクリプト ({rel_script}) も見つかりません。"
        "apply 付き dispatch は終了コード 2 で停止します。"
        f" / ci-command not configured and default script ({rel_script}) not found. "
        "Dispatch with apply will exit with code 2.",
        remediation=f'設定ファイルで ci-command を設定してください（例: ci-command = "pytest" または候補: {suggestions_str}）。',
    )


def _check_ci_path_executable(repo_root: Path, executable: str) -> Diagnostic:
    if Path(executable).is_absolute():
        p = Path(executable)
        if p.is_file():
            return Diagnostic(
                CODE_READINESS_CI_COMMAND,
                "ok",
                f"ci-command の実行ファイルが確認されました: {executable}"
                "（存在の診断であり、CI 成功を保証するものではありません）。"
                " / CI executable exists (existence diagnosed; does not guarantee CI success).",
                evidence=(executable,),
            )
        return Diagnostic(
            CODE_READINESS_CI_COMMAND,
            "warning",
            f"ci-command の実行ファイルが存在しないかディレクトリです: {executable}"
            " / CI executable not found or is a directory.",
            remediation='ci-command = "pytest" のように存在する実行ファイルまたはコマンドを設定してください。',
        )

    p = repo_root / executable
    if p.is_file():
        return Diagnostic(
            CODE_READINESS_CI_COMMAND,
            "ok",
            f"ci-command の実行ファイルがリポジトリ内に確認されました: {executable}"
            "（存在の診断であり、CI 成功を保証するものではありません）。"
            " / CI executable exists in repo (existence diagnosed; does not guarantee CI success).",
            evidence=(executable,),
        )
    return Diagnostic(
        CODE_READINESS_CI_COMMAND,
        "warning",
        f"ci-command の実行ファイルがリポジトリ内に見つからないかディレクトリです: {executable}"
        " / CI executable not found or is a directory.",
        remediation='ci-command = "pytest" のように存在する実行ファイルまたはコマンドを設定してください。',
    )


def _check_configured_executable(
    repo_root: Path,
    executable: str,
    which_fn: Callable[[str], str | None],
    cached_suggested: list[str],
) -> Diagnostic:
    if re.match(r"^[a-zA-Z]:[^/\\]", executable):
        return Diagnostic(
            CODE_READINESS_CI_COMMAND,
            "warning",
            f"ci-command の実行ファイル指定 '{executable}' はドライブ相対パスのため基準が曖昧です。"
            " / Drive-relative path is ambiguous.",
            remediation="絶対パスまたはリポジトリルート相対パスで指定してください。",
        )

    if (
        Path(executable).is_absolute()
        or executable.startswith(".")
        or "/" in executable
        or "\\" in executable
    ):
        return _check_ci_path_executable(repo_root, executable)

    found = which_fn(executable)
    if found:
        return Diagnostic(
            CODE_READINESS_CI_COMMAND,
            "ok",
            f"ci-command のコマンドが PATH 上に確認されました: {executable}"
            "（存在の診断であり、CI 成功を保証するものではありません）。"
            " / CI command found in PATH (existence diagnosed; does not guarantee CI success).",
            evidence=(found,),
        )
    suggestions_str = (
        ", ".join(f'"{c}"' for c in cached_suggested)
        if cached_suggested
        else 'ci-command = "pytest"'
    )
    return Diagnostic(
        CODE_READINESS_CI_COMMAND,
        "warning",
        f"ci-command のコマンド '{executable}' が PATH 上に見つかりません。"
        " / CI command not found in PATH.",
        remediation=f'コマンドをインストールするか、設定を変更してください（例: ci-command = "pytest" または候補: {suggestions_str}）。',
    )


def _check_ci_command(
    repo_root: Path,
    config: Mapping[str, Any],
    *,
    which_fn: Callable[[str], str | None],
    cached_suggested: list[str],
) -> Diagnostic:
    if "ci_command" not in config or config.get("ci_command") is None:
        return _check_unconfigured_ci_command(repo_root, cached_suggested)

    try:
        argv = parse_ci_command(config["ci_command"])
    except (ValueError, TypeError, OSError) as exc:
        return Diagnostic(
            CODE_READINESS_CI_COMMAND,
            "warning",
            f"ci-command の構文解析に失敗しました: {exc} / Failed to parse ci-command.",
            remediation='ci-command = "pytest" のように有効なコマンドを設定してください。',
        )

    if not argv:
        return Diagnostic(
            CODE_READINESS_CI_COMMAND,
            "warning",
            "ci-command が空です。 / ci-command is empty.",
            remediation='ci-command = "pytest" のように有効なコマンドを設定してください。',
        )

    return _check_configured_executable(repo_root, argv[0], which_fn, cached_suggested)


def _resolve_target_name(
    config: Mapping[str, Any],
    env: Mapping[str, str],
    which_fn: Callable[[str], str | None],
) -> str:
    if "dispatch_target" in config and config.get("dispatch_target"):
        target_name = config["dispatch_target"]
    else:
        target_name = resolve_default_dispatch_target_name(env)

    if target_name == "auto":
        detected_cli = detect_installed_local_cli(which_fn=which_fn)
        cli_to_target = {
            "claude": "claude-cli",
            "agy": "agy-cli",
            "codex": "codex-cli",
            "grok": "grok-cli",
        }
        return cli_to_target[detected_cli] if detected_cli in cli_to_target else "local"
    return str(target_name)


def _check_cloud_routine_target(
    config: Mapping[str, Any],
    env: Mapping[str, str],
) -> tuple[Diagnostic, str]:
    routine_id = env.get(ROUTINE_ID_ENV_VAR) or config.get("routine_id")
    routine_token = env.get(ROUTINE_TOKEN_ENV_VAR) or config.get("routine_token")
    missing_keys: list[str] = []
    if not routine_id:
        missing_keys.append(f"{ROUTINE_ID_ENV_VAR} または設定 routine_id")
    if not routine_token:
        missing_keys.append(ROUTINE_TOKEN_ENV_VAR)

    if not missing_keys:
        diag = Diagnostic(
            CODE_READINESS_EXECUTOR,
            "ok",
            "実行先 cloud-routine の必須設定が確認されました。"
            " / cloud-routine required settings are configured.",
            evidence=(
                f"{ROUTINE_ID_ENV_VAR}/routine_id: set",
                f"{ROUTINE_TOKEN_ENV_VAR}: set",
            ),
        )
        return diag, "cloud-routine"
    diag = Diagnostic(
        CODE_READINESS_EXECUTOR,
        "warning",
        f"実行先 cloud-routine の必須設定が不足しています ({', '.join(missing_keys)})。"
        "ローカルのダミー起動へフォールバックします。"
        " / cloud-routine missing required settings; falling back to local dummy.",
        remediation=f"環境変数 {ROUTINE_TOKEN_ENV_VAR} および {ROUTINE_ID_ENV_VAR}"
        "（または設定の routine_id）を設定してください。",
    )
    return diag, "local"


def _check_codex_cloud_target(
    config: Mapping[str, Any],
    env: Mapping[str, str],
) -> tuple[Diagnostic, str]:
    cloud_env = env.get(CODEX_CLOUD_ENV_VAR) or config.get("codex_cloud_env")
    if cloud_env:
        diag = Diagnostic(
            CODE_READINESS_EXECUTOR,
            "ok",
            "実行先 codex-cloud の必須設定が確認されました。"
            " / codex-cloud required settings are configured.",
            evidence=(f"{CODEX_CLOUD_ENV_VAR}/codex_cloud_env: set",),
        )
        return diag, "codex-cloud"
    diag = Diagnostic(
        CODE_READINESS_EXECUTOR,
        "warning",
        f"実行先 codex-cloud の必須設定が不足しています ({CODEX_CLOUD_ENV_VAR} または設定 codex_cloud_env)。"
        "ローカルのダミー起動へフォールバックします。"
        " / codex-cloud missing environment ID; falling back to local dummy.",
        remediation=f"環境変数 {CODEX_CLOUD_ENV_VAR} または設定の codex_cloud_env を設定してください。",
    )
    return diag, "local"


def _check_local_or_cli_target(
    resolved_target: str,
    which_fn: Callable[[str], str | None],
) -> tuple[Diagnostic, str]:
    if resolved_target == "local":
        diag = Diagnostic(
            CODE_READINESS_EXECUTOR,
            "ok",
            "実行先は local です（任意コマンド/ダミー実行。実行可否は保証されません）。"
            " / Execution target is local (arbitrary/dummy execution; execution viability not guaranteed).",
            evidence=("local (dummy/arbitrary command)",),
        )
        return diag, "local"

    if resolved_target in _LOCAL_CLI_BINARIES:
        bin_name = _LOCAL_CLI_BINARIES[resolved_target]
        found = which_fn(bin_name)
        if found:
            diag = Diagnostic(
                CODE_READINESS_EXECUTOR,
                "ok",
                f"実行先 '{resolved_target}' の実行ファイル '{bin_name}' が確認されました。"
                f" / Execution target CLI '{bin_name}' found in PATH.",
                evidence=(found,),
            )
            return diag, resolved_target
        diag = Diagnostic(
            CODE_READINESS_EXECUTOR,
            "warning",
            f"実行先 '{resolved_target}' の実行ファイル '{bin_name}' が PATH 上に見つかりません。"
            f" / Execution target CLI '{bin_name}' not found in PATH.",
            remediation=f"CLI '{bin_name}' をインストールするか、利用可能な dispatch_target を設定してください。",
        )
        return diag, resolved_target

    diag = Diagnostic(
        CODE_READINESS_EXECUTOR,
        "warning",
        f"未知の実行先 '{resolved_target}' です。 / Unknown dispatch target '{resolved_target}'.",
        remediation="有効な dispatch_target を設定してください。",
    )
    return diag, resolved_target


def _check_executor(
    config: Mapping[str, Any],
    *,
    which_fn: Callable[[str], str | None],
    env: Mapping[str, str],
) -> tuple[Diagnostic, str]:
    resolved_target = _resolve_target_name(config, env, which_fn)
    if resolved_target == "cloud-routine":
        return _check_cloud_routine_target(config, env)
    if resolved_target == "codex-cloud":
        return _check_codex_cloud_target(config, env)
    return _check_local_or_cli_target(resolved_target, which_fn)


def _check_reviewer(
    config: Mapping[str, Any],
    effective_target: str,
    *,
    which_fn: Callable[[str], str | None],
) -> Diagnostic:
    raw_bot = config.get("reviewer_bot")
    reviewer_bot: Literal["auto", "claude", "codex"] = (
        raw_bot if raw_bot in ("auto", "claude", "codex") else "auto"
    )
    overrides = extract_reviewer_by_target(dict(config))
    installed = frozenset(name for name in ("claude", "codex") if which_fn(name))

    resolved = reviewer_for_installed_vendors(
        reviewer_bot, effective_target, overrides, installed
    )
    if resolved is None:
        return Diagnostic(
            CODE_READINESS_REVIEWER,
            "warning",
            "子タスクのレビュー担当を自動解決できませんでした。"
            " / Could not automatically resolve reviewer bot.",
            remediation='設定ファイルで reviewer-bot = "claude" または "codex" を明示してください。',
        )

    if resolved in ("claude", "codex"):
        found = which_fn(resolved)
        if found:
            return Diagnostic(
                CODE_READINESS_REVIEWER,
                "ok",
                f"レビュー担当 '{resolved}' の実行ファイルが確認されました。"
                f" / Reviewer CLI '{resolved}' found in PATH.",
                evidence=(found,),
            )
        return Diagnostic(
            CODE_READINESS_REVIEWER,
            "warning",
            f"レビュー担当は '{resolved}' ですが、実行ファイルが PATH 上に見つかりません"
            "（ローカル CLI が無いことは、GitHub 上のレビューワークフローが無いことと同じではありません）。"
            f" / Reviewer is '{resolved}' but CLI not found in PATH "
            "(missing local CLI does not mean GitHub review workflow is absent).",
            remediation=f'CLI \'{resolved}\' をインストールするか、設定で reviewer-bot = "claude" または "codex" を指定してください。',
        )

    return Diagnostic(
        CODE_READINESS_REVIEWER,
        "ok",
        f"レビュー担当: {resolved} / Reviewer: {resolved}",
        evidence=(str(resolved),),
    )


def _check_ignore(repo_root: Path) -> Diagnostic:
    gi_path = repo_root / ".gitignore"
    if not gi_path.is_file():
        return Diagnostic(
            CODE_READINESS_IGNORE,
            "warning",
            ".gitignore が見つかりません。 / .gitignore not found.",
            remediation="orchestune.toml と .orchestune/ を .gitignore に追加してください。",
        )

    try:
        content = gi_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return Diagnostic(
            CODE_READINESS_IGNORE,
            "warning",
            f".gitignore の読込に失敗しました: {exc} / Failed to read .gitignore.",
            remediation="orchestune.toml と .orchestune/ を .gitignore に追加してください。",
        )

    has_toml = False
    has_dot_orchestune = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in ("orchestune.toml", "/orchestune.toml"):
            has_toml = True
        if stripped in ORCHESTUNE_GITIGNORE_LINES:
            has_dot_orchestune = True

    if has_toml and has_dot_orchestune:
        return Diagnostic(
            CODE_READINESS_IGNORE,
            "ok",
            ".gitignore に orchestune.toml と .orchestune/ の除外設定が確認されました"
            "（.orchestune/tmp/ 用の許容記述の存在確認であり、否定行や global ignore の評価は含みません）。"
            " / .gitignore includes orchestune.toml and .orchestune/ patterns.",
            evidence=(".gitignore",),
        )

    missing: list[str] = []
    if not has_toml:
        missing.append("orchestune.toml")
    if not has_dot_orchestune:
        missing.append(".orchestune/")
    remed = f"{' と '.join(missing)} を .gitignore に追加してください。"
    return Diagnostic(
        CODE_READINESS_IGNORE,
        "warning",
        f".gitignore に必要な除外行が不足しています ({', '.join(missing)})。"
        " / .gitignore is missing recommended entries.",
        remediation=remed,
    )


def _check_auth(
    which_fn: Callable[[str], str | None],
    forge_factory: Callable[[], Any] | None,
) -> Diagnostic:
    if which_fn("gh") is None:
        return Diagnostic(
            CODE_READINESS_AUTH,
            "warning",
            "gh CLI が見つかりません。 / gh CLI not found.",
            remediation="gh CLI をインストールし、gh auth login を実行してください。",
        )

    factory = forge_factory if forge_factory is not None else GitHubForge

    try:
        forge = factory()
        forge.check_auth()
        login = forge.get_authenticated_user()
        if not login or not str(login).strip():
            return Diagnostic(
                CODE_READINESS_AUTH,
                "warning",
                "GitHub の認証ユーザー名が空です。 / GitHub authenticated user is empty.",
                remediation="gh auth login を実行して再認証してください。",
            )
        return Diagnostic(
            CODE_READINESS_AUTH,
            "ok",
            f"GitHub 認証が確認されました (ユーザー: {login})。 / GitHub authentication verified.",
            evidence=(str(login).strip(),),
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except (
        ForgeError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        return Diagnostic(
            CODE_READINESS_AUTH,
            "warning",
            f"GitHub 認証の確認に失敗しました: {exc} / GitHub authentication check failed.",
            remediation="gh auth login を実行して認証を設定してください。",
        )


def run_readiness_checks(
    repo_root: Path,
    config: Mapping[str, Any],
    config_valid: bool,
    *,
    online: bool = False,
    which_fn: Callable[[str], str | None] | None = None,
    env: Mapping[str, str] | None = None,
    forge_factory: Callable[[], Any] | None = None,
) -> tuple[Diagnostic, ...]:
    """Run repository readiness checks (issue #1648)."""
    active_which = which_fn if which_fn is not None else shutil.which
    active_env = env if env is not None else os.environ
    cached_suggested = _suggest_ci_commands(repo_root)

    diagnostics: list[Diagnostic] = []
    diagnostics.append(_check_discipline(repo_root))
    diagnostics.append(
        _check_ci_definition(
            repo_root, config, config_valid, cached_suggested=cached_suggested
        )
    )

    if config_valid:
        diagnostics.append(
            _check_ci_command(
                repo_root,
                config,
                which_fn=active_which,
                cached_suggested=cached_suggested,
            )
        )
        exec_diag, effective_target = _check_executor(
            config, which_fn=active_which, env=active_env
        )
        diagnostics.append(exec_diag)
        diagnostics.append(
            _check_reviewer(config, effective_target, which_fn=active_which)
        )

    diagnostics.append(_check_ignore(repo_root))

    if online:
        diagnostics.append(_check_auth(active_which, forge_factory))

    return tuple(diagnostics)
