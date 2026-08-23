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
# Pythonインタプリタのオプションのうち、次のトークンを自身のオペランドとして
# 消費するもの（そのオペランドはスクリプトパスではない）。
_PYTHON_OPTIONS_WITH_OPERAND = frozenset({"-W", "-X", "--check-hash-based-pycs"})
# それ単独でスクリプトパスを取らない（＝以降を検証対象としない）オプション。
_PYTHON_OPTIONS_WITHOUT_SCRIPT_TARGET = frozenset({"-m", "-c"})


def _poetry_script_names() -> frozenset[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return frozenset(data["tool"]["poetry"].get("scripts", {}))


# `orchestune-dispatch --parent-issue ...` や `orchestune provision ...` の
# ように、`poetry run` を付けずプロジェクトのエントリポイントを直接呼び出す
# 形式で書かれているSKILL.mdもある。この「素の先頭語」を認識する条件を
# `[tool.poetry.scripts]` の現行内容そのものにしてしまうと、エントリ
# ポイントが誤字や削除でズレたときに「既知の名前ではない＝コマンド参照とは
# 認識しない」扱いになり、検証がすり抜けてしまう（レビュー指摘: リネーム/
# 削除されたエントリポイントが検出されない）。そのため認識自体は
# `pyproject.toml` の現状に依存しない命名規約（`orchestune` または
# `orchestune-<name>`）で行い、実在確認は `_command_exists` に委ねる。
_POETRY_SCRIPT_NAMES = _poetry_script_names()
_BARE_ENTRY_POINT_PATTERN = re.compile(r"^orchestune(-[a-z0-9]+)*$")


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
    names = set(_POETRY_SCRIPT_NAMES)

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
    返す。`-m <module>` / `-c <code>`（モジュール実行・コード直接実行）や
    `--version` のような、実ファイルに対応しないインタプリタオプションのみ
    の呼び出しは検証対象外として None を返す。`-W`/`-X` のように自身の
    オペランドを取るオプションは、そのオペランドをスクリプトパス候補と
    誤認しないよう合わせて読み飛ばす。"""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _PYTHON_OPTIONS_WITHOUT_SCRIPT_TARGET:
            return None
        if token in _PYTHON_OPTIONS_WITH_OPERAND:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token
    return None


def _extract_target(
    candidate: str, *, standalone_bare_command_allowed: bool
) -> str | None:
    """1行分のコマンド候補文字列から検証対象のスクリプト名/パスを抽出する。

    `poetry run` / `./scripts/` / `.\\scripts\\` のいずれの形式にも
    一致しない場合や、workflow-template のプレースホルダ
    （`<CI_ENTRYPOINT>` 等）を含む場合は None を返す。`$ ` や `PS>` の
    ようなシェルプロンプトの接頭辞は、コマンド本体の前に取り除く。

    `standalone_bare_command_allowed` は、引数を伴わない素の
    `orchestune`系コマンド単独行（例: フェンス付きコードブロック内の
    `orchestune-dag` のみの1行）を「実行文脈が明確なので引数なしでも
    コマンド呼び出しとみなしてよいか」を制御する。フェンス付きコード
    ブロックはTrue、地の文中のインラインコードスパンはFalseを渡す
    ——インラインでは「`orchestune-provision`が起票する」のような、
    コマンド名ではなくスキル/コンポーネント名としての言及と区別が
    つかないため、引数を伴う場合のみコマンド呼び出しとみなす。
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
        else:
            parts = candidate.split(maxsplit=1)
            if parts and _BARE_ENTRY_POINT_PATTERN.match(parts[0]):
                if len(parts) == 2 or standalone_bare_command_allowed:
                    target = parts[0]

    if target is None or "<" in target or ">" in target:
        return None
    return target


def _iter_command_targets(markdown_text: str) -> list[tuple[str, str]]:
    """フェンス付きコードブロックおよびインラインコードスパンから
    `poetry run` / `./scripts/` / `.\\scripts\\` / 既知のプロジェクト
    エントリポイントを素で呼び出す形式のコマンドを抽出する。

    戻り値は (表示用の生コマンド行, 検証対象のスクリプト名/パス) のタプル
    のリスト。
    """
    results: list[tuple[str, str]] = []

    for block_match in _FENCED_CODE_BLOCK.finditer(markdown_text):
        for raw_line in block_match.group(1).splitlines():
            target = _extract_target(raw_line, standalone_bare_command_allowed=True)
            if target is not None:
                results.append((raw_line.strip(), target))

    prose_only = _FENCED_CODE_BLOCK.sub("", markdown_text)
    for span_match in _INLINE_CODE_SPAN.finditer(prose_only):
        span = span_match.group(1)
        target = _extract_target(span, standalone_bare_command_allowed=False)
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


def test_bare_project_entry_point_is_extracted():
    """`orchestune-dispatch --parent-issue ...` のように `poetry run` を
    付けず直接プロジェクトのエントリポイントを呼び出す形式
    （skills/orchestune-dispatch/SKILL.md, skills/orchestune-provision/
    SKILL.md の実際の記法）も抽出・検証対象に含める。"""
    text = "```bash\norchestune-dispatch --parent-issue 42\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [("orchestune-dispatch --parent-issue 42", "orchestune-dispatch")]
    assert _command_exists(targets[0][1], _known_poetry_commands())


def test_standalone_bare_mention_without_args_is_not_extracted():
    """`` `orchestune-provision` `` のように、引数を伴わず文中で
    スキル/コンポーネント名として言及しているだけの単語は、コマンド呼び出し
    として誤抽出しない（`orchestune-provision` は実在しない
    `[tool.poetry.scripts]` エントリだが、これはコマンドではなく
    `skills/orchestune-provision/SKILL.md` を指すスキル名としての言及
    であり、壊れたコマンド参照ではない）。この「引数必須」の扱いは
    地の文中のインラインコードスパンに限る（下のフェンス付きコード
    ブロックのテストと対になる）。"""
    text = "起票は `orchestune-provision` が担当します。\n"

    assert _iter_command_targets(text) == []


def test_standalone_bare_command_in_fenced_block_is_validated():
    """インラインコードスパンとは異なり、フェンス付きコードブロック内に
    単独で書かれた `orchestune`系コマンドは実行文脈が明確なので、
    引数がなくても検証対象に含める（リネーム/削除されたエントリポイントが
    引数なしの単独行で書かれた場合の検出漏れを防ぐ）。"""
    text = "```bash\norchestune-not-a-real-entrypoint\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [
        ("orchestune-not-a-real-entrypoint", "orchestune-not-a-real-entrypoint")
    ]
    assert not _command_exists(targets[0][1], _known_poetry_commands())


