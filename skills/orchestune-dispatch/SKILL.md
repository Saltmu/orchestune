---
name: "orchestune-dispatch"
description: "「大きな石」（複数タスクからなる大規模な作業）の分解案生成(orchestune-dag)とディスパッチャー(orchestune-dispatch)のスケジュール登録を管理するオーケストレーションスキル。"
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

本スキルは、開発タスクを複数サブタスクに分解してDAG（有向非巡回グラフ）を構築し、並列開発を行うためのディスパッチャー機能を提供します。
自己修復機能（キャッシュ消失時の状態自動復元ロジック）を搭載しており、GitHub Actions等のステートレスな環境でも安全に動作します。

## トリガー条件

**人間が複数タスクからなる「大きな石」を提示し、並列実装したいと述べた場合**にロードする。

## 前提

* システムに `ochestune` CLIツール（`orchestune-dispatch`, `orchestune-dag`）がインストールされていること。
* ディスパッチャーの書き込み系操作（ラベル更新・`git worktree`作成・エージェント起動）は、既定で実行されます（`--apply`）。テスト確認したい場合は `--no-apply` を明示指定してください。
* エージェントの起動先は、クラウドルーチンを使う場合 `ORCHESTUNE_ROUTINE_ID` / `ORCHESTUNE_ROUTINE_TOKEN` 環境変数の設定が必要です。

## ステージ1: 分解案生成とDAG構築

1. 人間から受けた「大きな石」の説明を、サブタスク単位に分解します。各サブタスクについて `id` / `description` / `footprint`（想定変更ファイル一覧）/ `symbols`（想定シンボル一覧）/ `depends_on`（明示的な依存先ID一覧）を洗い出します。
2. `decomposition_plan.md` を作成します。YAMLフロントマター形式で以下のように記述します：

   ```markdown
   ---
   subtasks:
     - id: task-a
       description: "Aを実装する"
       footprint:
         - src/foo.py
       symbols:
         - foo.Foo
       depends_on: []
   ---

   # Decomposition Plan
   （本文は自由記述、パース対象外）
   ```

3. DAGを構築し、整合性を検証します：

   ```bash
   orchestune-dag --plan decomposition_plan.md
   ```

4. 出力されたトポロジカル順・並列実行可能leaf・競合リスクなどを人間に提示し、**この分解案の承認**を得ます。
5. 承認後、GitHub Issueを起票します。Issue本文には、ディスパッチャーが読み取れるように以下の形式でfootprint情報を埋め込みます：

   ```markdown
   ## Footprint
   ```yaml
   subtask_id: task-a
   footprint:
     - src/foo.py
   symbols:
     - foo.Foo
   depends_on: []
   ```
   ```

   ラベルには `status:queued`（依存関係が未解決なら `status:blocked`）を付与します。

## ステージ2: ディスパッチャーのスケジュール実行

1. ディスパッチャーを定期実行（Cron）または手動実行し、タスクをエージェントに割り振ります：

   ```bash
   # ドライラン（影響を出さずにプレビューのみ）
   orchestune-dispatch --no-apply

   # 実際に適用して並列ワークスペースを起動
   orchestune-dispatch
   ```

2. 状態ファイル `run_state.json` が消失した場合（GitHub Actionsのキャッシュ切れなど）でも、ディスパッチャーは `status:in-progress` になっている GitHub Issue の情報とオープンな PR のヘッドブランチを元に、自動的に実行状態を修復・再構築（自己修復）してディスパッチを継続します。
