# Implementation Plan: Issue #836 (skills-docs-and-command-contracts: uv 統一)

## 0. Preflight & Execution Environment
- Tooling:
  - `uv`: 0.12.10
  - `gitleaks`: 8.30.1
  - GitHub CLI (`gh`): Authenticated (Saltmu, scopes: gist, read:org, repo, workflow)
- GitHub Backend: `gh` CLI (authenticated)
- Target Issue: #836
- Parent Issue: #825
- Base Branch: `parent/issue-825` (`6782e03`)
- Task Branch: `feat/issue-836-skills-docs-and-command-contracts`
- Worktree Path: `worktree/feat-issue-836-skills-docs-and-command-contracts`
- Reviewer Bot: `claude` (resolved for agy agent / issue configuration)

## 1. Impact Scope Determination (Step 2.6)

Serena MCP サーバーが利用できない環境のため、`grep` / `git grep` によるテキスト検索にフォールバックして影響範囲を網羅的に列挙しました。

### Symbol & Footprint Classification Table

| Reference / File | Decision | Status | Rationale |
| :--- | :--- | :--- | :--- |
| `tests/test_skill_commands.py` | in scope | done | `_POETRY_RUN`, `_known_poetry_commands` を `uv run` および `_known_uv_commands` に更新し、PEP 621/uv 契約を検証 |
| `skills/local-ci-developer/SKILL.md` | in scope | done | Preflight チェックの `poetry --version` / `poetry check --lock` を `uv --version` / `uv lock --check` に更新 |
| `skills/local-ci-developer/references/tdd.md` | in scope | done | `poetry check --lock` / `poetry install` / `poetry run` コマンド群を `uv lock --check` / `uv sync` / `uv run` に更新 |
| `skills/local-ci-developer/references/worktree.md` | in scope | done | `poetry install` を `uv sync` に更新 |
| `skills/local-ci-developer/references/review-loop.md` | in scope | done | `poetry run python scripts/wait_for_review.py` を `uv run python scripts/wait_for_review.py` に更新 |
| `skills/local-ci-developer/references/impact-scope.md` | in scope | still out of scope | Poetry 固有記述が存在しないことを確認済み（修正不要） |
| `skills/local-ci-developer/references/pr.md` | in scope | still out of scope | Poetry 固有記述が存在しないことを確認済み（修正不要） |
| `skills/workflow-template/SKILL.md` | in scope | still out of scope | プレースホルダー形式であり Poetry 固有記述が存在しないことを確認済み（修正不要） |
| `skills/workflow-template/references/worktree.md` | in scope | done | 例示の `poetry install` を `uv sync` に更新 |
| `skills/workflow-template/references/tdd.md` | in scope | done | `poetry run` コマンド例を `uv run` に更新 |
| `skills/workflow-template/references/review-loop.md` | in scope | done | `poetry run python scripts/wait_for_review.py` を `uv run python scripts/wait_for_review.py` に更新 |
| `skills/workflow-template/references/pr.md` | in scope | still out of scope | Poetry 固有記述が存在しないことを確認済み（修正不要） |
| `skills/orchestune/SKILL.md` | in scope | done | `poetry run orchestune-dag` を `uv run orchestune-dag` に更新 |
| `docs/en/setup.md` | in scope | done | セットアップ要件・開発依存インストール手順を Poetry から uv に更新 |
| `docs/en/usage.md` | in scope | done | コマンド例の `poetry run` を `uv run` に更新 |
| `docs/ja/setup.md` | in scope | done | セットアップ要件・開発依存インストール手順を Poetry から uv に更新 |
| `docs/ja/usage.md` | in scope | done | コマンド例の `poetry run` を `uv run` に更新 |
| `README.md` | in scope | done | 前提条件（Poetry → uv）を更新 |
| `CONTRIBUTING.md` | in scope | done | 開発セットアップ・テストコマンド（`poetry install` → `uv sync`、`poetry run` → `uv run` 等）を更新 |
| `CONTRIBUTING.ja.md` | in scope | done | 開発セットアップ・テストコマンドを uv に更新 |
| `docs/en/architecture.md` / `docs/ja/architecture.md` | out of scope | still out of scope | アーキテクチャ解説文書（L1アダプタの説明など）。本Issueの受け入れ条件・Footprintに含まれず、概念説明のため変更不要 |

## 2. Changes Design

### 2.1 `tests/test_skill_commands.py`
- `_POETRY_RUN` 正規表現を `_UV_RUN = re.compile(r"^uv run (.+)$")` に変更。
- `_known_poetry_commands()` を `_known_uv_commands()` にリネーム。
- 関連する各テスト関数内の `poetry run` を `uv run` に更新。
- `test_workflow_skills_document_isolated_worktree_operations` 内の `assert "poetry install" in worktree_content` を `assert "uv sync" in worktree_content` に変更。
- `test_local_ci_developer_preflight_and_backend_selection` 内の `assert "poetry" in ...` を `assert "uv" in ...` に変更。

### 2.2 Skills & References
- `skills/local-ci-developer/` 配下の `poetry` コマンド参照をすべて `uv` に移行。
- `skills/workflow-template/` 配下の `poetry` コマンド参照をすべて `uv` に移行。
- `skills/orchestune/SKILL.md` の `poetry run orchestune-dag` を `uv run orchestune-dag` に更新。

### 2.3 Documentation (Docs & README & CONTRIBUTING)
- `README.md`: `Poetry` → `uv`
- `CONTRIBUTING.md`, `CONTRIBUTING.ja.md`: `poetry install` → `uv sync`、`poetry run pytest` → `uv run pytest`
- `docs/en/setup.md`, `docs/ja/setup.md`: `poetry add` → `uv add` 等
- `docs/en/usage.md`, `docs/ja/usage.md`: `poetry run pytest` → `uv run pytest` 等

## 3. TDD Results
1. **Red**: `tests/test_skill_commands.py` を uv 用に更新し、スキル内の古い poetry 参照によりテストが失敗することを確認。
2. **Green**: スキル群およびドキュメント群を uv に一括更新し、`tests/test_skill_commands.py` が全82件パスすることを確認。
3. **Verify**:
   - `git grep -E 'poetry run|poetry install|poetry-core' -- skills docs README.md CONTRIBUTING.md CONTRIBUTING.ja.md` でヒットゼロを確認。
   - `./scripts/local-ci.sh` を実行して全3315テスト・Bloat・Mypy・Ruff・Gitleaks の合格を確認。

## 4. Acceptance Criteria
- [x] 対象スキルとドキュメントに実行不能な Poetry コマンド例が残らない
- [x] コマンド契約テストが PEP 621 の entry points と uv run を検証する
- [x] `pytest tests/test_skill_commands.py` が成功する
- [x] `./scripts/local-ci.sh` の全チェックがパスする