def test_standalone_real_command_in_fenced_block_is_accepted():
    text = "```bash\norchestune-dag\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [("orchestune-dag", "orchestune-dag")]
    assert _command_exists(targets[0][1], _known_poetry_commands())


def test_bare_unknown_word_is_not_extracted():
    """既知のプロジェクトエントリポイント名と一致しない先頭語（例:
    `orchestune.toml` のような設定ファイル名）を誤ってコマンド参照として
    抽出しない。"""
    text = "```text\norchestune.toml\n```\n"

    assert _iter_command_targets(text) == []


def test_missing_bare_entry_point_is_detected():
    """`orchestune-<name>` 命名規約に合致する素のコマンドは、
    `[tool.poetry.scripts]` に現在存在するかどうかに関わらず抽出対象と
    なり、実在しない名前であれば `_command_exists` で検出される
    （リネーム/削除されたエントリポイントの検出漏れ防止）。"""
    text = "```bash\norchestune-not-a-real-entrypoint --help\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [
        ("orchestune-not-a-real-entrypoint --help", "orchestune-not-a-real-entrypoint")
    ]
    assert not _command_exists(targets[0][1], _known_poetry_commands())


def test_python_dash_c_invocation_is_not_validated_as_file():
    """`poetry run python -c "print(1)"` のようなコード直接実行は、
    コード文字列をファイルパスとして誤検証しない。"""
    text = '```bash\npoetry run python -c "print(1)"\n```\n'

    assert _iter_command_targets(text) == []


