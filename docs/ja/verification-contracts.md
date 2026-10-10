# 検証契約と誤動作の検出（対照）

Phase 1〜3の保証は、`tests/verification_contracts.py` の1行が1つに対応します。行には、保証・前提・観測境界・期待値・本番コードで検証するテスト、そしてそのテストが失敗しうることを示す**対照**を書きます。対照は、誤動作を1つだけ（テスト内で `monkeypatch` により。本番コードに誤動作用のフラグはありません）入れ、通常のテストと同じ決定的なシナリオを実行し、**期待した契約id**の違反（`ContractViolation`）になったときだけ成功とします。別の契約の違反、前提の不成立、無関係な例外、失敗しないことは、対照の失敗です。ランダム探索で見つかることは検出の根拠にしません。`tests/test_verification_contracts.py` が、この表を実際のpytest collection（parametrizeのケースを含む。skip・xfailは数えない）と下の表に照合します。

<!-- contract-table -->
## 契約表

状態: `verified` = 通常のテストと1つ以上の対照がある。`unverified` = 保証はあるが対照がない（理由は下記）。`known_defect` = 別Issueの本番欠陥を strict xfail または明示的な除外で固定している。`out_of_scope` = 文書化済みの保証外。この表は状態以上のことを主張しません。「全保証を確認済み」という意味ではありません。

| ID | Phase | 状態 | 検出する誤動作 | Issue |
|---|---|---|---|---|
| `P1-LIFECYCLE-NONEMPTY` | 1 | verified | `remove-before-add` | - |
| `P1-SUCCESS-TARGET-ONLY` | 1 | verified | `remove-missing` | - |
| `P1-RETRY-CONVERGES` | 1 | verified | `retry-noop` | - |
| `P1-ESCALATION-NOT-ACTIVE` | 1 | unverified | - | - |
| `P1-EXTERNAL-CHANGE` | 1 | out_of_scope | - | - |
| `P2-FINAL-ESCALATION-PROTECTED` | 2 | verified | `fresh-guard-bypass` | - |
| `P2-PLAN-HUMAN-GATE` | 2 | verified | `planner-gate-bypass` | - |
| `P2-PROMOTION-HOLD` | 2 | verified | `hold-guard-bypass` | - |
| `P2-RECOVERY-ONE-CYCLE` | 2 | verified | `repair-disabled`, `repair-delayed` | - |
| `P2-RECOVERY-STABLE` | 2 | unverified | - | - |
| `P2-LIVE-VERIFICATION` | 2 | verified | `event-only` | - |
| `P2-CONVERGENCE-UPPER-BOUND` | 2 | unverified | - | - |
| `P3A-ROUTE-COVERAGE` | 3a | verified | `unrouted-source`, `unexecuted-route-condition` | - |
| `P3A-DYNAMIC-CONFORMANCE` | 3a | verified | `misrouted-event`, `wrong-model-target` | - |
| `P3A-DOCUMENT-TABLE` | 3a | unverified | - | - |
| `P3B-BUDGET-CONSUMED` | 3b | verified | `budget-not-consumed` | - |
| `P3B-RETRY-BOUND` | 3b | verified | `budget-bound-exceeded` | - |
| `P3B-LEDGER-LOSS-KEEPS-PERSISTENT` | 3b | verified | `persistent-budget-reset` | - |
| `P3B-LAUNCH-AND-ESCALATION` | 3b | unverified | - | - |
| `P3B-PERSISTENT-BUDGET-PRESERVED` | 3b | known_defect | - | #1279, #1280 |
| `P3C-CASE-DELAY` | 3c | verified | `promotion-suppressed`, `promotion-delayed`, `empty-completion-set`, `throwaway-context` | - |
| `P3C-LIVENESS-BOUND` | 3c | verified | `promotion-suppressed`, `promotion-delayed`, `event-only`, `empty-completion-set`, `throwaway-context` | - |
| `P3C-INTERMEDIATE-LIVENESS` | 3c | verified | `intermediate-ignored`, `promotion-suppressed` | - |
| `P3C-SAFETY` | 3c | verified | `hold-guard-bypass`, `reservation-guard-bypass`, `stale-evidence`, `action-mapping-dropped`, `event-only` | - |
| `P3C-INTERMEDIATE-SAFETY` | 3c | verified | `reservation-guard-bypass` | - |
| `P3C-QUINT-MODEL` | 3c | verified | `promotion-suppressed`, `promotion-delayed`, `stale-evidence`, `intermediate-ignored`, `reservation-guard-bypass`, `hold-guard-bypass`, `event-only`, `dry-run-writes` | - |
| `P3C-DRYRUN-READONLY` | 3c | verified | `dry-run-writes` | - |
| `P3C-QUINT-OBSERVATION` | 3c | verified | `action-mapping-swapped` | - |
| `P3C-DRYRUN-DEPENDENCY-RESERVATION` | 3c | verified | `reservation-guard-bypass` | - |
| `P3C-DRYRUN-RESERVATION` | 3c | known_defect | - | #1281, #1283 |
| `P3C-LIVENESS-PRIOR-MERGE-DRYRUN` | 3c | known_defect | - | #1281 |

