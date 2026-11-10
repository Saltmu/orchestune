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

## 6. Dispatcher の 4 ステージ構成 (#1377)

Dispatcher は apply モードにおいて、1 回の実行を以下の 4 つのステージに分割して実行します。

1. **統合前cycle (`run_pre_integration_cycle`)**: `run_state_lock` を保持し、`gc_reclaim` までの完了検知・作業木回収・整合性検査を実行してイベントログに記録します（`stage="pre_integration"`）。選定・起動や external-lock 走査はスキップされます。完了後に `run_state_lock` を解放します。
2. **統合境界 (`pre_scheduling_integration`)**: `run_state_lock` のスコープ外で実行されます。認証検証、`status:not-needed` レビューのポーリング、第2層セマンティックレビューの進行、および `Integrator` の実行（1回）を行います。CI実行中に `run_state_lock` を保持しないため、他のエージェントプロセスの完了報告をブロックしません。
3. **scheduling cycle (`run_dispatch_cycle`)**: 再度 `run_state_lock` を取得し、最新の状態を再取得したコンテキストで `reconciliation_external_locks`（external-lock 判定・依存昇格）および `scheduling`（選定・起動）を実行し、イベントログに記録します（`stage="scheduling"`）。完了後に `run_state_lock` を解放します。
4. **post-cycle (`_post_cycle_steps`)**: `run_state_lock` のスコープ外で、親Issue完了処理、両サイクルのイベントを統合した親Issueイベントログコメントの投稿、整合性スーパーバイザの finding 通知を行います。

`--no-apply`（dry-run モード）では、統合前cycleおよび統合境界は実行されず、従来の単一 scheduling cycle のみが FULL で実行されます。

## 7. 候補選定における active 予約との重複除外 (#1547)

scheduling cycle のタスク候補選定（`_filter_candidates_for_forced_serial`）では、dispatch 所有の active 予約（Issue 本文の Footprint ∪ `declared_footprint`）またはリポジトリ予約と Footprint が重なる queued タスクを起動候補から除外します。

除外されたタスクは `status:queued` のまま次サイクルへ回され（スキップ記録に `forced-serial` として記録）、先行タスクの完了・予約解放後に安全に起動されます。これにより、実行中タスクが `--amend-footprint` 等で予約を拡張した場合でも、起動時の `CLAIM_CONFLICT` による意図しない `status:blocked` への遷移を防ぎます。なお、claim の衝突規則と同様に、interactive 予約との重複では除外されません。

## 8. サイクル内の GitHub 読み取り (#1836)

apply の統合前サイクルと scheduling サイクルは、それぞれ `_build_cycle_context` で子 Issue、ファイル付きのオープン PR、候補ブランチの存在を取り直します。統合境界では `run_state_lock` を外すため、統合前の読み取りを scheduling へ持ち越しません。

候補ブランチは、その context で検証済み候補がある正規名だけをまとめ、GraphQL で最大 50 件ずつ確認します。一括確認を持たない Forge は候補ごとに `branch_exists` を使います。回復経路と Integrator の単発確認はまとめません。未確定の候補は不在として扱わず、PR フォールバックへ進みません。

外部ロック判定はファイル付きのオープン PR 一覧を使います。attempt の探索、統合 PR の再利用照合、統合準備の `TaskBranchResolver` はファイル一覧の無い軽量取得を使います。attempt の最終化は、その直前に一覧を取り直します。

親完了は、子 Issue の発見より先に最終 PR の legacy 移行を判定します。移行の書き込みが無ければ、その候補を保留表示へ渡します。本文を書き換えた場合は、保留表示が移行後の軽量一覧を読み直します。移行に失敗したときは `unsafe_final_pr` で止まり、保留表示へ進みません。一覧を省略した単独の保留表示・移行・統合 PR 作成は、必要になったときだけ自前で取得します。


