"""skills/**/*.md 内のコマンド参照が実在することを検証する。

detect-bloat・baseline-aware CI・quarantine 機構のIssue群と同じ形の腐敗
（文書やコメントが存在しない機構を前提に判断を委ねる状態）の再発を止める
ため、SKILL.md のフェンス付きコードブロックおよびインラインコードスパン
（`` `...` ``）内で `uv run` / `./scripts/` 形式で参照されている
コマンドが、実際に pyproject.toml のスクリプト定義・仮想環境に
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
_UV_RUN = re.compile(r"^uv run (.+)$")
_SCRIPT_PATH = re.compile(r"^(\.[\\/]scripts[\\/]\S+)")
_EXECUTABLE_SUFFIXES = frozenset({".exe", ".cmd", ".bat"})
# Pythonインタプリタのオプションのうち、次のトークンを自身のオペランドとして
# 消費するもの（そのオペランドはスクリプトパスではない）。
_PYTHON_OPTIONS_WITH_OPERAND = frozenset({"-W", "-X", "--check-hash-based-pycs"})
# それ単独でスクリプトパスを取らない（＝以降を検証対象としない）オプション。
_PYTHON_OPTIONS_WITHOUT_SCRIPT_TARGET = frozenset({"-m", "-c"})


def _project_script_names() -> frozenset[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return frozenset(data["project"].get("scripts", {}))


# `orchestune-dispatch --parent-issue ...` や `orchestune provision ...` の
# ように、`uv run` を付けずプロジェクトのエントリポイントを直接呼び出す
# 形式で書かれているSKILL.mdもある。この「素の先頭語」を認識する条件を
# `[project.scripts]` の現行内容そのものにしてしまうと、エントリ
# ポイントが誤字や削除でズレたときに「既知の名前ではない＝コマンド参照とは
# 認識しない」扱いになり、検証がすり抜けてしまう（レビュー指摘: リネーム/
# 削除されたエントリポイントが検出されない）。そのため認識自体は
# `pyproject.toml` の現状に依存しない命名規約（`orchestune` または
# `orchestune-<name>`）で行い、実在確認は `_command_exists` に委ねる。
_PROJECT_SCRIPT_NAMES = _project_script_names()
_BARE_ENTRY_POINT_PATTERN = re.compile(r"^orchestune(-[a-z0-9]+)*$")


def _known_uv_commands() -> set[str]:
    """`uv run <name>` で実行できる名前の集合を返す。

    `[project.scripts]` のエントリポイントに加え、`uv run ruff` /
    `uv run pytest` のように依存パッケージが提供するコマンドも正当な
    参照として扱う必要があるが、依存パッケージ名（例: `pyyaml`,
    `pytest-cov`）がそのまま実行可能コマンド名になるとは限らない
    （実行ファイルを一切インストールしない依存も多い）。そのため、実際に
    このテストを実行している仮想環境の `bin/`（Windowsでは
    `Scripts/`）ディレクトリを走査し、そこに存在する実行可能ファイルの
    名前を正とする。
    """
    names = set(_PROJECT_SCRIPT_NAMES)

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
    """`uv run python <argv...>` のうち、検証すべきスクリプトパスを
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

    `uv run` / `./scripts/` / `.\\scripts\\` のいずれの形式にも
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
    uv_match = _UV_RUN.match(candidate)
    if uv_match:
        tokens = uv_match.group(1).split()
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
    `uv run` / `./scripts/` / `.\\scripts\\` / 既知のプロジェクト
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
    known_commands = _known_uv_commands()
    assert _command_exists(target, known_commands), (
        f"{skill_path} references a command that does not exist: {line!r} "
        f"(resolved target: {target!r})"
    )


def test_missing_uv_script_command_is_detected():
    text = "```bash\nuv run this-script-does-not-exist\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [
        ("uv run this-script-does-not-exist", "this-script-does-not-exist")
    ]
    assert not _command_exists(targets[0][1], _known_uv_commands())


def test_missing_script_file_is_detected():
    text = "```bash\n./scripts/does-not-exist.sh\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [("./scripts/does-not-exist.sh", "./scripts/does-not-exist.sh")]
    assert not _command_exists(targets[0][1], _known_uv_commands())


def test_powershell_script_path_is_recognized():
    text = "```powershell\n.\\scripts\\local-ci.ps1\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [(".\\scripts\\local-ci.ps1", ".\\scripts\\local-ci.ps1")]
    assert _command_exists(targets[0][1], _known_uv_commands())


