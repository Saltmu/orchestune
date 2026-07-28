# Orchestune アーキテクチャ調査 & リファクタリング計画

> **本ドキュメントの位置づけ**: ユーザー向けドキュメント（`docs/en/`, `docs/ja/`）ではなく、
> 開発者向けの内部作業計画書です。各フェーズの完了に伴い更新されます。
>
> 調査時点: `43ffd7d`（`main` マージ直後）

---

## 0. サマリ

| 指標 | 値 |
| --- | --- |
| プロダクションコード | 41 モジュール / 10,150 行 |
| テストコード | 36 ファイル / 22,864 行（プロダクションの **2.25 倍**） |
| 循環インポートに参加するモジュール | 41 中 **19**（`dispatch_*` / `integrator_*` のほぼ全域） |
| `git` を直接 subprocess 実行するモジュール | **12** |
| 最大モジュール | `github.py` (880), `dispatch_cycle.py` (870), `dispatch_gc.py` (846) |
| 最大テストファイル | `test_dispatcher.py` (4,288 行) — 対応する `dispatcher.py` は 565 行 |

責務分割そのものは概ね妥当（DAG 層 / dispatch 層 / integrator 層は明確に分かれている）。
問題は **層の内部ではなく層の「境界」に集中**しており、そのほぼすべてが
**後方互換ファサード（`dispatcher.py` / `dag.py`）とテストのモックパッチ先が結合している**
という一点に由来します。

---

## 1. 現状アーキテクチャ

### 1.1 意図された層構造

```
cli.py  ──┬─> dag.py ──────────> dag_cli / dag_graph / dag_parsing / dag_similarity
          │                      / dag_contracts / dag_models
          ├─> dispatcher.py ───> dispatch_cycle ─> dispatch_{launch,gc,rebase,locks,
          │                                          escalation,recovery,rules,scoring,
          │                                          state,targets,worktree,config}
          ├─> monitor.py
          ├─> setup_skills.py
          └─> bootstrap.py ────> forge.py

                integrator.py ─> integrator_{git_ops,pr,tasks,worktree}
                                 integration_coordinator / parent_completion

  共通基盤: github.py / process_utils.py / json_state.py / not_needed_review_state.py
```

この構造自体は健全です。`dag_*` 群（6 モジュール）と `integrator_*` 群は、
過去のリファクタリングで適切に分割済みであることが確認できます。

### 1.2 実際の依存グラフ

しかし静的解析の結果、**実際には巨大な循環の塊**になっています。

```
dispatch_config -> dispatch_targets -> dispatcher -> integration_coordinator
  -> dispatch_worktree -> dispatch_gc -> dispatch_rules -> dispatch_config   （8 ノード循環）

integrator_pr -> dispatcher -> integrator -> integrator_git_ops -> integrator_pr

dispatcher -> dispatch_report -> dispatch_cycle -> dispatch_recovery -> dispatcher
```

**扇入（fan-in）上位**:

| モジュール | 被参照数 | 備考 |
| --- | --- | --- |
| `github` | 21 | 唯一の GitHub/git アクセス層。妥当だが肥大化 |
| `dispatch_scoring` | 12 | `Task` 型の所在地。名前と責務が不一致 |
| `dispatch_state` | 10 | 妥当 |
| **`dispatcher`** | **8** | **CLI エントリポイントが被参照 8 — 逆流** |

---

## 2. 発見された課題（優先度順）

### 【P1】`dispatcher.py` が後方互換ファサードとして循環の中心になっている

`dispatcher.py` は本来 `orchestune dispatch` の CLI エントリポイントですが、
実体は **28 シンボルの再エクスポート `__all__`** を持つファサードです。

そして下位モジュールがこのファサードから型を輸入し返しています:

| 参照元 | 参照内容 | 実体の所在 |
| --- | --- | --- |
| `integrator.py:11` | `from orchestune.dispatcher import Task, file_lock` | `dispatch_scoring` / `dispatch_worktree` |
| `integrator_pr.py:6` | `from orchestune.dispatcher import Task` | `dispatch_scoring` |
| `integrator_git_ops.py:9` | `from orchestune.dispatcher import Task` | `dispatch_scoring` |
| `integrator_tasks.py:7` | `from orchestune.dispatcher import Task, parse_task_from_issue` | `dispatch_scoring` |
| `dispatch_targets.py:30` | `from orchestune.dispatcher import Task`（TYPE_CHECKING） | 同上 |
| `dispatch_recovery.py:16` | `from orchestune.dispatcher import DispatcherConfig`（TYPE_CHECKING） | `dispatch_config` |
| `dispatch_actor_verification.py:13` | 同上（TYPE_CHECKING） | 同上 |

