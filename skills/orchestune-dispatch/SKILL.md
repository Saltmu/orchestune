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
* エージェントの起動先（`--dispatch-target`）は、未指定時は実行環境に応じて自動選択されます：ローカル/対話実行時は`auto`（PATH上にインストールされているローカルCLIを`claude`優先・`agy`次点・`codex`次々点で自動検出しsubprocess起動、いずれも未検出なら警告してダミー起動にフォールバック）、GitHub Actions実行時（`GITHUB_ACTIONS=true`）は`cloud-routine`（Claude Code Cloud Routine）です。明示的に`local`を指定した場合のみ、後方互換のダミー起動（`true`のno-op、テスト・dry-run用途）になります。現時点でサポートしているクラウド実行先は**Claude Code Cloud Routine（`cloud-routine`）のみ**で、利用する場合は`ORCHESTUNE_ROUTINE_ID` / `ORCHESTUNE_ROUTINE_TOKEN` 環境変数の設定が必要です（GitHub Actions上で自動選択された場合も同様。未設定なら警告してローカルのダミー起動にフォールバックします）。ルーチン自体の作成手順（[claude.ai/code/routines](https://claude.ai/code/routines)でのAPIトリガー設定・トークン発行）は[README.ja.mdのセットアップ手順](../../README.ja.md#claude-code-cloud-routineのセットアップ手順)を参照してください。将来的にはCodex Cloud等、他のクラウドエージェント基盤への対応も予定しています。

## ステージA: Issue起票

1. **事前準備**: `orchestune bootstrap` を実行し、gh認証と必須ラベル（`status:*`, `priority:*`, `risk:flagged`, `progress:partial`, `not-needed-review:*`）の存在を確認・起票します。失敗した場合（exit 1）はここで停止し、案内に従って認証設定等を行ってから再実行してください。
2. **親Issueの起票（冪等）**: `decomposition_plan.md`の`title`を用いて、「大きな石」自体を表す親Issueを用意します。サブタスクIssueより先に、必ずこの手順を実行してください。手順3のサブタスク起票が部分的に失敗して本ステージを再実行した場合でも、親Issueを重複作成しないよう、以下の順で「既存を再利用できないか」を先に確認します。

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
3. **サブタスクIssueの起票（冪等）**: 承認済みの`decomposition_plan.md`の各サブタスクについて、`depends_on`のトポロジカル順（依存元を先に処理する順）で以下を行います。手順2と同様、部分失敗からの再実行でサブタスクIssueを重複作成しないよう、起票前に必ず「既存を再利用できないか」を確認します。

   a. そのサブタスクの`issue_number`が既に設定されている場合は、そのIssue番号をそのまま再利用します。`gh issue view <番号>`で存在確認と、`parent`が手順2で確定した親Issue番号と一致することを確認してください。問題なければ手順b・cをスキップし、次のサブタスクへ進みます。
   b. `issue_number`が未設定の場合、親Issue配下の既存子Issueの本文に埋め込まれたFootprint YAMLの`subtask_id`が、このサブタスクの`id`と一致するものが無いか検索します（Issueタイトルの一致より`subtask_id`の方が構造化されており安定するため、こちらを優先する）。**`gh issue view --json subIssues`は`number`/`title`/`state`程度しか返さず本文（body）を含まないため、この照合には使えません。** 代わりに、`gh api graphql`で`subIssues`の`body`まで含めて直接取得してください：

      ```bash
      gh api graphql -F owner='{owner}' -F name='{repo}' -F number=<親Issue番号> -f query='
      query($owner: String!, $name: String!, $number: Int!) {
        repository(owner: $owner, name: $name) {
          issue(number: $number) {
            subIssues(first: 100) {
              nodes { number body }
            }
          }
        }
      }'
      ```

      （本リポジトリの`orchestune/github.py`の`list_sub_issues()`が同じ理由・同じ手法でこのフィールドを取得している。100件を超えるサブタスクがある大きな石を扱う場合は`pageInfo`によるページネーションが必要になる点も同様。）各`node.body`からFootprint YAMLの`subtask_id`を読み取って照合し、一致するIssueが見つかった場合はそのIssue番号を再利用します。同一親配下・同一`subtask_id`という強い一致のため、手順2bのような人間への確認は不要です。
    c. それでも見つからない場合のみ、新規にIssueを起票します。`.github/issue_template.md` のテンプレートファイルをベースに、サブタスクの情報をプレースホルダーに埋め込んで一時ファイル（例: `/tmp/issue_body.md`）を作成し、本文として使用します。
       * **タイトル**: `[FEAT] <subtask_id>: <description の要約>`
       * **置換ルール**:
         * `{{subtask_id}}`: サブタスクID
         * `{{description}}`: `description` の内容
         * `{{overview}}`: `overview` の内容。未定義の場合は「特になし」とする。
         * `{{proposed_changes}}`: `proposed_changes` の各項目を `- ` による箇条書き形式にしたもの。未定義の場合は「特になし」とする。
         * `{{acceptance_criteria}}`: `acceptance_criteria` の各項目を `- ` による箇条書き形式にしたもの。未定義の場合は「特になし」とする。
         * `{{verification_plan}}`: `verification_plan` の各項目を `- ` による箇条書き形式にしたもの。未定義の場合は「特になし」とする。
         * `{{footprint}}`: YAMLのリスト形式で置換。例: `[path1, path2]`（空の場合は `[]`）
         * `{{symbols}}`: YAMLのリスト形式で置換. 例: `[class1, class2]`（空の場合は `[]`）
         * `{{depends_on}}`: YAMLのリスト形式で置換。例: `[dep_task1, dep_task2]`（空の場合は `[]`）

      ラベルおよびGitHub関係性の付与:
      * **親子関係の紐付け**: 手順2で確定した親Issueの番号（例: `#100`）を設定するため `--parent <親Issue番号>` を必ず付与します。
      * **依存関係の紐付け**: 依存関係（`depends_on`）がある場合、先行タスクの`issue_number`（トポロジカル順で処理しているため、この時点で手順a〜cのいずれかにより確定済み）を `--blocked-by <先行Issue番号>` として付与します。
      * **初期ステータスラベル**: 依存関係が未解決（未完了の先行タスクがある）なら `status:blocked`、依存がない/全て解決済みなら `status:queued`。
      * **優先度・リスク**: 優先度に応じて `priority:high` / `priority:medium` / `priority:low`、また `risk: true` であれば `risk:flagged` を付与。
   d. 上記a〜cで確定したIssue番号を、**必ず**`decomposition_plan.md`の該当サブタスクエントリの`issue_number`フィールドへ書き戻してから、次のサブタスクへ進みます。これを怠ると、後続のサブタスク起票中にエラーが発生し本ステージを再実行した際、このサブタスクのIssueが重複作成されます。

4. **Issue起票コマンド例（GitHub CLI使用、手順3cの新規作成の場合）**:
   * 親Issueが `#100` で、先行依存Issueとして `#101` がある場合の例：
     ```bash
     gh issue create --title "[FEAT] task-b: Implement bar feature" --body-file /tmp/issue_body.md --parent 100 --blocked-by 101 --label "status:blocked,priority:medium"
     ```
   * 親Issueが `#100` で、依存関係がない場合の例：
     ```bash
     gh issue create --title "[FEAT] task-a: Implement foo feature" --body-file /tmp/issue_body.md --parent 100 --label "status:queued,priority:medium"
     ```

## ステージB: ディスパッチャーのスケジュール実行

1. ディスパッチャーを実行し、タスクをエージェントに割り振ります。ステージA手順2で確定した親Issue番号を、必ず `--parent-issue` に渡してください。これにより、子Issueのブランチが親ブランチ（`parent/issue-{番号}`）から分岐し、完了した子ブランチはIntegratorが人間の確認を待たずに親ブランチへ自動マージ・自動クローズするようになります（`parent/issue-{番号}` → `main` への最終マージのみ、引き続き人間が行います）。このフラグを渡さないと、親ブランチによる二層マージモデルが有効化されず、フラットモード（`main`への直接統合、常に人間によるマージ待ち）で動作してしまいます。

   ```bash
   # ドライラン（影響を出さずにプレビューのみ）
   orchestune-dispatch --no-apply --parent-issue <decomposition_plan.mdのparent_issue_number>

   # 実際に適用して並列ワークスペースを起動
   orchestune-dispatch --parent-issue <decomposition_plan.mdのparent_issue_number>
   ```

2. 状態ファイル `run_state.json` が消失した場合（GitHub Actionsのキャッシュ切れなど）でも、ディスパッチャーは `status:in-progress` になっている GitHub Issue の情報とオープンな PR のヘッドブランチを元に、自動的に実行状態を修復・再構築（自己修復）してディスパッチを継続します。
3. 起票したIssue一覧とディスパッチ結果を[orchestune スキル](../orchestune/SKILL.md)に返し、ユーザーへの最終報告に用いさせます。