### 詳細

| ID | 保証 | 前提 | 観測境界 | 期待値 |
|---|---|---|---|---|
| `P1-LIFECYCLE-NONEMPTY` | 全てのForge add/removeの直後に lifecycle ラベルが1つ以上ある | 外部変更なし・未回復の操作は1つ | Forge操作ごと（アダプターの戻り時だけではない） | 常に（障害注入直後も） |
| `P1-SUCCESS-TARGET-ONLY` | 完全な除去リストで正常終了すると target だけが残る | 旧ラベル一覧が完全・障害なし | アダプターの戻り | lifecycle == {target}、auxiliaryは不変 |
| `P1-RETRY-CONVERGES` | 部分失敗のあと1回の完全な再試行で target に収束する | 再試行の add・callback・全 remove が成功 | 再試行の戻り、再実行後もラベル不変 | 完全な再試行1回 |
| `P1-ESCALATION-NOT-ACTIVE` | 通常のルールは ESCALATION → ACTIVE を選ばない | テストのルール選択であり、アダプターは拒否しない | ルール選択 | 選ばない |
| `P1-EXTERNAL-CHANGE` | 任意の外部 relabel のあと | external_relabel は lifecycle ラベルを自由に増減する | - | 無条件の保証なし（新しいepochから再開） |
| `P2-FINAL-ESCALATION-PROTECTED` | 古い計画が FINAL / ESCALATION のタスクを再活性化しない | 計画から実行までの間にラベルが変わる | executor の fresh guard | 保護ラベルだけが残る |
| `P2-PLAN-HUMAN-GATE` | planner は human gate の除去を計画しない | report は何でも主張できる | planner の出力 | 空の計画 |
| `P2-PROMOTION-HOLD` | 実行前に現れた昇格 hold が昇格を止める | 計画後に `ci:base-branch-red` / `status:blocked-recompute` が付く | executor の fresh guard | hold がある間 `status:queued` を付与しない |
| `P2-RECOVERY-ONE-CYCLE` | 修復可能なケースは回復後ちょうど1cycleで収束する | 完全なKNOWN fact・安定した証拠・apply・全command許可・holdなし | 最初のcycleの終わり（期待値1。上限k=3は別） | 1cycle後に labels == target で finding なし |
| `P2-RECOVERY-STABLE` | その後2cycleはラベル・計画・履歴・Intent集合が不変 | P2-RECOVERY-ONE-CYCLE のあと | 2・3回目のcycle | 変化なし |
| `P2-LIVE-VERIFICATION` | 適用済みの修復は実ラベルで裏付けられる | ForgeがcommandをAPIで受理した | repair result の status | 実際の primary status が target のときだけ APPLIED |
| `P2-CONVERGENCE-UPPER-BOUND` | 上限 k=3 cycle 以内に収束する | P2-RECOVERY-ONE-CYCLE と同じ | k回目のcycle | k=3 |
| `P3A-ROUTE-COVERAGE` | 全ての本番ソースに経路があり、全ての経路条件がケースで実行される | 呼び出し元・対象外経路・不変条件経路の静的表 | 経路表 | 網羅されている |
| `P3A-DYNAMIC-CONFORMANCE` | 本番ドライバーが Event モデルの予測したラベルと状態に到達する | 実行されるケース表。静的網羅は別契約 | 各ステップ（labels・result・completion・execution・retries・counts） | apply_event と一致 |
| `P3A-DOCUMENT-TABLE` | status-labels.md が全経路の source / target を載せる | 文書が読者の仕様になる | 文書の表 | 経路表と一致 |
| `P3B-BUDGET-CONSUMED` | 新しい論理リトライごとに予算を1つ消費し、resume は消費しない | 別々のoperationを持つ予算付きEvent | 各 delivery | count + 1（resume・枯渇は不変） |
| `P3B-RETRY-BOUND` | 台帳のepoch内で予算ごとの新規リトライが仕様表の上限を超えない | 既定のlimits。ローカル予算は台帳消失でのみ戻る | カウントされる各リトライ | RETRY_BOUNDS |
| `P3B-LEDGER-LOSS-KEEPS-PERSISTENT` | 台帳消失でリセットされるのはローカル予算だけ | recompute / base-branch-red は Forge に保持される | restart(ledger_loss=True) | 永続カウントが不変 |
| `P3B-LAUNCH-AND-ESCALATION` | 二重の active execution なし・完了後の再launchなし・エスカレーションは不可逆 | ランダム系列で Event モデルを駆動 | 各 delivery | 不変条件 3〜5 |
| `P3B-PERSISTENT-BUDGET-PRESERVED` | GC回収は backoff 予算を、再launchは recompute 予算を保つ | TaskReclaimRecord と Issue 本文カウンターの永続化 | 永続化されたレコード | 予算が残る |
| `P3C-CASE-DELAY` | 完了経路ごとに T がケース表のcycle（d = 0）ちょうどで昇格する | 証拠が cycle 0 の昇格判定に利用可能 | 証拠投入のcycle。早くも遅くもない | ケース表の d |
| `P3C-LIVENESS-BOUND` | 公平な N = 1 cycle で T が queued（dry run は preview）になる | 公平なcycle: cycle 開始時と昇格判定時に前提が成り立ち、エラー・障害注入がない | 最初の公平なcycleの終わり。apply は実ラベル、dry run は当該cycleの preview | N = 1 |
| `P3C-INTERMEDIATE-LIVENESS` | 中間ノードが公平な N = 1 cycle で昇格（dry run は preview）される | そのノード自身の依存が両境界で有効かつ予約なし | そのノードの最初の公平なcycleの終わり | N = 1 |
| `P3C-SAFETY` | 有効な証拠がない、または hold・予約があるときに昇格（preview）しない | apply は昇格判定時点、dry run は cycle 開始時点で判定 | 全cycle | 起きない |
| `P3C-INTERMEDIATE-SAFETY` | 中間ノードを、自身の依存が有効かつ予約なしになる前に昇格させない | apply は昇格判定時点で判定 | 全cycle | 起きない |
| `P3C-QUINT-MODEL` | Quintモデルが safety・bounded liveness（N = 1）・イベント = 実ラベル（apply）・読み取り専用の dry run を守る | 有限のトポロジー（依存 Issue は最大4つ）、モデルの公平性、記録した seed と境界でのサンプリング探索 | 各 action 後のモデルの状態 | 不変条件の違反なし。モデルの各誤動作は期待した不変条件だけに違反する |
| `P3C-DRYRUN-READONLY` | dry run の cycle は T・中間ノード・依存 Issue の lifecycle ラベルを変えない | dry run は Forge を変更しない。hold ラベルと fixture 自身の stale 変更は cycle のものではない | 再生するトレースの各 dry run cycle | ラベルは不変 |
| `P3C-QUINT-OBSERVATION` | 再生の各 step で、action を実行する前の本番の状態がモデルと一致する | `tests/quint_replay.py` の action 対応表 | 再生するITFトレースの各遷移 | 全ての前提が実ハーネスで成り立つ |
| `P3C-DRYRUN-DEPENDENCY-RESERVATION` | dry run の T または中間ノードの preview が、そのノードの依存の未解放 completion reservation を尊重する | preview は context snapshot 上で定義される（この側は #1267 で修正済み） | dry run の cycle。決定的なシナリオで検査（machine は T の dry run の予約 preview を除外している） | 依存の予約が未解放の間 preview しない |
| `P3C-DRYRUN-RESERVATION` | dry run の T または中間ノードの preview が、そのノード自身の未解放 reservation を尊重する | preview は context snapshot 上で定義される | dry run の cycle | T 自身・中間ノードの予約下で preview しない |
| `P3C-LIVENESS-PRIOR-MERGE-DRYRUN` | Forge 障害つきの apply cycle の後でも、先行マージ証拠だけの依存を持つ T が dry run で preview される | 公平なcycle（両境界で昇格可能・障害注入なし） | 障害つき apply cycle 2回のあとの dry run cycle | T が preview に現れる（N = 1） |

