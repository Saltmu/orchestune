---
name: "orchestune-dispatch"
description: "Internal follow-up skill invoked by orchestune or orchestune-provision to schedule and dispatch eligible tasks to local/cloud coding agents."
version: "1.0.0"
category: "Development"
input_schema:
  type: "object"
  properties: {}
output_schema:
  type: "object"
  properties: {}
---

# Orchestune Dispatch Skill

本スキルは、GitHub上に起票済みのIssue（または`orchestune-provision`で起票されたIssue群）を受け取り、`orchestune-dispatch` CLIによるディスパッチ設定・ワークツリー管理・エージェントプロセス起動および実行監視を行います。

## トリガー条件

**通常はユーザーが直接呼び出すスキルではありません。** [orchestune スキル](../orchestune/SKILL.md)または[orchestune-provision スキル](../orchestune-provision/SKILL.md)が分解・起票完了後に内部で引き継ぐ形でロードします。

例外的に、Issue起票済みのサブタスクに対してディスパッチだけを再実行・再開したい場合（例: 状態ファイル消失後の手動再開、cron再実行の確認）は、人間が直接このスキルを指定してロードしてよい。

## 前提

* システムに `orchestune` CLIツール（`orchestune-dispatch`, `orchestune-dag`）がインストールされていること。
* GitHub CLI (`gh` command) がインストール・認証済み（`gh auth status`）であること。
* ディスパッチャーの書き込み系操作（ラベル更新・`git worktree`作成・エージェント起動）は、既定で実行されます（`--apply`）。テスト確認したい場合は `--no-apply` を明示指定してください。
* エージェントの起動先（`--dispatch-target`）は、未指定時は実行環境に応じて自動選択されます：ローカル/対話実行時は`auto`（PATH上にインストールされているローカルCLIを`claude`優先・`agy`次点・`codex`次々点で自動検出しsubprocess起動、いずれも未検出なら警告してダミー起動にフォールバック）、GitHub Actions実行時（`GITHUB_ACTIONS=true`）は`cloud-routine`（Claude Code Cloud Routine）です。明示的に`local`を指定した場合のみ、後方互換のダミー起動（`true`のno-op、テスト・dry-run用途）になります。クラウド実行先は、Claude Code Cloud Routine（`cloud-routine`、`ORCHESTUNE_ROUTINE_ID` / `ORCHESTUNE_ROUTINE_TOKEN` が必要）と Codex Cloud（`codex-cloud`、`ORCHESTUNE_CODEX_CLOUD_ENV` または `--codex-cloud-env` が必要）をサポートします。`codex-cloud` はタスクブランチを `origin` にpushしてから `codex cloud exec` を起動し、対象ブランチのopen PRを完了シグナルとして扱います。セットアップは[セットアップガイド](../../docs/ja/setup.md#4-codex-cloud-のセットアップ手順)を参照してください。

## ワークフロー: ディスパッチャーのスケジュール実行

1. ディスパッチャーを実行し、タスクをエージェントに割り振ります。親Issueが存在する場合は、必ず `--parent-issue <N>` を渡してください。これにより、子Issueのブランチが親ブランチ（`parent/issue-{番号}`）から分岐し、完了した子ブランチはIntegratorが人間の確認を待たずに親ブランチへ自動マージ・自動クローズするようになります（`parent/issue-{番号}` → `main` への最終マージのみ、引き続き人間が行います）。このフラグを渡さないと、親ブランチによる二層マージモデルが有効化されず、フラットモード（`main`への直接統合、常に人間によるマージ待ち）で動作してしまいます。

   ```bash
   # ドライラン（影響を出さずにプレビューのみ）
   orchestune-dispatch --no-apply --parent-issue <親Issue番号>

   # 実際に適用して並列ワークスペースを起動
   orchestune-dispatch --parent-issue <親Issue番号>
   ```

2. 状態ファイル `run_state.json` が消失した場合（GitHub Actionsのキャッシュ切れなど）でも、ディスパッチャーは `status:in-progress` になっている GitHub Issue の情報とオープンな PR のヘッドブランチを元に、自動的に実行状態を修復・再構築（自己修復）してディスパッチを継続します。
3. ディスパッチ結果（起動したタスク、worktreeパス、ログ）を[orchestune スキル](../orchestune/SKILL.md)に返し、ユーザーへの最終報告に用いさせます。
