"""skills/**/*.md 内のコマンド参照が実在することを検証する。

detect-bloat・baseline-aware CI・quarantine 機構のIssue群と同じ形の腐敗
（文書やコメントが存在しない機構を前提に判断を委ねる状態）の再発を止める
ため、SKILL.md のフェンス付きコードブロック内で `poetry run` / `./scripts/`
形式で参照されているコマンドが、実際に pyproject.toml のスクリプト定義・
Poetry仮想環境にインストールされた実行可能ファイル、またはリポジトリ内の
実ファイルに対応していることを機械的に検証する。
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"

_FENCED_CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_POETRY_RUN = re.compile(r"^poetry run (\S+)(?:\s+(\S+))?")
_SCRIPT_PATH = re.compile(r"^(\.[\\/]scripts[\\/]\S+)")
_EXECUTABLE_SUFFIXES = frozenset({".exe", ".cmd", ".bat"})


def _known_poetry_commands() -> set[str]:
    """`poetry run <name>` で実行できる名前の集合を返す。

    `[tool.poetry.scripts]` のエントリポイントに加え、`poetry run ruff` /
    `poetry run pytest` のように依存パッケージが提供するコマンドも正当な
    参照として扱う必要があるが、依存パッケージ名（例: `pyyaml`,
    `pytest-cov`）がそのまま実行可能コマンド名になるとは限らない
    （実行ファイルを一切インストールしない依存も多い）。そのため、実際に
    このテストを実行しているPoetry仮想環境の `bin/`（Windowsでは
    `Scripts/`）ディレクトリを走査し、そこに存在する実行可能ファイルの
    名前を正とする。
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = set(data["tool"]["poetry"].get("scripts", {}))

    venv_bin = Path(sys.executable).parent
    for candidate in venv_bin.iterdir():
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        name = (
            candidate.stem
            if candidate.suffix.lower() in _EXECUTABLE_SUFFIXES
            else candidate.name
        )
        names.add(name)
    return names


def _iter_command_targets(markdown_text: str) -> list[tuple[str, str]]:
    """フェンス付きコードブロックから `poetry run` / `./scripts/` /
    `.\\scripts\\` 形式のコマンドを抽出する。

    戻り値は (表示用の生コマンド行, 検証対象のスクリプト名/パス) のタプル
    のリスト。workflow-template のプレースホルダ（`<CI_ENTRYPOINT>` 等）を
    含む行は検証対象から除外する。
    """
    results: list[tuple[str, str]] = []
    for block_match in _FENCED_CODE_BLOCK.finditer(markdown_text):
        for raw_line in block_match.group(1).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            target: str | None = None
            poetry_match = _POETRY_RUN.match(line)
            if poetry_match:
                first, second = poetry_match.group(1), poetry_match.group(2)
                target = second if first == "python" and second else first
            else:
                script_match = _SCRIPT_PATH.match(line)
                if script_match:
                    target = script_match.group(1)

            if target is None or "<" in target or ">" in target:
                continue

            results.append((line, target))
    return results


def _command_exists(target: str, known_commands: set[str]) -> bool:
    normalized = target.replace("\\", "/")
    if normalized.startswith("./") or normalized.startswith("scripts/"):
        return (REPO_ROOT / normalized).is_file()
    return target in known_commands


def _collect_all_command_references() -> list[tuple[str, str, str]]:
    references: list[tuple[str, str, str]] = []
    for skill_md in sorted(SKILLS_ROOT.glob("**/*.md")):
        text = skill_md.read_text(encoding="utf-8")
        for line, target in _iter_command_targets(text):
            references.append((str(skill_md.relative_to(REPO_ROOT)), line, target))
    return references


@pytest.mark.parametrize(
    "skill_path, line, target",
    _collect_all_command_references(),
    ids=[f"{r[0]}::{r[2]}" for r in _collect_all_command_references()],
)
def test_skill_command_reference_exists(skill_path, line, target):
    known_commands = _known_poetry_commands()
    assert _command_exists(target, known_commands), (
        f"{skill_path} references a command that does not exist: {line!r} "
        f"(resolved target: {target!r})"
    )


def test_missing_poetry_script_command_is_detected():
    text = "```bash\npoetry run this-script-does-not-exist\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [
        ("poetry run this-script-does-not-exist", "this-script-does-not-exist")
    ]
    assert not _command_exists(targets[0][1], _known_poetry_commands())


def test_missing_script_file_is_detected():
    text = "```bash\n./scripts/does-not-exist.sh\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [("./scripts/does-not-exist.sh", "./scripts/does-not-exist.sh")]
    assert not _command_exists(targets[0][1], _known_poetry_commands())


def test_powershell_script_path_is_recognized():
    text = "```powershell\n.\\scripts\\local-ci.ps1\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [(".\\scripts\\local-ci.ps1", ".\\scripts\\local-ci.ps1")]
    assert _command_exists(targets[0][1], _known_poetry_commands())


def test_missing_powershell_script_is_detected():
    text = "```powershell\n.\\scripts\\does-not-exist.ps1\n```\n"

    targets = _iter_command_targets(text)

    assert not _command_exists(targets[0][1], _known_poetry_commands())


def test_workflow_template_placeholder_is_excluded():
    text = "```bash\npoetry run <CI_ENTRYPOINT>\n```\n"

    assert _iter_command_targets(text) == []


def test_inline_code_span_is_not_extracted():
    """フェンス付きコードブロック外のインラインコード
    （`poetry run detect-bloat` 等）は抽出対象としない。"""
    text = "この警告は `poetry run detect-bloat` 等で検知します。\n"

    assert _iter_command_targets(text) == []


def test_python_script_target_resolves_to_script_path():
    text = "```bash\npoetry run python scripts/wait_for_review.py --pr 1\n```\n"

    targets = _iter_command_targets(text)

    assert targets[0][1] == "scripts/wait_for_review.py"
    assert _command_exists(targets[0][1], _known_poetry_commands())


def test_poetry_dependency_command_is_accepted():
    """`ruff`/`pytest`/`mypy` のような、依存パッケージが提供するコマンドは
    `[tool.poetry.scripts]` になくても正当な参照として扱う。"""
    text = "```bash\npoetry run ruff format\n```\n"

    targets = _iter_command_targets(text)

    assert targets[0][1] == "ruff"
    assert _command_exists(targets[0][1], _known_poetry_commands())


def test_dependency_without_executable_is_rejected():
    """`pyyaml`/`pytest-cov`/`types-pyyaml` のように、依存パッケージ名では
    あっても実行可能ファイルを一切インストールしない名前は、依存表に
    載っているというだけで正当なコマンドとして誤って通過させてはならない
    （`poetry run pyyaml` はCommand not foundになる）。"""
    known_commands = _known_poetry_commands()

    for non_executable_dependency in ("pyyaml", "pytest-cov", "types-pyyaml"):
        assert not _command_exists(non_executable_dependency, known_commands)