def test_missing_powershell_script_is_detected():
    text = "```powershell\n.\\scripts\\does-not-exist.ps1\n```\n"

    targets = _iter_command_targets(text)

    assert not _command_exists(targets[0][1], _known_uv_commands())


def test_workflow_template_placeholder_is_excluded():
    text = "```bash\nuv run <CI_ENTRYPOINT>\n```\n"

    assert _iter_command_targets(text) == []


def test_inline_code_span_is_extracted():
    """地の文中のインラインコードスパン（例:
    `` `uv run detect-bloat` ``）も抽出・検証対象に含める。
    フェンス付きコードブロックに書かれていなければ検証を逃れられる、
    という抜け穴を作らないため。"""
    text = "この警告は `uv run detect-bloat` 等で検知します。\n"

    targets = _iter_command_targets(text)

    assert targets == [("uv run detect-bloat", "detect-bloat")]
    assert not _command_exists(targets[0][1], _known_uv_commands())


def test_inline_code_span_inside_fenced_block_is_not_double_counted():
    text = "```bash\nuv run ruff format\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [("uv run ruff format", "ruff")]


def test_python_script_target_resolves_to_script_path():
    text = "```bash\nuv run python scripts/wait_for_review.py --pr 1\n```\n"

    targets = _iter_command_targets(text)

    assert targets[0][1] == "scripts/wait_for_review.py"
    assert _command_exists(targets[0][1], _known_uv_commands())


def test_uv_dependency_command_is_accepted():
    """`ruff`/`pytest`/`mypy` のような、依存パッケージが提供するコマンドは
    `[project.scripts]` になくても正当な参照として扱う。"""
    text = "```bash\nuv run ruff format\n```\n"

    targets = _iter_command_targets(text)

    assert targets[0][1] == "ruff"
    assert _command_exists(targets[0][1], _known_uv_commands())


def test_bare_project_entry_point_is_extracted():
    """`orchestune-dispatch --parent-issue ...` のように `uv run` を
    付けず直接プロジェクトのエントリポイントを呼び出す形式
    （skills/orchestune-dispatch/SKILL.md, skills/orchestune-provision/
    SKILL.md の実際の記法）も抽出・検証対象に含める。"""
    text = "```bash\norchestune-dispatch --parent-issue 42\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [("orchestune-dispatch --parent-issue 42", "orchestune-dispatch")]
    assert _command_exists(targets[0][1], _known_uv_commands())