結果として `integrator_pr` を import しただけで、argparse・tomllib・
dispatch サブシステム全体がロードされます。

さらに、この循環を回避するために **関数内 import が 12 箇所**に散在しています
（`dispatch_gc.py:328-329`, `dispatcher.py:281/318/327/362`,
`dispatch_cycle.py:649-650/733` など）。これは循環を「隠している」だけで
解消しておらず、実行時にしか壊れが露見しないリスクを抱えています。

**根本原因**は `dispatcher.py:12-16` のコメントが自ら明言しています:

```python
# 実装コード自体はgithub.*を直接呼ばないが、tests/test_dispatcher.pyが
# orchestune.dispatcher.github.* をmock.patchの対象にしている（...）
# このimportを消すとそれらのパッチがAttributeErrorで壊れるため、意図的に保持する。
```

**プロダクションコードの構造が、テストのモックパッチ先によって固定されている**
状態です。テストは `patch("orchestune.dispatcher...")` を **270 回**使用しており、
ファサード経由の参照はテスト全体で **364 箇所**あります。

### 【P2】`git` 実行が 12 モジュールに散在し、共通の抽象が無い

`gh` CLI の呼び出しは `github.py` の `_run()` に集約されていますが、
**生の `git` コマンドは 12 モジュールがそれぞれ独自に `subprocess.run` しています**。

しかも規約が統一されていません:

| 流儀 | 例 |
| --- | --- |
| `git -C <path>` + `text=True` + `check=True` | `dispatch_gc.py:30` |
| `cwd=` + `capture_output=True` + `check=True`（バイト列） | `integrator_worktree.py:29` |
| `cwd=` + `capture_output=True`（`check` なし・戻り値無視） | `integrator_worktree.py:43` |

エラーハンドリングも `(CalledProcessError, OSError)` を握り潰す / 個別に文字列化する /
そのまま伝播させる、が混在しています。

これは **テスト量が肥大している主因**でもあります。12 モジュールそれぞれで
`subprocess.run` をモックする必要があり、テスト/プロダクション比が 2.25 倍に
達している大きな要因です。

### 【P3】`github.py` が「GitHub API」と「ローカル git」を同居させている

880 行・33 関数のうち、**5 関数は `gh` ではなく `git` を実行**しています:

- `list_remote_branches` (24 行)
- `branch_changed_files` (45 行)
- `ensure_parent_branch` (53 行)
- `resolve_local_or_remote_branch` (47 行)
- `fetch_remote_branch` (14 行)

「リモートの GitHub 状態を問い合わせる」層に、「ローカルワークツリーを操作する」
関数が混ざっており、テスト時のモック境界が曖昧になっています。

### 【P4】`forge.py` の抽象化が中途半端

`Forge` (ABC) / `GitHubForge` という抽象が定義されていますが、
**利用者は `bootstrap.py` 1 モジュールのみ**です。他の 21 モジュールは
`github.py` のモジュールレベル関数を直接呼んでいます。

抽象を導入する方針だったのか、`bootstrap` 専用のユーティリティなのかが
コードから読み取れません。どちらかに倒す必要があります。

### 【P5】`dispatch_scoring.py` が名前と責務不一致の事実上のドメインモデル置き場

被参照 12 の `dispatch_scoring.py` (207 行) が抱えているのは 3 つの別責務です:

1. **ドメインモデル**: `Task` dataclass ← 12 モジュールが参照するのはほぼこれだけ
2. **パース**: `parse_task_from_issue` (86 行) と `_FOOTPRINT_BLOCK_PATTERN`
3. **スコアリング**: `quota_available` / `compute_priority_score` / `select_next_tasks`

さらに `_FOOTPRINT_BLOCK_PATTERN`（プライベート定数）が
`dispatch_recovery.py:13` と `integrator_tasks.py:13` から輸入されており、
パースロジックが 3 箇所に漏れています。

### 【P6】テストの構造がプロダクションの分割に追従していない

`dispatcher.py` は 565 行まで縮小済みですが、`test_dispatcher.py` は **4,288 行**のままです。
中身を見ると、テスト対象は既に他モジュールへ移動しています:

| `test_dispatcher.py` 内のクラス | 実際のテスト対象 |
| --- | --- |
| `TestGC` (2474-), `TestIsWorktreeComplete` (881-) | `dispatch_gc.py` |
| `TestSyncExternalLocks` (3200-) | `dispatch_cycle.py` / `dispatch_locks.py` |
| `TestRunDispatchCycle*`（5 クラス） | `dispatch_cycle.py` |
| `TestBranchStacking` (1866-) | `dispatch_rebase.py` |

