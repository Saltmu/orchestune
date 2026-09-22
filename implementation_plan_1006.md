# #1006 実装計画

## 事前確認

- `uv 0.12.13`、`uv lock --check`、`gitleaks 8.30.1` は正常。
- GitHub 操作は認証済み `gh` CLI を使用する。
- 既存 Issue のため計画承認は省略し、レビュー担当は Claude とする。
- `orchestune claim 1006` により専用作業ツリーを作成済み。
- Serena MCP はこのセッションの利用可能ツールにないため、`rg` による参照列挙へフォールバックした。動的アクセス、文字列指定 mock、設定、文書もテキスト検索する。

## 設計

- provision 専用の限定再試行を導入する。429、502/503/504、接続の一時失敗だけを対象にし、`Retry-After` を優先して指数バックオフと総待機時間上限を適用する。待機関数はテストから差し替え可能にする。
- Issue 作成の応答消失時は再作成前に親と `subtask_id` を再検索し、既存番号を計画に保存する。親 Issue の場合もタイトルと marker で再検索する。
- 429 で作成が拒否された場合は再 POST する。5xx/接続断で作成結果が不明な場合は検索を上限付きで繰り返し、見つからなければ再 POST せず診断とともに停止する。GitHub の検索反映遅延下で重複を防ぐため。
- 関係設定と本文同期は、再試行前に現状を取得して達成済みなら成功とみなす。途中停止では作成済み・再利用済み・未完了数と再実行手順を表示する。
- 共通 `GitHubForge._run` の全操作に無条件の再試行は入れない。

## 影響範囲

| 参照 | 判定 | 根拠 |
| :--- | :--- | :--- |
| `provisioning/flow.py:_apply_provisioning`, `_provision_subtasks_loop` | 修正対象 | provision の全操作と進捗診断の入口 |
| `provisioning/subtasks.py:_provision_subtask`, `_link_subtask_relationships` | 修正対象 | Issue 作成と関係設定の冪等化が必要 |
| `provisioning/parent.py:_resolve_derived_parent_issue` | 修正対象 | 親 Issue 作成後の応答消失を照合する |
| `provisioning/plan.py:sync_parent_decomposition_plan` | 修正対象 | 親本文同期の一時失敗を扱う |
| `provisioning/cli.py:provision_issues` | 修正対象 | 非ゼロ終了時の進捗を表示する |
| `forge/__init__.py:GitHubForge._run`、`forge/issues.py` | 対象外 | 共通 Forge の全利用者に再試行を広げない。既存例外から分類する |
| `tests/test_provisioning_flow.py`, `test_provisioning_parent.py`, `test_plan_sync_guard.py` | 修正対象 | 既存の直接呼出しと失敗注入を検証する |
| `tests/test_forge.py`, `test_forge_issues.py` | 対象外 | 共通 Forge の契約を変えない |
| `tests/test_architecture.py`, `docs/en/architecture.md`, `docs/ja/architecture.md` | 修正対象（列挙漏れ） | 新モジュール追加時に L2 一覧の同期が必須。CI の失敗で発見 |
| `tests/test_execution_profile_e2e.py`, `test_plan_persistence.py`, `test_provisioning_core.py`, `test_provisioning_repo_root.py`, `test_provisioning_degraded.py` | 対象外 | 公開 API のシグネチャを維持し、全テストで回帰確認する |
| 文書・設定・文字列 mock 検索 | 対象外 | リトライ設定は公開設定キーを追加せず provision 内に閉じる |

実装後に各行を再照合して PR 本文へ転記する。

## 実装後の照合

| 参照 | 状態 | 照合結果 |
| :--- | :--- | :--- |
| `provisioning/flow.py`、`provisioning/cli.py` | done | provision 専用ラッパーと中断時診断を追加 |
| `provisioning/retry.py` | 列挙漏れ | 新設モジュール。GitHub 操作の限定再試行と書き込み後照合を集約 |
| `provisioning/subtasks.py`、`provisioning/parent.py`、`provisioning/plan.py` | 変更不要 | 各関数は `IssueForge` を受け取るため、flow から渡すラッパーで対応できた。直接編集が必要という当初判断を修正 |
| `forge/__init__.py`、`forge/issues.py`、Forge 関連テスト | still out of scope | 共通 Forge の契約と呼出しは変更していない |
| provision 関連テスト | done | 失敗注入と再実行を追加。既存テストも再実行 |
| `tests/test_architecture.py`、英日アーキテクチャ文書 | 列挙漏れ・done | 新モジュールを L2 一覧に登録。初回 CI 失敗で検出 |
| その他の利用元・設定・文書 | still out of scope | 公開 API と設定キーは変更していない |