def test_standalone_bare_mention_without_args_is_not_extracted():
    """`` `orchestune-provision` `` のように、引数を伴わず文中で
    スキル/コンポーネント名として言及しているだけの単語は、コマンド呼び出し
    として誤抽出しない（`orchestune-provision` は実在しない
    `[project.scripts]` エントリだが、これはコマンドではなく
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
    assert not _command_exists(targets[0][1], _known_uv_commands())


def test_standalone_real_command_in_fenced_block_is_accepted():
    text = "```bash\norchestune-dag\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [("orchestune-dag", "orchestune-dag")]
    assert _command_exists(targets[0][1], _known_uv_commands())


def test_bare_unknown_word_is_not_extracted():
    """既知のプロジェクトエントリポイント名と一致しない先頭語（例:
    `orchestune.toml` のような設定ファイル名）を誤ってコマンド参照として
    抽出しない。"""
    text = "```text\norchestune.toml\n```\n"

    assert _iter_command_targets(text) == []


def test_missing_bare_entry_point_is_detected():
    """`orchestune-<name>` 命名規約に合致する素のコマンドは、
    `[project.scripts]` に現在存在するかどうかに関わらず抽出対象と
    なり、実在しない名前であれば `_command_exists` で検出される
    （リネーム/削除されたエントリポイントの検出漏れ防止）。"""
    text = "```bash\norchestune-not-a-real-entrypoint --help\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [
        ("orchestune-not-a-real-entrypoint --help", "orchestune-not-a-real-entrypoint")
    ]
    assert not _command_exists(targets[0][1], _known_uv_commands())


def test_python_dash_c_invocation_is_not_validated_as_file():
    """`uv run python -c "print(1)"` のようなコード直接実行は、
    コード文字列をファイルパスとして誤検証しない。"""
    text = '```bash\nuv run python -c "print(1)"\n```\n'

    assert _iter_command_targets(text) == []


def test_python_option_with_operand_before_script_path_is_skipped():
    """`-W`/`-X` のように自身のオペランドを取るオプションの引数を
    スクリプトパスと誤認しない。"""
    text = "```bash\nuv run python -W ignore scripts/wait_for_review.py\n```\n"

    targets = _iter_command_targets(text)

    assert targets[0][1] == "scripts/wait_for_review.py"
    assert _command_exists(targets[0][1], _known_uv_commands())


def test_shell_prompt_prefix_is_stripped_before_matching():
    """`$ uv run <cmd>` のようなシェルプロンプト表記でも、
    プロンプト記号に阻まれず本体のコマンドを抽出・検証できることを
    確認する。"""
    text = "```bash\n$ uv run this-script-does-not-exist\n```\n"

    targets = _iter_command_targets(text)

    assert targets == [
        ("$ uv run this-script-does-not-exist", "this-script-does-not-exist")
    ]
    assert not _command_exists(targets[0][1], _known_uv_commands())


def test_powershell_prompt_prefix_is_stripped_before_matching():
    text = "```powershell\nPS> .\\scripts\\does-not-exist.ps1\n```\n"

    targets = _iter_command_targets(text)

    assert targets[0][1] == ".\\scripts\\does-not-exist.ps1"
    assert not _command_exists(targets[0][1], _known_uv_commands())


def test_python_module_invocation_is_not_validated_as_file():
    """`uv run python -m pytest` のようなモジュール実行は、`-m` の
    引数がファイルパスではないため検証対象から除外する（誤検知防止）。"""
    text = "```bash\nuv run python -m pytest\n```\n"

    assert _iter_command_targets(text) == []


def test_python_interpreter_option_before_script_path_is_skipped():
    text = "```bash\nuv run python -u scripts/wait_for_review.py --pr 1\n```\n"

    targets = _iter_command_targets(text)

    assert targets[0][1] == "scripts/wait_for_review.py"
    assert _command_exists(targets[0][1], _known_uv_commands())


def test_python_version_flag_alone_yields_no_target():
    text = "```bash\nuv run python --version\n```\n"

    assert _iter_command_targets(text) == []


def test_dependency_without_executable_is_rejected():
    """`pyyaml`/`pytest-cov`/`types-pyyaml` のように、依存パッケージ名では
    あっても実行可能ファイルを一切インストールしない名前は、依存表に
    載っているというだけで正当なコマンドとして誤って通過させてはならない
    （`uv run pyyaml` はCommand not foundになる）。"""
    known_commands = _known_uv_commands()

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
        # Review skip warns about the engine's gate; it never mutates labels.
        if md_file.name == "review-loop.md":
            status_labels = [
                label
                for label in status_labels
                if label != "status:blocked-human-review"
            ]
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
    """Worker skills route all outcomes through complete and the task Issue."""
    skill_dir = SKILLS_ROOT / skill_name
    skill_md = skill_dir / "SKILL.md"
    skill_text = skill_md.read_text(encoding="utf-8")

    assert "orchestune complete --issue <N> --result not-needed" in skill_text
    assert "orchestune complete --issue <N> --pr <PR> --result done" in skill_text
    assert (
        "orchestune complete --issue <N> --result blocked --reason <REASON>"
        in skill_text
    )
    assert "Issue comments" in skill_text
    assert "Post to **PR comments**" not in skill_text
    assert "to PR/Issue comments" not in skill_text
    assert "<!-- orchestune:outcome -->" not in skill_text


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_workflow_skills_document_isolated_worktree_operations(skill_name: str):
    """変更作業はリポジトリ直下の隔離 worktree で完結させる。"""
    skill_dir = SKILLS_ROOT / skill_name
    skill_content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    worktree_reference = skill_dir / "references" / "worktree.md"

    assert worktree_reference.is_file()
    assert "references/worktree.md" in skill_content

    worktree_content = worktree_reference.read_text(encoding="utf-8")

    if skill_name == "local-ci-developer":
        assert "orchestune claim" in worktree_content
        assert "uv sync" in worktree_content
        assert "Auto-Dispatch" not in worktree_content
        assert "dispatcher-provisioned worktree" not in skill_content
    else:
        assert "orchestune claim" in worktree_content
        assert "orchestune claim <issue_number> --resume <claim_id>" in worktree_content
        assert "<worktree_path>" in worktree_content
        assert "<INSTALL_COMMAND>" in worktree_content

    assert "git worktree remove" not in worktree_content
    assert "orchestune complete" in worktree_content

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
        if "resources" in skill_md.parts:
            continue
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
    assert "uv" in skill_md_lower
    assert "lock" in skill_md_lower or "lockfile" in skill_md_lower
    assert "gitleaks" in skill_md_lower
    assert "gh auth status" in skill_md_lower or "auth" in skill_md_lower
    assert "mcp" in skill_md_lower
    assert "backend" in skill_md_lower
    assert "selected backend" in skill_md_lower

    # tdd.md prerequisites
    tdd_md_lower = tdd_md.lower()
    assert "uv" in tdd_md_lower
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


def test_local_ci_developer_bloat_autonomous_refactoring():
    """local-ci-developer tdd.md specifies autonomous bloat refactoring without pausing for approval."""
    skill_dir = SKILLS_ROOT / "local-ci-developer"
    tdd_md = (skill_dir / "references" / "tdd.md").read_text(encoding="utf-8")
    tdd_md_lower = tdd_md.lower()

    assert "autonomously" in tdd_md_lower or "autonomous" in tdd_md_lower
    assert "for approval" not in tdd_md_lower
    assert "pause code modification" not in tdd_md_lower
    assert "escalat" in tdd_md_lower
    assert (
        "re-run" in tdd_md_lower
        or "re-verify" in tdd_md_lower
        or "steps 1" in tdd_md_lower
    )


def test_workflow_template_bloat_autonomous_refactoring():
    """workflow-template tdd.md specifies autonomous bloat refactoring without pausing for approval."""
    skill_dir = SKILLS_ROOT / "workflow-template"
    tdd_md = (skill_dir / "references" / "tdd.md").read_text(encoding="utf-8")
    tdd_md_lower = tdd_md.lower()

    assert "autonomously" in tdd_md_lower or "autonomous" in tdd_md_lower
    assert "for approval" not in tdd_md_lower
    assert "pause code modification" not in tdd_md_lower
    assert "escalat" in tdd_md_lower
    assert (
        "re-run" in tdd_md_lower
        or "re-verify" in tdd_md_lower
        or "steps 1" in tdd_md_lower
    )


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_worker_skills_plan_approval_and_reviewer_selection(skill_name: str):
    skill = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
    plan = next(
        line for line in skill.splitlines() if "**Plan Approval (Step 1)**" in line
    )
    assert "after PR creation" in plan
    assert "bypass user approval" in plan and "existing Issue" in plan
    review = next(
        line for line in skill.splitlines() if "**Review Execution (Step 11)**" in line
    )
    assert "explicit `claude` / `codex` / `skip`" in review
    assert "no inference/default" in review
    assert "review, merge, or completion before selection" in review
    assert "--bot-name skip" in review
    loop = (SKILLS_ROOT / skill_name / "references/review-loop.md").read_text(
        encoding="utf-8"
    )
    assert (
        "Only per-finding procedure Step 5 with Step 6 satisfied permits Step 12"
        in loop
    )
    assert "Exit 11/30" in loop and "forbids done" in loop
    assert "review_target_sha" in loop and "orchestune-review-judgments" in loop
    assert "review_head_mismatch" in loop and "Step 11 for re-review" in loop
    completion = next(
        line for line in skill.splitlines() if "**Outcome Declaration**" in line
    )
    assert "--reviewer" in completion and "--review-reply" in completion


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_worker_skills_require_posting_review_reply_as_pr_comment(skill_name: str):
    """指摘対応の返信は PR コメントへ一本化し、Step 12 の前提にする (#1197)。"""
    loop = (SKILLS_ROOT / skill_name / "references/review-loop.md").read_text(
        encoding="utf-8"
    )
    lines = loop.splitlines()

    assert "review-results" not in loop

    step5 = next(line for line in lines if line.startswith("5. Advance to Step 12"))
    assert "posted as a PR comment" in step5
    assert "at least one finding" in step5

    step2 = next(line for line in lines if line.startswith("2. For every distinct"))
    assert "Zero findings" in step2
    assert "no PR reply" in step2

    assert "gh pr comment <PR_NUMBER> --body-file <session-dir>/review-reply.md" in loop
    assert "GitHub MCP" in loop and "equivalent PR comment" in loop
    assert "--body-file" in loop and "do not post a separate trigger comment" in loop
    assert "--issue <N> --result blocked --reason review-round-limit" in loop
    assert "only when the PR head is unchanged" in loop
    assert "adopted fixes always need another `wait_for_review.py` round" in loop

    skill = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
    review_step = next(
        line for line in skill.splitlines() if "**Automated LLM PR Review**" in line
    )
    outcome_step = next(
        line for line in skill.splitlines() if "**Outcome Declaration**" in line
    )
    assert "review-reply" in review_step and "PR comment" in review_step
    assert "posted" in outcome_step and "PR comment" in outcome_step
    assert "post to PR comments" not in skill
    assert "post the Outcome Record to PR comments" in skill


_CI_ENTRYPOINT_LINE = "`./scripts/local-ci.sh` / `.\\\\scripts\\\\local-ci.ps1`"


def _mcp_posting_section(skill_name: str) -> str:
    loop = (SKILLS_ROOT / skill_name / "references/review-loop.md").read_text(
        encoding="utf-8"
    )
    heading = "### MCP posting (GitHub MCP / App)"
    assert heading in loop, f"{skill_name} review-loop.md lacks the MCP posting section"
    return loop.split(heading, 1)[1].split("\n### ", 1)[0]


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_review_loop_defines_mcp_combined_rereview_posting(skill_name: str):
    """MCP 経路は結合コメントを MCP クライアントが一度だけ投稿する (#1206 方式 A)。"""
    section = _mcp_posting_section(skill_name)

    # 投稿主体・投稿先・単一コメント
    assert "MCP client" in section and "not the offline CLI" in section
    assert "one combined" in section and "`issue_comments`" in section
    for forbidden in ("inline reply", "GitHub review", "two separate comments"):
        assert forbidden in section, forbidden

    # 結合コメントの構成: 前ラウンド r の判断表 + 次ラウンド n の marker
    assert "`@<bot> review`" in section
    assert "judged previous round r" in section and "next round n" in section
    for marker in (
        "<!-- orchestune:review-trigger bot=<bot> -->",
        "<!-- orchestune:review-round <n> -->",
        "<!-- orchestune:review-head <40-hex HEAD SHA> -->",
    ):
        assert marker in section, marker
    assert "inherit" in section and "explicit user instruction" in section
    # 投稿済みで結果未取得の trigger は再開し、n を進めない
    assert "no acquired result yet" in section and "it is outstanding" in section
    assert "skip posting and resume with its round" in section
    # 初回 (n = 1) は前ラウンドが無いため判断表なし、n >= 2 のみ判断表必須
    assert "n = 1 needs no table" in section and "n >= 2 needs" in section
    assert "(omitted when n = 1)" in section

    # 投稿前確認・投稿後の再取得・再送禁止
    assert "same bot and round" in section and "do not repost" in section
    assert "re-fetch" in section and "never resend" in section
    assert "same n" in section

    # オフライン評価の完了条件: 取得は合格ではなく、完了は独立再検証 (#1210)
    assert "--review-state-file" in section
    assert "Exit 0 is acquisition only" in section
    assert "do not advance to done" in section
    assert "not atomic" in section


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_review_loop_documents_snapshot_round_evidence_contract(skill_name: str):
    """MCP/オフライン経路: snapshot v1・投稿前検証・投稿後評価・再開の手順 (#1210)。"""
    section = _mcp_posting_section(skill_name)
    # 1. snapshot v1: 入力版は結果の schema_version と別。HEAD は MCP 取得値のみ
    assert "`snapshot_version` 1 (not the result's `schema_version`)" in section
    assert "`head_before`/`head_after`" in section
    assert "`--max-snapshot-age` (default 300s)" in section
    assert "never substitute the local Git HEAD or a CLI SHA" in section
    assert "legacy" in section and "Exit 30" in section
    # 2. 投稿前検証: receipt は valid のときだけ許可証。既投稿は再投稿しない
    assert "--validate-request" in section and "review-request.json" in section
    assert "`validation_status: valid`" in section and "`already_posted`" in section
    assert "`--max-rounds` 1-5 cannot raise it; Exit 12" in section
    assert "`--switch-reviewer` only on explicit user instruction" in section
    assert "Any other status: do not post" in section
    # 3. 投稿は receipt の本文を一度だけ
    assert "receipt's `trigger_body` once" in section
    # 4. 投稿後評価: 同一ラウンドの再評価は投稿もラウンド増加もしない
    assert "re-running never posts or adds a round" in section
    assert "`reply_validation`" in section
    assert (
        "`trigger_head_verified` only when the trigger head equals the fetched head"
        in section
    )
    assert "past rounds, later edits" in section
    assert "re-verifies fresh evidence itself" in section
    assert "never passes the gate alone" in section


_DOC_COMMAND = re.compile(
    r"uv(?: --cache-dir \S+)? run python scripts/wait_for_review\.py"
    r"(?P<args>[^`\n]*--review-state-file[^`\n]*)"
)


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_documented_offline_commands_are_accepted_by_the_real_parser(skill_name: str):
    """文書の例コマンドは実際の CLI 契約 (review_cli.parse_args) に通る (#1210)。"""
    import shlex

    from scripts.review_cli import parse_args

    loop = (SKILLS_ROOT / skill_name / "references/review-loop.md").read_text(
        encoding="utf-8"
    )
    commands = [m["args"] for m in _DOC_COMMAND.finditer(loop)]
    assert len(commands) >= 2, "loop evaluation and --validate-request examples"
    for args in commands:
        text = (
            args.replace("<PR_NUMBER>", "1")
            .replace("<bot>", "claude")
            .replace("<n>", "2")
            .replace("<session-dir>", "/session")
            .replace("[", "")
            .replace("]", "")
        )
        namespace = parse_args(shlex.split(text), stall_grace_default=600)
        assert namespace.review_state_file == "/session/review-state.json"
    flags = {"--validate-request" in a for a in commands}
    assert flags == {True, False}


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_review_loop_keeps_cli_body_file_contract_separate_from_mcp(skill_name: str):
    loop = (SKILLS_ROOT / skill_name / "references/review-loop.md").read_text(
        encoding="utf-8"
    )
    assert "CLI/gh Round 2+" in loop
    assert "CLI/gh re-review: pass it via `--body-file`" in loop
    assert "do not post a separate trigger comment" in loop
    # 再レビューなしの返信は mention / trigger marker を含めない
    assert "without a mention or trigger markers" in loop
    # 再レビュー有無・指摘ゼロ時の既存ルールは維持
    assert "with zero findings no PR reply is needed" in loop
    assert "Exit 0\nmeans content for the round was fully acquired" in loop


def test_review_loop_copies_match_except_ci_entrypoint():
    paths = [
        SKILLS_ROOT / name / "references/review-loop.md"
        for name in ("local-ci-developer", "workflow-template")
    ]
    local_ci, template = (p.read_text(encoding="utf-8") for p in paths)
    assert local_ci.replace(_CI_ENTRYPOINT_LINE, "`<CI_ENTRYPOINT>`") == template


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_skill_markdown_total_lines_stay_within_bloat_limit(skill_name: str):
    total = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in (SKILLS_ROOT / skill_name).rglob("*.md")
    )
    assert total <= 500, f"{skill_name} skill markdown is {total} lines (limit 500)"