加えて `conftest.py` が存在せず、テストヘルパが重複しています:

- `_task()` ファクトリ: **12 ファイルで重複定義**
- `_issue()` ファクトリ: **5 ファイルで重複定義**
- `file_lock` の no-op スタブ（autouse）: `test_integrator.py` と `test_dispatcher.py` に別実装

### 【P7】`__init__.py` が無く、公開 API 境界が未定義

`orchestune/` に `__init__.py` が無い暗黙名前空間パッケージです。
`mypy` の `explicit_package_bases` と pytest の `pythonpath=["."]` で辻褄を
合わせていますが、「どれが公開 API でどれが内部実装か」の宣言が存在しません。
これが P1 の「下位モジュールが CLI モジュールから型を輸入する」を
誰も止められない構造的な理由になっています。

---

## 3. 目標アーキテクチャ

```
  L4  entrypoints/     cli.py, dispatcher.py, dag.py, monitor.py, bootstrap.py
        ↓                  （argparse と main() のみ。再エクスポートしない）
  L3  workflows/       dispatch_cycle, integrator, integration_coordinator,
        ↓                parent_completion
  L2  domain/          models.py (Task/SubTask/RunState), issue_parsing.py,
        ↓                scoring.py, dag_*, dispatch_{gc,launch,rebase,locks,...}
  L1  adapters/        github_api.py (gh), git_cli.py (git), forge.py
        ↓
  L0  infra/           process_utils, json_state, file_lock
```

**不変条件（CI で機械的に検証する）**:
1. 依存は上から下へのみ。L4 は誰からも import されない。
2. `git` / `gh` の `subprocess` 呼び出しは L1 の中だけに存在する。
3. 循環インポートはゼロ。関数内 import による回避も禁止。

---

## 4. リファクタリング計画（フェーズ分割）

各フェーズは **独立した PR** とし、`./scripts/local-ci.sh` を通してからプッシュします。
**フェーズ 0 → 1 → 2 の順序は依存関係があるため固定**、3 以降は並行可能です。

---

### フェーズ 0: 安全網の整備（リスク: 低）

リファクタリング前に、退行を検知する仕組みを先に入れます。

| # | 作業 | 成果物 |
| --- | --- | --- |
| 0-1 | `tests/conftest.py` を新設し、`_task()` / `_issue()` / `_pr()` ファクトリと `file_lock` no-op fixture を集約（12 + 5 箇所の重複を解消） | `tests/conftest.py` |
| 0-2 | 循環インポート検知テストを追加。`ast` で `orchestune/` の import グラフを構築し、循環がゼロであることを表明（**現状は失敗するので `xfail` で開始し、フェーズ 2 完了時に解除**） | `tests/test_architecture.py` |
| 0-3 | 「`subprocess` で `git`/`gh` を呼ぶのは許可リスト内のモジュールのみ」を表明するテストを追加（現状の 12 モジュールを初期許可リストとし、フェーズ 3 で縮小） | 同上 |
| 0-4 | 層違反検知テスト（L4 モジュールが被 import されていないこと）を追加。同じく `xfail` で開始 | 同上 |

**なぜ最初か**: 以降のフェーズが「本当に循環を減らしたか」を主観でなく
CI で判定できるようにするため。CLAUDE.md の TDD 方針にも沿います。

**検証**: `./scripts/local-ci.sh` がグリーン（新規テストは `xfail` 込み）。

---

### フェーズ 1: ドメインモデルの抽出（リスク: 中）

P1 と P5 の根本原因を断ちます。**これが計画全体の要**です。

| # | 作業 |
| --- | --- |
| 1-1 | `orchestune/models.py` を新設し、`Task` dataclass を `dispatch_scoring.py` から移設 |
| 1-2 | `parse_task_from_issue` と `_FOOTPRINT_BLOCK_PATTERN` を `orchestune/issue_parsing.py` へ分離（`_FOOTPRINT_BLOCK_PATTERN` は公開名 `FOOTPRINT_BLOCK_PATTERN` に改名し、`dispatch_recovery` / `integrator_tasks` の privateシンボル輸入を解消） |
| 1-3 | `dispatch_scoring.py` はスコアリング専任に縮小（`quota_available` / `compute_priority_score` / `select_next_tasks`、約 80 行） |
| 1-4 | `DispatcherConfig` を参照する 2 つの TYPE_CHECKING import を `dispatch_config` 直参照に付け替え |
| 1-5 | `integrator*.py` 4 ファイルの `from orchestune.dispatcher import Task` を `from orchestune.models import Task` に付け替え |
| 1-6 | `dispatch_targets.py` の TYPE_CHECKING import を `orchestune.models` に付け替え |

