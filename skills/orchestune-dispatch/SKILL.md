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
* ステージA開始前に`orchestune bootstrap`を実行し、gh認証状態と必須ラベルの存在を確認しておくこと（詳細はステージAの手順1を参照）。
* ディスパッチャーの書き込み系操作（ラベル更新・`git worktree`作成・エージェント起動）は、既定で実行されます（`--apply`）。テスト確認したい場合は `--no-apply` を明示指定してください。
* エージェントの起動先（`--dispatch-target`）は、未指定時は実行環境に応じて自動選択されます：ローカル/対話実行時は`claude-cli`（ローカルの`claude` CLIをsubprocess起動）、GitHub Actions実行時（`GITHUB_ACTIONS=true`）は`cloud-routine`（Claude Code Cloud Routine）です。明示的に`local`を指定した場合のみ、後方互換のダミー起動（`true`のno-op、テスト・dry-run用途）になります。現時点でサポートしているクラウド実行先は**Claude Code Cloud Routine（`cloud-routine`）のみ**で、利用する場合は`ORCHESTUNE_ROUTINE_ID` / `ORCHESTUNE_ROUTINE_TOKEN` 環境変数の設定が必要です（GitHub Actions上で自動選択された場合も同様。未設定なら警告してローカルのダミー起動にフォールバックします）。ルーチン自体の作成手順（[claude.ai/code/routines](https://claude.ai/code/routines)でのAPIトリガー設定・トークン発行）は[README.ja.mdのセットアップ手順](../../README.ja.md#claude-code-cloud-routineのセットアップ手順)を参照してください。将来的にはCodex Cloud等、他のクラウドエージェント基盤への対応も予定しています。

## ステージA: Issue起票

1. **事前準備**: `orchestune bootstrap` を実行し、gh認証と必須ラベル（`status:*`, `priority:*`, `risk:flagged`, `progress:partial`, `not-needed-review:*`）の存在を確認・起票します。失敗した場合（exit 1）はここで停止し、案内に従って認証設定等を行ってから再実行してください。
2. **親Issueの起票（冪等）**: `decomposition_plan.md`の`title`を用いて、「大きな石」自体を表す親Issueを用意します。サブタスクIssueより先に、必ずこの手順を実行してください。手順4のサブタスク起票が部分的に失敗して本ステージを再実行した場合でも、親Issueを重複作成しないよう、以下の順で「既存を再利用できないか」を先に確認します。

   a. `decomposition_plan.md`の`parent_issue_number`が既に設定されている（`null`でない）場合は、そのIssue番号をそのまま再利用します。念のため`gh issue view <番号>`で存在・オープン状態を確認し、問題なければ手順bをスキップして手順3へ進みます。

   b. `parent_issue_number`が未設定の場合、同一タイトルのopenな親Issueが既に存在しないか検索します（過去の実行が親Issue作成後・`decomposition_plan.md`書き戻し前に中断した可能性があるため）：

      ```bash
      gh issue list --search "in:title \"[EPIC] <decomposition_plan.mdのtitle>\"" --state open
      ```

      該当するIssueが見つかった場合は、それを誤って重複作成しないよう、そのIssue番号を再利用してよいか必ず人間に確認を求めてください。見つからない場合のみ、新規に起票します：

      ```bash
      gh issue create --title "[EPIC] <decomposition_plan.mdのtitle>" --body "decomposition_plan.md記載の設計方針の要約。配下のサブタスクはこのIssueのSub-issueとして紐付けられます。"
      ```

   c. 上記a/bで確定したIssue番号を、**必ず**`decomposition_plan.md`のフロントマターの`parent_issue_number`フィールドへ書き戻してから、次の手順へ進みます。これを怠ると、後続のサブタスク起票中にエラーが発生し本ステージを再実行した際、親Issueが重複作成されSub-issue階層が分裂します。

   確定した親Issue番号は、以降すべてのサブタスクIssue起票の`--parent`として使用します。
3. 承認済みの`decomposition_plan.md`の各サブタスクについて、GitHub Issueを起票します。
4. Issueのタイトル・本文は以下の形式とします：
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

5. ラベルおよびGitHub関係性を付与します：
   * **親子関係の紐付け**: 手順2で起票した親Issueの番号（例: `#100`）を、新しく作成するサブタスクIssueに設定するため `--parent <親Issue番号>` を必ず付与します。
   * **依存関係の紐付け**: 依存関係（`depends_on`）がある場合、先行タスクを先に起票してそのIssue番号（例: `#101`）を確定させ、後続タスク起票時に `--blocked-by <先行Issue番号>` を付与します。
   * **初期ステータスラベル**: 依存関係が未解決（未完了の先行タスクがある）なら `status:blocked`、依存がない/全て解決済みなら `status:queued`。
   * **優先度・リスク**: 優先度に応じて `priority:high` / `priority:medium` / `priority:low`、また `risk: true` であれば `risk:flagged` を付与。

6. **Issue起票コマンド例（GitHub CLI使用）**:
   * 親Issueが `#100` で、先行依存Issueとして `#101` がある場合の例：
     ```bash
     gh issue create --title "[FEAT] task-b: Implement bar feature" --body-file /tmp/issue_body.md --parent 100 --blocked-by 101 --label "status:blocked,priority:medium"
     ```
   * 親Issueが `#100` で、依存関係がない場合の例：
     ```bash
     gh issue create --title "[FEAT] task-a: Implement foo feature" --body-file /tmp/issue_body.md --parent 100 --label "status:queued,priority:medium"
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