def test_python_option_with_operand_before_script_path_is_skipped():
    """`-W`/`-X` のように自身のオペランドを取るオプションの引数を
    スクリプトパスと誤認しない。"""
    text = "```bash\npoetry run python -W ignore scripts/wait_for_review.py\n```\n"

    targets = _iter_command_targets(text)

    assert targets[0][1] == "scripts/wait_for_review.py"
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


def test_local_ci_developer_structure():
    """local-ci-developer スキルの references 分割と薄いルータ構造を検証する。"""
    skill_dir = SKILLS_ROOT / "local-ci-developer"
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file()

    skill_lines = len(skill_md.read_text(encoding="utf-8").splitlines())
    assert skill_lines < 100, f"SKILL.md must be under 100 lines, got {skill_lines}"

    assert (skill_dir / "references" / "tdd.md").is_file()
    assert (skill_dir / "references" / "pr.md").is_file()
    assert (skill_dir / "references" / "review-loop.md").is_file()

    skill_content = skill_md.read_text(encoding="utf-8")
    for forbidden_label in (
        "status:in-progress",
        "status:not-needed",
        "status:blocked-human-review",
    ):
        assert (
            forbidden_label not in skill_content
        ), f"SKILL.md must not contain direct label string {forbidden_label}"

    total_lines = sum(
        len(p.read_text(encoding="utf-8").splitlines()) for p in skill_dir.rglob("*.md")
    )
    assert (
        total_lines <= 500
    ), f"local-ci-developer total markdown lines must be <= 500, got {total_lines}"


def test_workflow_template_structure():
    """workflow-template スキルの references 分割と薄いルータ構造を検証する。"""
    skill_dir = SKILLS_ROOT / "workflow-template"
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file()

    skill_lines = len(skill_md.read_text(encoding="utf-8").splitlines())
    assert skill_lines < 100, f"SKILL.md must be under 100 lines, got {skill_lines}"

    assert (skill_dir / "references" / "tdd.md").is_file()
    assert (skill_dir / "references" / "pr.md").is_file()
    assert (skill_dir / "references" / "review-loop.md").is_file()

    skill_content = skill_md.read_text(encoding="utf-8")
    for forbidden_label in (
        "status:in-progress",
        "status:not-needed",
        "status:blocked-human-review",
    ):
        assert (
            forbidden_label not in skill_content
        ), f"SKILL.md must not contain direct label string {forbidden_label}"

    total_lines = sum(
        len(p.read_text(encoding="utf-8").splitlines()) for p in skill_dir.rglob("*.md")
    )
    assert (
        total_lines <= 500
    ), f"workflow-template total markdown lines must be <= 500, got {total_lines}"


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_worker_skills_forbid_direct_label_operations(skill_name: str):
    """Worker skills must explicitly prohibit direct label operations and avoid status label references."""
    skill_dir = SKILLS_ROOT / skill_name
    skill_md = skill_dir / "SKILL.md"
    skill_text = skill_md.read_text(encoding="utf-8")
    skill_text_lower = skill_text.lower()

    # Must explicitly state that direct label modifications are prohibited
    assert "label" in skill_text_lower
    assert (
        "no direct github label operations" in skill_text_lower
        or "never add, remove, or modify" in skill_text_lower
    )
    assert "outcome record" in skill_text_lower

    # Prohibit all status:* label references and unconstrained label mutation commands
    for md_file in skill_dir.rglob("*.md"):
        file_text = md_file.read_text(encoding="utf-8")
        status_labels = re.findall(r"\bstatus:[a-zA-Z0-9_-]+", file_text)
        assert (
            not status_labels
        ), f"{md_file} must not contain status label references: {status_labels}"

        for line in file_text.splitlines():
            line_lower = line.lower()
            if any(
                cmd in line_lower
                for cmd in (
                    "--add-label",
                    "--remove-label",
                    "add_label",
                    "remove_label",
                )
            ):
                assert any(
                    guard in line_lower
                    for guard in ("never", "no direct", "prohibit", "forbidden")
                ), f"{md_file} contains label mutation command outside prohibition admonition: {line}"


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_worker_skills_document_all_outcome_record_patterns(skill_name: str):
    """Worker skills must document complete JSON templates for all 3 outcome results (done, not-needed, blocked)."""
    skill_dir = SKILLS_ROOT / skill_name
    skill_md = skill_dir / "SKILL.md"
    skill_text = skill_md.read_text(encoding="utf-8")

    assert '"result": "done"' in skill_text or 'result: "done"' in skill_text
    assert (
        '"result": "not-needed"' in skill_text or 'result: "not-needed"' in skill_text
    )
    assert '"result": "blocked"' in skill_text or 'result: "blocked"' in skill_text
    assert "base-branch-red" in skill_text


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_workflow_skills_document_isolated_worktree_operations(skill_name: str):
    """変更作業はリポジトリ直下の隔離 worktree で完結させる。"""
    skill_dir = SKILLS_ROOT / skill_name
    skill_content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    worktree_reference = skill_dir / "references" / "worktree.md"

    assert worktree_reference.is_file()
    assert "references/worktree.md" in skill_content

    worktree_content = worktree_reference.read_text(encoding="utf-8")
    assert "git worktree add" in worktree_content
    assert "worktree/<BRANCH_SLUG>" in worktree_content
    assert "git worktree remove" in worktree_content
    assert "replace('/', '-')" in worktree_content

    if skill_name == "local-ci-developer":
        assert "poetry install" in worktree_content
        assert "Auto-Dispatch" in worktree_content
        assert "skip this step" in worktree_content.lower()
        assert "skip this step and the cleanup section" in worktree_content.lower()
        assert "dispatcher-provisioned worktree" in skill_content
    else:
        assert "<INSTALL_COMMAND>" in worktree_content

    for reference_name in ("tdd.md", "pr.md", "review-loop.md"):
        reference = (skill_dir / "references" / reference_name).read_text(
            encoding="utf-8"
        )
        assert (
            "worktree" in reference.lower()
        ), f"{skill_name}/references/{reference_name} must direct worktree use"


