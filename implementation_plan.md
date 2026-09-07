# Implementation Plan: Issue #846 (Poetryからuv移行後の残存記述を更新する)

## 0. Preflight & Execution Environment
- Tooling:
  - `uv`: 0.12.10
  - `gitleaks`: 8.30.1
  - GitHub CLI (`gh`): Authenticated (Saltmu, scopes: gist, read:org, repo, workflow)
- GitHub Backend: `gh` CLI (authenticated)
- Target Issue: #846
- Parent Issue: #825
- Base Branch: `parent/issue-825` (`253bac4`)
- Task Branch: `docs/issue-846-update-poetry-uv-residual-docs`
- Worktree Path: `worktree/docs-issue-846-update-poetry-uv-residual-docs`
- Reviewer Bot: `codex` (specified by user)

## 1. Impact Scope Determination (Step 2.6)

Serena MCP サーバーが利用できない環境のため、`grep` / `git grep` によるテキスト検索にフォールバックして影響範囲を網羅的に列挙しました。

### Symbol & Footprint Classification Table

| Reference / File | Decision | Status | Rationale |
| :--- | :--- | :--- | :--- |
| `.github/ISSUE_TEMPLATE/bug_report.md` | in scope | done | バグ報告テンプレートの環境情報で `Poetry version` を要求している箇所を `uv version` に更新 |
| `docs/en/architecture.md` | in scope | done | L1 `infra.python_env` の説明文を Poetry から uv 依存同期およびリポジトリローカル `.venv` の仮想環境解決に更新 |
| `docs/ja/architecture.md` | in scope | done | 同上（日本語版） |
| `tests/test_integrator_step_merge.py` | in scope | done | モジュール docstring 内の「Poetry環境検出」を「uv依存同期・仮想環境解決」に更新 |
| `tests/test_residual_poetry.py` | in scope | done | ドキュメントおよびIssueテンプレートに残存するPoetry記述を検出し、互換性維持のための意図的な参照のみを許可する回帰検査テストを追加 |
| `docs/en/usage.md` | out of scope | still out of scope | ターゲットリポジトリの `poetry.lock` 互換性（dependency-manifest, dag_ignore_patterns）に関する説明であり、Orchestune 本体の環境説明ではないため維持 |
| `docs/ja/usage.md` | out of scope | still out of scope | 同上（日本語版） |
| `docs/refactoring-plan.md` | out of scope | still out of scope | 歴史的なリファクタリング計画の記録であり、Issue #846 の概要で明記されている通り対象外 |
| `orchestune/dag/contracts.py` | out of scope | still out of scope | ターゲットリポジトリの `poetry.lock` 検出コード（互換性維持のため必須） |
| `orchestune/dag/models.py` | out of scope | still out of scope | ターゲットリポジトリの `poetry.lock` ignore パターン（互換性維持のため必須） |
| `orchestune/dispatch/locks.py` | out of scope | still out of scope | ターゲットリポジトリの `poetry.lock` 競合検出コード（互換性維持のため必須） |
| `tests/test_dag_contracts.py` | out of scope | still out of scope | ターゲットリポジトリの `poetry.lock` 分類テスト（互換性維持のため必須） |
| `tests/test_dispatch_locks.py` | out of scope | still out of scope | ターゲットリポジトリの `poetry.lock` 競合検出テスト（互換性維持のため必須） |
| `tests/test_ci_workflow.py` | out of scope | still out of scope | ci.yml に poetry が含まれないことの検証テスト（すでに uv 移行済みであることを担保するテスト） |

## 2. Changes Design

### 2.1 `.github/ISSUE_TEMPLATE/bug_report.md`
- `- Poetry version: <!-- 例: 1.8.2 -->` を `- uv version: <!-- 例: 0.5.0 -->` に更新。

### 2.2 `docs/en/architecture.md` & `docs/ja/architecture.md`
- `docs/en/architecture.md`: L1 `infra.python_env` の記述において、Poetry による依存関係と仮想環境の操作と記載されていた部分を、uv による依存同期およびリポジトリローカルな `.venv` の仮想環境操作の説明に更新。
- `docs/ja/architecture.md`: 同様に「Poetryによる依存関係と仮想環境の操作は、L1アダプタの `infra.python_env` にカプセル化しています。」を「uvによる依存関係の同期やリポジトリローカルな `.venv` の仮想環境操作は、L1アダプタの `infra.python_env` にカプセル化しています。」に更新。

### 2.3 `tests/test_integrator_step_merge.py`
- モジュール docstring 内の「CI実行そのものを担う`IntegrationMerger`のPoetry環境検出も併せて検証する。」を「CI実行そのものを担う`IntegrationMerger`のuv依存同期・仮想環境解決も併せて検証する。」に更新。

### 2.4 Regression Test (`tests/test_residual_poetry.py`)
- 回帰テストを追加し、`.github/ISSUE_TEMPLATE`、`docs/`、`tests/test_integrator_step_merge.py` などの対象パスにおいて、意図的に残す allowlist（`usage.md` のロックファイル互換性記述、`refactoring-plan.md` の歴史的記録など）以外の不要な Poetry 参照が存在しないことを機械的に検査。

## 3. TDD Results
1. **Red**: 回帰テスト `tests/test_residual_poetry.py` を追加し、4件すべて失敗（Red）することを確認。
2. **Green**: 対象ファイルを更新し、`tests/test_residual_poetry.py` の全4件がパス（Green）することを確認。
3. **Verify**:
   - `git grep -n -i 'poetry' -- .github/ISSUE_TEMPLATE docs tests/test_integrator_step_merge.py` で不要な記述が消え、意図的な allowlist のみ残存していることを確認。
   - `tests/test_integrator_step_merge.py`, `tests/test_skill_commands.py`, `tests/test_architecture.py` も全件パスすることを確認。

## 4. Acceptance Criteria
- [x] バグ報告テンプレートがuvのバージョン情報を要求する
- [x] 日英architecture文書が orchestune.infra.python_env の現行uv実装と一致する
- [x] テストdocstringに存在しないPoetry環境検出の説明が残らない
- [x] Poetry互換性のため意図的に残すコード参照は削除しない
- [x] 対象スキル・README・CONTRIBUTING・setup/usage文書の既存uvコマンドを維持する
- [x] LinuxローカルCIが全件成功する
