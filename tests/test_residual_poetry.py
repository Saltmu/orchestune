"""Poetryからuvへの移行後における不要な残存記述の回帰防止テスト。

Issue #846:
現行環境をPoetry前提として説明する記述の残存を防ぎ、
意図的に残す互換性記述（ターゲットリポジトリの poetry.lock 判定など）や
歴史的記録のみを allowlist として許容する。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs"
ISSUE_TEMPLATES_ROOT = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"


def test_bug_report_template_requests_uv_version() -> None:
    """バグ報告テンプレートがPoetryではなくuvのバージョン情報を要求すること。"""
    bug_report_path = ISSUE_TEMPLATES_ROOT / "bug_report.md"
    assert bug_report_path.is_file()
    content = bug_report_path.read_text(encoding="utf-8")

    assert "uv version" in content.lower()
    assert "poetry version" not in content.lower()


def test_architecture_docs_match_python_env_uv_implementation() -> None:
    """日英architecture文書がorchestune.infra.python_envの現行uv実装と一致すること。"""
    en_doc = (DOCS_ROOT / "en" / "architecture.md").read_text(encoding="utf-8")
    ja_doc = (DOCS_ROOT / "ja" / "architecture.md").read_text(encoding="utf-8")

    # infra.python_env の説明で uv が言及されていること
    assert "uv" in en_doc
    assert "uv" in ja_doc

    # infra.python_env の説明で Poetry が現行環境として説明されていないこと
    assert "poetry dependency" not in en_doc.lower()
    assert "poetryによる依存関係" not in ja_doc.lower()


def test_test_integrator_step_merge_docstring_matches_uv() -> None:
    """tests/test_integrator_step_merge.py のdocstringがuv同期・環境解決の実態と一致すること。"""
    test_file = REPO_ROOT / "tests" / "test_integrator_step_merge.py"
    content = test_file.read_text(encoding="utf-8")
    # 先頭20行（モジュールdocstring）を対象
    header = "\n".join(content.splitlines()[:20])

    assert "poetry" not in header.lower()
    assert "uv" in header.lower()


def check_text_for_unexpected_poetry(text: str, rel_path: str) -> list[tuple[int, str]]:
    """指定されたテキスト内から未許可のPoetry参照を検出する。

    行全体を免除するのではなく、許可されたトークン（例: `poetry.lock`）のみを除去した
    残りの文字列に `poetry` が含まれていないかを判定する。
    """
    exempt_files = {"docs/refactoring-plan.md"}
    if rel_path in exempt_files:
        return []

    allowed_context_removals: dict[str, list[re.Pattern[str]]] = {
        "docs/en/usage.md": [
            re.compile(
                r"`package\.json` / `poetry\.lock` / `uv\.lock` / `package-lock\.json`"
            ),
            re.compile(
                r"\(`pyproject\.toml`, `poetry\.lock`, `uv\.lock`, `logging\.py`"
            ),
        ],
        "docs/ja/usage.md": [
            re.compile(
                r"`package\.json` / `poetry\.lock` / `uv\.lock` / `package-lock\.json`"
            ),
            re.compile(
                r"（`pyproject\.toml`、`poetry\.lock`、`uv\.lock`、`logging\.py`"
            ),
        ],
    }

    violations: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        cleaned_line = line
        for pattern in allowed_context_removals.get(rel_path, []):
            cleaned_line = pattern.sub("", cleaned_line)

        if "poetry" in cleaned_line.lower():
            violations.append((lineno, line.strip()))
    return violations


def test_no_unexpected_poetry_references_in_docs_and_templates() -> None:
    """ドキュメントおよびIssueテンプレートに未許可のPoetry参照が存在しないこと。

    意図的に残す互換性参照:
    - docs/en/usage.md, docs/ja/usage.md: ターゲットリポジトリの `poetry.lock` サポート
    - docs/refactoring-plan.md: 過去のリファクタリング計画・履歴記録
    """
    scan_targets: list[Path] = []
    if ISSUE_TEMPLATES_ROOT.is_dir():
        scan_targets.extend(ISSUE_TEMPLATES_ROOT.glob("*.md"))
    if DOCS_ROOT.is_dir():
        scan_targets.extend(DOCS_ROOT.rglob("*.md"))

    violations: list[str] = []
    for path in sorted(scan_targets):
        rel_path = path.relative_to(REPO_ROOT).as_posix()
        content = path.read_text(encoding="utf-8")
        file_violations = check_text_for_unexpected_poetry(content, rel_path)
        for lineno, line in file_violations:
            violations.append(f"{rel_path}:{lineno}: {line}")

    assert violations == [], (
        "Unexpected residual poetry references found:\n" + "\n".join(violations)
    )


def test_allowlist_catches_residual_command_on_same_line_as_permitted_token() -> None:
    """同一行に許可された `poetry.lock` が含まれていても、不要な poetry 記述があれば検知すること。"""
    mixed_line = "Run `poetry install` with `package.json` / `poetry.lock` / `uv.lock` / `package-lock.json`"
    violations = check_text_for_unexpected_poetry(mixed_line, "docs/en/usage.md")
    assert len(violations) == 1
    assert violations[0][1] == mixed_line

    pure_line = (
        "Supported: `package.json` / `poetry.lock` / `uv.lock` / `package-lock.json`"
    )
    assert check_text_for_unexpected_poetry(pure_line, "docs/en/usage.md") == []

    # 許可された完全な文脈と一致しない任意の変形・サフィックス・URL・フラグメントは厳密に拒否されること
    for invalid in (
        "Supports `Poetry.LOCK` file.",
        "Backup file `poetry.lock.bak` is ignored.",
        "Backup file `poetry.lock~` is ignored.",
        "Reference `poetry.lock:backup` is invalid.",
        "URL `poetry.lock?raw=1` is invalid.",
        "URL `poetry.lock`#fragment is invalid.",
        "URL `poetry.lock`%2Fsubfile is invalid.",
        "Path `poetry.lock/subfile` is unsupported.",
        "Path `poetry.lock`/subfile is unsupported.",
        "File `poetry.lock`.bak is unsupported.",
        "File `poetry.lock`~ is unsupported.",
        "URL `poetry.lock`?raw=1 is unsupported.",
        "Unquoted poetry.lock is not an exact code token.",
        "Standalone `poetry.lock` without expected manifest list context is invalid.",
    ):
        assert (
            len(check_text_for_unexpected_poetry(invalid, "docs/en/usage.md")) == 1
        ), invalid
