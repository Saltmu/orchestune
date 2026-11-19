# 類似計画カード PoC 比較評価レポート（Issue #1874）

親 EPIC: [#1842](https://github.com/Saltmu/orchestune/issues/1842) / 比較契約: [#1872](https://github.com/Saltmu/orchestune/issues/1872)（[`planning-history-poc-protocol.md`](planning-history-poc-protocol.md)） / カード生成 CLI: [#1873](https://github.com/Saltmu/orchestune/issues/1873)（[`scripts/planning_history_poc.py`](../../scripts/planning_history_poc.py), commit `7c2292774bfb1721b38705ff3f136ace8f9c958a`） / 本評価 Issue: [#1874](https://github.com/Saltmu/orchestune/issues/1874) / 測定形式参照例: [#1869](https://github.com/Saltmu/orchestune/issues/1869)（[`context-poc-eval.md`](context-poc-eval.md)）

## 1. 概要・照合基準・責務境界

本レポートは、[#1872](https://github.com/Saltmu/orchestune/issues/1872) で定義されたデータ契約・比較評価手順および [#1873](https://github.com/Saltmu/orchestune/issues/1873) で実装された読み取り専用セレクタ CLI（`scripts/planning_history_poc.py`）を用い、過去の分解計画と記録された Outcome を根拠付きカード（`planning-history-cards-v1`）として提示することが親 EPIC の分解計画作成（品質およびトークン利用量）を改善するかを実測評価し、共有コンテキスト基盤拡張（`context-index-v2` への計画履歴統合と分解手順への任意接続）の採否を記録するものである。

- **照合基準**: fetch 済みの `origin/main` `cab843ebc20ef38a5a3c9a6f3740abc7aa2dc22d`（`cab843eb`、2026-11-19 JST）。
- **カード生成用 checkout と分解試行用 worktree の分離**: カード生成は `#1873`（`7c2292774bfb1721b38705ff3f136ace8f9c958a`）が統合された作業ブランチ HEAD `f97d9ae462b89314e440ef8607c10454ad08b96a` で実行し、各課題の分解試行（条件 A / 条件 B）は対象課題の実装前 `base_sha` をチェックアウトした独立・初期状態の detached `git worktree` で実行した。
- **関連 Issue との境界**: [#1869](https://github.com/Saltmu/orchestune/issues/1869)（子タスク起動時の限定コンテキスト注入 PoC）は測定記録形式の参照例としてのみ扱い、本レポートの数値と合算しない。また [#1844](https://github.com/Saltmu/orchestune/issues/1844) の計画構造指標は本比較の必須入力としない。

| 置き場所（リポジトリ相対パス） | 責務 |
| --- | --- |
| `docs/plans/planning-history-poc-eval.md`（本書） | 候補選定・除外理由、事前登録、固定資料・リクエスト・出力カードの SHA-256 と出典 manifest、全試行の実測値と盲検採点、ペア差と指標別分母、継続・再設計・見送りの判定根拠、参照先と保持期間を記録する。 |
| `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/` | Git 管理外の一意な評価セッション。固定した原本（`raw-sources/`）、固定資料（`corpus.json`）、検索リクエスト（`requests/`）、生成カード（`cards/`）、プロンプト（`prompts/`）、生計画（`raw-plans/`）、匿名化計画（`blind-plans/`）、実行イベントログ（`logs/`）、集計 JSON を保管する。 |

---

## 2. 候補選定・除外理由と事前登録（Pre-Registration）

### 2.1 候補母集団の棚卸しと選定・除外理由

`origin/main`（`cab843ebc20ef38a5a3c9a6f3740abc7aa2dc22d`）までの `Saltmu/orchestune` リポジトリにおいて、`orchestune:decomposition-plan` または `plan_format: manifest-v1` を含む親 EPIC / 分解候補 Issue 全 17 件を調査し、[`planning-history-poc-protocol.md` §6](planning-history-poc-protocol.md#6-実資料の採取出典照合手順corpus-curation--verification-protocol) の完全性・時刻証明・単一カード予算適合性に照らして以下のとおり採否を確定した（試行実行後の課題差し替えは一切行っていない）。

| 親 Issue | タイトル | 状態 / 完了日 (UTC) | 子タスク数 | `corpus.json` 採否 | 評価対象課題（Target）採否と選定・除外理由 |
| ---: | --- | --- | ---: | :---: | --- |
| `#1053` | `[EPIC] [FEAT] ledger-package: dispatch から共有基盤を ledger パッケージへ切り出す` | CLOSED (`2026-09-27`) | 5 | **採用 (`case-1053-rev1`)** | 履歴先輩例として corpus に収録（`done` 4 件 + Outcome 未記録 `unknown` 1 件を含むパッケージ分割の基準例）。初期履歴層を確保するため Target には含めない。 |
| `#1059` | `[EPIC] [FEAT] complete-status-transition: orchestune complete 実行時にステータスラベル遷移を完結させる` | CLOSED (`2026-09-28`) | 5 | **採用 (`case-1059-rev1`)** | 履歴先輩例として corpus に収録（全 5 件 `done`、CLI・ライフサイクル・ドキュメント同期の分解例）。初期履歴層確保のため Target には含めない。 |
| `#1106` | `[EPIC] [FEAT] active-worktree-subrecords: ActiveWorktree を所有者別サブレコードへ分割する` | CLOSED (`2026-10-02`) | 14 | **除外** | **除外理由（単一カード予算超過）**: 14 子タスク＋27 出典を持つため、正規化後の単一カード最小サイズが `16,272` bytes となり、既定 `--max-bytes 12288`（12 KiB）を単独で超過する。#1872 §4 Step 4 の末尾削除（`cards.pop()`）では Rank 1 が 12 KiB 超のとき Rank 2/3 も巻き込んで `over_budget`（0 枚）になるため除外。 |
| `#1219` | `[EPIC] #661 Phase 3: Eventモデル・retry/reclaim/review予算の終端性・依存解決liveness` | CLOSED (`2026-10-09`) | 3 | **採用 (`case-1219-rev1`)** | 履歴先輩例として corpus に収録（`done` 2 件 + `blocked` 1 件を含む失敗／ブロック履歴保持ケース）。初期履歴層確保のため Target には含めない。 |
| `#1033` | `[EPIC] 統合PRの第2層AIレビュー・修正ループを子統合の自動マージ必須ゲートにする` | CLOSED (`2026-10-22`) | 5 | **採用 (`case-1033-rev1`)** | 履歴先輩例として corpus に収録（ブロック後の再開を経て全 5 件 `done`）。初期履歴層確保のため Target には含めない。 |
| `#1374` | `[FEAT] parent-plan-manifest: 親Issueの分解計画を目次（manifest）化する` | CLOSED (`2026-10-24`) | 0 | **除外** | **除外理由（非親 EPIC）**: 本文中に `plan_format: manifest-v1` の説明例を含む単一機能 Issue であり、親分解 manifest を持たない。 |
| `#1392` | `[FEAT] plan-restore-from-children: provision --restore-plan で子Issue本文から復元する` | CLOSED (`2026-10-24`) | 0 | **除外** | **除外理由（非親 EPIC）**: `#1374` 関連の単一機能 Issue であり、親分解 manifest を持たない。 |
| `#1368` | `[EPIC] [FEAT] 各コマンドにリッチCLIを導入し、表示・進捗・診断結果を統一する` | CLOSED (`2026-10-30`) | 8 | **採用 (`case-1368-rev1`)** | 履歴先輩例として corpus に収録（`manifest-v1` 初代、`shared_contract: cli-output` 先行＋6 並列扇出＋集約、`done` 6 件 + `unknown` 2 件）。直後の `#1421` 以降の参照基盤とする。 |
| `#1421` | `[EPIC] [REFACTOR] model_tiers / model_tier を廃止し execution_profile に統合する` | CLOSED (`2026-10-31`) | 4 | **採用 (`case-1421-rev1`)** | **評価対象 Target 1（選定）**: 設定・モデル・Provisioning・日英 docs/skills 横断の廃止／統合リファクタリング。`as_of` 前に適格履歴 5 件が存在。 |
| `#1417` | `[EPIC] [企画] Luna等のモデルでもlocal-ci-developerの手順を遵守できる実行制御と評価を設計する` | CLOSED (`2026-11-01`) | 0 | **除外** | **除外理由（manifest 子タスク未登録）**: 企画 Issue であり、本文の分解ブロックが `plan_format: null`, `subtasks: []` で子タスク定義を持たない。 |
| `#1398` | `[EPIC] RunStateのcompletion系台帳をdictから型付きモデルへ移行` | CLOSED (`2026-11-02`) | 2 | **採用 (`case-1398-rev1`)** | 履歴先輩例として corpus に収録（`shared_contract: typed-runstate-ledgers` の 2 段階直列移行）。2 サブタスクと小規模なため Target にはせず、後続のリファクタリング EPIC 群の参照履歴とする。 |
| `#1588` | `[EPIC] [REFACTOR] dispatch パッケージを cycle / semantic_review / doctor などのサブパッケージに分ける` | CLOSED (`2026-11-03`) | 8 | **採用 (`case-1588-rev1`)** | **評価対象 Target 2（選定）**: 巨大パッケージのサブパッケージ分割と層テーブル・string-patch baseline 更新（8 サブタスク）。`#1053`（`dispatch -> ledger` 分割）が先行履歴として存在する。 |
| `#1589` | `[EPIC] [REFACTOR] 引数 10 個以上の関数をパラメータオブジェクト化し、PLR0913 を baseline 付きで有効にする` | CLOSED (`2026-11-04`) | 7 | **採用 (`case-1589-rev1`)** | **評価対象 Target 3（選定）**: baseline 導入先行＋サブシステム横断の段階的リファクタリング（7 サブタスク）。 |
| `#1590` | `[EPIC] [REFACTOR] 広い except Exception を棚卸しし、想定例外への絞り込みと握りつぶしの理由明記を行う` | CLOSED (`2026-11-06`) | 8 | **採用 (`case-1590-rev1`)** | **評価対象 Target 4（選定）**: Ruff `BLE001` 導入とパッケージ別例外絞り込み（8 サブタスク）。 |
| `#1730` | `[EPIC] GitHub設定不要で1件を完走できるローカル初回体験` | CLOSED (`2026-11-08`) | 5 | **採用 (`case-1730-rev1`)** | **評価対象 Target 5（選定）**: `shared_contract: local-trial-v1` 先行＋workspace/runner 並列実装＋ドキュメント・スモーク検証（`done` 4 件 + `blocked` 1 件）。 |
| `#1230` | `[EPIC] stale-read（read/write競合）の検出と下流タスク再検証` | CLOSED (`2026-11-09`) | 18 | **除外** | **除外理由（単一カード予算超過）**: 18 子タスク＋37 出典を持つため、正規化後の単一カード最小サイズが `21,744` bytes となり、既定 `--max-bytes 12288` を単独で大幅超過するため除外。 |
| `#1841` | `[EPIC] [企画] ScaleMASを参考に共有成果の限定検索とコンテキスト構成を設計する` | CLOSED (`2026-11-17`) | 4 | **採用 (`case-1841-rev1`)** | **評価対象 Target 6（選定）**: `shared_contract: shared-artifact-v1` 先行＋索引/検索＋起動注入＋PoC 評価文書（4 サブタスク）。`#1730`（契約先行＋実装＋評価であり `blocked` タスクを含む）が直近の類似履歴として存在する。 |
| `#1842` | `[EPIC] [企画] WorkflowOpsを参考に実行履歴を次の分解計画へ還元する` | OPEN（本 EPIC） | 3 | **除外** | **除外理由（自己系列・未完了）**: 本評価 `#1874` の親 EPIC 自身であるため除外。 |

**候補数の記録**: `manifest-v1` 導入以降の完了済み親 EPIC は全 9 件（`#1368, #1421, #1398, #1588, #1589, #1590, #1730, #1230, #1841`）であり、うち単一カードが 12 KiB 予算を超える `#1230`（18 タスク）、履歴基盤の初代である `#1368`、および 2 タスク最小構成の `#1398` を除く **全 6 件（`#1421, #1588, #1589, #1590, #1730, #1841`）** を評価対象課題（5〜10 件の範囲内）として事前登録した。

### 2.2 事前登録（Pre-Registration）固定パラメータ

| 項目 | 事前登録値 |
| --- | --- |
| 事前登録マニフェスト (`pre_registration_manifest.json`) | `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/pre_registration_manifest.json`（固定時刻: `2026-11-19T02:05:03Z`, SHA-256: `b7c8bd199da96c581dbe6a675e1ddb682b6ce50d29bbf531125c5b0380b06ac5`） |
| 固定資料 (`corpus.json`) | `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/corpus.json`（`120,615` bytes, 全 `12` cases, SHA-256: `6c9fac76039321bf2d2c33307524b7258128e52ba0312b82b1fd2bb5d4919ae7`） |
| カード生成用 checkout SHA | `f97d9ae462b89314e440ef8607c10454ad08b96a`（`#1873` 実装コミット `7c2292774bfb1721b38705ff3f136ace8f9c958a` を含む作業ツリー） |
| セレクタ CLI と予算設定 | `uv run python -m scripts.planning_history_poc --corpus <corpus.json> --request <request.json> --limit 3 --max-bytes 12288`（BM25: `TOKENIZER_VERSION="char-bigram-v1"`, `BM25_PARAMS_VERSION="bm25-v1"`） |
| 分解試行実行エンジン・モデル設定 | `codex-cli 0.169.0`、ユーザー設定固定モデル `model = "gpt-6.1-sol"`、`model_reasoning_effort = "high"`（`codex exec --json --sandbox read-only --ephemeral --skip-git-repo-check`） |
| 試行隔離とリーク防止 | 各試行ごとに課題の実装前 `base_sha` で独立した detached `git worktree` を新規作成し、`GH_HOST=offline.invalid`, `GH_TOKEN=""`, `GITHUB_TOKEN=""` でライブ GitHub API を遮断。条件 B の出典参照用には `.eval-adopted-sources/` に固定原本のみを配置。 |
| 時間・トークン予算と再実行規則 | 1 試行あたりプロセス実行上限 `240` 秒（マニフェスト上限 `300` 秒）、シェル探索は対象モジュール確認の `1〜3` 回以内を指示。再実行は事前登録した同一条件の技術的失敗（初期化不備・プロセス中断）に限定し、原試行の記録も保持する。 |
| 条件割当順序（AB / BA 交互） | 課題番号順に交互割当: `#1421 (A→B)`, `#1588 (B→A)`, `#1589 (A→B)`, `#1590 (B→A)`, `#1730 (A→B)`, `#1841 (B→A)` |
| 盲検採点ルーブリック | [`planning-history-poc-protocol.md` §7.2](planning-history-poc-protocol.md#72-盲検採点ルーブリック4-項目--02-点--合計-8-点満点) の 4 項目（1. 必須要件網羅 `requirement_coverage`, 2. 実コードと整合する変更範囲 `footprint_alignment`, 3. 根拠のある依存と DAG `dependency_dag_validity`, 4. 検証計画 `verification_plan`）各 `0〜2` 点（合計 `0〜8` 点）＋リーク監査・必須要件落ち監査。盲検 ID `blind-01`〜`blind-12` への事前シャッフル割当と `## Reference Card Adoption` 除去により条件名を伏せて採点。 |

### 2.3 代表 6 課題の事前登録仕様（開始前 `base_sha`・`as_of`・除外集合・ダイジェスト・完全要件）

各課題の開始前 `base_sha` は、最初の実装子 Issue が起票される直前の `origin/main` first-parent コミットとし、対象系列（`exclude_issue_numbers` および `exclude_pr_numbers`）のコミットが `base_sha` の祖先に 1 件も含まれないことを `git log <base_sha>` で検証済みである。

| # | 対象親 Issue | 交互順序 | 開始前 `base_sha`（分解試行 worktree） | 評価基準時刻 `as_of` (UTC) | 対象系列除外 `exclude_issue_numbers` | 対象系列除外 `exclude_pr_numbers` | リクエスト JSON SHA-256 (`bytes`) |
| :---: | ---: | :---: | --- | --- | --- | --- | --- |
| 1 | `#1421` | `A → B` | `95822058eec1243bdec3d13b710a64dee05597ab` | `2026-10-30T12:40:00Z` | `[1421, 1472, 1473, 1474, 1475]` | `[1476, 1479, 1483, 1485]` | `62ae415f0c78f4e04645ac4e36cff9e1bc9a091ddf7c9178fe70bae8998356e8` (`18143` B) |
| 2 | `#1588` | `B → A` | `64ea517732c31a419fbfce291a2332ce88effe4d` | `2026-11-03T07:05:00Z` | `[1588, 1614, 1615, 1616, 1617, 1618, 1619, 1620, 1621]` | `[1623, 1626, 1628, 1633, 1635, 1639, 1641, 1645]` | `08f9b2a4d92596f777ccc6c8e970044610726ec35f4bc0a8cd85b92fbf462ea0` (`8733` B) |
| 3 | `#1589` | `A → B` | `9b8806397d830e0b6ea8009d5a722cc89a7c282c` | `2026-11-04T15:30:00Z` | `[1589, 1661, 1662, 1663, 1664, 1665, 1666, 1667]` | `[1668, 1670, 1672, 1674, 1676, 1678, 1680]` | `fdea97420e4ebf11ff7d23b9c8bf13f920db78dfec0134bb2b213601ca629af0` (`8456` B) |
| 4 | `#1590` | `B → A` | `dd78adc1a64d92271209b4aaff117cea13ad3c68` | `2026-11-05T03:50:00Z` | `[1590, 1683, 1684, 1685, 1686, 1687, 1688, 1689, 1708, 1710]` | `[1690, 1692, 1694, 1696, 1699, 1701, 1704, 1709]` | `eb7cbc8dd41afd169fb63947e80a052fe62763c7894100858cf56a402e85e0e0` (`6865` B) |
| 5 | `#1730` | `A → B` | `840a99ce3bc3e92488f9b719690efff86d76fdf5` | `2026-11-07T10:40:00Z` | `[1730, 1734, 1735, 1736, 1737, 1738, 1746, 1751, 1752, 1753, 1754, 1757]` | `[1739, 1741, 1742, 1747]` | `0539d10da56ce2a4ddfa0000d58eb40dca61b0109dcd3957a353b70c568244cd` (`28812` B) |
| 6 | `#1841` | `B → A` | `2309bef03661af822625708844c1a485dbc1232a` | `2026-11-14T23:40:00Z` | `[1841, 1851, 1852, 1853, 1854, 1868, 1869]` | `[1855, 1857, 1861, 1863]` | `04d014da883eb0281a9f989eac7884536d3b287f64065c0961b17c9799982563` (`21021` B) |

#### 各課題の固定要件概要と受入条件（`requests/task-<N>-request.json` に全文固定）

1. **`#1421` (`[EPIC] [REFACTOR] model_tiers / model_tier を廃止し execution_profile に統合する`)**:
   - **要件**: `orchestune.toml` / `pyproject.toml` の `[model_tiers]` と Issue/計画の `model_tier` を廃止し、`execution_profile`（`orchestune/dispatch/execution_profiles.py`）による target 別の `(model, reasoning_effort)` 解決に一本化する。古い `model_tiers` / `model_tier` 入力は移行案内付きの明確なエラーで拒否し、機械可読出力（JSON/YAML/テンプレート）から `model_tier` を除去し、日英 `usage.md` と `skills/orchestune/SKILL.md` を更新する。
   - **主な受入条件**: (a) `resolve_execution_selection` への一本化と `model_tiers.py` 廃止、(b) 設定・計画・Issue・テンプレートでの旧キー拒否、(c) 機械可読出力からの `model_tier` 除去、(d) 日英ドキュメントとスキル更新および `./scripts/local-ci.sh` 通過。
2. **`#1588` (`[EPIC] [REFACTOR] dispatch パッケージを cycle / semantic_review / doctor などのサブパッケージに分ける`)**:
   - **要件**: 67 モジュールを抱える `orchestune/dispatch/` を責務別の 8 サブパッケージ（`doctor`, `reporting`, `recovery`, `launch`, `phase`, `semantic_review`, `cycle`, `config`）へ段階的に分割し、パッケージ間循環依存ゼロ（`test_architecture.py`）、層定義テーブル（日英 `architecture.md` と `architecture_layers_support.py`）、状態ラベル遷移 callsite レジストリ（`test_status_transition_callsites.py`）、および `.orchestune/string-patch-baseline.json` を各段階で整合させる。
   - **主な受入条件**: (a) 8 サブパッケージへの移動と `orchestune/dispatch/` 直下の整理、(b) 既存の CLI エントリポイント互換性維持、(c) 層検査・private import 検査・string-patch baseline 更新と標準 local CI 通過。
3. **`#1589` (`[EPIC] [REFACTOR] 引数 10 個以上の関数をパラメータオブジェクト化し、PLR0913 を baseline 付きで有効にする`)**:
   - **要件**: Ruff `PLR0913`（`max-args = 9`）を有効化して進行防止テスト・baseline を導入した上で、`claim`, `complete`, `consistency`, `provisioning`, `integrator`, `dispatch`, `scripts` に存在する引数 10 個以上の関数（30 箇所）を `dataclass(frozen=True, slots=True)` 等のパラメータオブジェクトへ段階的に置換し、最終的に baseline を空にして撤去する。
   - **主な受入条件**: (a) `PLR0913` 設定と上限検査テスト導入、(b) サブシステム別の引数 10 個以上関数のパラメータオブジェクト化と呼出側・テスト追随、(c) 全件解消後の baseline ファイル撤去と local CI 通過。
4. **`#1590` (`[EPIC] [REFACTOR] 広い except Exception を棚卸しし、想定例外への絞り込みと握りつぶしの理由明記を行う`)**:
   - **要件**: `orchestune/` と `scripts/` の広い `except Exception`（79 箇所）を棚卸しし、Ruff `BLE001` を段階的に有効化する。具体的な想定例外タプルへ絞り込める箇所は置換し、最上位境界やベストエフォート後始末で広い捕捉を残す箇所には `# noqa: BLE001 - boundary: <理由>` を明記し、完全な握りつぶし箇所は診断記録へ置き換える。
   - **主な受入条件**: (a) `pyproject.toml` での `BLE001` 有効化とパッケージ別の `per-file-ignores` 段階解消、(b) `infra`, `consistency`, `claim`, `complete`, `integrator`, `dispatch` 等の例外絞り込みと境界理由コメント付与、(c) `uv run ruff check --select BLE .` および `./scripts/local-ci.sh` 通過。
5. **`#1730` (`[EPIC] GitHub設定不要で1件を完走できるローカル初回体験`)**:
   - **要件**: GitHub 認証・Issue 起票なしでローカル Git リポジトリ単独で「初期設定 (`trial init`) → 課題入力 → 計画 → 実装 → ローカル CI 検証 → 修正ループ（最大 3 回）→ 結果サマリ (`trial run` / `trial status` / `trial clean`)」を 1 件完走できる `orchestune/local_trial` パッケージと CLI サブコマンド（`orchestune trial`）、初回ガイド、およびスモーク評価記録を実装する。
   - **主な受入条件**: (a) 共有 DTO・状態スキーマ (`local-trial-v1`) と層登録、(b) ローカル設定・worktree 準備・ロック管理、(c) エージェント実行・有限修正ループ・現 HEAD に紐付いた CI 完了証明、(d) CLI 接続・日英 First-Run ガイド・スモーク評価と local CI 通過。
6. **`#1841` (`[EPIC] [企画] ScaleMASを参考に共有成果の限定検索とコンテキスト構成を設計する`)**:
   - **要件**: GitHub Issue / PR / Outcome コメントを正本（SoT）として正規化共有成果レコード（`shared-artifact-v1`）を抽出し、再構築可能なローカル SQLite 索引（`context-index.sqlite3`）と決定論的 BM25 検索（`char-bigram-v1`）・UTF-8 バイト予算調整（`fit_context_budget`）、必須 Tier 1＋限定 Tier 2 の起動コンテキスト注入（`context.json`）および対話型 `orchestune context export` CLI、評価マニフェストとドキュメントを整備する。
   - **主な受入条件**: (a) `shared-artifact-v1` 抽出契約と出典・訂正管理、(b) ローカル索引再構築・BM25 検索・バイト予算 CLI、(c) `dispatch` 起動注入と対話型 export 経路（必須 Tier 1 非脱落保証）、(d) PoC 評価マニフェスト固定と日英 `usage.md` / `SKILL.md` 同期。

---

## 3. 固定資料・出典 Manifest と #1873 セレクタ決定的検証

### 3.1 固定資料（`corpus.json`）収録 12 Cases 一覧

採取したすべての Issue 本文および Outcome コメントは `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/raw-sources/` に UTF-8 生バイト列として保存し、事後編集（`updated_at > created_at`）がある記録にはすべて編集完了時刻 `updated_at` を `source.available_at` として割り当てた（過去の `created_at` の流用は `0` 件）。また `restore_plan_from_parent` による現在の厳格テンプレート復元可否を併せて確認した。

| `case_id` | `parent_issue` | `available_at` (UTC) | `plan_revision_digest` (SHA-256) | タスク数 | Outcome 内訳 (`done` / `blocked` / `unknown`) | 出典数 (`plan` / `task` / `outcome`) | `restore_plan_from_parent` 現契約互換性 |
| --- | ---: | --- | --- | ---: | --- | --- | --- |
| `case-1053-rev1` | `#1053` | `2026-09-27T04:36:49Z` | `e3288d1fa61137252196670a0ea7a754926d3f5ac511842676f512adbac9b7a2` | 5 | `4` / `0` / `1` | `1` / `5` / `4` (計 10) | 対象外（`manifest-v1` 導入前の旧形式分解ブロック） |
| `case-1059-rev1` | `#1059` | `2026-09-28T23:55:56Z` | `aa5891a574d5a61f4ede0c07663034cc4cd888f60256b8ff7ccacf4e862e43b7` | 5 | `5` / `0` / `0` | `1` / `5` / `5` (計 11) | 対象外（`manifest-v1` 導入前の旧形式分解ブロック） |
| `case-1219-rev1` | `#1219` | `2026-10-09T15:59:51Z` | `9c347e51e7ff4bb92c99a95c9ab7a42f480ffd3fc66710623ab1afedb7acc40a` | 3 | `2` / `1` / `0` | `1` / `3` / `3` (計 7) | 対象外（`manifest-v1` 導入前の旧形式分解ブロック） |
| `case-1033-rev1` | `#1033` | `2026-10-22T23:56:21Z` | `f2eb92c00eb037d692b7367ec3060a2f4b3fad9a893c6a38075403cc4f935b59` | 5 | `5` / `0` / `0` | `1` / `5` / `5` (計 11) | 対象外（`manifest-v1` 導入前の旧形式分解ブロック） |
| `case-1368-rev1` | `#1368` | `2026-10-30T12:17:01Z` | `8e883a3ef6bbcb618e876e962bbed1cb5c366b2aa146afdc2e2e3d0657c49c21` | 8 | `6` / `0` / `2` | `1` / `8` / `6` (計 15) | 非互換（退役済み `model_tier` キー拒否） |
| `case-1421-rev1` | `#1421` | `2026-10-31T14:12:41Z` | `35b7886034c12e397f30f6f8b5d344df6af8c1ef3d835625046cabd61e958153` | 4 | `4` / `0` / `0` | `1` / `4` / `4` (計 9) | 非互換（子 `#1472` 本文行数不一致） |
| `case-1398-rev1` | `#1398` | `2026-11-02T00:59:18Z` | `d314e7b8fbd3f49909cc0c6b84893f7cd1c9779e71492dd82f723025566aae67` | 2 | `2` / `0` / `0` | `1` / `2` / `2` (計 5) | 成功（`manifest-v1` 完全一致） |
| `case-1588-rev1` | `#1588` | `2026-11-03T12:18:53Z` | `caa028a923b563c891b7f598708e1ec6f94a2507b2db3a4e93fa72bb3342efd0` | 8 | `8` / `0` / `0` | `1` / `8` / `8` (計 17) | 成功（`manifest-v1` 完全一致） |
| `case-1589-rev1` | `#1589` | `2026-11-04T22:52:45Z` | `2191e8b40632a6e1a4ddeb5aa09f5c5f9bf06d9ec161605b076837eb57ac5f19` | 7 | `7` / `0` / `0` | `1` / `7` / `7` (計 15) | 成功（`manifest-v1` 完全一致） |
| `case-1590-rev1` | `#1590` | `2026-11-06T09:16:17Z` | `d03198743e52cd691e2bed45bc979cf15cc0ea11a3a6f78594e28a83a4dd453d` | 8 | `8` / `0` / `0` | `1` / `8` / `8` (計 17) | 成功（`manifest-v1` 完全一致） |
| `case-1730-rev1` | `#1730` | `2026-11-08T12:18:23Z` | `8e7fa878a23844ffd99266313f69b8dc44ed3b3b8895e350e522dca7173f4947` | 5 | `4` / `1` / `0` | `1` / `5` / `5` (計 11) | 成功（`manifest-v1` 完全一致） |
| `case-1841-rev1` | `#1841` | `2026-11-17T22:49:52Z` | `237083cc816836bbc62e165ae1d09612ad94ec644c43e6313bbcf6886b05146b` | 4 | `4` / `0` / `0` | `1` / `4` / `4` (計 9) | 非互換（子 `#1851` に設計レビュー見出し追記） |

### 3.2 採用カードと出典（Sources）の完全追跡 Manifest

代表 6 課題に対して `#1873` セレクタ（`scripts/planning_history_poc.py --limit 3 --max-bytes 12288`）が生成したカードと、再実行による決定的出力照合結果は以下のとおりである。全 6 課題で `status="ready"`、`returned_count=1`（BM25 上位 3 件のうち 2〜3 位の 2 枚は `--max-bytes 12288` 予算調整により `budget_dropped=2` として安全に削減）、対象系列リーク `0` 件、時刻違反（`available_at >= as_of`）`0` 件、再実行 SHA-256 完全一致を確認した。

| 対象課題 | `as_of` (UTC) | `excluded_counts` (`repo` / `lineage` / `after_as_of` / `superseded`) | `status` (`returned` / `dropped`) | 採用カード `case_id` (`parent_issue`) | 採用カード `available_at` | 採用カード `plan_revision_digest` | 出力カード SHA-256 (`injected_bytes`) | 検索時間 `search_ms` (秒) |
| ---: | --- | --- | --- | --- | --- | --- | --- | ---: |
| `#1421` | `2026-10-30T12:40:00Z` | `0` / `1` / `6` / `0` | `ready` (`1` / `2`) | `case-1368-rev1` (`#1368`) | `2026-10-30T12:17:01Z` | `8e883a3ef6bbcb618e876e962bbed1cb5c366b2aa146afdc2e2e3d0657c49c21` | `3f18786620f8d2e51bbf8cbf4cf4d33c135554a6a4799ac2d45660f03730b0c9` (`10847` B) | `139.61 ms` (`0.1396 s`) |
| `#1588` | `2026-11-03T07:05:00Z` | `0` / `1` / `4` / `0` | `ready` (`1` / `2`) | `case-1053-rev1` (`#1053`) | `2026-09-27T04:36:49Z` | `e3288d1fa61137252196670a0ea7a754926d3f5ac511842676f512adbac9b7a2` | `7958c70377336a97a767f4ac3af38c91a5ea5b3f2a85c9eff23e8dea314b7608` (`7322` B) | `129.04 ms` (`0.1290 s`) |
| `#1589` | `2026-11-04T15:30:00Z` | `0` / `1` / `3` / `0` | `ready` (`1` / `2`) | `case-1588-rev1` (`#1588`) | `2026-11-03T12:18:53Z` | `caa028a923b563c891b7f598708e1ec6f94a2507b2db3a4e93fa72bb3342efd0` | `c9110b6528ae8677121d3c52792863f4e4a0b2e8336bf81f8819a160bfeb511d` (`10071` B) | `128.39 ms` (`0.1284 s`) |
| `#1590` | `2026-11-05T03:50:00Z` | `0` / `1` / `2` / `0` | `ready` (`1` / `2`) | `case-1589-rev1` (`#1589`) | `2026-11-04T22:52:45Z` | `2191e8b40632a6e1a4ddeb5aa09f5c5f9bf06d9ec161605b076837eb57ac5f19` | `4631c895ba8061cdc8ebe4cf9c54ad90cfa57556cbbc3e7a73290acc5023d433` (`9691` B) | `131.47 ms` (`0.1315 s`) |
| `#1730` | `2026-11-07T10:40:00Z` | `0` / `1` / `1` / `0` | `ready` (`1` / `2`) | `case-1059-rev1` (`#1059`) | `2026-09-28T23:55:56Z` | `aa5891a574d5a61f4ede0c07663034cc4cd888f60256b8ff7ccacf4e862e43b7` | `d6a6dc4b8d2e2ac555c649a10e3db84c3674b3e0ee1fb0b661ae988b33733716` (`7810` B) | `137.13 ms` (`0.1371 s`) |
| `#1841` | `2026-11-14T23:40:00Z` | `0` / `1` / `0` / `0` | `ready` (`1` / `2`) | `case-1730-rev1` (`#1730`) | `2026-11-08T12:18:23Z` | `8e7fa878a23844ffd99266313f69b8dc44ed3b3b8895e350e522dca7173f4947` | `294bb6ea539923998d04e90a93dc75ffd09745165fb581f67ff20e0bd2c49f75` (`7230` B) | `136.43 ms` (`0.1364 s`) |

#### 採用された 6 カード（`case-1053-rev1`, `case-1059-rev1`, `case-1368-rev1`, `case-1588-rev1`, `case-1589-rev1`, `case-1730-rev1`）の全出典（`Source`）ダイジェスト一覧

条件 B で注入された全 6 カードが参照する `plan` / `task` / `outcome` 出典（計 77 件）の URL、`available_at`、および固定原本（`.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/raw-sources/`）の UTF-8 生バイト SHA-256 は以下のとおりである。

<details>
<summary>採用 6 カードの全 77 出典（Source ID / Kind / URL / available_at / UTF-8 SHA-256 Digest）を展開</summary>

| `case_id` | `source_id` | `kind` | `available_at` (UTC) | `url` | `digest` (UTF-8 raw bytes SHA-256) |
| --- | --- | :---: | --- | --- | --- |
| `case-1053-rev1` | `case-1053-outcome-1052-5846395796` | `outcome` | `2026-09-26T12:48:18Z` | https://github.com/Saltmu/orchestune/issues/1052#issuecomment-5846395796 | `c0463dc07219c17747f5aa9491c355d9631f07f3b21898e7127a7e5d6db60664` |
| `case-1053-rev1` | `case-1053-outcome-1060-5851238724` | `outcome` | `2026-09-27T00:23:26Z` | https://github.com/Saltmu/orchestune/issues/1060#issuecomment-5851238724 | `693ac4e7d973324a834c6d319f7458976178beaa56025532df9f27a68656002a` |
| `case-1053-rev1` | `case-1053-outcome-1061-5851584876` | `outcome` | `2026-09-27T01:21:48Z` | https://github.com/Saltmu/orchestune/issues/1061#issuecomment-5851584876 | `c076c04b1284b54936854584ea5b2a3655d5d117e1273463ae9a06fa4e50a816` |
| `case-1053-rev1` | `case-1053-outcome-1062-5851831533` | `outcome` | `2026-09-27T02:04:14Z` | https://github.com/Saltmu/orchestune/issues/1062#issuecomment-5851831533 | `74efaaf7ee12f1a2444e19227671f4285b4b602acc84f7a79a4b83c1987873ab` |
| `case-1053-rev1` | `case-1053-plan-1053` | `plan` | `2026-09-27T04:28:47Z` | https://github.com/Saltmu/orchestune/issues/1053 | `07b1cbab00e77eec47a46a98a523391c8c2c3658f9494546384c7940b35cafed` |
| `case-1053-rev1` | `case-1053-task-1052` | `task` | `2026-09-26T14:28:22Z` | https://github.com/Saltmu/orchestune/issues/1052 | `95e7c09c2b434c13564b74decd548af0bd4c19c7cd94a33d70b04533dbfc6385` |
| `case-1053-rev1` | `case-1053-task-1060` | `task` | `2026-09-27T00:53:49Z` | https://github.com/Saltmu/orchestune/issues/1060 | `f7ab6108e21aa2f0840bf8189d13b44812ee8bbaba35c3b0f56524040b9660e7` |
| `case-1053-rev1` | `case-1053-task-1061` | `task` | `2026-09-27T01:36:00Z` | https://github.com/Saltmu/orchestune/issues/1061 | `6054f34e66898ae81cb8572ba2f3352bb63e093a7cfe0c0d4611b557b2f86206` |
| `case-1053-rev1` | `case-1053-task-1062` | `task` | `2026-09-27T03:05:41Z` | https://github.com/Saltmu/orchestune/issues/1062 | `2a081639346ce12acf7cbd6dd9942365e948b143dfe360433cc3ee90b64237d9` |
| `case-1053-rev1` | `case-1053-task-1063` | `task` | `2026-09-27T04:36:49Z` | https://github.com/Saltmu/orchestune/issues/1063 | `9873c8af580756f0ddace71d7ba32e80a7357a84d23e4cce8b4cf17b3c8eb953` |
| `case-1059-rev1` | `case-1059-outcome-1108-5861470899` | `outcome` | `2026-09-28T00:57:17Z` | https://github.com/Saltmu/orchestune/issues/1108#issuecomment-5861470899 | `e74a340c43229ca051580b06b971e5a377ffa790d38b1dffc821e2f7a2d45a77` |
| `case-1059-rev1` | `case-1059-outcome-1109-5862777902` | `outcome` | `2026-09-28T03:28:46Z` | https://github.com/Saltmu/orchestune/issues/1109#issuecomment-5862777902 | `cea74d9bbf1559098328d5b35d9f00aea3454e79fd62f3744beedac873c8f927` |
| `case-1059-rev1` | `case-1059-outcome-1110-5863675059` | `outcome` | `2026-09-28T04:50:52Z` | https://github.com/Saltmu/orchestune/issues/1110#issuecomment-5863675059 | `422b0e23581d5a9309a086000291d27feb91f4ec819b69a4187c8cb499bcc909` |
| `case-1059-rev1` | `case-1059-outcome-1111-5871948421` | `outcome` | `2026-09-28T14:24:59Z` | https://github.com/Saltmu/orchestune/issues/1111#issuecomment-5871948421 | `2458f9d5955919c1871ad6718aa60876cbe3bd45e5ec83bcaa880cf61a813520` |
| `case-1059-rev1` | `case-1059-outcome-1112-5880287670` | `outcome` | `2026-09-28T23:02:31Z` | https://github.com/Saltmu/orchestune/issues/1112#issuecomment-5880287670 | `83322357454e8d3c145fb4d71e48a89745f0610e4b6aaf6c35a5b982cb76f8c0` |
| `case-1059-rev1` | `case-1059-plan-1059` | `plan` | `2026-09-28T23:55:56Z` | https://github.com/Saltmu/orchestune/issues/1059 | `ad58d5059ac4fcdc142c506c15165e67e825fd7014aeb1f137ed71cf532b9430` |
| `case-1059-rev1` | `case-1059-task-1108` | `task` | `2026-09-28T03:04:48Z` | https://github.com/Saltmu/orchestune/issues/1108 | `b0f5406f67ce4954f9612809957dc5114ffb07b034dde4e0c3f50cb90cc81efd` |
| `case-1059-rev1` | `case-1059-task-1109` | `task` | `2026-09-28T03:33:07Z` | https://github.com/Saltmu/orchestune/issues/1109 | `ed524784fb8dd1aad97a7a1b2481192f2fb6a25a5460e4b66e4a095814c73811` |
| `case-1059-rev1` | `case-1059-task-1110` | `task` | `2026-09-28T05:53:45Z` | https://github.com/Saltmu/orchestune/issues/1110 | `634c4760d12f7651d8d218e8e961c8f7e3d1b32d8ccb985f645823aee8565924` |
| `case-1059-rev1` | `case-1059-task-1111` | `task` | `2026-09-28T14:39:07Z` | https://github.com/Saltmu/orchestune/issues/1111 | `11e7c2d4a94dbb1a66f2948f8a826a7dcd880acba9ab3e802ea264eb0dcffb9b` |
| `case-1059-rev1` | `case-1059-task-1112` | `task` | `2026-09-28T23:03:36Z` | https://github.com/Saltmu/orchestune/issues/1112 | `b7ecf9797d90ea3a6fa3f605549fcdb423dbeabaa85aec497978dddfa0af75fd` |
| `case-1368-rev1` | `case-1368-outcome-1408-6397119394` | `outcome` | `2026-10-25T11:34:55Z` | https://github.com/Saltmu/orchestune/issues/1408#issuecomment-6397119394 | `e4f3f98cc7fe055b7a436d9567f321a9feae3162ca78fc631700804f72f57101` |
| `case-1368-rev1` | `case-1368-outcome-1409-6398112233` | `outcome` | `2026-10-25T13:15:35Z` | https://github.com/Saltmu/orchestune/issues/1409#issuecomment-6398112233 | `d04bdf285a6bf399d41a681e5c8a03f8fc24349a7e426dbccdf582d0bece8a54` |
| `case-1368-rev1` | `case-1368-outcome-1410-6397584549` | `outcome` | `2026-10-25T12:23:03Z` | https://github.com/Saltmu/orchestune/issues/1410#issuecomment-6397584549 | `69445640eea9f14a4cc7801279f4ba12f1729a0c5bf69d0e0d169dfe39f6e56b` |
| `case-1368-rev1` | `case-1368-outcome-1411-6398148539` | `outcome` | `2026-10-25T13:19:11Z` | https://github.com/Saltmu/orchestune/issues/1411#issuecomment-6398148539 | `4a39a023623e60ac0e2f28fd37a24f83139ebca240f600e87fd90d652d0041cc` |
| `case-1368-rev1` | `case-1368-outcome-1414-6397716098` | `outcome` | `2026-10-25T12:36:25Z` | https://github.com/Saltmu/orchestune/issues/1414#issuecomment-6397716098 | `d6fd28d494649a254f9fb8bb6a0cf4f9314cc2cc9c635c9aab19fb7e662c02d0` |
| `case-1368-rev1` | `case-1368-outcome-1415-6399390295` | `outcome` | `2026-10-25T15:16:22Z` | https://github.com/Saltmu/orchestune/issues/1415#issuecomment-6399390295 | `1b8412e25c1b5bc3229c2b6bf7dbfd3ef2e9e25bb2f7f4a49780d4dee12767d7` |
| `case-1368-rev1` | `case-1368-plan-1368` | `plan` | `2026-10-30T12:17:01Z` | https://github.com/Saltmu/orchestune/issues/1368 | `92c00f0e9449b48378e151c134d967b355cc45164d9707082c669b5b227a7064` |
| `case-1368-rev1` | `case-1368-task-1408` | `task` | `2026-10-25T11:58:38Z` | https://github.com/Saltmu/orchestune/issues/1408 | `4e5ee6f5fc5fbce69b910061b1fb773a9dd892bfb78dd17840f7836a0b779179` |
| `case-1368-rev1` | `case-1368-task-1409` | `task` | `2026-10-25T14:33:32Z` | https://github.com/Saltmu/orchestune/issues/1409 | `e22815587531b6de4c30b6c5442aa715615bc938c877b12a513fcc6a4431433d` |
| `case-1368-rev1` | `case-1368-task-1410` | `task` | `2026-10-25T14:33:35Z` | https://github.com/Saltmu/orchestune/issues/1410 | `868c850e692ae0eaabb796c93962c6b923e0fd18f64733dc4ac3eff6e5f5efeb` |
| `case-1368-rev1` | `case-1368-task-1411` | `task` | `2026-10-25T14:33:38Z` | https://github.com/Saltmu/orchestune/issues/1411 | `befd72a775b6bc2427f3a56fe57f3d259933baed552fd4fd9cfb09a7423ed686` |
| `case-1368-rev1` | `case-1368-task-1412` | `task` | `2026-10-25T23:58:54Z` | https://github.com/Saltmu/orchestune/issues/1412 | `1e60066d2052dc64690c3769cd48fbdf7d79e0bb03ad41aff0c1dfe67d4b8746` |
| `case-1368-rev1` | `case-1368-task-1413` | `task` | `2026-10-25T23:58:51Z` | https://github.com/Saltmu/orchestune/issues/1413 | `7893e1b5dfa2f9fad7a0695aef503b7999a2d6e8ff5c4970de298d076f3c433b` |
| `case-1368-rev1` | `case-1368-task-1414` | `task` | `2026-10-25T14:33:41Z` | https://github.com/Saltmu/orchestune/issues/1414 | `924fdfcee93d13a0040d4eccdc36d09258704d62fc246ba9868e37d1fcc63dda` |
| `case-1368-rev1` | `case-1368-task-1415` | `task` | `2026-10-25T22:55:47Z` | https://github.com/Saltmu/orchestune/issues/1415 | `511e4e62fecbbe445762197d12ffdce9c5b859d8da6c1c9ec5729a7e885f917f` |
| `case-1588-rev1` | `case-1588-outcome-1614-6597846952` | `outcome` | `2026-11-03T07:22:38Z` | https://github.com/Saltmu/orchestune/issues/1614#issuecomment-6597846952 | `3a0c9baacd6f2ec59c76f90d3c3fb1d47bd6f511205f36aefbc14eb444c62605` |
| `case-1588-rev1` | `case-1588-outcome-1615-6598184437` | `outcome` | `2026-11-03T07:44:40Z` | https://github.com/Saltmu/orchestune/issues/1615#issuecomment-6598184437 | `14ee052c99dae5bf9158d97b211cf7e9f5ac18df9174fe19b102b3cbb9adc5a4` |
| `case-1588-rev1` | `case-1588-outcome-1616-6598566616` | `outcome` | `2026-11-03T08:08:09Z` | https://github.com/Saltmu/orchestune/issues/1616#issuecomment-6598566616 | `16de3ec778bdd38ff1e8653451ce3fff50b387f845ac0ce79e857f34c598b2ae` |
| `case-1588-rev1` | `case-1588-outcome-1617-6599132594` | `outcome` | `2026-11-03T08:41:01Z` | https://github.com/Saltmu/orchestune/issues/1617#issuecomment-6599132594 | `ed78d903bbc2fa4af3c42616c7d2bc7d36f0345bdf016801e0b460b6fc9f265d` |
| `case-1588-rev1` | `case-1588-outcome-1618-6599602151` | `outcome` | `2026-11-03T09:07:20Z` | https://github.com/Saltmu/orchestune/issues/1618#issuecomment-6599602151 | `ec4a9eadd007bcdd90103ae9b850f5d9e8ca199a2efbc266f14db6a591d6ed14` |
| `case-1588-rev1` | `case-1588-outcome-1619-6599960446` | `outcome` | `2026-11-03T09:26:16Z` | https://github.com/Saltmu/orchestune/issues/1619#issuecomment-6599960446 | `38af25dee58bb01442f5a24e37271876aa8648e795b3fa4d1dab2a45a08affaa` |
| `case-1588-rev1` | `case-1588-outcome-1620-6600365540` | `outcome` | `2026-11-03T09:47:15Z` | https://github.com/Saltmu/orchestune/issues/1620#issuecomment-6600365540 | `3a8c29d6de2a93396b56314bda1531f5df88f1dd418644003e2e6d5c7ee20c70` |
| `case-1588-rev1` | `case-1588-outcome-1621-6602562516` | `outcome` | `2026-11-03T11:25:33Z` | https://github.com/Saltmu/orchestune/issues/1621#issuecomment-6602562516 | `e58eb9efd3768a6ae2ccc135c619bc2cfb4224fd9928e8868e0ae04898f0df61` |
| `case-1588-rev1` | `case-1588-plan-1588` | `plan` | `2026-11-03T12:18:53Z` | https://github.com/Saltmu/orchestune/issues/1588 | `a37dbff95dc1b0b33d292edbb8f31648cf894b296be510aead1d9518e1cd275b` |
| `case-1588-rev1` | `case-1588-task-1614` | `task` | `2026-11-03T07:32:45Z` | https://github.com/Saltmu/orchestune/issues/1614 | `f17f6c3534bd5b5975d36994a2c890d47c3fd8ad5bf7670b1070bcd0e647e589` |
| `case-1588-rev1` | `case-1588-task-1615` | `task` | `2026-11-03T07:53:25Z` | https://github.com/Saltmu/orchestune/issues/1615 | `4455427f08d2b723fa92b5f0a3f2fa566ebf8cf3f41e29daf4e901be5f3d34d8` |
| `case-1588-rev1` | `case-1588-task-1616` | `task` | `2026-11-03T08:23:00Z` | https://github.com/Saltmu/orchestune/issues/1616 | `e4fcdc10657278ee4912c47d4d35e33677ae42a92e9a9e1f20c508a8d95b4224` |
| `case-1588-rev1` | `case-1588-task-1617` | `task` | `2026-11-03T08:46:49Z` | https://github.com/Saltmu/orchestune/issues/1617 | `7610d9a584762c6348355e2595e1a9a4ecde85d75da7a22784abc20a8af4bd76` |
| `case-1588-rev1` | `case-1588-task-1618` | `task` | `2026-11-03T09:13:28Z` | https://github.com/Saltmu/orchestune/issues/1618 | `a4c284fbfbace49133215274bf3a117466403f80a68bfc060b0bbba26485ad9e` |
| `case-1588-rev1` | `case-1588-task-1619` | `task` | `2026-11-03T09:33:13Z` | https://github.com/Saltmu/orchestune/issues/1619 | `9af1e35e37ac36579d110eb0e23bce6831d834f54084061b863978f0f9faaf67` |
| `case-1588-rev1` | `case-1588-task-1620` | `task` | `2026-11-03T09:54:20Z` | https://github.com/Saltmu/orchestune/issues/1620 | `5d6fd42af16e15f5847ae8bab67fcccf3ff75ea58ad8b4adb35160b789d7856b` |
| `case-1588-rev1` | `case-1588-task-1621` | `task` | `2026-11-03T11:31:38Z` | https://github.com/Saltmu/orchestune/issues/1621 | `e53a94162dca93734e6be5869ffd514e9bcf15e8c3f55ebc225c4662de155272` |
| `case-1589-rev1` | `case-1589-outcome-1661-6635810341` | `outcome` | `2026-11-04T16:19:25Z` | https://github.com/Saltmu/orchestune/issues/1661#issuecomment-6635810341 | `790649473be2c8f83825c27a7fdebbef4b74f7d80b55e2bc1f129c99b1ed9729` |
| `case-1589-rev1` | `case-1589-outcome-1662-6636745892` | `outcome` | `2026-11-04T16:59:53Z` | https://github.com/Saltmu/orchestune/issues/1662#issuecomment-6636745892 | `004563c3cc9f4be818db3646a8b603a6f4fea6746a25c41dacb608c395f84e47` |
| `case-1589-rev1` | `case-1589-outcome-1663-6637707682` | `outcome` | `2026-11-04T17:43:41Z` | https://github.com/Saltmu/orchestune/issues/1663#issuecomment-6637707682 | `99d971d722ae25d45f59a2bcc6827e214eabd1f78fd122cf4888632360e4b912` |
| `case-1589-rev1` | `case-1589-outcome-1664-6638492598` | `outcome` | `2026-11-04T18:19:04Z` | https://github.com/Saltmu/orchestune/issues/1664#issuecomment-6638492598 | `5ade25aaebf79db08a17cf540e92a53114a7399e47440c743e9e253ba3f3d803` |
| `case-1589-rev1` | `case-1589-outcome-1665-6639340054` | `outcome` | `2026-11-04T18:57:39Z` | https://github.com/Saltmu/orchestune/issues/1665#issuecomment-6639340054 | `d817acad146418a199c2eaa52aa784b2aab6ec34679987b2b39c22391561d27d` |
| `case-1589-rev1` | `case-1589-outcome-1666-6640425268` | `outcome` | `2026-11-04T19:46:22Z` | https://github.com/Saltmu/orchestune/issues/1666#issuecomment-6640425268 | `a3a792f6ef8c8db6b6203a4a8f7021a70dd0b2f9edb3b81b85890844fed8c767` |
| `case-1589-rev1` | `case-1589-outcome-1667-6641346741` | `outcome` | `2026-11-04T20:29:41Z` | https://github.com/Saltmu/orchestune/issues/1667#issuecomment-6641346741 | `39b82598a17d5d35466e4edf3dc9ceb5a389df39c4a1f87fe3a5c04e6402c8e2` |
| `case-1589-rev1` | `case-1589-plan-1589` | `plan` | `2026-11-04T22:52:45Z` | https://github.com/Saltmu/orchestune/issues/1589 | `ff95c885671736585436367327c752da0510201a97d03e0b9a695faafdb72e3e` |
| `case-1589-rev1` | `case-1589-task-1661` | `task` | `2026-11-04T16:29:45Z` | https://github.com/Saltmu/orchestune/issues/1661 | `ff1dc55aa45faf2dfc4edff00a5385f44b51e66a57f2f3c046b3792cb183a513` |
| `case-1589-rev1` | `case-1589-task-1662` | `task` | `2026-11-04T17:09:11Z` | https://github.com/Saltmu/orchestune/issues/1662 | `956f9119e9086a1e2f3c7544ac97fafa27b69408a4db216adeddc5c73f3e06be` |
| `case-1589-rev1` | `case-1589-task-1663` | `task` | `2026-11-04T17:49:36Z` | https://github.com/Saltmu/orchestune/issues/1663 | `fd54d70035497c5e808703936ffa0bed7412a2922f2ea90f5a0788a00b1d0c75` |
| `case-1589-rev1` | `case-1589-task-1664` | `task` | `2026-11-04T18:28:09Z` | https://github.com/Saltmu/orchestune/issues/1664 | `56fc2e342f1cbea0539289cd0a9a8a00059e668577f6394477de8232eb74cbdf` |
| `case-1589-rev1` | `case-1589-task-1665` | `task` | `2026-11-04T19:06:04Z` | https://github.com/Saltmu/orchestune/issues/1665 | `d392e90d45038369ee4b747a3f29054f336e9a442b2ec487714edffd0e8bb330` |
| `case-1589-rev1` | `case-1589-task-1666` | `task` | `2026-11-04T19:54:33Z` | https://github.com/Saltmu/orchestune/issues/1666 | `b7d44b11afb4e7961f4453c9d75a547ef515cab2769392d96543a31f9bf7d6e9` |
| `case-1589-rev1` | `case-1589-task-1667` | `task` | `2026-11-04T20:44:02Z` | https://github.com/Saltmu/orchestune/issues/1667 | `0e3518ff43775f662929a2351c47ab15beb593bc768d9f9161f3d987278b5cc9` |
| `case-1730-rev1` | `case-1730-outcome-1734-6706718162` | `outcome` | `2026-11-07T12:05:54Z` | https://github.com/Saltmu/orchestune/issues/1734#issuecomment-6706718162 | `08df7419738b958f5b897a94c0a2122734909bc9fdfe5eb34a9bd337b2324dc7` |
| `case-1730-rev1` | `case-1730-outcome-1735-6707180012` | `outcome` | `2026-11-07T12:52:06Z` | https://github.com/Saltmu/orchestune/issues/1735#issuecomment-6707180012 | `4323c932c0025e0c90e9dabcfafb3cebaf47c1fa0f02c3e5ea89f215d399361d` |
| `case-1730-rev1` | `case-1730-outcome-1736-6707517526` | `outcome` | `2026-11-07T13:25:04Z` | https://github.com/Saltmu/orchestune/issues/1736#issuecomment-6707517526 | `d8f5c89dc5aeb33a1690ff01a28fdab2d7bb83aa96c60c422af5d505c6ad47be` |
| `case-1730-rev1` | `case-1730-outcome-1737-6709161280` | `outcome` | `2026-11-07T15:39:32Z` | https://github.com/Saltmu/orchestune/issues/1737#issuecomment-6709161280 | `4cc2b15aac6ab2fd9fa99e668e5a43d2d805a6882e5aa1d5d8a4546a50135100` |
| `case-1730-rev1` | `case-1730-outcome-1738-6710633071` | `outcome` | `2026-11-07T17:25:17Z` | https://github.com/Saltmu/orchestune/issues/1738#issuecomment-6710633071 | `eff8d499f6d281f3284d4c5263216531328b3e2710a51215ee6b917b227fc535` |
| `case-1730-rev1` | `case-1730-plan-1730` | `plan` | `2026-11-08T12:18:23Z` | https://github.com/Saltmu/orchestune/issues/1730 | `e9423312cdef392f56b4fe31e5330704907a0bee505549d18680bff5cdba64a4` |
| `case-1730-rev1` | `case-1730-task-1734` | `task` | `2026-11-07T12:18:52Z` | https://github.com/Saltmu/orchestune/issues/1734 | `5954b812c6adefa9edb0161a0afda13d8ea52819f900905cd055a1d81464a694` |
| `case-1730-rev1` | `case-1730-task-1735` | `task` | `2026-11-07T13:46:49Z` | https://github.com/Saltmu/orchestune/issues/1735 | `ab28a9701ae96ea116b1db61c4fcd86d5ba4002cbd1f8cdf0b2924052a0d6a11` |
| `case-1730-rev1` | `case-1730-task-1736` | `task` | `2026-11-07T13:46:59Z` | https://github.com/Saltmu/orchestune/issues/1736 | `0ba4048416f895bb668b2eecf3cbf94d6a76d63e51a43ce129d4494ba170bdbc` |
| `case-1730-rev1` | `case-1730-task-1737` | `task` | `2026-11-07T15:47:07Z` | https://github.com/Saltmu/orchestune/issues/1737 | `792177585be377aef03a2ba7d253262ac95477545b37e5ccb24a17ed4b50c885` |
| `case-1730-rev1` | `case-1730-task-1738` | `task` | `2026-11-08T00:19:04Z` | https://github.com/Saltmu/orchestune/issues/1738 | `25d54d02f829ab7c7285834b73084d05a3fe163f058388215e7279d2014568f9` |

</details>

---

## 4. 全試行の実測結果・技術的失敗の原試行記録・盲検採点

### 4.1 初回技術的失敗（原試行）の記録と同一条件再実行の境界

プロトコル（[#1872 §7.3](planning-history-poc-protocol.md#73-測定項目欠測処理理由付き-nullと指標別ペア分母) および本 Issue 要件「再実行は事前登録した同一条件の技術的失敗に限り、原試行も残す」）に従い、本セッションで発生した技術的失敗の原試行と再実行判断を以下のとおり記録する。

1. **原試行バッチ 1（プロンプト隔離ガード追加前の技術的タイムアウト — 4 試行）**:
   - **現象**: 初回起動時（`trial-01-task-1421-A`, `trial-03-task-1588-B`, `trial-05-task-1589-A`, `trial-07-task-1590-B`）、worktree 直下の `AGENTS.md` が Codex CLI により自動読込され、エージェントが `.agents/AGENTS.md` → `skills/local-ci-developer/SKILL.md` の通常実装ワークフローを再帰的に辿った結果、4 試行すべてが `300` 秒上限で `TimeoutExpired`（出力計画 `0` bytes）となった。
   - **処置**: 条件 A・条件 B の共通プロンプト冒頭に「独立した読み取り専用の分解評価であり、`.agents/AGENTS.md` や `skills/*` の開発ワークフロー定義を読まず、ファイル変更や CI/GitHub コマンドを実行しない」旨の同一ガード文を追加し、全 12 試行を同一条件で実行した。原試行の 0-byte 痕跡と診断ログは評価セッション履歴に保持されている。
2. **本番バッチ（`trial-01`〜`trial-12`）における `trial-04-task-1588-A`（`blind-04`）のタイムアウト中断**:
   - **現象**: 本番バッチ 12 試行のうち **11 試行（条件 A: 5/6、条件 B: 6/6）は正常完了（`status=succeeded`, `exit_code=0`, `valid_dag=true`）** したが、`trial-04-task-1588-A`（`#1588` 条件 A、`blind-04`）のみ、67 モジュールからなる `orchestune/dispatch/` と関連テスト群に対して 3 回の広域 `rg` 実行（出力 JSONL `145,700` bytes）を行った後の推論中にプロセス予算 `240` 秒（実測 `240,028.90 ms`）に達してタイムアウト中断（`status=timed_out`, `exit_code=-1`）となった。
   - **再実行を行わず欠測（`null`）として保持した理由**: これは環境障害や CLI 起動失敗ではなく、**カードなし（条件 A）における広域探索と長考による実質的な時間予算超過** であり、残る 5 組（`#1421, #1589, #1590, #1730, #1841`）で継続基準に必要な比較可能ペア数（5 組以上）を満たしているため、結果を恣意的に改善するための再実行は行わず、`input_tokens` / `output_tokens` / `total_tokens` / `cached_tokens` / 採点スコアをすべて理由付き `null` としてそのまま計上した。

### 4.2 全 12 試行の実測メトリクス一覧（実行順 `seq_index: 1..12`）

- **`cost_usd`**: `codex exec --json` が課金額フィールドを出力しないため、全 12 試行で `null`（`missing_reason: "codex exec --json does not emit cost_usd"`）。バイト数やトークン数からの推定・換算は行わない。
- **実装後の競合・再レビュー・手戻り**: 本評価は開始前 `base_sha` 上での読み取り専用の分解計画生成評価であるため、全 12 試行で `null`（`missing_reason: "planning-only evaluation; post-implementation rework not executed"`）。

| 実行順 | `trial_id` | 盲検 ID | 課題 | 条件 | `status` (`exit`) | DAG 検証 (`subtasks` / `edges`) | `input_tokens` | `cached_tokens` | `output_tokens` | `total_tokens` | `cost_usd` | 計画時間 `wall_ms` | 検索時間 `search_ms` (秒) | 注入 `injected_bytes` | リポジトリ読取 / 出典読取 / `gh` |
| ---: | --- | :---: | ---: | :---: | --- | --- | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: | :---: |
| 1 | `trial-01-task-1421-A` | `blind-07` | `#1421` | **A** | `succeeded` (`0`) | `valid` (`8` / `10`) | `93,127` | `65,152` | `6,775` | `99,902` | `null` | `145,950.66` | `0.00` (`—`) | `0` | `3` / `0` / `0` |
| 2 | `trial-02-task-1421-B` | `blind-02` | `#1421` | **B** | `succeeded` (`0`) | `valid` (`9` / `13`) | `101,212` | `73,344` | `6,875` | `108,087` | `null` | `148,948.58` | `139.61` (`0.1396s`) | `10,847` | `3` / `0` / `0` |
| 3 | `trial-03-task-1588-B` | `blind-11` | `#1588` | **B** | `succeeded` (`0`) | `valid` (`8` / `7`) | `126,959` | `99,712` | `9,675` | `136,634` | `null` | `210,774.45` | `129.04` (`0.1290s`) | `7,322` | `3` / `0` / `0` |
| 4 | `trial-04-task-1588-A` | `blind-04` | `#1588` | **A** | `timed_out` (`-1`) | `invalid` (`no_subtasks_yaml_block`) | `null` | `null` | `null` | `null` | `null` | `240,028.90` | `0.00` (`—`) | `0` | `3` / `0` / `0` |
| 5 | `trial-05-task-1589-A` | `blind-09` | `#1589` | **A** | `succeeded` (`0`) | `valid` (`10` / `9`) | `122,031` | `82,432` | `9,679` | `131,710` | `null` | `203,817.72` | `0.00` (`—`) | `0` | `3` / `0` / `0` |
| 6 | `trial-06-task-1589-B` | `blind-01` | `#1589` | **B** | `succeeded` (`0`) | `valid` (`13` / `12`) | `136,915` | `103,168` | `8,617` | `145,532` | `null` | `186,329.89` | `128.39` (`0.1284s`) | `10,071` | `3` / `0` / `0` |
| 7 | `trial-07-task-1590-B` | `blind-06` | `#1590` | **B** | `succeeded` (`0`) | `valid` (`20` / `19`) | `143,936` | `108,160` | `10,167` | `154,103` | `null` | `218,126.82` | `131.47` (`0.1315s`) | `9,691` | `3` / `0` / `0` |
| 8 | `trial-08-task-1590-A` | `blind-12` | `#1590` | **A** | `succeeded` (`0`) | `valid` (`20` / `19`) | `129,548` | `97,792` | `9,523` | `139,071` | `null` | `205,267.74` | `0.00` (`—`) | `0` | `3` / `0` / `0` |
| 9 | `trial-09-task-1730-A` | `blind-03` | `#1730` | **A** | `succeeded` (`0`) | `valid` (`5` / `6`) | `106,036` | `74,624` | `5,151` | `111,187` | `null` | `116,923.62` | `0.00` (`—`) | `0` | `3` / `0` / `0` |
| 10 | `trial-10-task-1730-B` | `blind-10` | `#1730` | **B** | `succeeded` (`0`) | `valid` (`5` / `5`) | `93,476` | `73,600` | `5,773` | `99,249` | `null` | `128,353.58` | `137.13` (`0.1371s`) | `7,810` | `2` / `0` / `0` |
| 11 | `trial-11-task-1841-B` | `blind-05` | `#1841` | **B** | `succeeded` (`0`) | `valid` (`10` / `15`) | `101,296` | `76,160` | `6,970` | `108,266` | `null` | `149,536.50` | `136.43` (`0.1364s`) | `7,230` | `3` / `0` / `0` |
| 12 | `trial-12-task-1841-A` | `blind-08` | `#1841` | **A** | `succeeded` (`0`) | `valid` (`7` / `8`) | `104,355` | `76,416` | `4,240` | `108,595` | `null` | `94,915.18` | `0.00` (`—`) | `0` | `3` / `0` / `0` |

### 4.3 条件 B における提示カードの採否と理由（プランナー記録）

| 試行 (`blind_id`) | 対象課題 | 提示カード `case_id` (`parent_issue`) | プランナー採否 (`decision`) | 記録された採用・部分採用・不採用の根拠 |
| --- | ---: | --- | :---: | --- |
| `trial-02-task-1421-B` (`blind-02`) | `#1421` | `case-1368-rev1` (`#1368`) | `partially_adopted` | - **case-1368-rev1 — decision: partially_adopted** 共通契約を先行させ、競合しない利用領域を並列化し、最後に結合検証する構造と、実装・対応テストを同じ footprint に置く粒度を採用した。表示刷新の機能分割は今回に転用せず、`unknown` は未完了と断定できないため、履歴上の全タスク成功も前提にしていない。 |
| `trial-03-task-1588-B` (`blind-11`) | `#1588` | `case-1053-rev1` (`#1053`) | `partially_adopted` | - **`case-1053-rev1`: `partially_adopted`** — 実装と対応テストを同じサブタスクへ含め、共有契約の変更を依存辺で直列化する粒度を採用しました。今回は契約の新設を伴わない移動なので、独立した契約先行PRや文書専用PRは追加せず、各PRで登録・文書も完結させます。履歴の `done` は分解例として参照し、最終タスクの `unknown` を成功実績とは扱いません。 |
| `trial-06-task-1589-B` (`blind-01`) | `#1589` | `case-1588-rev1` (`#1588`) | `partially_adopted` | - **`case-1588-rev1` — `partially_adopted`**：パッケージ単位の PR、実装と関連テストを対にする粒度、共有ファイルの更新を直列化する方式を採用しました。全タスクの `done` は方式の参考にとどめ、旧パスや八分割の順序は転用せず、今回確認したパスとパラメータ型の依存に合わせています。 |
| `trial-07-task-1590-B` (`blind-06`) | `#1590` | `case-1589-rev1` (`#1589`) | `partially_adopted` | - **case-1589-rev1 — decision: partially_adopted** 全サブタスクの `result: done` を、共有 lint 設定を先に導入し、パッケージ単位で実装と対応テストを組み合わせて直列に移行する粒度の参考として採用した。PLR0913 用 baseline、パラメータオブジェクト、過去の具体的なファイル配置は転用せず、今回指定の BLE 一時除外方式と PR 順序を優先した。 |
| `trial-10-task-1730-B` (`blind-10`) | `#1730` | `case-1059-rev1` (`#1059`) | `partially_adopted` | - **case-1059-rev1 — decision: partially_adopted** 共有契約を先行させ、実装と担当テストを同じタスクへまとめ、保存・副作用の境界ごとに復旧を検証する構成を採用しました。履歴の5件はすべて`done`ですが、今回の成功保証には使わず、GitHubラベル・journal・GCの具体的実装と直列DAGは移植していません。 |
| `trial-11-task-1841-B` (`blind-05`) | `#1841` | `case-1730-rev1` (`#1730`) | `partially_adopted` | - **`case-1730-rev1` — `partially_adopted`** 共通契約を先行し、独立adapter／保存基盤を並列化してからservice・CLIへ統合する粒度と、実装・担当テストを対にする構成を採用しました。`trial-flow`の`blocked`は後続CLIの`done`で解消済みとは判断せず、今回も構成serviceの検証と最終統合検証を分けます。ローカル試行固有の状態機械・承認フローは導入しません。 |

### 4.4 盲検採点結果（`blind-01`〜`blind-12` 順）

採点前に全計画から `## Reference Card Adoption` セクションおよびカード識別子を除去して `blind-01`〜`blind-12` のみを提示し、4 項目×0〜2 点（8 点満点）のルーブリックで採点した後に条件（A / B）および対象課題と照合した。

| 盲検 ID | 開示後の課題・条件 (`trial_id`) | 1. 要件網羅 (`0..2`) | 2. 変更範囲整合 (`0..2`) | 3. 依存と DAG (`0..2`) | 4. 検証計画 (`0..2`) | **合計点 (`0..8`)** | リーク有無 | 必須要件落ち | 実装後手戻り | 採点根拠・監査所見 |
| :---: | --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | --- |
| `blind-01` | `#1589` 条件 **B** (`trial-06-task-1589-B`) | `2` | `2` | `1` | `2` | **`7 / 8`** | `false` | `false` | `null` | 全要件・対象モジュール・シンボル・検証コマンドを網羅。全13サブタスクが共有baseline更新のため単一直列鎖となっており並列余地が制限されるためDAG項目は1点。 |
| `blind-02` | `#1421` 条件 **B** (`trial-02-task-1421-B`) | `2` | `2` | `2` | `2` | **`8 / 8`** | `false` | `false` | `null` | 共有契約 migration-contract 先行後に設定解決・入力拒否・永続化を並列扇出し、metadata-output-cutover へ集約する過不足ない9サブタスクDAG。全サブタスクに実在footprintとsymbolsを定義。 |
| `blind-03` | `#1730` 条件 **A** (`trial-09-task-1730-A`) | `2` | `2` | `2` | `2` | **`8 / 8`** | `false` | `false` | `null` | local-trial-v1 共有契約先行から workspace / agent-planning の並列実行、supervisor、cli-integration への5サブタスクDAGと検証計画が完全に整合。 |
| `blind-04` | `#1588` 条件 **A** (`trial-04-task-1588-A`) | `null` | `null` | `null` | `null` | **`null`** | `false` | `null` | `null` | 240秒予算上限でタイムアウト中断（部分JSONLログ上は3回の広域rg探索のみ完了、リーク0件）。（欠測理由: `trial timed out at 240s budget before final decomposition plan message was emitted`） |
| `blind-05` | `#1841` 条件 **B** (`trial-11-task-1841-B`) | `2` | `2` | `2` | `2` | **`8 / 8`** | `false` | `false` | `null` | shared-artifact-context-v1 共有契約先行から抽出・索引・検索/予算・設定の4並列扇出と段階統合で構成された10サブタスクDAG。全サブタスクに具体的な実ファイルfootprintとsymbolsを明記。 |
| `blind-06` | `#1590` 条件 **B** (`trial-07-task-1590-B`) | `2` | `1` | `1` | `2` | **`6 / 8`** | `false` | `false` | `null` | 全パッケージのBLE001棚卸し要件と検証計画を網羅するが、全20サブタスクが pyproject.toml を予約して完全直列化され、一部監査サブタスクで対象モジュールやsymbolsが省略されているため変更範囲1点・DAG1点。 |
| `blind-07` | `#1421` 条件 **A** (`trial-01-task-1421-A`) | `2` | `1` | `2` | `2` | **`7 / 8`** | `false` | `false` | `null` | 要件網羅と並列扇出DAG（契約先行→3並列→集約）は妥当だが、末尾サブタスク final-local-ci-and-release-review の footprint が空配列 [] であり後半3サブタスクの symbols も空のため変更範囲は1点。 |
| `blind-08` | `#1841` 条件 **A** (`trial-12-task-1841-A`) | `2` | `1` | `2` | `2` | **`7 / 8`** | `false` | `false` | `null` | 要件・DAG構造・symbols・検証観点は妥当だが、親企画本文の『現在の空Footprintから予約しない』という記述に引きずられ全7サブタスクのYAML footprintが空配列 [] となっているため変更範囲は1点。 |
| `blind-09` | `#1589` 条件 **A** (`trial-05-task-1589-A`) | `2` | `2` | `1` | `2` | **`7 / 8`** | `false` | `false` | `null` | 全要件・footprint・symbols・検証コマンドを網羅するが、全10サブタスクが .orchestune/plr0913-baseline.txt を更新する単一直列鎖として構成されており並列化が制限されるためDAGは1点。 |
| `blind-10` | `#1730` 条件 **B** (`trial-10-task-1730-B`) | `2` | `2` | `2` | `2` | **`8 / 8`** | `false` | `false` | `null` | local-trial-v1 共有契約先行から workspace / agent-planning の2並列扇出、supervisor、cli-integration への5サブタスクDAG・footprint・symbols・検証計画が過不足なく整合。 |
| `blind-11` | `#1588` 条件 **B** (`trial-03-task-1588-B`) | `2` | `2` | `2` | `2` | **`8 / 8`** | `false` | `false` | `null` | dispatch の8サブパッケージ（doctor, reporting, recovery, launch, phase, semantic_review, cycle, config）を層境界・architecture登録・string-patch baseline更新と整合する順序で分解。 |
| `blind-12` | `#1590` 条件 **A** (`trial-08-task-1590-A`) | `2` | `1` | `1` | `2` | **`6 / 8`** | `false` | `false` | `null` | 全パッケージのBLE001棚卸し要件と検証計画を網羅するが、全20サブタスクが pyproject.toml を過剰予約して完全直列化されているため変更範囲1点・DAG1点。 |

---

## 5. ペア比較（条件 B − 条件 A）と指標別分母・欠測集計

### 5.1 課題別ペア比較表（6 課題）

| 対象課題 | 実行順 | 品質合計 A (`blind_id`) | 品質合計 B (`blind_id`) | **品質差 `B - A`** | `total_tokens` A | `total_tokens` B | **`total_tokens` 差 `B - A` (`%`)** | `input_tokens` 差 `B - A` | `output_tokens` 差 `B - A` | `wall_ms` 差 `B - A` | リポジトリ読取差 `B - A` | 比較可否・欠測理由 |
| ---: | :---: | :---: | :---: | :---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: | --- |
| `#1421` | `A→B` | `7/8` (`blind-07`) | `8/8` (`blind-02`) | **`+1`** | `99,902` | `108,087` | **`+8,185` (`+8.2%`)** | `+8,085` | `+100` | `+2,997.92` | `0` | 比較可能（両腕実測値あり） |
| `#1588` | `B→A` | `null` (`blind-04`) | `8/8` (`blind-11`) | **`null`** | `null` | `136,634` | **`null`** | `null` | `null` | `null` | `null` | **欠測（除外）**: `Arm A (trial-04-task-1588-A, blind-04) timed out at 240s budget before turn.completed` |
| `#1589` | `A→B` | `7/8` (`blind-09`) | `7/8` (`blind-01`) | **`0`** | `131,710` | `145,532` | **`+13,822` (`+10.5%`)** | `+14,884` | `-1,062` | `-17,487.83` | `0` | 比較可能（両腕実測値あり） |
| `#1590` | `B→A` | `6/8` (`blind-12`) | `6/8` (`blind-06`) | **`0`** | `139,071` | `154,103` | **`+15,032` (`+10.8%`)** | `+14,388` | `+644` | `+12,859.08` | `0` | 比較可能（両腕実測値あり） |
| `#1730` | `A→B` | `8/8` (`blind-03`) | `8/8` (`blind-10`) | **`0`** | `111,187` | `99,249` | **`-11,938` (`-10.7%`)** | `-12,560` | `+622` | `+11,429.96` | `-1` | 比較可能（両腕実測値あり） |
| `#1841` | `B→A` | `7/8` (`blind-08`) | `8/8` (`blind-05`) | **`+1`** | `108,595` | `108,266` | **`-329` (`-0.3%`)** | `-3,059` | `+2,730` | `+54,621.32` | `0` | 比較可能（両腕実測値あり） |

### 5.2 指標別ペア分母・欠測数・代表値サマリー

| 評価指標 | 登録ペア総数 | 比較可能ペア数（分母） | 欠測ペア数 | 欠測理由（該当ペア） | ペア差 `B - A` 中央値 (Median) | ペア差 `B - A` 平均値 (Mean) | ペア差 最小〜最大 (`Min .. Max`) |
| --- | ---: | ---: | ---: | --- | ---: | ---: | --- |
| **品質合計点 (`0..8`)** | 6 | **5** | 1 | `#1588` Arm A 240s タイムアウト中断 | **`0`** | `+0.40` | `0 .. +1`（改善 2 組・同等 3 組・悪化 0 組） |
| **1. 必須要件網羅 (`0..2`)** | 6 | **5** | 1 | `#1588` Arm A 240s タイムアウト中断 | **`0`** | `0.00` | `0 .. 0`（全比較可能ペアで両腕 `2/2`） |
| **2. 変更範囲整合 (`0..2`)** | 6 | **5** | 1 | `#1588` Arm A 240s タイムアウト中断 | **`0`** | `+0.40` | `0 .. +1`（`#1421`, `#1841` の 2 組で `1→2` へ改善） |
| **3. 依存と DAG (`0..2`)** | 6 | **5** | 1 | `#1588` Arm A 240s タイムアウト中断 | **`0`** | `0.00` | `0 .. 0` |
| **4. 検証計画 (`0..2`)** | 6 | **5** | 1 | `#1588` Arm A 240s タイムアウト中断 | **`0`** | `0.00` | `0 .. 0`（全比較可能ペアで両腕 `2/2`） |
| **実測 `total_tokens`** | 6 | **5** | 1 | `#1588` Arm A `turn.completed` 未到達 | **`+8,185`** | `+4,954.4` | `-11,938 .. +15,032`（削減 2 組・増加 3 組） |
| **実測 `input_tokens`** | 6 | **5** | 1 | `#1588` Arm A `turn.completed` 未到達 | **`+8,085`** | `+4,347.6` | `-12,560 .. +14,884` |
| **実測 `output_tokens`** | 6 | **5** | 1 | `#1588` Arm A `turn.completed` 未到達 | **`+622`** | `+606.8` | `-1,062 .. +2,730` |
| **計画作成時間 `wall_ms`** | 6 | **5** | 1 | `#1588` Arm A 240s タイムアウト中断 | **`+11,429.96 ms`** | `+12,884.09 ms` | `-17,487.83 .. +54,621.32 ms` |
| **リポジトリ読取回数 (`repo_reads`)** | 6 | **5** | 1 | `#1588` Arm A 240s タイムアウト中断 | **`0`** | `-0.20` | `-1 .. 0`（`#1730` で `3→2` 回へ削減） |
| **セレクタ検索時間 `search_ms`（条件 B）** | 6 | **6**（条件 B 単腕） | 0 | 欠測なし | **`134.05 ms` (`0.1341 s`)** | `133.68 ms` (`0.1337 s`) | `128.39 .. 139.61 ms` (`0.1284 .. 0.1396 s`) |
| **課金額 `cost_usd`** | 6 | **0** | 6 | `codex exec --json does not emit cost_usd` | `null` | `null` | `null` |
| **実装後の競合・再レビュー・手戻り** | 6 | **0** | 6 | `planning-only evaluation; post-implementation rework not executed` | `null` | `null` | `null` |

---

## 6. 継続・再設計・見送りの判定と基盤拡張への提言

### 6.1 継続判定基準（6 条件）の照合結果

| # | 判定項目 | 継続（Continue）の必須達成基準（#1872 §7.4） | 実測結果 | 判定 |
| :---: | --- | --- | --- | :---: |
| 1 | **リークゼロ** | 全試行において、対象系列および `as_of` 以降の事後情報の混入が `0` 件であること。 | 全 12 試行（完了 11 件＋タイムアウト部分ログ 1 件）および生成 6 カードすべてで対象系列・事後情報の混入 **`0` 件**（`gh_calls=0`, `leak_detected=false`）。 | **達成 (PASS)** |
| 2 | **B の必須要件落ちゼロ** | 条件 B の全試行において、必須要件の脱落が `0` 件であること。 | 条件 B の全 6 試行（`6/6` 完走）で `requirement_coverage = 2/2`、必須要件落ち **`0` 件**。 | **達成 (PASS)** |
| 3 | **品質比較可能ペア数と非悪化** | 品質スコアを比較可能なペアが 5 組以上あり、すべての比較可能ペアで `Score(B) - Score(A) >= 0` であること。 | 比較可能ペア数 **`5` 組**（`>= 5`）、全 5 組で `Score(B) - Score(A) >= 0`（`+1` が 2 組、`0` が 3 組、悪化 `0` 組）。 | **達成 (PASS)** |
| 4 | **品質または実測 Total Tokens の改善** | 比較可能ペアのうち少なくとも 1 組以上で `Score(B) > Score(A)` または `TotalTokens(B) < TotalTokens(A)` が観測されること。 | **3 組** で改善を観測: `#1421`（品質 `7→8`, `+1` 点）、`#1730`（`total_tokens` `111,187→99,249`, `-11,938` / `-10.7%`）、`#1841`（品質 `7→8`, `+1` 点 かつ `total_tokens` `108,595→108,266`, `-329` / `-0.3%`）。 | **達成 (PASS)** |
| 5 | **実測 Total Tokens の中央値非増加** | 実測 `total_tokens` を比較可能なペアが 5 組以上あり、ペア差 `TotalTokens(B) - TotalTokens(A)` の中央値が `0` 以下であること。 | 比較可能ペア **`5` 組**（`>= 5`）における `TotalTokens(B) - TotalTokens(A)` は `[-11,938, -329, +8,185, +13,822, +15,032]` であり、**中央値は `+8,185`（`> 0`）**。 | **未達 (FAIL)** |
| 6 | **検索レイテンシ上限** | 条件 B の全試行でカード検索・生成時間が各回 10 秒以内（`<= 10.0s`）であること。 | 全 6 回とも `128.39 ms`〜`139.61 ms`（最大 **`0.1396` 秒** `<= 10.0` 秒）。 | **達成 (PASS)** |

### 6.2 最終判定: **再設計／追加観測（Redesign / Observe More）**

**判定理由**: 6 基準のうち基準 1・2・3・4・6 の 5 項目を満たし、類似計画カードの提示によって **分解品質の改善（`#1421`, `#1841` での変更範囲 `footprint` 欠落防止）**、**大規模パッケージ分割での完走安定性向上（`#1588` で条件 A が 240 秒タイムアウトしたのに対し条件 B は `#1053` カードを参照して 210.8 秒・`8/8` 点で完走）**、および **探索ターン削減時のトークン削減（`#1730` で `repo_reads` が `3→2` 回へ減少し `-11,938` tokens / `-10.7%` 削減、`#1841` で `-329` tokens 削減）** が実証された。一方で、探索回数が減らなかった 3 課題（`#1421`, `#1589`, `#1590`）ではカード JSON（`7.3〜10.8 KB`）のマルチターン累積入力コストにより `total_tokens` が `+8.2%〜+10.8%` 増加し、5 ペア中央値が `+8,185 tokens (> 0)` となったため、[#1872 §7.4](planning-history-poc-protocol.md#74-継続再設計見送りの判定基準continuation-criteria) の決定規則（「品質改善とトークン増加（`B - A` 中央値 `> 0`）が混在して上記 6 条件の一部を満たさない場合」）に従い、**「再設計／追加観測（Redesign / Observe More）」** と判定する。

### 6.3 改善が確認されたケースと失敗・費用増の構造要因分析

1. **カード提示が明確に有効だったケース（品質・完走性・探索削減）**:
   - **企画・設計 EPIC から実装サブタスクへの分解（`#1841`）**: 条件 A（`blind-08`）では親 Issue 本文の「現在の空 Footprint から実装ファイルを予約しない」という文言に引きずられ、全 7 サブタスクの YAML `footprint` を空配列 `[]` として出力する欠陥（変更範囲整合 `1` 点）が発生した。条件 B（`blind-05`）では `#1730` の類似計画カード（契約先行＋実装＋テスト対の具体的 `footprint` と `blocked` / `done` 履歴）を参照したことで、全 10 サブタスクに実在ファイルとテストの `footprint` を漏れなく定義し（`8/8` 点）、`input_tokens` も `-3,059` tokens（`total_tokens` `-329` tokens）削減した。
   - **廃止・移行リファクタリングでのダミー最終タスク抑止（`#1421`）**: 条件 A（`blind-07`）では `footprint: []`, `symbols: []` の CI 実行専用ダミータスクを末尾に切ったのに対し、条件 B（`blind-02`）では `#1368` カードの「共有契約先行 → 並列扇出 → 統合・E2E 検証タスクにも具体的な回帰テスト footprint を持たせる」構造を採用して全 9 サブタスクに有効な `footprint` / `symbols` を割り当て、`7/8 → 8/8` 点へ改善した。
   - **ローカル体験機能分解での探索ターン削減（`#1730`）**: 条件 B（`blind-10`）では `#1059` カードから CLI・ライフサイクル・ドキュメント同期の 5 分割粒度を即座に把握し、`repo_reads` が条件 A の `3` 回から `2` 回へ 1 ターン減少した結果、`input_tokens` を `-12,560` tokens、`total_tokens` を `-11,938` tokens（`-10.7%`）削減した。
2. **トークン増加と予算脱落を招いた 2 つの構造要因（再設計ターゲット）**:
   - **要因 ①（カード JSON 内の出典 SHA-256・URL 配列のマルチターン再送コスト）**: `planning-history-cards-v1` の単一カード（`7,230〜10,847` bytes）のうち約 **35%〜45%（`2,154〜4,086` bytes）** を `sources` 配列（各子 Issue・Outcome コメントの 64 文字 SHA-256 `digest`、フル GitHub URL、`available_at`）が占めている。エージェントが 3 回のシェル探索を行う 4 ターン対話では、初期プロンプトに注入されたカード JSON が毎ターン再送されるため、`repo_reads` が 1 回以上減らない課題（`#1421, #1589, #1590`）では `4 turns × 約 2,500 tokens ≒ +10,000 input_tokens` の固定オーバーヘッドが発生した。
   - **要因 ②（14〜18 サブタスク級 EPIC における単一カード 12 KiB 超過と末尾削除の飢餓）**: `#1106`（14 タスク、最小 `16,272` B）や `#1230`（18 タスク、最小 `21,744` B）のような大規模 EPIC は 1 枚で既定 `--max-bytes 12288` を超える。#1872 §4 Step 4 の末尾削除（`cards.pop()`）は Rank 1 が予算超過の場合でも Rank 3 → Rank 2 → Rank 1 の順に削除するため、Rank 2・Rank 3 に 12 KiB 以内の有用なカードがあっても `status="over_budget"`（返却 0 枚）となる。

### 6.4 必要なデータ網羅性と共有索引拡張・分解手順接続の再設計候補

次段階（再設計後の追加観測および基盤拡張 Issue 化）では、以下の 3 点を反映する。

| 区分 | 再設計・拡張候補 | 具体的な契約・実装方針 |
| --- | --- | --- |
| **1. プロンプト注入形式の軽量化（再設計 R1）** | **検証用フルカードと LLM 注入用コンパクト投影の分離** | `corpus_digest` / `request_digest` / `plan_revision_digest` および全 `sources`（URL・64 文字 SHA-256 `digest`・`available_at`）はローカル検証 receipt ファイル（`.orchestune/tmp/.../history-cards-receipt.json`）に完全保持しつつ、LLM プロンプトへ直接注入するブロックからは 64 文字 hex `digest` と冗長 URL を除き、`case_id`, `parent_issue`, `available_at`, `title`, `purpose`, `tasks[subtask_id, depends_on, primary_footprint(最大4件), result, missing_reason]`, `declared_edges` のみに絞る（1 カードあたり `7〜10 KB → 1.8〜2.5 KB`、マルチターン入力トークン増分を約 70% 削減）。 |
| **2. 予算調整アルゴリズムの改善（再設計 R2）** | **Rank-Aware Skip-Oversized 予算適合と大規模計画の要約** | `select_cards` Step 4 において、上位カードが単独で残り予算を超過する場合は下位カードをすべて巻き込んで 0 枚（`over_budget`）にするのではなく、予算超過した当該カードをスキップ（`budget_dropped += 1`）して次順位の予算内カードを採用できるようにする。あわせて 10 タスク超の親計画ではタスク別 `footprint` の上位代表パスのみをカード投影に含める。 |
| **3. データ網羅性と共有索引・分解手順接続（基盤拡張候補 E1/E2）** | **`orchestune/context`（`context-index-v2`）への計画単位 snapshot 追加と `skills/orchestune/SKILL.md` への任意接続** | (a) **データ網羅性**: `restore_plan_from_parent` は厳格な行数・見出し一致を要求するため実履歴 9 件中 4 件（`#1230, #1368, #1421, #1841`）で失敗した。共有索引の `fetch_context` / `extract_shared_artifacts` 拡張時は `decomposition_plan_from_parent_body`＋`parse_task_from_issue`＋`parse_from_comments` の寛容な読み取り経路を使い、Outcome 未記録の子タスク（`result="unknown"`, `missing_reason` 必須）および `blocked` タスクを欠落させず親単位の `plan_revision_digest` とともに索引化する。<br>(b) **分解手順への任意接続**: 上記 R1/R2 適用後に 5 ペア中央値 `TotalTokens(B) - TotalTokens(A) <= 0` を確認した段階で、`skills/orchestune/SKILL.md` の Step 1（初期コードベース調査の前）に「任意の類似計画カード参照手順（失敗・`no_matches` 時は警告のみで通常分解へ進む soft guidance）」として接続する。 |

---

## 7. 固定原本・実行ログの参照先と保持期間・セキュリティ確認

### 7.1 評価セッション成果物と SHA-256 ダイジェスト一覧

本評価で生成・使用したすべての固定原本・リクエスト・カード・匿名化計画・実行ログは、リポジトリ相対パス `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/` 配下に保管されている。

| 成果物種別 | リポジトリ相対パス | 件数 / サイズ | SHA-256 ダイジェスト |
| --- | --- | --- | --- |
| 事前登録マニフェスト | `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/pre_registration_manifest.json` | 1 ファイル (`9,012` B) | `b7c8bd199da96c581dbe6a675e1ddb682b6ce50d29bbf531125c5b0380b06ac5` |
| 固定資料 (`planning-history-corpus-v1`) | `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/corpus.json` | 12 cases (`120,615` B) | `6c9fac76039321bf2d2c33307524b7258128e52ba0312b82b1fd2bb5d4919ae7` |
| 固定原本スナップショット群 | `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/raw-sources/` | Issue 本文・Outcome コメント原本 | §3.2 出典 Manifest に全件記載 |
| 検索リクエスト群 (`planning-history-query-v1`) | `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/requests/task-<N>-request.json` | 6 ファイル | §2.3 テーブルに全 6 件記載 |
| 生成カード群 (`planning-history-cards-v1`) | `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/cards/task-<N>-cards.json` | 6 ファイル | §3.2 テーブルに全 6 件記載 |
| 全試行実測・プロンプト・ログ台帳 | `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/measurements.json` | 12 試行 (`68,558` B) | `57dd3cec90e80edd9daa9fb0ca154c3d1d544d220fed2db912f2b35e91f14c28` |
| 盲検採点・ペア比較集計 JSON | `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/blind_scores_and_summary.json` | 1 ファイル (`6,759` B) | `2c3f40977d3ebffaa5adf18f716fc1426575fa636f65ac306e0e311aa7b6fa1a` |

#### 試行別プロンプト・JSONL イベントログ・生計画・盲検化計画の SHA-256 一覧

| 実行順 | `trial_id` (`blind_id`) | プロンプト SHA-256 (`prompts/task-<N>-arm-<A|B>.txt`) | 実行ログ SHA-256 (`logs/<trial_id>.jsonl`) | 生計画 SHA-256 (`raw-plans/<trial_id>-raw.md`) | 盲検計画 SHA-256 (`blind-plans/<blind_id>.md`) |
| ---: | --- | --- | --- | --- | --- |
| 1 | `trial-01-task-1421-A` (`blind-07`) | `7eac073b5d953164ee9124a6b797b70efb9b0c5fd0c29fe771e4f54c5bd79427` | `1df0f24ee4c74ba0e8295ade8232c1c2ab367f81025a9b77aa356ce59199aefe` | `0a69767f0ca0f45d4c40b948237715a042f5a734aa3b1d007770bb386042d96f` | `e7219d81e1617bafa6921870514618b029f03d62fcb7f80e8aff369a76525d06` |
| 2 | `trial-02-task-1421-B` (`blind-02`) | `b9cb4ac2d7cc9a22fc523b38a1e3095500c23f8c113142a9ab564d5bfdbc4a2b` | `89b23627c5c7b44495bc11271afe90631dc84ffb3557a15f9460fed28839ff54` | `6411f5351587c7f3f681f795c03ac5068e6afb7be086fb234a4130f94c23f571` | `1e44ccae7d12806c75224f8b4d0f299fd31f4923694ac4b873f32ab0170ced40` |
| 3 | `trial-03-task-1588-B` (`blind-11`) | `f2f952ada3ef8f7d1d6bc0c09c21b4fc5eeb850dc88cf172403c52b8d1b34c9c` | `f7ed5df2a7252a186679be89edfe89914304ad82da8d82a7f14d1083da3e2165` | `3369b915f9e2b7823a292bc37a538c309ccb214d183e8ec3933f8bbdf556d297` | `e5d1d5e003492a4ffe948d0f4e78cea5ce9cf4dd8037be8e88fcc1cebd9ed75b` |
| 4 | `trial-04-task-1588-A` (`blind-04`) | `d96f7e1d4f015da62f2ee10e42e2f755ff502eb795000b5dc7932e5ed61cbf77` | `cd6e87a28afea7b9c8b807e491621c4f4b070db2b7af0b71b1d478cdf115b4f2` | `c39540484faaa9619286bf824b74586a02606fcd9688feb8b9e68f0fd7571abc` | `c39540484faaa9619286bf824b74586a02606fcd9688feb8b9e68f0fd7571abc` |
| 5 | `trial-05-task-1589-A` (`blind-09`) | `46300f9c6547bb3a7d7349b2696a9d3adc9e6b6796dc5b61b49d30bced760ee5` | `47a7b6102b1caf1a3fb3a8c322a788e1129ff2b32ebc13d141c895317a51773a` | `12c05e7d446026c133a09d5dae5eb4192ddf425f458ccb3d77c5cb3b76a816ed` | `aaa92cd51f45926c040e5b53e89913eb0ef1c5dae541fcc82bec1d8f7b7524c8` |
| 6 | `trial-06-task-1589-B` (`blind-01`) | `e357ae0df123969f0e7b34835faa1905ce80b2cc74a157d02e54b409e5788790` | `682ede14b0129178733f563fb15fe4e428998d69876b288eea5d88a16f39d5c5` | `f9da4da617f2618642e1a5ff27ab9aa08a8f93ad9cf6cc14f6ad3d6298d6b31a` | `5415cabc79d2536121df182a92e3caf6457e6343ea18ca56c2a066ed34625650` |
| 7 | `trial-07-task-1590-B` (`blind-06`) | `8400536cf0df2ee1cfea0a138551bddb50b230071ae17330c92909ce980a11c6` | `d4152049ce795d6d6c18faba597fa72a9f64b7f232ad5d46bfa8411df372634b` | `c5b5ded0573a8b93bbaa2b98d014f7ee420d0b944c0986fad397fa8cca9442c2` | `663a57571e0f7a0a5087659e7c2d37cb397875992c6ba63851e9d78c67bdef0a` |
| 8 | `trial-08-task-1590-A` (`blind-12`) | `9040c5e86d4796cd448bd2e9310bb6b9ded195ff6e01d052e892d3ec5b3f091b` | `b2f8a49d0e7aa683d59f75a157df3bd93b69639caeeb3b5bd96b2dc000aa725c` | `b784b940a4f9018cd2f42642df7f7871a672ae3c8be6ce05ec2b214c09db8d65` | `c8b5192fc5f58c412b17c3d32ff09b73d2d57e068d6d35e4942a968bd90b7be7` |
| 9 | `trial-09-task-1730-A` (`blind-03`) | `8cf1e312b52b27289114e5a3dcbd76f681243a2e81032adfbc1286091c3cd098` | `ff5bb7f464a0b1607d8d515d6ccb4c202d763cad0b1c9751e44b244083149980` | `86611be260085d62c0388703dea32841fad2ecdc2fe3d425678e49cbfefd08ee` | `eb8d4cd97a835c5c5070fd7f90153608cf9bac9c91fe95fa6a52ac43e1979fc0` |
| 10 | `trial-10-task-1730-B` (`blind-10`) | `8016e5ee15ffd96531b27283107a16dae0441f9ccfd1a96a9befdce056fd6545` | `e3fbdef744fa3df916cfbeed37def807edcd9bcfaf6a79e10f59291d5b48429a` | `2e29cb198a3c1fa3282b2bae8a53b8ba103b5f496da00138a417dac9852d9dfe` | `194955bcb35663b60eaa9616db1748c8b8007a138ca7847b7020b82f32a728de` |
| 11 | `trial-11-task-1841-B` (`blind-05`) | `48c3d144ec06acc5c7fe8646ad06f38fe1685a883a4f8bd8bb44edd6e7363f0b` | `351251ccddef0007a7dad2885ad3f2e2fe164c93189918ddb8fd983b4934326e` | `ba452572e793d4662d5d52f580307e0d6944c5abd79d77229f238b46e02653f8` | `13c596b02ff209e137379fb2d187fbcdb678f5d31eef643a79e2ac757f5e663c` |
| 12 | `trial-12-task-1841-A` (`blind-08`) | `a314c987c6a777d5648899fee26ced6752676080454af7730b3301ed66f43bf2` | `b72c09211cfcf4ccf4e87cb43020ad44829122f6e20f923be17e769b6be0da23` | `dda827d857e584f3ff82ad6a605b678fe06d559d84f90b5e153ca9530a507430` | `b6470646a087b5cfbf6ea7819acb9c7a6834b4a4ff3e016d3df2c39638d8262f` |

### 7.2 保持期間・再現検証手順・セキュリティ／パス規律

1. **保持期間（Retention Period）**:
   - `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/` 配下の固定原本・リクエスト・生成カード・匿名化計画・JSONL 実行ログは、**親 EPIC [#1842](https://github.com/Saltmu/orchestune/issues/1842) が完了（クローズ）し、かつ本 PR 統合後少なくとも 30 日間（または §6.4 の再設計 R1/R2 適用後の再評価が完了するまで）** ローカル評価セッションとして保持する。
   - Git 管理下の本レポート（`docs/plans/planning-history-poc-eval.md`）には全資料の GitHub パーマリンク、`available_at`、および UTF-8 生バイト SHA-256 ダイジェストを恒久記録しているため、一時ディレクトリ清掃後も GitHub API から同一版を取得して SHA-256 を照合可能である。出典または版時点の SHA-256 一致が確認できない外部資料は再現可能として扱わない。
2. **セレクタ出力の決定論的再現コマンド**:
   ```bash
   for pnum in 1421 1588 1589 1590 1730 1841; do
     uv run python -m scripts.planning_history_poc \
       --corpus .orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/corpus.json \
       --request ".orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/requests/task-${pnum}-request.json" \
       --limit 3 --max-bytes 12288 | sha256sum
   done
   ```
3. **機密情報および環境固有の絶対パス排除の確認**:
   - 本レポート内のすべてのファイル参照はリポジトリ相対パス（`docs/plans/...`, `scripts/...`, `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/...`）および正規化済み `"Saltmu/orchestune"` / GitHub URL のみで記述しており、環境固有の絶対ローカルパスや API キー・トークン等の機密情報を一切含まない（`./scripts/local-ci.sh` 内の `gitleaks` およびパス検査で検証済み）。