**この時点で解消される循環**: `integrator_pr ↔ dispatcher`,
`integrator_git_ops ↔ dispatcher`, `integrator_tasks ↔ dispatcher`,
`dispatch_targets ↔ dispatcher`, `dispatch_recovery ↔ dispatcher`,
`dispatch_actor_verification ↔ dispatcher`（**6 系統**）

**リスクと緩和**: `dispatch_scoring.Task` を参照している 12 モジュール +
テストが影響を受けます。`dispatch_scoring.py` に一時的な再エクスポート
（`from orchestune.models import Task`）を残し、テストの付け替えは
フェーズ 5 で行うことで、1 PR あたりの差分を抑えます。

**検証**: `local-ci.sh` グリーン + フェーズ 0-2 の循環数が 6 系統減っていること。

---

### フェーズ 2: ファサードの解体（リスク: 中〜高）

| # | 作業 |
| --- | --- |
| 2-1 | `dispatcher.py` の `__all__` 再エクスポート（28 シンボル）を削除。`_build_arg_parser` / `main` / `_run_semantic_integrator` などの CLI 責務のみ残す（565 → 約 300 行を想定） |
| 2-2 | `dispatcher.py:17` の `from orchestune import github  # noqa: F401` を削除（フェーズ 5 のテスト付け替えが前提） |
| 2-3 | `dispatcher.py` 内の関数内 import 4 箇所（`integration_coordinator`, `integrator`, `parent_completion`）をモジュールレベルに引き上げ |
| 2-4 | `dispatch_gc.py` の `is_process_alive` 再エクスポート（`# noqa: F401`）を削除し、`monitor.py:import` を `process_utils` 直参照へ |
| 2-5 | `dispatch_gc.py:328-329` の関数内 import を解消（`integration_coordinator` への依存方向を見直し。`_finalize_not_needed_worktree` の呼び出し側注入に変更） |
| 2-6 | `dispatch_cycle.py:649/650/733` の関数内 import をモジュールレベルへ |
| 2-7 | フェーズ 0-2 / 0-4 の `xfail` を解除 |

**リスクが最も高いフェーズ**。`dag.py` のファサードは
（`dag_*` 群が既に非循環で、`cli.py` からのみ使われるため）**あえて残します** —
コストに対して得るものが小さいためです。ただし `dispatch_rebase.py:14` と
`dispatch_cycle.py:649` の `from orchestune.dag import ...` は
`dag_graph` 直参照に変更します。

**検証**: `local-ci.sh` グリーン + 循環インポート **ゼロ** をテストで表明。

---

### フェーズ 3: git アダプタの新設（リスク: 中）

P2 / P3 に対応します。フェーズ 1・2 と独立して着手可能です。

| # | 作業 |
| --- | --- |
| 3-1 | `orchestune/git_cli.py` を新設。`run_git(args, *, cwd, check=True) -> GitResult` を単一の実行口とし、`text=True` 統一・`CalledProcessError`/`OSError` の一貫したハンドリングを提供 |
| 3-2 | `github.py` から git 実行の 5 関数（`list_remote_branches`, `branch_changed_files`, `ensure_parent_branch`, `resolve_local_or_remote_branch`, `fetch_remote_branch`）を `git_cli.py` へ移設。`github.py` は 880 → 約 700 行 |
| 3-3 | `integrator_worktree.py` / `integrator_git_ops.py`（`subprocess.run` 13 箇所）を `run_git` 経由に統一 |
| 3-4 | `dispatch_gc.py`（9 箇所）/ `dispatch_worktree.py`（7 箇所）/ `dispatch_rebase.py`（4 箇所）を同様に移行 |
| 3-5 | 残り（`dispatch_launch`, `dispatch_locks`, `dispatch_recovery`, `dispatch_targets`, `bootstrap`）を移行し、フェーズ 0-3 の許可リストを `git_cli.py` / `github.py` / `forge.py` の 3 つに縮小 |

**期待効果**: テストのモック対象が `run_git` 1 点に集約され、
12 モジュール分の `subprocess.run` モック（テスト肥大の主因）が不要になります。
**テストコード削減が最も見込めるフェーズ**です。

**注意**: `bootstrap.py` の `_initialize_empty_repo` は空リポジトリ初期化という
特殊操作のため、移行対象から外すか個別判断とします。

---

### フェーズ 4: `forge.py` の方針決定（リスク: 低）

P4 に対応。**実装前にユーザー判断が必要**です。以下 2 案:

- **案 A（推奨）**: `Forge` 抽象を破棄し、`forge.py` を
  `bootstrap_labels.py` へ改名して bootstrap 専用ユーティリティと明示する。
  他の 21 モジュールの利用実態（モジュール関数直呼び）に合わせる。
  差分小。GitHub 以外のフォージ対応予定が無いなら妥当。
- **案 B**: `Forge` 抽象を本気で採用し、`github.py` の関数群も
  `Forge` プロトコル配下に移す。GitLab 等への対応余地を残せるが、
  21 モジュール + テスト 102 箇所の `patch("orchestune.github...")` に波及し、
  影響は本計画中で最大。

現時点で GitLab 等の対応要件がドキュメント・Issue に見当たらないため、
**案 A を推奨**します。

---

### フェーズ 5: テスト構造の再編（リスク: 低・工数大）

P6 に対応。プロダクション側の構造が固まってから実施します。

| # | 作業 |
| --- | --- |
| 5-1 | `test_dispatcher.py` (4,288 行) を分割。`TestGC` / `TestIsWorktreeComplete` → `test_dispatch_gc.py`、`TestRunDispatchCycle*` 5 クラス → `test_dispatch_cycle.py`、`TestSyncExternalLocks` → `test_dispatch_locks.py`、`TestBranchStacking` → `test_dispatch_rebase.py` へ移設。残る `test_dispatcher.py` は CLI/引数解析/`main()` のテストのみ（約 900 行を想定） |
| 5-2 | `patch("orchestune.dispatcher.github...")`（270 箇所）を実体モジュール（`dispatch_cycle` / `dispatch_gc` 等）へのパッチに付け替え |
| 5-3 | `test_integrator.py` (2,577 行) / `test_github.py` (2,017 行) を責務単位に分割 |
| 5-4 | フェーズ 3 完了後、`run_git` モックに統一できるテストを整理し、重複した subprocess モックを削除 |

**期待効果**: テスト/プロダクション比 2.25 倍 → 1.5 倍程度への縮小。
カバレッジ閾値（`--cov-fail-under=75`）は維持します。

---

### フェーズ 6: パッケージ境界の明示（リスク: 低）

| # | 作業 |
| --- | --- |
| 6-1 | `orchestune/__init__.py` を追加し、`__version__` と公開 API のみを `__all__` で宣言 |
| 6-2 | `docs/en/architecture.md` / `docs/ja/architecture.md` に「モジュール層構造」節を追記し、L0〜L4 の不変条件を明文化 |
| 6-3 | `pyproject.toml` の `explicit_package_bases` 設定が不要になれば整理 |

**注意**: `__init__.py` の追加は poetry のパッケージング挙動に影響し得るため、
`poetry build` の成果物を追加前後で比較検証すること。

---

## 5. 実施順序とフェーズ間の依存

```
0 (安全網)
 └─> 1 (ドメインモデル抽出)
      └─> 2 (ファサード解体) ──┐
                                ├─> 5 (テスト再編) ─> 6 (パッケージ境界)
3 (git アダプタ) ───────────────┘
4 (forge 方針) ── 独立（ユーザー判断待ち）
```

フェーズ 0・1・2 が本計画の中核で、これだけで P1・P5・P7 の大部分が解消します。
**工数対効果が最大なのはフェーズ 3**（テスト肥大の主因への対処）です。

---

## 6. 本計画で扱わないもの（Non-goals）

- **機能変更・振る舞いの変更**: すべて純粋なリファクタリングとし、
  既存テストが（移設・パッチ先変更を除いて）意味的に不変であることを担保します。
- **`dag_*` 群の再分割**: 6 モジュールに適切に分割済みで、循環もありません。
- **`dag.py` ファサードの撤廃**: 循環を生んでおらず、撤廃コストが便益を上回ります。
- **`integrator.py` の 677 行**: `IntegrationComponent` によるパイプライン
  パターンで既に構造化されており、行数以外の問題は見当たりません。
  フェーズ 3 で `run_git` 移行後に再評価します。
- **CLI インタフェース・`decomposition_plan.md` スキーマの変更**:
  `tests/test_docs_spec_sync.py` がドキュメントとの同期を検証しているため、
  変更する場合は docs（en/ja 両方）も同時更新が必要です。

---

## 7. 承認をお願いしたい点

1. **フェーズ全体の方針**と実施順序（特にフェーズ 0 の安全網を先に入れること）
2. **フェーズ 4 の案 A / 案 B の選択**（GitHub 以外のフォージ対応予定の有無）
3. **フェーズ分割の粒度** — 各フェーズを個別 Issue + PR とするか、
   0〜2 をまとめて 1 Issue とするか