### `verified` ではない行

- `P1-ESCALATION-NOT-ACTIVE` (unverified): 本番コードの性質ではなくテストのルール選択であり、誤動作を注入できない
- `P1-EXTERNAL-CHANGE` (out_of_scope): 文書化済みの保証外（status-labels.md）。回復は Phase 2 の対象
- `P2-RECOVERY-STABLE` (unverified): 対照なし: 収束後に振動する修復を注入していない
- `P2-CONVERGENCE-UPPER-BOUND` (unverified): 1パスを超えて駆動するテストがなく、期待値（1cycle）だけを検査している
- `P3A-DOCUMENT-TABLE` (unverified): 対照なし: 文書のずれを注入していない
- `P3B-LAUNCH-AND-ESCALATION` (unverified): 契約idのないassertで、誤動作を注入していない
- `P3B-PERSISTENT-BUDGET-PRESERVED` (known_defect): strict xfail が反例を固定している。修正でマークを外す
- `P3C-DRYRUN-RESERVATION` (known_defect): strict xfail の反例で固定している（T は #1283、中間ノードは #1281 の反例1）。本番修正が入るまで assert_safe と中間ノードの検査がこれらの preview を除外している（依存側は別の verified 契約）
- `P3C-LIVENESS-PRIOR-MERGE-DRYRUN` (known_defect): #1281 の反例2を strict xfail で固定している。本番が先行マージの完了集合を落とすのか、公平性判定の問題なのかはまだ切り分けていない