def test_agents_rules_allow_single_mcp_combined_post_only():
    """AGENTS.md の単一 CLI ルールへ、MCP の一度の結合投稿だけを限定例外にする。"""
    rules = (REPO_ROOT / ".agents/AGENTS.md").read_text(encoding="utf-8")
    rule = rules.split("外部CI・PRレビュー待機", 1)[1].split("\n- **", 1)[0]
    assert "uv run python scripts/wait_for_review.py --pr" in rule
    assert "限定例外" in rule and "GitHub MCP" in rule
    assert "結合" in rule and "一度だけ" in rule
    assert "skills/local-ci-developer/references/review-loop.md" in rule
    assert "別の trigger" in rule and "多重待機" in rule
    assert "review-reply.md" in rule and "対象外" in rule
    # 絶対ローカルパスを書かない
    assert "file:///" not in rule


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_issue_footprint_example_selects_file_reservation(skill_name: str):
    """起票例を claim/parser へ渡し、実ファイル単位の予約として解釈できる。"""
    from orchestune.claim.contracts import ReservationKind
    from orchestune.claim.preflight import _resolve_reservation_kind
    from orchestune.issue_parsing import FOOTPRINT_BLOCK_PATTERN, parse_task_from_issue
    from orchestune.models import IssueRecord

    text = (SKILLS_ROOT / skill_name / "references" / "worktree.md").read_text(
        encoding="utf-8"
    )
    match = FOOTPRINT_BLOCK_PATTERN.search(text)
    assert (
        match is not None
    ), f"{skill_name} 起票手順に機械可読な Footprint YAML の例が必要"
    issue = IssueRecord(1044, "Example", match.group(0), (), "2026-09-27")
    task = parse_task_from_issue(issue)
    assert not task.yaml_error
    assert task.footprint
    assert _resolve_reservation_kind(issue) == ReservationKind.FOOTPRINT
    for path in task.footprint:
        assert not Path(path).is_absolute()
        assert ".." not in Path(path).parts
        if skill_name == "local-ci-developer":
            assert (REPO_ROOT / path).is_file()
        else:
            assert path.startswith("<") and path.endswith(">")
            assert not any(
                p in path
                for p in ("skills/", "orchestune/", "tests/test_skill_commands.py")
            )


