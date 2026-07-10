---
name: "orchestune-dispatch"
description: "Internal follow-up skill invoked by orchestune once a decomposition plan is approved: creates GitHub Issues for each subtask and configures/runs the orchestune-dispatch CLI. Not normally invoked directly by the user."
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

本スキルは、**`orchestune`スキルで承認済みの`decomposition_plan.md`**を受け取り、(1) 各サブタスクのGitHub Issue起票と、(2) `orchestune-dispatch` CLIによるディスパッチ設定・実行を行います。

## トリガー条件

**通常はユーザーが直接呼び出すスキルではありません。** [orchestune スキル](../orchestune/SKILL.md)が分解案の承認後に内部で引き継ぐ形でロードします。

例外的に、Issue起票済みのサブタスクに対してディスパッチだけを再実行・再開したい場合（例: 状態ファイル消失後の手動再開、cron再実行の確認）は、人間が直接このスキルを指定してロードしてよい。その場合はステージAをスキップしてステージBから開始する。

## 前提

* システムに `ochestune` CLIツール（`orchestune-dispatch`, `orchestune-dag`）がインストールされていること。
* GitHub CLI (`gh` command) がインストール・認証済み（`gh auth status`）であること。
  * `gh` が利用できない場合は、GitHub MCPサーバーを使うか、ユーザーにWeb UIでの手動起票を案内すること。
* ディスパッチャーの書き込み系操作（ラベル更新・`git worktree`作成・エージェント起動）は、既定で実行されます（`--apply`）。テスト確認したい場合は `--no-apply` を明示指定してください。
* エージェントの起動先は、クラウドルーチンを使う場合 `ORCHESTUNE_ROUTINE_ID` / `ORCHESTUNE_ROUTINE_TOKEN` 環境変数の設定が必要です。

## ステージA: Issue起票

1. 承認済みの`decomposition_plan.md`の各サブタスクについて、GitHub Issueを起票します。
2. Issueのタイトル・本文は以下の形式とします：
   * **タイトル**: `[FEAT] <subtask_id>: <description の要約>`
   * **本文**: ディスパッチャーがパースできるよう、末尾に以下のFootprint YAMLブロックを埋め込みます：

     ```markdown
     ## Footprint
     ```yaml
     subtask_id: <subtask_id>
     footprint:
       - <path/to/file>
     symbols:
       - <class_or_function>
     depends_on:
       - <dep_subtask_id>
     ```
     ```

3. ラベルを付与します：
   * 依存関係(`depends_on`)が未解決なら `status:blocked`、依存がない/全て解決済みなら `status:queued`。
   * 優先度ラベル: `priority:high` / `priority:medium` / `priority:low`。
   * `risk: true` のサブタスクには `risk:flagged` を付与。

4. **Issue起票コマンド例（GitHub CLI使用）**:
   ```bash
   gh issue create --title "[FEAT] task-a: Implement foo feature" --body-file /tmp/issue_body.md --label "status:queued,priority:medium"
   ```

## ステージB: ディスパッチャーのスケジュール実行

1. ディスパッチャーを実行し、タスクをエージェントに割り振ります：

   ```bash
   # ドライラン（影響を出さずにプレビューのみ）
   orchestune-dispatch --no-apply

   # 実際に適用して並列ワークスペースを起動
   orchestune-dispatch
   ```

2. 状態ファイル `run_state.json` が消失した場合（GitHub Actionsのキャッシュ切れなど）でも、ディスパッチャーは `status:in-progress` になっている GitHub Issue の情報とオープンな PR のヘッドブランチを元に、自動的に実行状態を修復・再構築（自己修復）してディスパッチを継続します。
3. 起票したIssue一覧とディスパッチ結果を[orchestune スキル](../orchestune/SKILL.md)に返し、ユーザーへの最終報告に用いさせます。