<a id="cycle-definitions"></a>
<!-- cycle-definitions -->
## cycleの定義

- **cycle 0**: 有効な証拠が、昇格（または修復）判定の入力として初めて利用可能になったcycle。cycle の判定より前に投入された証拠ならそのcycle、後ならば次のcycleが 0 です。
- **期待遅延 d**（決定的なケース表）: cycle 0 から数えて、何cycle目の判定で昇格するか。依存解決は、観測できる全ケースで d = 0 です。d より早い昇格もケースの失敗です。
- **上限 N**（ランダム系列）: N 回目の*公平な*cycleの終わりまでの昇格。依存解決は N = 1 です。#1219 設計 §4 の「N+1 cycle」は、同じ保証を N = 1 で言い換えた表現です。#1219 は close 済みなので本文は変更しません。
- **公平なcycle**: cycle の開始時と昇格判定時の両方で前提が成り立ち、エラー・障害注入がない。何も変えない外乱は数え直しません。apply は昇格判定時点（executor が Forge を再取得する）、dry run と listing lag のcycleは開始時点（context の snapshot）で判定します。
- **観測**: apply のcycleは実ラベル `status:queued` を、ラベルを変えない dry run は当該cycle自身の `PromotionEvent` の preview（または T が既に queued）を要求します。過去のcycleの preview は後続cycleの成功に使いません。
- **Phase 2**: *期待値*は、修復可能なケースが回復後ちょうど1cycleで収束すること（`P2-RECOVERY-ONE-CYCLE`）。*上限*は k = 3（`P2-CONVERGENCE-UPPER-BOUND`、未検証）。両者は別の契約です。

### どのテストが何を検出するか（実測）

