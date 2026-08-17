---
name: "orchestune-provision"
description: "Internal follow-up skill invoked by orchestune once a decomposition plan is approved: creates GitHub Issues for each subtask using the orchestune provision CLI (or manual fallback)."
version: "1.0.0"
category: "Development"
input_schema:
  type: "object"
  properties: {}
output_schema:
  type: "object"
  properties: {}
---

# Orchestune Provision Skill

本スキルは、**`orchestune`スキルで承認済みの`decomposition_plan.md`**を受け取り、`orchestune provision` CLIによる各サブタスクのGitHub Issue起票および親子・依存関係の構築を行います。

## トリガー条件

**通常はユーザーが直接呼び出すスキルではありません。** [orchestune スキル](../orchestune/SKILL.md)が分解案の承認後に内部で引き継ぐ形でロードします。

例外的に、未起票の`decomposition_plan.md`からIssue起票のみを手動実行したい場合は、人間が直接このスキルを指定してロードしてよい。

## 前提

* システムに `orchestune` CLIツール（`orchestune provision`, `orchestune bootstrap`）がインストールされていること。
* 起票開始前に`orchestune bootstrap`を実行し、gh認証状態と必須ラベルの存在を確認しておくこと（手順1を参照）。

## ワークフロー: Issue起票

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
   `title`から親Issue（`[EPIC] <title>`）を起票し、`decomposition_plan.md`の`depends_on`のトポロジカル順で各サブタスクIssueを起票して`--parent`/`--blocked-by`相当の関係を`gh issue edit --set-parent`/`--add-blocked-by`で設定する。起票済みのIssue番号は起票の都度`decomposition_plan.md`のフロントマター（`parent_issue_number`、各サブタスクの`issue_number`）へ書き戻される。**冪等かつ部分失敗から再開可能**: 既に`issue_number`が設定済みのサブタスク、または親Issue配下の既存子Issueの本文に埋め込まれたFootprint YAMLの`subtask_id`が一致するサブタスクは、再作成されず既存のIssue番号がそのまま再利用される。各サブタスクIssueのFootprint YAMLには親番号（`parent_issue_number`）も常に埋め込まれるため、`add_sub_issue`/`set_blocked_by`が失敗する（後述のnative関係非対応など）縮退環境でも、この本文metadataを頼りに`--parent-issue`モードのDispatcherが対象Issueを発見でき、`orchestune provision`自体は中断せず完走する（`ProvisionResult.degraded_subtask_ids`で縮退したサブタスクを報告する）。詳細な導出規則（ラベル付与規則、`.github/issue_template.md`のプレースホルダー置換規則等）は`orchestune/provisioning.py`のdocstringおよび`docs/ja/usage.md`を参照。
4. **起票したIssue一覧を[orchestune スキル](../orchestune/SKILL.md)に返し、ユーザーへの報告、または[orchestune-dispatch スキル](../orchestune-dispatch/SKILL.md)へのハンドオフに用いさせます。**

### `gh` が利用できない環境でのフォールバック（手動起票）

`orchestune provision`は内部で`gh` CLIを呼び出す。`gh`自体がインストール・認証できない環境では、GitHub MCPサーバーを使うか、ユーザーにWeb UIでの手動起票を案内すること。この場合、以下の対応関係を守って手作業で代替する：

* 親Issue: `decomposition_plan.md`の`title`から`[EPIC] <title>`のタイトルで起票し、確定した番号を`parent_issue_number`へ書き戻す。
* 各サブタスクIssue: `.github/issue_template.md`のプレースホルダー（`{{subtask_id}}`, `{{subtask_id_yaml}}`, `{{description}}`, `{{overview}}`, `{{proposed_changes}}`, `{{acceptance_criteria}}`, `{{verification_plan}}`, `{{footprint}}`, `{{symbols}}`, `{{depends_on}}`, `{{parent_issue_number}}`）へサブタスクの情報を埋め込み、確定した番号を各サブタスクの`issue_number`へ書き戻す。`{{subtask_id}}`は見出し等の表示用（生の値）、`{{subtask_id_yaml}}`はFootprint YAMLブロック内専用（`:`や`#`を含むIDでも安全な、YAMLスカラーとしてクォート済みの値）で、両者は必ず使い分けること。`{{parent_issue_number}}`は親Issueの番号（未確定なら`null`）で、native関係が使えない環境でDispatcherが子Issueを発見するための必須フォールバックなので省略しないこと。
* **ラベル付与を必ず行うこと**: `dispatch_cycle._group_by_status`はステータスラベル（`status:queued`/`status:blocked`等）が付いていないIssueを一切拾わないため、ラベルを付け忘れるとディスパッチ実行時にそのサブタスクが永久にディスパッチされない。`depends_on`が空、またはその依存先が全て完了済みなら`status:queued`、そうでなければ`status:blocked`を付与する。加えて`priority:{subtask.priority}`（`decomposition_plan.md`の`priority`。未指定時は`medium`）を必ず、`risk`が真の場合は`risk:flagged`も付与する（導出規則は`orchestune/provisioning.py`の`_derive_labels`と同一）。
* GitHub MCPによるIssue起票ではnativeの`blocked_by`/`parent`関係を設定できない場合がある。この場合もFootprint YAMLの`depends_on`を必ず保持すること。ディスパッチャーはこの値を依存判定と自己修復時のブランチスタッキング復元に使用する。GitHub上の関係を可視化したい場合は、起票後にWeb UIまたは`gh issue edit --set-parent`/`--add-blocked-by`でnativeの関係を追加する。