def test_all_skills_english_only():
    """All skill instructions and references must contain English prose only (no Japanese or CJK fullwidth characters)."""
    cjk_pattern = re.compile(
        r"[\u3000-\u303F\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uFF01-\uFF60\uFFE0-\uFFE6]"
    )
    for skill_md in sorted(SKILLS_ROOT.glob("**/*.md")):
        text = skill_md.read_text(encoding="utf-8")
        matches = cjk_pattern.findall(text)
        assert not matches, f"{skill_md.relative_to(REPO_ROOT)} contains {len(matches)} Japanese/CJK characters: {''.join(matches[:20])}..."


def test_skills_require_locale_aware_user_responses():
    """Each repository skill must explicitly separate English skill instructions from locale-aware user-facing responses."""
    skill_dirs = [
        d
        for d in sorted(SKILLS_ROOT.iterdir())
        if d.is_dir() and (d / "SKILL.md").is_file()
    ]
    assert len(skill_dirs) >= 5
    for skill_dir in skill_dirs:
        skill_md = skill_dir / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8").lower()
        assert (
            "user-facing" in text
            or "response language" in text
            or "preferred language" in text
        ), f"{skill_md.relative_to(REPO_ROOT)} must contain explicit directive for user-facing response language"


def test_local_ci_developer_preflight_and_backend_selection():
    """local-ci-developer defines execution environment preflight and fixes the GitHub backend."""
    skill_dir = SKILLS_ROOT / "local-ci-developer"
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    tdd_md = (skill_dir / "references" / "tdd.md").read_text(encoding="utf-8")
    pr_md = (skill_dir / "references" / "pr.md").read_text(encoding="utf-8")

    # SKILL.md preflight checks and backend locking
    skill_md_lower = skill_md.lower()
    assert "preflight" in skill_md_lower
    assert "poetry" in skill_md_lower
    assert "lock" in skill_md_lower or "lockfile" in skill_md_lower
    assert "gitleaks" in skill_md_lower
    assert "gh auth status" in skill_md_lower or "auth" in skill_md_lower
    assert "mcp" in skill_md_lower
    assert "backend" in skill_md_lower
    assert "selected backend" in skill_md_lower

    # tdd.md prerequisites
    tdd_md_lower = tdd_md.lower()
    assert "poetry" in tdd_md_lower
    assert "lock" in tdd_md_lower or "install" in tdd_md_lower

    # pr.md fallback and MCP continuation
    pr_md_lower = pr_md.lower()
    assert "mcp" in pr_md_lower