Issue の当初の読みは、同じcycleの中で証拠が入る経路（`record_completion`）の1cycle遅延を、ケース表は検出するが乱択の machine は検出しない、というものでした。対照テストの実測では、apply のcycleについてこれは当たりません。証拠はcycleの開始時点で既に台帳に保存されているので、そのcycleは公平に数えられ、ケース表も machine も遅延を検出します。違いは、ほかに何を固定するかです。ケース表は d より**早い**昇格も失敗にしますが、machine は公平なcycleの終わりに昇格していることだけを要求します（早い昇格は安全性 `P3C-SAFETY` の契約）。`record_completion` の dry run には観測できるものがなく（#882）、どちら側も遅延を検出できません。`test_control_dry_run_record_completion_has_nothing_to_detect` がこの穴を隠さずに記録しています。

<!-- detection-matrix -->
## 検出の対応表

| 誤動作 | テスト内での入れ方 | 違反すべき契約 | 検出するシナリオ |
|---|---|---|---|
| `remove-missing` | `plan_transition` が最初の除去を落とす | `P1-SUCCESS-TARGET-ONLY` | アダプターのシナリオ |
| `remove-before-add` | アダプターが add より先に remove する | `P1-LIFECYCLE-NONEMPTY` | 操作ごとの観測器（最終ラベルは正しい） |
| `retry-noop` | 対象ラベルが既にあると除去を飛ばす | `P1-RETRY-CONVERGES` | `add-response-lost` の位置 |
| `fresh-guard-bypass` | `_fresh_preconditions_hold` が常に通る | `P2-FINAL-ESCALATION-PROTECTED` | 古い計画、`done` と `blocked-human-review` |
| `planner-gate-bypass` | planner の保護判定が常に通る | `P2-PLAN-HUMAN-GATE` | planner のシナリオ |
| `hold-guard-bypass` | executor の `PROMOTION_HOLD_LABELS` を空にする | `P2-PROMOTION-HOLD` | 計画後に hold が付く古い計画 |
| `repair-disabled` / `repair-delayed` | planner が常に／最初の呼び出しで何も返さない | `P2-RECOVERY-ONE-CYCLE` | 依存解決済みの blocked |
| `event-only` | commandは何も変えないが検証が成功を主張する | `P2-LIVE-VERIFICATION`（Phase 2）、`P3C-LIVENESS-BOUND`（3c） | result の status と実ラベル、apply cycle のラベル |
| `unrouted-source` / `unexecuted-route-condition` | ソースを消す／実行されない条件を足す | `P3A-ROUTE-COVERAGE` | 静的検査 |
| `misrouted-event` / `wrong-model-target` | 経路が別の Event を指す／モデルが別の遷移先を返す | `P3A-DYNAMIC-CONFORMANCE` | 本番ドライバーとモデルの比較 |
| `budget-not-consumed` | `apply_event` が以前の retries を保つ | `P3B-BUDGET-CONSUMED` | reclaim ループ |
| `budget-bound-exceeded` | reclaim の上限を表より大きくする | `P3B-RETRY-BOUND` | reclaim ループ |
| `persistent-budget-reset` | `restart` が永続カウントも0にする | `P3B-LEDGER-LOSS-KEEPS-PERSISTENT` | 台帳消失のシナリオ |
| `promotion-suppressed` / `promotion-delayed` | 昇格境界を常に／証拠投入のcycleだけ飛ばす | `P3C-CASE-DELAY`（ケース表）、`P3C-LIVENESS-BOUND`、`P3C-INTERMEDIATE-LIVENESS` | label、`record_completion`（apply）、prior merge（apply・dry run） |
| `empty-completion-set` / `throwaway-context` | #902 Round 4/5 の誤配線 | `P3C-CASE-DELAY` | ケース表 |
| `intermediate-ignored` | 中間ノードの判定を落とす | `P3C-INTERMEDIATE-LIVENESS` | 中間ノードのトポロジー |
| `hold-guard-bypass`（3c） | 全ての `PROMOTION_HOLD_LABELS` を空にする | `P3C-SAFETY` | cycle 前の `ci:base-branch-red` |
| `reservation-guard-bypass` | 依存側・対象側・中間ノードの依存の予約判定が常に通る | `P3C-SAFETY`、`P3C-INTERMEDIATE-SAFETY`（machine の apply cycle）、`P3C-DRYRUN-DEPENDENCY-RESERVATION`（決定的な dry run） | machine は apply cycle。dry run の依存側は決定的なシナリオ |
| `stale-evidence` | 再オープンされた依存を完了とみなす | `P3C-SAFETY` | 取り消されたlabel証拠 |

