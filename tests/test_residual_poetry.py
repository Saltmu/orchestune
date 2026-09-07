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


def test_no_unexpected_poetry_references_in_docs_and_templates() -> None:
    """ドキュメントおよびIssueテンプレートに未許可のPoetry参照が存在しないこと。

    意図的に残す互換性参照:
    - docs/en/usage.md, docs/ja/usage.md: ターゲットリポジトリの `poetry.lock` サポート
    - docs/refactoring-plan.md: 過去のリファクタリング計画・履歴記録
    """
    allowed_files_patterns: dict[str, list[re.Pattern[str]]] = {
        "docs/en/usage.md": [
            re.compile(r"poetry\.lock"),
        ],
        "docs/ja/usage.md": [
            re.compile(r"poetry\.lock"),
        ],
        "docs/refactoring-plan.md": [
            re.compile(r".*"),  # 歴史的記録のため全行許容
        ],
    }

    scan_targets: list[Path] = []
    if ISSUE_TEMPLATES_ROOT.is_dir():
        scan_targets.extend(ISSUE_TEMPLATES_ROOT.glob("*.md"))
    if DOCS_ROOT.is_dir():
        scan_targets.extend(DOCS_ROOT.rglob("*.md"))

    violations: list[str] = []
    for path in sorted(scan_targets):
        rel_path = path.relative_to(REPO_ROOT).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            if "poetry" not in line.lower():
                continue

            patterns = allowed_files_patterns.get(rel_path)
            if patterns is not None and any(p.search(line) for p in patterns):
                continue

            violations.append(f"{rel_path}:{lineno}: {line.strip()}")

    assert violations == [], (
        "Unexpected residual poetry references found:\n" + "\n".join(violations)
    )
