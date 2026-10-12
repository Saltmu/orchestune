# 共有実行台帳とパッケージ境界

本ドキュメントでは、`claim`・`complete`・`dispatch`・`integrator`・状態表示が共有するL2の基盤`ledger`の責務と依存規則、所有権契約、および`targets`・リポジトリ設定・consistency status kernelのパッケージ境界について説明します。層構造の全体像は [アーキテクチャと設計思想 §4](../architecture.md#4-モジュール層構造とパッケージ境界) を参照してください。

---

## 1. ledger のモジュールと責務

- `ledger.completion_reservations`: completion の確定証拠が保存されるまで、claim・GC・repair・依存 reader が Issue を変更・解放しないための共通ガード。
- `ledger.run_state`: 実行状態モデル、読込、検証、正規化、保存、保持方針のヘルパー。
  dispatch からの分離で JSON 形式や、保存時に既存の状態ロックを保持する条件は変えません。
- `ledger.active_records`: `ActiveWorktree` とその不変構造を所有します。`ActiveWorktree` はメモリ上の唯一の情報源で、`core` と3つの frozen サブレコード（`launch`, `claim`, `completion`）から成ります。
  - 過去のフラット属性プロパティは全廃されており、`slots=True` により不正なアクセスを拒絶します。
  - `from_records()` は型付きのnested構築経路、`with_core()` は共通fieldを更新する関数です。recordを二重に保存しません。
  - 所有者fieldは所有者APIだけで置き換えます。claim fieldは `claim.ownership`（`build_claim_info()`・`with_claim()`）、launch fieldは `dispatch.launch_state`（`build_launch_record()`・`with_launch()`・`with_launch_phase()`）、completion fieldは `complete.journal`（`active_completion_from_record()`・`with_completion()`）です。
  - 詳細な仕様とライフサイクルの優先順位は [ステートレスCIと自己修復 (state-recovery.md)](state-recovery.md#active-worktree-lifecycle) を参照してください。
- `ledger.active_codec`: 既存のフラットなActiveWorktree JSON形式を明示的にdecode/encodeします。
  field順と、`completion_policy_config`がnullの場合の省略を維持し、台帳の検証処理からcodecの詳細を分離します。
- `ledger.active_lifecycle`: `lifecycle(active)` は永続フィールドから、completion → launch → recovery sentinel → claim → reservation の順で候補段階を返します。`HANDOFF_READY` は候補だけを示し、completion receipt や remote evidence の検証は既存処理が引き続き担います。enum と helper は `ledger.run_state` からも利用できます。

  | 候補段階 | 上から順に適用する条件 |
  | --- | --- |
  | `HANDOFF_READY` | handoff-ready flag、またはhandoff段階のcompletionがある。 |
  | `COMPLETING` | completion IDがある。 |
  | `RUNNING` | PID、external handle、またはlaunched phaseがある。 |
  | `LAUNCHING` | 開始時刻、launch attempt ID、または別のlaunch phaseがある。 |
  | `RECOVERY_REQUIRED` | recovered claim ID、またはowner不確実を示すdigest sentinelがある。 |
  | `CLAIMED` | ownerがinteractiveで、`reserved`以外のclaim stageが設定されている。 |
  | `RESERVED` | 優先条件に一致しない。初期の`reserved` claimと、in-memory recordでclaim stageが未設定の場合も含む。永続recordでは`claim_stage`が必須。 |
- `ledger.status_labels`: 共通の状態ラベル定数と遷移の適用。`status_machine`の計画を、
  追加 → 台帳確定コールバック → 旧ラベルの逐次除去の順でForgeへ適用します。
- `ledger.status_machine`: Forgeに依存しない純粋モジュール。`status:*`ラベルの役割表、
  通常のlifecycle遷移表、スナップショットから導く追加・除去の計画を持ちます。
  ラベルの読み書きやコールバックは行わず、本番では不正遷移を拒否しません。
- `ledger.status_events`: `status:*`のEventモデル（#1219 Phase 3）。Event・実行identity・
  retry予約・予算を副作用なしで判定する純粋モジュールで、依存は`status_machine`・`labels`・
  `bounded_limit`だけです。本番の判定は置き換えず、本番関数との対応は適合テストで結びます。
  backoff付きretryの判定は`dispatch.retry_policy`が所有するため、ledger→dispatchの依存を
  作らないよう呼び出し側が`BudgetLimits.plan_backoff`として注入します。
- `ledger.escalation`: 共通エスカレーション操作。終端ラベル追加、利用側の台帳確定
  コールバック、指定された旧ラベル削除、理由コメント投稿の順で実行します。
  追加またはコールバックの失敗は後続処理を停止し、その後の削除・投稿の失敗では
  確定済みの台帳更新を巻き戻しません。dispatch 固有のルール評価・プロセス停止・
  台帳更新は dispatch ワークフローとコールバックに残します。

## 2. 所有権契約

`ownership_contracts` は `OwnerKind`・`ClaimStage`・`ReservationKind` を定義する
独立した L0 モジュールです。ledger はここを直接参照します。`claim.contracts` は
同じ enum の再エクスポートを許可し、claim 固有の Request・Outcome を保持します。
共有 enum は claim・ledger・dispatch の実装に依存しません。

## 3. ledger の依存規則

ledger の専用の依存規則は一般のレイヤー規則より厳しく、ledger 内部参照を許可する一方、
`orchestune` 内の外部依存は L0/L1 に限定します。標準・外部ライブラリはこの
レイヤー検査の対象外です。

- ledger は claim・dispatch に依存しません。
- `complete` から dispatch への依存は解消済みで、旧 `dispatch.state`・`dispatch.labels` は削除済みです。
- integrator は共通エスカレーションを ledger から、`file_lock` を `infra.process_utils` から直接参照します。

CIでの検査内容は [アーキテクチャと設計思想 §4.2](../architecture.md#42-ciで機械的に検証される不変条件) のルール6を参照してください。

## 4. 共有パッケージの境界

### 4.1 targets

coordinator は `targets.cloud_routine` の routine 定数・実装と `targets.contracts` の実行契約を参照します。共通の起動指示・ブランチ検証・完了証拠判定は `targets.support` に置きます。dispatch は互換性のため同一オブジェクトを再公開し、共有 targets パッケージは dispatch・integrator を import しません。

### 4.2 共通の型と判定の置き場所

複数のワークフローが使う型と判定は、ワークフローのパッケージではなく共通のモジュールに置きます。これにより、`claim`・`integrator` から `dispatch` への依存を持たずに済みます。

| 対象 | 置き場所 |
| --- | --- |
| 共通のロック型・定数（`KIND_BRANCH`/`KIND_PR`/`ExternalLockConflict`/`ExternalLockScanResult`） | L0 の `lock_contracts` |
| 依存の判定・方針・legacy 解決 | L2 の `dependencies` パッケージ（`dependencies.resolution`・`dependencies.assessment`・`dependencies.policy`） |
| 計画識別子 | L0 の `plan_identity` |
| シンボル検証 | `dag` パッケージ配下の `dag.symbol_verification` |

### 4.3 リポジトリ設定

リポジトリの TOML 検出は L1 アダプタ `infra.repository_config` に置きます。
L2 の `targets.completion_policy` が実効トークン上限を解釈し、共通の
`targets.usage` provider から利用量を取得します。互換性のため
`dispatch.config_loader` は共通設定 loader を再公開します。complete policy は
dispatch の設定・workflow モジュールを import しません。

### 4.4 consistency status kernel

consistency status kernelはstatus-machineの役割表からlifecycleを導出し、7種tupleとledgerの3種ACTIVEを維持します。

- invariant・planner・fresh executorはFINAL / ESCALATIONを保護し、正確なdone+queued差し戻し例外を維持し、初期化・競合修復でもpromotion holdを守ります。
- executorはIntentを永続化・検証し、通常遷移表の警告は観測のみです。
- Supervisorのpass/cycleの区別と収束の前提は [statusラベル](../status-labels.md#status-reconciliation-の安全性と収束性1218) を参照してください。

RuleChain・GC・rebase・Integratorの方針選択は別責務です。