既知の穴は、弱めずに記録します。乱択の machine の `assert_safe` は未解放の予約下の dry run の preview をすべて除外するため、`reservation-guard-bypass` を apply のcycleでしか検出しません。そのため dry run の依存側（#1267 で修正済み）は、T と中間ノードのどちらも専用の決定的なシナリオで検査します（`P3C-DRYRUN-DEPENDENCY-RESERVATION`。中間ノードの検査が除外するのはそのノード自身の予約だけです）。T 自身の予約と中間ノード自身の予約下の preview は、#1281・#1283 が直るまで既知の欠陥です（`P3C-DRYRUN-RESERVATION`）。2つの guard（`status_repair_preserves_protection` だけの迂回、中間ノードの早すぎる評価）は第2の層が再検証するため、誤動作1つでは迂回できません。対照は guard 全体を差し替えます。

<a id="quint-model"></a>
<!-- quint-model -->
## Quintモデルと本番再生

`specs/quint/dependency_liveness.qnt` は依存解決の bounded liveness を表すモデルです（#1276）。Quint のシミュレータ（`quint run`）による**サンプリング探索であり、証明ではありません**。任意のグラフ、無限の外乱列、サンプルが届かなかった状態については何も主張しません。`quint verify`（Apalache。JVM が必要）は必須にも任意の手順にもしていません。

**3つの層（どれも別の層の代わりにしません）。**

1. *モデル*: 固定 seed の探索と保存した全シナリオで、モデル自身の不変条件（`P3C-QUINT-MODEL`）を守ります。`safety`（義務に反する昇格をしない）、`liveness`（公平な cycle は昇格で終わる。N = 1。apply は実ラベル、dry run は当該 cycle の preview）、`eventMatchesLabel`（apply cycle のイベントは実際のラベル変更と一致する）、`dryRunReadOnly` です。
2. *再生*: 生成したITFトレースと保存したITFトレースの全遷移を、本番ハーネス（`LivenessWorld`。本物の `_prepare_cycle_context` と `execute_pipeline`）で実行します。cycle ごとに本番の観測（ラベル、`PromotionEvent` のプレビュー）をモデルの義務と照合し（ラベルが変わらなくても昇格イベントは昇格として数える。乱れのない apply cycle のイベントは実際のラベル変更でなければならない。dry run は fixture 自身の stale 変更を除き lifecycle ラベルを変えない）、action ごとに実行前の本番の状態をモデルの状態と照合します（`P3C-QUINT-OBSERVATION`）。期待値はモデルの状態だけから作り、本番に依存の完了を問い合わせません。
3. *対照*: モデルの誤動作を1つずつ入れたもの（正常なモデルでは成功し、誤動作版では予定の step で期待した不変条件だけに違反する）と、本番の誤動作を1つずつ入れたもの（同じトレースを再生し、期待した契約の違反になる）です。parse・型・実行のエラーと timeout はツールのエラーであり、検出には数えません。

**ツール。** Node.js（`package.json` の `engines` の major。CI は `actions/setup-node`）は local CI の必須依存です。`scripts/quint-check.sh` / `scripts/quint-check.ps1` が `npm ci` を実行し、固定した `@informalsystems/quint`（`package.json` と `package-lock.json` の exact な版）を確認します。Node.js がなければ local CI は skip せず、導入手順を表示して Exit 2 で止まります。シミュレータは `--backend=typescript` で動かします（固定したパッケージに同梱されています。既定の `rust` バックエンドは初回に固定されていないバイナリをダウンロードします）。

