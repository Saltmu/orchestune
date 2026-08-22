"""skills/**/*.md 内のコマンド参照が実在することを検証する。

detect-bloat・baseline-aware CI・quarantine 機構のIssue群と同じ形の腐敗
（文書やコメントが存在しない機構を前提に判断を委ねる状態）の再発を止める
ため、SKILL.md のフェンス付きコードブロック内で `poetry run` / `./scripts/`
形式で参照されているコマンドが、実際に pyproject.toml のスクリプト定義
またはリポジトリ内の実ファイルに対応していることを機械的に検証する。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"

_FENCED_CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_POETRY_RUN = re.compile(r"^poetry run (\S+)(?:\s+(\S+))?")
_SCRIPT_PATH = re.compile(r"^(\./scripts/\S+)")


def _poetry_script_names() -> set[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(data["tool"]["poetry"]["scripts"])


def _iter_command_targets(markdown_text: str) -> list[tuple[str, str]]:
    """フェンス付きコードブロックから `poetry run` / `./scripts/` 形式の
    コマンドを抽出する。

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


def _command_exists(target: str, poetry_scripts: set[str]) -> bool:
    if target.startswith("./") or target.startswith("scripts/"):
        return (REPO_ROOT / target).is_file()
    return target in poetry_scripts


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
    poetry_scripts = _poetry_script_names()
    assert _command_exists(target, poetry_scripts), (
        f"{skill_path} references a command that does not exist: {line!r} "
        f"(resolved target: {target!r})"
    )


def test_missing_poetry_script_command_is_detected():
    text = "```bash\npoetry run this-script-does-not-exist\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [
        ("poetry run this-script-does-not-exist", "this-script-does-not-exist")
    ]
    assert not _command_exists(targets[0][1], _poetry_script_names())


def test_missing_script_file_is_detected():
    text = "```bash\n./scripts/does-not-exist.sh\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [("./scripts/does-not-exist.sh", "./scripts/does-not-exist.sh")]
    assert not _command_exists(targets[0][1], _poetry_script_names())


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
    assert _command_exists(targets[0][1], _poetry_script_names())