def test_local_ci_developer_mcp_post_write_verification():
    """pr.md defines post-write verification procedures for GitHub MCP operations."""
    skill_dir = SKILLS_ROOT / "local-ci-developer"
    pr_md = (skill_dir / "references" / "pr.md").read_text(encoding="utf-8")
    pr_md_lower = pr_md.lower()

    # MCP post-write verification section or procedures
    assert "post-write" in pr_md_lower or "verification" in pr_md_lower
    # Blob SHA and remote branch content reconciliation
    assert "blob" in pr_md_lower and "sha" in pr_md_lower
    # Cumulative diff inspection before PR creation for multi-commit writes
    assert "cumulative diff" in pr_md_lower or (
        "diff" in pr_md_lower and "commit" in pr_md_lower
    )
    # PR head diff verification
    assert "head diff" in pr_md_lower or ("pr" in pr_md_lower and "diff" in pr_md_lower)
    # Escape / formatting remote discrepancy detection
    assert (
        "escape" in pr_md_lower
        or "discrepancy" in pr_md_lower
        or "mismatch" in pr_md_lower
    )


def test_workflow_template_preflight_and_backend_selection():
    """workflow-template defines execution environment preflight and fixes the GitHub backend."""
    skill_dir = SKILLS_ROOT / "workflow-template"
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    tdd_md = (skill_dir / "references" / "tdd.md").read_text(encoding="utf-8")
    pr_md = (skill_dir / "references" / "pr.md").read_text(encoding="utf-8")

    # SKILL.md preflight checks and backend locking
    skill_md_lower = skill_md.lower()
    assert "preflight" in skill_md_lower
    assert (
        "<preflight_check_command>" in skill_md_lower or "preflight" in skill_md_lower
    )
    assert "gh auth status" in skill_md_lower or "auth" in skill_md_lower
    assert "mcp" in skill_md_lower
    assert "backend" in skill_md_lower
    assert "selected backend" in skill_md_lower

    # tdd.md prerequisites
    tdd_md_lower = tdd_md.lower()
    assert "<install_command>" in tdd_md_lower or "install" in tdd_md_lower
    assert "lock" in tdd_md_lower or "prerequisites" in tdd_md_lower

    # pr.md fallback and MCP continuation
    pr_md_lower = pr_md.lower()
    assert "mcp" in pr_md_lower
    assert "backend" in pr_md_lower and "selected" in pr_md_lower


def test_workflow_template_mcp_post_write_verification():
    """workflow-template pr.md defines post-write verification procedures for GitHub MCP operations."""
    skill_dir = SKILLS_ROOT / "workflow-template"
    pr_md = (skill_dir / "references" / "pr.md").read_text(encoding="utf-8")
    pr_md_lower = pr_md.lower()

    # MCP post-write verification section or procedures
    assert "post-write" in pr_md_lower or "verification" in pr_md_lower
    # Blob SHA and remote branch content reconciliation
    assert "blob" in pr_md_lower and "sha" in pr_md_lower
    # Cumulative diff inspection before PR creation for multi-commit writes
    assert "cumulative diff" in pr_md_lower or (
        "diff" in pr_md_lower and "commit" in pr_md_lower
    )
    # PR head diff verification
    assert "head diff" in pr_md_lower or ("pr" in pr_md_lower and "diff" in pr_md_lower)
    # Escape / formatting remote discrepancy detection
    assert (
        "escape" in pr_md_lower
        or "discrepancy" in pr_md_lower
        or "mismatch" in pr_md_lower
    )


def test_workflow_template_bloat_baseline():
    """workflow-template tdd.md defines bloat warning baseline distinction."""
    skill_dir = SKILLS_ROOT / "workflow-template"
    tdd_md = (skill_dir / "references" / "tdd.md").read_text(encoding="utf-8")
    tdd_md_lower = tdd_md.lower()

    assert "bloat" in tdd_md_lower
    assert "baseline" in tdd_md_lower
    assert (
        "pre-existing" in tdd_md_lower
        or "new" in tdd_md_lower
        or "distinguish" in tdd_md_lower
    )
