"""skills/**/*.md 内のコマンド参照が実在することを検証する。

detect-bloat・baseline-aware CI・quarantine 機構のIssue群と同じ形の腐敗
（文書やコメントが存在しない機構を前提に判断を委ねる状態）の再発を止める
ため、SKILL.md のフェンス付きコードブロックおよびインラインコードスパン
（`` `...` ``）内で `poetry run` / `./scripts/` 形式で参照されている
コマンドが、実際に pyproject.toml のスクリプト定義・Poetry仮想環境に
インストールされた実行可能ファイル、またはリポジトリ内の実ファイルに
対応していることを機械的に検証する。
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
_INLINE_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_SHELL_PROMPT_PREFIX = re.compile(r"^(?:\$\s+|PS[^>\n]*>\s*)")
_POETRY_RUN = re.compile(r"^poetry run (.+)$")
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


def _python_script_target(argv: list[str]) -> str | None:
    """`poetry run python <argv...>` のうち、検証すべきスクリプトパスを
    返す。`-m <module>`（モジュール実行）や `--version` のような、実ファイル
    に対応しないインタプリタオプションのみの呼び出しは検証対象外として
    None を返す。"""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "-m":
            return None
        if token.startswith("-"):
            i += 1
            continue
        return token
    return None


def _extract_target(candidate: str) -> str | None:
    """1行分のコマンド候補文字列から検証対象のスクリプト名/パスを抽出する。

    `poetry run` / `./scripts/` / `.\\scripts\\` のいずれの形式にも
    一致しない場合や、workflow-template のプレースホルダ
    （`<CI_ENTRYPOINT>` 等）を含む場合は None を返す。`$ ` や `PS>` の
    ようなシェルプロンプトの接頭辞は、コマンド本体の前に取り除く。
    """
    candidate = _SHELL_PROMPT_PREFIX.sub("", candidate.strip(), count=1).strip()
    if not candidate or candidate.startswith("#"):
        return None

    target: str | None = None
    poetry_match = _POETRY_RUN.match(candidate)
    if poetry_match:
        tokens = poetry_match.group(1).split()
        if tokens and tokens[0] == "python":
            target = _python_script_target(tokens[1:])
        elif tokens:
            target = tokens[0]
    else:
        script_match = _SCRIPT_PATH.match(candidate)
        if script_match:
            target = script_match.group(1)

    if target is None or "<" in target or ">" in target:
        return None
    return target


def _iter_command_targets(markdown_text: str) -> list[tuple[str, str]]:
    """フェンス付きコードブロックおよびインラインコードスパンから
    `poetry run` / `./scripts/` / `.\\scripts\\` 形式のコマンドを抽出する。

    戻り値は (表示用の生コマンド行, 検証対象のスクリプト名/パス) のタプル
    のリスト。
    """
    results: list[tuple[str, str]] = []

    for block_match in _FENCED_CODE_BLOCK.finditer(markdown_text):
        for raw_line in block_match.group(1).splitlines():
            target = _extract_target(raw_line)
            if target is not None:
                results.append((raw_line.strip(), target))

    prose_only = _FENCED_CODE_BLOCK.sub("", markdown_text)
    for span_match in _INLINE_CODE_SPAN.finditer(prose_only):
        span = span_match.group(1)
        target = _extract_target(span)
        if target is not None:
            results.append((span.strip(), target))

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


def test_inline_code_span_is_extracted():
    """地の文中のインラインコードスパン（例:
    `` `poetry run detect-bloat` ``）も抽出・検証対象に含める。
    フェンス付きコードブロックに書かれていなければ検証を逃れられる、
    という抜け穴を作らないため。"""
    text = "この警告は `poetry run detect-bloat` 等で検知します。\n"

    targets = _iter_command_targets(text)

    assert targets == [("poetry run detect-bloat", "detect-bloat")]
    assert not _command_exists(targets[0][1], _known_poetry_commands())


def test_inline_code_span_inside_fenced_block_is_not_double_counted():
    text = "```bash\npoetry run ruff format\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [("poetry run ruff format", "ruff")]


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


def test_shell_prompt_prefix_is_stripped_before_matching():
    """`$ poetry run <cmd>` のようなシェルプロンプト表記でも、
    プロンプト記号に阻まれず本体のコマンドを抽出・検証できることを
    確認する。"""
    text = "```bash\n$ poetry run this-script-does-not-exist\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [
        ("$ poetry run this-script-does-not-exist", "this-script-does-not-exist")
    ]
    assert not _command_exists(targets[0][1], _known_poetry_commands())


def test_powershell_prompt_prefix_is_stripped_before_matching():
    text = "```powershell\nPS> .\\scripts\\does-not-exist.ps1\n```\n"

    targets = _iter_command_targets(text)

    assert targets[0][1] == ".\\scripts\\does-not-exist.ps1"
    assert not _command_exists(targets[0][1], _known_poetry_commands())


def test_python_module_invocation_is_not_validated_as_file():
    """`poetry run python -m pytest` のようなモジュール実行は、`-m` の
    引数がファイルパスではないため検証対象から除外する（誤検知防止）。"""
    text = "```bash\npoetry run python -m pytest\n```\n"

    assert _iter_command_targets(text) == []


def test_python_interpreter_option_before_script_path_is_skipped():
    text = "```bash\npoetry run python -u scripts/wait_for_review.py --pr 1\n```\n"

    targets = _iter_command_targets(text)

    assert targets[0][1] == "scripts/wait_for_review.py"
    assert _command_exists(targets[0][1], _known_poetry_commands())


def test_python_version_flag_alone_yields_no_target():
    text = "```bash\npoetry run python --version\n```\n"

    assert _iter_command_targets(text) == []


def test_dependency_without_executable_is_rejected():
    """`pyyaml`/`pytest-cov`/`types-pyyaml` のように、依存パッケージ名では
    あっても実行可能ファイルを一切インストールしない名前は、依存表に
    載っているというだけで正当なコマンドとして誤って通過させてはならない
    （`poetry run pyyaml` はCommand not foundになる）。"""
    known_commands = _known_poetry_commands()

    for non_executable_dependency in ("pyyaml", "pytest-cov", "types-pyyaml"):
        assert not _command_exists(non_executable_dependency, known_commands)
