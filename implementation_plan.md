# Implementation Plan: Issue #835 (github-actions: setup-uv & uv cache 移行)

## 0. Preflight & Execution Environment
- Tooling:
  - `uv`: 0.12.10
  - `gitleaks`: 8.30.1
  - GitHub CLI (`gh`): Authenticated (Saltmu, scopes: gist, read:org, repo, workflow)
- GitHub Backend: `gh` CLI (authenticated)
- Target Issue: #835
- Parent Issue: #825
- Base Branch: `parent/issue-825` (`c5b98e7`)
- Task Branch: `feat/issue-835-github-actions`
- Worktree Path: `worktree/feat-issue-835-github-actions`
- Reviewer Bot: `claude` (resolved for agy agent / issue configuration)

## 1. Impact Scope Determination (Step 2.6)

Serena MCP サーバーが利用できない環境のため、`grep` / `git grep` によるテキスト検索にフォールバックして影響範囲を網羅的に列挙しました。

### Symbol & Footprint Classification Table

| Reference / File | Decision | Status | Rationale |
| :--- | :--- | :--- | :--- |
| `.github/workflows/ci.yml` | in scope | done | Poetry の導入（pipx install poetry）、setup-python の poetry キャッシュ設定、poetry install を setup-uv（enable-cache: true）および uv sync --frozen に移行 |
| `.github/workflows/claude-code-review.yml` | out of scope | still out of scope | Poetry や setup-python、依存解決を使用しておらず変更不要（検査・確認済み） |
| `.github/workflows/claude.yml` | out of scope | still out of scope | Poetry や setup-python、依存解決を使用しておらず変更不要（検査・確認済み） |
| `.github/workflows/deploy-pages.yml` | out of scope | still out of scope | Poetry や setup-python、依存解決を使用しておらず変更不要（検査・確認済み） |
| `tests/test_ci_workflow.py:test_ci_workflow_has_explicit_permissions` | in scope | done | ci.yml の検証テスト。permissions の検証を維持しつつ、setup-uv と uv sync --frozen の検証テスト `test_ci_workflow_uses_setup_uv_and_frozen_sync` を追加 |
| `tests/test_skill_commands.py` | out of scope | still out of scope | Issue #836 (skills-docs-and-command-contracts) の担当領域であり本タスクの Footprint 外 |

## 2. Changes Design

### 2.1 `.github/workflows/ci.yml`
- `Install Poetry` ステップ（`pipx install poetry`）を削除。
- `astral-sh/setup-uv@v5` ステップを追加（`enable-cache: true`）。
- `actions/setup-python@v5` の `cache: 'poetry'` を削除。
- `Install dependencies` ステップを `uv sync --frozen` に変更。
- permissions（`contents: read`）および OS マトリクス（ubuntu-latest, windows-latest）は維持。

### 2.2 `tests/test_ci_workflow.py`
- `test_ci_workflow_uses_setup_uv_and_frozen_sync` を追加：
  - `astral-sh/setup-uv` を使用するステップが存在すること
  - `enable-cache: true` が設定されていること
  - `uv sync --frozen` が実行されていること
  - `poetry` 関連のコマンドやキャッシュ指定がステップ内に存在しないこと

## 3. TDD Results
1. **Red**: `tests/test_ci_workflow.py` に `test_ci_workflow_uses_setup_uv_and_frozen_sync` を追加し、失敗することを確認。
2. **Green**: `.github/workflows/ci.yml` を修正し、テストがパスすることを確認。
3. **Verify**: `./scripts/local-ci.sh` を実行し、全3315テストパス、カバレッジ95.08%、Ruff/Mypy/Gitleaks/Bloat検出すべてエラーゼロを確認。

## 4. Acceptance Criteria
- [x] GitHub Actions CI が Poetry の導入・キャッシュに依存しない
- [x] `astral-sh/setup-uv` のキャッシュ有効化（`enable-cache: true`）と `uv.lock` に基づく同期（`uv sync --frozen`）を行う
- [x] `pytest tests/test_ci_workflow.py` が成功する
- [x] `./scripts/local-ci.sh` の全チェックがパスする
