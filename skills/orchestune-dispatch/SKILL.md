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
2. 承認済みの`decomposition_plan.md`の各サブタスクについて、GitHub Issueを起票します。
3. **Issueのテンプレート適用**: `.github/issue_template.md` のテンプレートファイルをベースに、サブタスクの情報をプレースホルダーに埋め込んで一時ファイル（例: `/tmp/issue_body.md`）を作成します。
   * **タイトル**: `[FEAT] <subtask_id>: <description の要約>`
   * **置換ルール**:
     * `{{subtask_id}}`: サブタスクID
     * `{{description}}`: `description` の内容
     * `{{overview}}`: `overview` の内容。未定義の場合は「特になし」とする。
     * `{{acceptance_criteria}}`: `acceptance_criteria` の各項目を `- ` による箇条書き形式にしたもの。未定義の場合は「特になし」とする。
     * `{{footprint}}`: YAMLフォーマットでインデント（スペース4つ）されたリスト形式
       ```yaml
           - <path/to/file>
       ```
     * `{{symbols}}`: YAMLフォーマットでインデント（スペース4つ）されたリスト形式
       ```yaml
           - <class_or_function>
       ```
     * `{{depends_on}}`: YAMLフォーマットでインデント（スペース4つ）されたリスト形式
       ```yaml
           - <dep_subtask_id>
       ```
       ※依存関係が無い場合は、YAMLフォーマット上は `depends_on: []` などの形にします（プレースホルダーのままであれば `depends_on:` のままか、`[]` に置換）。

4. ラベルおよびGitHub関係性を付与します：
   * **親子関係の紐付け**: 親となる「大きな石」のIssue番号（例: `#100`）がある場合、新しく作成するサブタスクIssueに親を設定するため `--parent <親Issue番号>` を付与します。
   * **依存関係の紐付け**: 依存関係（`depends_on`）がある場合、先行タスクを先に起票してそのIssue番号（例: `#101`）を確定させ、後続タスク起票時に `--blocked-by <先行Issue番号>` を付与します。
   * **初期ステータスラベル**: 依存関係が未解決（未完了の先行タスクがある）なら `status:blocked`、依存がない/全て解決済みなら `status:queued`。
   * **優先度・リスク**: 優先度に応じて `priority:high` / `priority:medium` / `priority:low`、また `risk: true` であれば `risk:flagged` を付与。

5. **Issue起票コマンド例（GitHub CLI使用）**:
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