**探索と記録。** local CI は毎回、同じ seed と境界で探索します（`tests/quint_scenarios.py` の `EXPLORATION`: seed `0x2f9c`、最大1500サンプル、各最大30 step、`--mbt --out-itf --n-traces` で100トレース）。出力先は `scripts/create-session-dir.*` で作る `.orchestune/tmp/quint-replay-1276-…/` で、同じ実行内で再利用します。全トレースを再生し、トレース数・遷移数・cycle 数・照合した義務の数がいずれも正であることを要求します。そのディレクトリの `exploration-summary.json` にツールの版・seed・境界・実行時間・件数を記録し、local CI がそれを表示します。不変条件に違反した場合は、違反した不変条件の名前、正確なコマンド（seed とバックエンド）、反例のITFトレース（同じディレクトリに残ります）を報告します。出力の空・欠け、壊れたITF、未対応の action や値、初期状態しかないトレースは失敗です。

**モデルが述べること。** cycle の状態に `mustNot`（昇格してはならないノード）と `mustQueue`（queued または preview されていなければならないノード）を記録します。証拠は3値（`yes` / `no` / `maybe`）です。結果が分からない場合（注入した Forge 障害は変更のどこでも cycle を中断しうる。listing lag・予約・障害の下での台帳証拠の回収。台帳を失った後の証拠）は、推測せず**義務を述べません**。モデルにないもの、つまり主張しないものは次のとおりです。障害を仕込んだ apply cycle に未消化の stale 変更が重なる場合（障害が変更の前に cycle を中断しうる）、中間ノードへの hold（ハーネスにその経路がありません）、依存 Issue が4つを超える場合や深さ2を超える連鎖。apply cycle に仕込んだ障害は、実際には変更に当たらなくてもモデルではその cycle を公平でないものとして扱います。Hypothesis の machine より弱く、強くはありません。

**action 対応表**（`tests/quint_replay.py` の `ACTIONS`。表にない action や選択値は再生が失敗します）:

| モデルの action | 選択値（`mbt::nondetPicks`） | ハーネスの操作 | 境界 |
|---|---|---|---|
| `init` / `init_<シナリオ>` | トポロジーと `recompute`（状態から読む） | 状態の `shape` から `LivenessWorld(topology=…)` を作る。通常の action として再実行しない | 全 Issue の初期ラベルを照合 |
| `complete_dependency` | `path`（`label`・`record_completion`・`outcome_not_needed`・`prior_merge`）、`dep`（11〜14） | `LivenessWorld.complete(dep, path)` | 前提: `dep` が queued で証拠なし |
| `duplicate_completion` | `dep` | `LivenessWorld.duplicate(dep)` | 前提: `dep` に証拠あり |
| `cycle` | `apply`、`lag` | `listing_lag = lag` で本物の cycle を1回実行。保留中の stale 変更は `before_promotion` で実行 | `mustNot` / `mustQueue` をラベルと `PromotionEvent` と照合 |
| `restart` | `ledgerLoss` | true なら `lose_ledger()`（run-state と intent journal。予約と未回収の台帳証拠が消える） | - |
| `fail_next` | `op`（`add`・`remove`）、`mode`（`before`・`after`） | `FaultPlan.forge_operation` | 次の cycle が消費 |
| `toggle_hold` | `kind`（`base_red`・`recompute`・`reservation`）、`target` | T のラベル、または T・依存・中間ノードの `set_reservation(target)`。新しい値は次のモデル状態から取る | - |
| `stale_snapshot` | `kind`（`add_hold`・`revoke`・`complete`）、`target` | 次の cycle で context 構築から昇格までの間に入る変更 | モデルが適用したと述べた場合（`staleApplied`）だけ実行 |

**保存シナリオ。** モデルの台本付きシナリオ（`init_<id>`。各 step の選択値を固定するので、ITFの形は乱択トレースと同じ）が、final / 中間ノード、apply / dry run、live / lagged、no-op / 実変更、依存側 / 対象側の hold、障害、restart、重複、stale 変更を網羅します。`tests/fixtures/quint/dependency_liveness_traces.json` にITFを seed・境界・Quint の版・生成コマンドとともに保存します。テストが各トレースを生成し直して比較し、モデルを変えて `uv run python -m tests.quint_scenarios regenerate` を実行していなければ失敗します。`tests/fixtures/quint/dependency_liveness_fault_scenarios.json` には、モデルの誤動作シナリオを期待する不変条件と予定の step とともに保存します。

