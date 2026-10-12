# ディスパッチサイクルレポートと計測

本ドキュメントでは、applyモードの各ディスパッチサイクルが親Issueへ投稿するサイクルレポートと、サイクルレコードに記録する計測値（`timings`）について説明します。全体像およびコア設計思想については [アーキテクチャと設計思想](../architecture.md) を参照してください。

---

## 1. 目的

`orchestune-dispatch`のサイクル毎イベントログ（`events.jsonl`）は`.gitignore`対象であり、CI実行のたびに揮発するため、それ単独では恒久的な履歴になりません。このログに頼らずにディスパッチサイクルの意思決定を追跡できるよう、サイクルレポートを親Issueへコメントとして投稿します。

## 2. 投稿の条件

**apply（`--no-apply`ではない）モードの**各ディスパッチサイクル終了時に、設定された親Issue（`--parent-issue`、#396）へ`## 🤖 Orchestune Dispatch Cycle Report`という見出しのコメントを投稿します。

次の場合は投稿しません。

- `--no-apply`で実行した場合。`--no-apply`はpost-cycleブロック全体をスキップします。
- 報告すべき内容が無いサイクル。
- 親Issueが未設定の場合。

## 3. コメントの内容

コメントの内容は、そのサイクルで選定されたタスク・注目すべき`footprint`逸脱イベント・完了イベント・昇格イベントの要約です。

状態が変化しない定常的な逸脱イベント（例: 既に強制直列化済みのworktreeについての再通知）は、スキップ判定・コメント本文の両方から除外されます。そのため、同一内容のコメントが毎サイクル親Issueに積み上がることはありません。

## 4. 投稿に失敗した場合

投稿に失敗した場合の扱いは、他のpost-cycleフェーズと同様です。例外は送出せず、サイクル自体は常に最後まで実行されます。ただし失敗は`orchestune-dispatch`の非ゼロ終了ステータスとして表面化し、CIのステップとしては失敗扱いになります。

| 失敗の種類 | ログ | 終了コード |
| --- | --- | --- |
| 通常の投稿エラー（一時的なネットワークエラー等） | 警告 | 2 |
| GitHub認証エラー | エラー | 1 |

## 5. サイクルレコードの計測値（`timings`）

apply時のサイクルレコードには`timings`（version 1）も追加します。

| 項目 | 内容 |
| --- | --- |
| 記録する値 | `elapsed_seconds`、`phases`（各phaseの子処理を含む`seconds`と区間数`count`）、`gh`（試行数`calls`と合計`seconds`）。 |
| 計測範囲 | scopeの`cycle_before_events_record`は既存の`cycle: started`から`events_record: started`までで、events書込み、lock解放、post-cycle、結果ファイル保存を含みません。 |
| フェーズ境界 | 既存の進捗通知を再利用します。複数task launchは合算しますが、その時間はschedulingにも含まれるため、phase時間の総和をcycle全体の時間と解釈しないでください。 |
| `gh.calls` | `GitHubForge._run`経由の試行数で、stdin・失敗・timeoutも含みます。HTTP request数や子エージェント内のコマンド数ではありません。 |
| 計測障害 | `collection_status: partial`または`unavailable`と、不明なscalar値のnullで表し、dispatch結果・終了コードを変更しません。 |
| 互換性 | 過去レコードには`timings`が無い場合があります。結果JSONと`CycleReport`は維持し、dry-runと途中中断時のevents保存方針も従来どおりです。 |
