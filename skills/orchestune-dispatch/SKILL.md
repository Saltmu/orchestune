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
* GitHub CLI (`gh` command) がインストール・認証済み（`gh auth status`）であること。`gh`が利用できない環境での代替手順は、ステージAの「`gh`が利用できない環境でのフォールバック」を参照。
* ステージA開始前に`orchestune bootstrap`を実行し、gh認証状態と必須ラベルの存在を確認しておくこと（詳細はステージAの手順1を参照）。
* ディスパッチャーの書き込み系操作（ラベル更新・`git worktree`作成・エージェント起動）は、既定で実行されます（`--apply`）。テスト確認したい場合は `--no-apply` を明示指定してください。
* エージェントの起動先（`--dispatch-target`）は、未指定時は実行環境に応じて自動選択されます：ローカル/対話実行時は`auto`（PATH上にインストールされているローカルCLIを`claude`優先・`agy`次点・`codex`次々点で自動検出しsubprocess起動、いずれも未検出なら警告してダミー起動にフォールバック）、GitHub Actions実行時（`GITHUB_ACTIONS=true`）は`cloud-routine`（Claude Code Cloud Routine）です。明示的に`local`を指定した場合のみ、後方互換のダミー起動（`true`のno-op、テスト・dry-run用途）になります。クラウド実行先は、Claude Code Cloud Routine（`cloud-routine`、`ORCHESTUNE_ROUTINE_ID` / `ORCHESTUNE_ROUTINE_TOKEN` が必要）と Codex Cloud（`codex-cloud`、`ORCHESTUNE_CODEX_CLOUD_ENV` または `--codex-cloud-env` が必要）をサポートします。`codex-cloud` はタスクブランチを `origin` にpushしてから `codex cloud exec` を起動し、対象ブランチのopen PRを完了シグナルとして扱います。セットアップは[セットアップガイド](../../docs/ja/setup.md#4-codex-cloud-のセットアップ手順)を参照してください。

## ステージA: Issue起票

`decomposition_plan.md`からのIssue起票は`orchestune provision`コマンドに完全にコード化されている（#306）。承認済みplanがあれば起票は決定的な変換であり、エージェントが手順を解釈する必要はない。

1. **事前準備**: `orchestune bootstrap` を実行し、gh認証と必須ラベル（`status:*`, `priority:*`, `risk:flagged`, `progress:partial`, `not-needed-review:*`）の存在を確認・起票します。失敗した場合（exit 1）はここで停止し、案内に従って認証設定等を行ってから再実行してください。
2. **プレビュー**: 書き込み前に内容を確認します。
   ```bash
   orchestune provision --plan decomposition_plan.md --no-apply
   ```
   生成される各Issueのタイトル・ラベル・本文が出力される。GitHubへの書き込みは行われない。
3. **起票**: 問題なければ実際に適用します。
   ```bash
   orchestune provision --plan decomposition_plan.md
   ```
   `title`から親Issue（`[EPIC] <title>`）を起票し、`decomposition_plan.md`の`depends_on`のトポロジカル順で各サブタスクIssueを起票して`--parent`/`--blocked-by`相当の関係を`gh issue edit --set-parent`/`--add-blocked-by`で設定する。起票済みのIssue番号は起票の都度`decomposition_plan.md`のフロントマター（`parent_issue_number`、各サブタスクの`issue_number`）へ書き戻される。**冪等かつ部分失敗から再開可能**: 既に`issue_number`が設定済みのサブタスク、または親Issue配下の既存子Issueの本文に埋め込まれたFootprint YAMLの`subtask_id`が一致するサブタスクは、再作成されず既存のIssue番号がそのまま再利用される。詳細な導出規則（ラベル付与規則、`.github/issue_template.md`のプレースホルダー置換規則等）は`orchestune/provisioning.py`のdocstringおよび`docs/ja/usage.md`を参照。
4. **起票したIssue一覧とディスパッチ結果を[orchestune スキル](../orchestune/SKILL.md)に返し、ユーザーへの最終報告に用いさせます。**

### `gh` が利用できない環境でのフォールバック（手動起票）

`orchestune provision`は内部で`gh` CLIを呼び出す。`gh`自体がインストール・認証できない環境では、GitHub MCPサーバーを使うか、ユーザーにWeb UIでの手動起票を案内すること。この場合、以下の対応関係を守って手作業で代替する：

* 親Issue: `decomposition_plan.md`の`title`から`[EPIC] <title>`のタイトルで起票し、確定した番号を`parent_issue_number`へ書き戻す。
* 各サブタスクIssue: `.github/issue_template.md`のプレースホルダー（`{{subtask_id}}`, `{{description}}`, `{{overview}}`, `{{proposed_changes}}`, `{{acceptance_criteria}}`, `{{verification_plan}}`, `{{footprint}}`, `{{symbols}}`, `{{depends_on}}`）へサブタスクの情報を埋め込み、確定した番号を各サブタスクの`issue_number`へ書き戻す。
* GitHub MCPによるIssue起票ではnativeの`blocked_by`/`parent`関係を設定できない場合がある。この場合もFootprint YAMLの`depends_on`を必ず保持すること。ディスパッチャーはこの値を依存判定と自己修復時のブランチスタッキング復元に使用する。GitHub上の関係を可視化したい場合は、起票後にWeb UIまたは`gh issue edit --set-parent`/`--add-blocked-by`でnativeの関係を追加する。

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