**既知の本番の反例（#1281・#1283）。** モデルは義務を述べ、本番がそれに違反します。探索ではモデルの `guard` を有効にして、乱択トレースがそれらを検査しないようにします（Hypothesis の machine も同じ場合を除外しています）。保存した3つの `defect_*` シナリオは guard なしで実行し、Issue・契約・step・ノードを登録した strict xfail のテストで再生します。その不一致だけが欠陥で、同じトレースの別の不一致や別のトレースの不一致は失敗です。

| 誤動作 | モデル: 違反する不変条件（シナリオ） | 再生: 違反する契約（保存シナリオ） |
|---|---|---|
| `promotion-suppressed` | `liveness`（`final_apply`） | `P3C-LIVENESS-BOUND`（`final_apply`） |
| `promotion-delayed` | `liveness`（`final_apply`） | `P3C-LIVENESS-BOUND`（`final_apply`） |
| `stale-evidence` | `safety`（`stale_revoke`） | `P3C-SAFETY`（`stale_revoke`） |
| `intermediate-ignored` | `liveness`（`chain_apply`） | `P3C-INTERMEDIATE-LIVENESS`（`chain_apply`） |
| `reservation-guard-bypass` | `safety`（`dependency_reservation_apply`） | `P3C-SAFETY`（`dependency_reservation_apply`） |
| `hold-guard-bypass` | `safety`（`base_red_apply`） | `P3C-SAFETY`（`base_red_apply`） |
| `event-only` | `eventMatchesLabel`、`liveness`（`final_apply`） | `P3C-LIVENESS-BOUND`（`final_apply`）。義務に反してイベントだけが出る場合は `P3C-SAFETY`（`base_red_apply`） |
| `dry-run-writes` | `dryRunReadOnly`（`final_dry`） | `P3C-DRYRUN-READONLY`（`final_dry`） |
| `empty-completion-set` | - | `P3C-LIVENESS-BOUND`（`recompute_release`） |
| `throwaway-context` | - | `P3C-LIVENESS-BOUND`（`lagged_apply`） |
| `action-mapping-swapped` | - | `P3C-QUINT-OBSERVATION`（`duplicate_completion`） |
| `action-mapping-dropped` | - | `P3C-SAFETY`（`restart_loses_ledger`） |

## 保証の範囲と限界

verified は、上の表の誤動作を、記述した決定的なシナリオで検出できることを意味します。ほかの誤動作がないこと、任意のグラフや無限の外乱列を網羅していること、ランダム探索が全状態に届くことは意味しません。監査で見つかった本番の欠陥は、反例付きの別Issueへ切り出し、strict xfail で固定します。監査を通すために期待値・公平性・上限を弱めることはしません。

## 再現

```bash
uv run pytest tests/test_verification_contracts.py tests/test_status_machine_stateful.py tests/test_status_reconciliation_stateful.py tests/test_consistency_status_repairs.py tests/test_status_events.py tests/test_status_events_stateful.py tests/test_status_event_retry_resume.py tests/test_dependency_liveness_stateful.py -n0 --hypothesis-profile ci
```

ランダムな失敗の再生は `--hypothesis-seed=<seed>` を使います。対照のシナリオにseedは不要です。

Quintモデルと再生（Node.js が必要です。`scripts/quint-check.sh` が固定したツールを導入します）:

```bash
./scripts/quint-check.sh                       # Windows: .\scripts\quint-check.ps1
uv run pytest tests/test_quint_dependency_replay.py -n0
uv run python -m tests.quint_scenarios regenerate   # モデルを変えたあと
```

1つのシナリオを手元で再実行するには、`tests/fixtures/quint/dependency_liveness_traces.json` のトレースの隣に保存した生成コマンド（`node_modules/.bin/quint run specs/quint/dependency_liveness.qnt --backend=typescript --init=init_<id> …`）を使います。探索のコマンド・seed・境界は `exploration-summary.json` にあります。