def test_workflow_template_declares_footprint_before_claim():
    """workflow-template が claim 前の footprint 宣言手順を含んでいることを検証する。"""
    skill_text = (SKILLS_ROOT / "workflow-template" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    worktree_text = (
        SKILLS_ROOT / "workflow-template" / "references" / "worktree.md"
    ).read_text(encoding="utf-8")

    # ## Initial Footprint が ## Development Steps より前にある
    assert "## Initial Footprint" in skill_text
    assert "## Development Steps" in skill_text
    assert skill_text.index("## Initial Footprint") < skill_text.index(
        "## Development Steps"
    )

    # Step 1/2/2.5 の各行が footprint に言及している
    lines_by_step = {}
    for line in skill_text.splitlines():
        for step in ("1", "2", "2.5"):
            if f"| **{step}** |" in line:
                lines_by_step[step] = line
    assert set(lines_by_step.keys()) == {"1", "2", "2.5"}
    for step, line in lines_by_step.items():
        assert (
            "footprint" in line.lower()
        ), f"Step {step} line must mention footprint: {line}"

    # worktree.md に ../SKILL.md#initial-footprint-before-issue-creation-or-claim へのリンクがある
    assert (
        "../SKILL.md#initial-footprint-before-issue-creation-or-claim" in worktree_text
    )

    # 必須の文言が含まれる: re-fetch、Skipping Issue creation does not skip this check、repository reservation、`footprint: []`、does not shrink or expand
    for required_phrase in (
        "re-fetch",
        "Skipping Issue creation does not skip this check",
        "repository reservation",
        "`footprint: []`",
        "does not shrink or expand",
    ):
        assert (
            required_phrase in worktree_text
        ), f"worktree.md must contain '{required_phrase}'"

    # Step 2.6 に言及していない
    assert "Step 2.6" not in skill_text
    assert "Step 2.6" not in worktree_text


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_review_loop_defines_review_reply_marker_contract(skill_name: str):
    """同一bot名義の返信を証跡から除外する返信マーカーの手順 (#1207)。"""
    from orchestune.review.judgment import parse_judgments
    from orchestune.review.markers import is_review_reply, review_reply_marker

    loop = (SKILLS_ROOT / skill_name / "references/review-loop.md").read_text(
        encoding="utf-8"
    )
    marker = review_reply_marker()
    no_rereview = next(
        line for line in loop.splitlines() if line.startswith("No re-review")
    )
    # 先頭の非空行・投稿手順・complete への同一ファイル指定
    assert marker in no_rereview and "first non-blank line" in no_rereview
    assert "gh pr comment <PR_NUMBER> --body-file <session-dir>/review-reply.md" in (
        no_rereview
    )
    assert "complete --review-reply" in no_rereview
    assert "GitHub MCP" in no_rereview and "without a mention or trigger markers" in (
        no_rereview
    )
    # 返信専用コメントへ trigger / 新規レビュー要求を付けない・二重 trigger 禁止の維持
    assert "Never add trigger/round/head markers" in no_rereview
    assert "`@<bot> review` line" in no_rereview
    assert "double-posting ban does not apply" in no_rereview
    # 再レビュー経路とマーカー付きファイル再利用
    assert "`wait_for_review.py --body-file`" in no_rereview
    assert "accepted" in no_rereview and "trigger" in no_rereview
    # ゼロ指摘時の既存例外は維持
    assert "with zero findings no PR reply is needed" in loop

    # 例は正規マーカーを先頭に持ち、判断表がちょうど1つパースできる
    example = re.search(
        r"````markdown\n(<!-- orchestune:review-reply -->\n.*?)````", loop, re.S
    )
    assert example, "reply example with the canonical marker is missing"
    assert is_review_reply(example[1])
    assert "Round 5/5" in example[1]
    assert parse_judgments(example[1])["findings"][0]["judgment"] == "adopt"


def _terminal_posting_section(skill_name: str) -> str:
    loop = (SKILLS_ROOT / skill_name / "references/review-loop.md").read_text(
        encoding="utf-8"
    )
    heading = "### Terminal judgment posting (round limit / blocked)"
    assert (
        heading in loop
    ), f"{skill_name} review-loop.md lacks the terminal posting section"
    return loop.split(heading, 1)[1].split("\n### ", 1)[0]


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_review_loop_defines_terminal_judgment_posting_contract(skill_name: str):
    """ラウンド上限到達時に最終ラウンド判断表を投稿して blocked へ進む終端手順を検証 (#1215)。"""
    section = _terminal_posting_section(skill_name)
    loop = (SKILLS_ROOT / skill_name / "references/review-loop.md").read_text(
        encoding="utf-8"
    )

    # 1. 適用条件・ラウンド維持・順序
    assert "round limit" in section.lower() or "exit 12" in section.lower()
    assert "HEAD was changed by adopted fixes" in section
    assert "Review target round r is preserved" in section
    assert "Round 5/5" in section and "`round: 5`" in section
    assert "differing HEAD forbids done" in section
    assert (
        "orchestune complete --issue <N> --result blocked --reason review-round-limit"
        in section
    )

    # 2. CLI と MCP の同一手順内での規定
    assert (
        "gh pr comment <PR_NUMBER> --body-file <session-dir>/review-reply.md" in section
    )
    assert "GitHub MCP backend" in section and "`issue_comments`" in section

    # 6. 投稿前確認・再利用・ID保存・応答不明時再取得・complete失敗時再投稿禁止
    assert "across all pages" in section
    assert "reuse it and do not repost" in section
    assert "save the returned comment id and URL" in section
    assert "re-fetch and check before any action" in section
    assert "never resend without verification" in section
    assert "If `complete` fails, do not repost" in section

    # 7. Bounded Exit 12, No re-review, MCP posting からの誘導
    heading = "Terminal judgment posting"
    assert heading in loop
    bounded = loop.split("### Bounded review loop", 1)[1].split("### MCP posting", 1)[0]
    assert 'Exit 12: see "Terminal judgment posting"' in bounded
    no_rereview = next(
        line for line in loop.splitlines() if line.startswith("No re-review")
    )
    assert 'see "Terminal judgment posting"' in no_rereview
    mcp_section = _mcp_posting_section(skill_name)
    assert 'Exit 12 beyond: see "Terminal judgment posting"' in mcp_section
    # 即時 escalate の旧記述を残さない
    assert "Exit 12 escalates" not in loop
    assert "Exit 2 or 12: record and escalate" not in loop


@pytest.mark.parametrize("skill_name", ["local-ci-developer", "workflow-template"])
def test_review_loop_terminal_reply_example_contract(skill_name: str):
    """終端返信例の marker / schema / coverage / trigger-exclusion 検証 (#1215)。"""
    from orchestune.review.judgment import parse_judgments, validate_coverage
    from orchestune.review.markers import is_review_reply

    loop = (SKILLS_ROOT / skill_name / "references/review-loop.md").read_text(
        encoding="utf-8"
    )
    example_match = re.search(
        r"````markdown\n(<!-- orchestune:review-reply -->\n.*?)````", loop, re.S
    )
    assert example_match, "reply example with canonical marker is missing"
    example = example_match[1]

    # 先頭 reply marker
    assert is_review_reply(example)
    assert "Round 5/5" in example

    # 再レビュー誘発要素の排除
    assert "@" not in example
    assert "review-trigger" not in example
    assert "review-round" not in example
    assert "review-head" not in example
    assert "review-selection" not in example

    # 判断表パースと Round 5 検証
    judgments = parse_judgments(example)
    assert judgments["round"] == 5
    assert judgments["findings"][0]["judgment"] == "adopt"
    assert judgments["findings"][0]["status"] == "resolved"

    # 人工 review 結果との coverage 照合
    artificial_result = {
        "round": 5,
        "review_items": [],
        "inline_comments": [
            {
                "id": 123,
                "kind": "inline_comment",
                "provenance": "current",
                "body": "Fix regression in interface contract",
            }
        ],
    }
    validate_coverage(judgments, artificial_result)
