"""Guided project setup for an existing Git repository."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from orchestune.bootstrap import prepare_github_labels
from orchestune.config_wizard.ci_candidates import suggest_ci_commands
from orchestune.config_wizard.cli import TerminalPromptPort
from orchestune.config_wizard.wizard import WizardResult, run_config_wizard
from orchestune.dispatch.doctor.check import DoctorRequest, run_doctor
from orchestune.dispatch.doctor.cli import render_report
from orchestune.dispatch.targets import LOCAL_CLI_CANDIDATES, detect_installed_local_cli
from orchestune.infra.cli_output import Level, make_output
from orchestune.infra.git_cli import inspect_checkout, read_origin_url
from orchestune.init_ignore import (
    IgnoreApplyError,
    IgnorePlan,
    apply_ignore_plan,
    base_candidates,
    build_ignore_plan,
    plan_diff,
    runtime_candidates,
)
from orchestune.installer.cli import main as installer_main

_PRECONDITION = {
    "not_git": "Gitリポジトリではありません。",
    "bare": "bareリポジトリでは init を実行できません。",
    "linked_worktree": "linked worktree では init を実行できません。",
    "no_head": "HEAD がないリポジトリでは init を実行できません。",
    "unreadable": "Git の状態を読めませんでした。",
}
_TARGETS = ("codex", "claude", "antigravity", "grok", "all", "skip")
_OWNER_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass
class _Progress:
    root: Path
    dry_run: bool
    offline: bool
    skip_save: bool = False
    failure: bool = False
    notes: list[str] = field(default_factory=list)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.dry_run and not _interactive():
        _say(
            Level.FAILURE,
            "通常モードは対話端末だけで実行できます。"
            " orchestune init --dry-run、または skills / config / bootstrap / doctor"
            " を個別に使ってください。",
        )
        return 2
    try:
        return _run(args.dry_run, args.offline)
    except (KeyboardInterrupt, EOFError):
        _say(Level.WARNING, "中断しました。後続の手順は実行していません。")
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestune init",
        description="Guide setup of an existing Git repository.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan without prompts, writes, locks, or network.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip GitHub authentication and label creation.",
    )
    return parser


def _run(dry_run: bool, offline: bool) -> int:
    inspection = inspect_checkout(Path.cwd())
    if inspection.kind != "ok" or inspection.toplevel is None:
        _say(Level.FAILURE, _PRECONDITION.get(inspection.kind, "前提を満たしません。"))
        return 2
    progress = _Progress(inspection.toplevel, dry_run, offline)
    _print_discovery(progress, inspection.status_text)
    stopped = _base_step(progress)
    if stopped is not None:
        return stopped
    stopped = _skill_step(progress)
    if stopped is not None:
        return stopped
    stopped = _config_step(progress)
    if stopped is not None:
        return stopped
    stopped = _runtime_step(progress)
    if stopped is not None:
        return stopped
    stopped = _label_step(progress)
    if stopped is not None:
        return stopped
    return _doctor_step(progress)


def _print_discovery(progress: _Progress, status: str) -> None:
    _say(Level.INFO, f"Git root: {progress.root}")
    text = status.strip() or "(clean)"
    _say(Level.INFO, f"git status:\n{text}")
    found = [name for name in LOCAL_CLI_CANDIDATES if shutil.which(name)]
    preferred = detect_installed_local_cli()
    _say(Level.INFO, f"AI CLI: {', '.join(found) if found else '(none)'}")
    _say(Level.INFO, f"preferred CLI: {preferred or '(none)'}")
    commands = suggest_ci_commands(progress.root)
    _say(
        Level.INFO,
        f"CI candidates: {', '.join(commands) if commands else '(none)'}",
    )


def _base_step(progress: _Progress) -> int | None:
    plan = build_ignore_plan(progress.root, base_candidates(progress.root))
    _show_plan("基本の除外", plan)
    if progress.dry_run:
        progress.notes.append("基本の除外は dry-run のため未実施")
        return None
    if not _confirm("基本の除外を .gitignore に反映しますか?"):
        progress.skip_save = True
        progress.notes.append(
            "除外を承認しなかったため、スキル導入と設定保存を見送ります。"
        )
        return None
    return _commit_plan(progress, plan)


def _runtime_step(progress: _Progress) -> int | None:
    candidates = runtime_candidates(progress.root)
    if candidates is None:
        progress.failure = True
        progress.notes.append(
            "設定を読めないため、ランタイムパスの除外は保留しました。"
        )
        return None
    plan = build_ignore_plan(progress.root, candidates)
    _show_plan("ランタイムパスの除外", plan)
    if progress.dry_run:
        progress.notes.append("ランタイムパスの除外は dry-run のため未実施")
        return None
    if not _confirm("ランタイムパスの除外を .gitignore に反映しますか?"):
        progress.notes.append("ランタイムパスの除外をスキップしました。")
        return None
    return _commit_plan(progress, plan)


def _commit_plan(progress: _Progress, plan: IgnorePlan) -> int | None:
    if not any(item.action == "append" for item in plan.decisions):
        progress.notes.append(".gitignore への追記はありません。")
        return None
    try:
        apply_ignore_plan(progress.root, plan)
    except IgnoreApplyError as exc:
        _say(Level.FAILURE, str(exc))
        _say(
            Level.INFO,
            "再実行すると現在のファイルを読み直します。保存済みの変更は戻しません。",
        )
        if exc.code == 3:
            return 3
        progress.failure = True
        return None
    progress.notes.append(".gitignore を更新しました。")
    return None


def _skill_step(progress: _Progress) -> int | None:
    if progress.dry_run:
        progress.notes.append("スキル導入は dry-run のため未実施")
        return None
    if progress.skip_save:
        return None
    target = _choose_target()
    if target is None:
        progress.notes.append("スキル導入をスキップしました。")
        return None
    preview = ["install", *_project_args(progress.root, target), "--dry-run"]
    _say(Level.INFO, f"installer dry-run: {' '.join(preview)}")
    code = installer_main(preview)
    if code == 3:
        _say(Level.FAILURE, "スキル導入の確認で競合しました。")
        return 3
    if code != 0:
        progress.failure = True
        progress.notes.append(
            "スキル導入の dry-run が失敗しました。導入はしていません。"
        )
        return None
    if not _confirm("この内容でプロジェクトへスキルを導入しますか?"):
        progress.notes.append("スキル導入をスキップしました。")
        return None
    return _install(progress, target)


def _install(progress: _Progress, target: str) -> int | None:
    code = installer_main(["install", *_project_args(progress.root, target)])
    if code == 3:
        _say(
            Level.FAILURE,
            "スキル導入が既存内容と競合しました。強制適用はしていません。",
        )
        return 3
    if code != 0:
        progress.failure = True
        progress.notes.append(
            "スキル導入に失敗しました。再実行すると現物を読み直します。"
        )
        return None
    progress.notes.append(f"スキルを導入しました: {target}")
    return None


def _config_step(progress: _Progress) -> int | None:
    if progress.dry_run:
        progress.notes.append("設定ウィザードは dry-run のため未実施")
        return None
    if progress.skip_save:
        return None
    exists = (progress.root / "orchestune.toml").is_file()
    question = (
        "既存の orchestune.toml を編集しますか?"
        if exists
        else "orchestune.toml を新規作成しますか?"
    )
    if not _confirm(question):
        progress.notes.append("設定の保存をスキップしました。")
        return None
    result = run_config_wizard(
        "edit" if exists else "init", progress.root, TerminalPromptPort()
    )
    return _wizard_result(progress, result)


def _wizard_result(progress: _Progress, result: WizardResult) -> int | None:
    if result.status == "cancelled":
        _say(
            Level.WARNING,
            "設定ウィザードを取り消しました。後続の手順は実行していません。",
        )
        return 130
    progress.notes.append(f"設定ウィザード: {result.status}")
    return None


def _label_step(progress: _Progress) -> int | None:
    slug = _github_slug(read_origin_url(progress.root))
    if progress.offline or progress.dry_run:
        why = "offline" if progress.offline else "dry-run"
        progress.notes.append(
            f"GitHub ラベル準備は {why} のため未実施 ({slug or '対象不明'})"
        )
        return None
    if slug is None:
        progress.notes.append(
            "checkout の GitHub リポジトリを照合できないため、ラベルは作りません。"
        )
        return None
    _say(Level.INFO, f"GitHub ラベルの対象: {slug}")
    if not _confirm("このリポジトリの必須ラベルを準備しますか?"):
        progress.notes.append("ラベル準備をスキップしました。")
        return None
    if prepare_github_labels(cwd=progress.root) != 0:
        progress.failure = True
        progress.notes.append(
            "ラベル準備に失敗したか、対象が一致しないため作成を見送りました。"
        )
        _say(Level.INFO, "再実行すると既存ラベルを確認し、不足分だけを作成します。")
        return None
    progress.notes.append("GitHub ラベルを確認しました。")
    return None


def _doctor_step(progress: _Progress) -> int:
    online = not progress.offline and not progress.dry_run
    report = run_doctor(DoctorRequest("local", progress.root, online=online))
    render_report(make_output(sys.stdout), report)
    for note in progress.notes:
        _say(Level.INFO, note)
    _say(
        Level.INFO,
        "次の依頼例: このリポジトリの規律と検証コマンドを確認し、実装前に作業計画を出してください",
    )
    if progress.failure or report.has_error:
        return 1
    return 0


def _project_args(root: Path, target: str) -> list[str]:
    return ["--scope", "project", "--project-dir", str(root), "--target", target]


def _choose_target() -> str | None:
    _say(Level.INFO, "スキル導入先: " + ", ".join(_TARGETS))
    while True:
        answer = input("導入先 [skip]: ").strip().lower()
        if answer in {"", "skip"}:
            return None
        if answer in _TARGETS and answer != "skip":
            return answer
        _say(Level.WARNING, "候補のいずれかを入力してください。")


def _confirm(message: str) -> bool:
    while True:
        answer = input(f"{message} [y/N] ").strip().lower()
        if answer in {"", "n", "no"}:
            return False
        if answer in {"y", "yes"}:
            return True
        _say(Level.WARNING, "y または n を入力してください。")


def _show_plan(title: str, plan: IgnorePlan) -> None:
    _say(Level.INFO, f"{title}:")
    _say(Level.INFO, plan_diff(plan))
    for item in plan.decisions:
        if item.action != "append":
            _say(Level.INFO, f"{item.action}: {item.path} ({item.reason})")


def _github_slug(url: str | None) -> str | None:
    if url is None:
        return None
    text = url.strip().removesuffix(".git")
    if text.lower().startswith("git@github.com:"):
        slug = text.split(":", 1)[1]
    else:
        parsed = urlparse(text)
        host = parsed.hostname
        if host is None or host.lower() != "github.com":
            return None
        slug = parsed.path.strip("/")
    parts = [part for part in slug.split("/") if part]
    if len(parts) != 2:
        return None
    slug = f"{parts[0]}/{parts[1]}"
    if _OWNER_REPO.fullmatch(slug) is None:
        return None
    return slug


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _say(level: Level, message: str) -> None:
    make_output(sys.stdout).message(level, message)
