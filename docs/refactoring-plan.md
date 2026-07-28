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

P2 / P3 に対応します。案 B 採用に伴い、`github.py` を gh 専任にする
**フェーズ 4 の前提**でもあります。

> **訂正**: 当初「フェーズ 1・2 と独立して着手可能」と記載しましたが、
> footprint レベルでは衝突します。`orchestune-dag` による検証で、
> `dispatch_recovery.py` / `dispatch_targets.py` の重複により
> フェーズ 1 との間に暗黙エッジ（similarity 0.30）が自動挿入されました。
> `dispatch_gc.py` / `dispatch_rebase.py` はフェーズ 2 とも衝突します。
> 独立して着手できるのは 3-1・3-2（`git_cli.py` 新設と `github.py` からの分離）
> までで、3-3 以降の展開はフェーズ 2 の後になります。

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

### フェーズ 4: `Forge` 抽象の全面採用（リスク: 高）— **案 B 採用決定**

P4 に対応。2 案を提示した結果、**案 B（`Forge` 抽象を全面採用し、`github.py`
の関数群も `Forge` プロトコル配下に移す）が採用**されました。GitLab 等への
対応余地を残せる一方、21 モジュール + テスト 102 箇所の
`patch("orchestune.github...")` に波及し、影響は本計画中で最大です。

#### 呼び出し実態の実測（`github.*` 参照 122 箇所 / 20 モジュール / 28 シンボル）

| 分類 | 参照数 | 移行先 |
| --- | --- | --- |
| ラベル・コメント変更（`remove_label` 21 / `add_label` 19 / `add_comment` 16） | **56 (46%)** | `IssueForge` |
| その他 forge 操作（issue/PR の照会・作成・マージ） | 38 | `IssueForge` / `PullRequestForge` |
| **DTO（`IssueRecord` 9 / `PrRecord` 8）** | **17** | **`models.py`（Forge 配下ではない）** |
| git 実行（`resolve_local_or_remote_branch` ほか 5 関数） | 9 | `git_cli.py`（フェーズ 3） |
| private バリデータ（`_validate_ref_name` / `_validate_label`） | 2 | `validation.py` |

#### 設計上の決定

1. **DTO を Forge 配下に置かない**: `IssueRecord` / `PrRecord` は操作ではなく
   ドメインモデルです。`models.py` へ移すことで Forge を操作専任にできます。
   併せて、現状 `forge.py` が `github.py` から `_validate_label` を輸入している
   依存方向の逆流も解消します。
2. **プロトコルを 3 分割する**: 28 シンボルを単一インタフェースにすると
   神インタフェースになるため、`IssueForge` / `PullRequestForge` /
   `RepoAdminForge` に分けます。
3. **git は Forge に含めない**: git はローカル VCS 操作であり、フォージ
   （GitHub/GitLab）の関心事ではありません。フェーズ 3 の `git_cli.py` として
   別アダプタのままにします。
4. **移行は委譲シム方式で段階化する**: プロトコル定義の時点で `github.py` の
   モジュール関数を既定 Forge インスタンスへの委譲シムに変え、21 モジュールの
   呼び出しを**無変更のまま**動かします。呼び出し側の移行はモジュール群ごとに
   分割し、最後にシムを撤去します。

#### 注入経路の問題

DI の注入経路が半分しか存在しません。

| 状態 | モジュール |
| --- | --- |
| `config` が流れている（フィールド追加のみで済む） | `dispatch_cycle`(13) / `integrator`(12) / `dispatch_gc`(10) / `dispatch_rebase`(5) |
| **流れていない（引数追加が必要）** | **`integrator_pr`(github 8 呼) / `parent_completion`(7) / `integration_coordinator`(5) / `integrator_tasks`(3)** |

`DispatcherConfig` / `IntegratorConfig` に `forge` フィールドを追加し、
config が流れていない 4 モジュールには明示的な `forge` 引数を追加します。

**`forge` フィールドには必ずデフォルトを持たせること。** 両 config の構築箇所は
**208 箇所**（`orchestune/dispatcher.py` + テスト 12 ファイル）あり、必須引数にすると
1 つの Issue で 208 箇所すべてを書き換えることになり、`main` へ直接マージする
運用と両立しません。既存の `dispatch_target` と同じイディオムを使います:

```python
forge: Forge | None = None   # __post_init__ で None なら GitHubForge() を生成
```

これにより既存の 208 箇所は**無変更のまま**動作し、テストは
`DispatcherConfig(forge=fake_forge)` で差し替えられるようになります。

**期待効果**: テストが `mock.patch` ではなく `fake_forge` の注入で書けるように
なります。`patch("orchestune.github...")` 102 箇所の解消が最終目標です。

---

### フェーズ 5: テスト構造の再編（リスク: 低・工数大）

P6 に対応。

> **訂正**: 当初「プロダクション側の構造が固まってから実施」と記載し、
> 順序図でもフェーズ 2 の後に置いていましたが、**5-1 と 5-2 はフェーズ 2 より
> 前に実施する必要があります**。`dispatcher.py:17` の
> `from orchestune import github  # noqa: F401` を削除できるのは、
> 270 箇所の `patch("orchestune.dispatcher.github...")` を実体モジュールへ
> 向け直した後だからです（フェーズ 2-2 の本文でも前提条件として言及して
> いましたが、順序図と矛盾していました）。5-3・5-4 は従来どおり後段です。

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

## 5. 実施順序と Issue 分割

フェーズは「作業のまとまり」を説明する単位であり、**Issue の単位ではありません**。
Issue は orchestune の流儀に従い、**footprint が互いに素になる単位**で切ります。
上記フェーズを組み替えた 17 subtask の分解結果を `decomposition_plan.md`
（リポジトリルート・`.gitignore` 対象の作業ファイル）に置いており、
`orchestune-dag` による検証を通過しています（Warnings なし）。

### 実施方式: 手作業 / Issue ごとに `main` へ直接マージ

ディスパッチャー（`orchestune-dispatch`）と Integrator の二層ブランチモデル
（`parent/issue-{N}`）は**使用しません**。各 Issue につき 1 ブランチ・1 PR を作成し、
それぞれ `main` へ直接マージします。親 Issue #280 は純粋なトラッキング Issue です。

この方式では **17 回の中間状態がすべて `main` に載る**ため、以下が要件になります。

1. **各 Issue 単独で `main` がグリーンかつリリース可能であること。**
   `./scripts/local-ci.sh`（ruff format / ruff check / mypy / pytest 93%+ / gitleaks）を
   各 PR で通す。CI は `main` への PR で自動実行されます。
2. **後方互換シムは「便宜」ではなく「必須」。**
   `dispatch_scoring.Task` の再エクスポート、`github.py` の git 関数再エクスポート、
   `github.py` の Forge 委譲シムは、Issue と Issue の間で `main` を動作させるための
   ものです。**`forge-cleanup` (#295) まで削除してはいけません。**
   再エクスポートは必ず同一オブジェクトへの別名とし、型を再定義しないこと
   （`isinstance` 判定が壊れるため）。
3. **`arch-guard` (#281) の `xfail` は対応 Issue が `main` に入るまで維持する。**
   `strict=True` にすると、解消前の `main` で CI が落ちます。

### 実施順序（Issue 番号順がそのまま妥当なトポロジカル順）

**#281 → #297 を番号順に進めれば依存関係を満たします。** 手作業では並列実行の
意味が薄いため、Wave ではなく直列順で示します。

| # | Issue | subtask | 依存 |
| --- | --- | --- | --- |
| 1 | #281 | `arch-guard` | — |
| 2 | #282 | `test-fixtures` | — |
| 3 | #283 | `domain-models` | #281 |
| 4 | #284 | `git-adapter` | #281 |
| 5 | #285 | `split-test-dispatcher` | #282 |
| 6 | #286 | `rewire-dispatch-imports` | #283 |
| 7 | #287 | `rewire-integrator-imports` | #283 |
| 8 | #288 | `forge-records` | #283, #284 |
| 9 | #289 | `dismantle-facade` | #285, #286, #287 |
| 10 | #290 | `forge-protocol` | #288 |
| 11 | #291 | `adapter-migrate-integrator` | #287, #290 |
| 12 | #292 | `adapter-migrate-dispatch-core` | #289, #290 |
| 13 | #293 | `adapter-migrate-dispatch-aux` | #286, #289, #290 |
| 14 | #294 | `adapter-migrate-entrypoints` | #289, #290 |
| 15 | #295 | `forge-cleanup` | #291〜#294 |
| 16 | #296 | `split-large-tests` | #295 |
| 17 | #297 | `package-boundary` | #295 |

`#291`〜`#294` は相互に独立、`#296` と `#297` も相互に独立なので、この 2 グループ内は
任意の順序で構いません。それ以外は上表の順序に従ってください。

PR 本文に `Closes #<番号>` を記載すればマージ時に Issue が自動クローズされます。
`status:*` ラベルはディスパッチャー向けの機構なので、手作業運用では
維持しなくても問題ありません（Issue のクローズ状態が実質的な進捗になります）。

### 分割にあたっての要点

- **`github.py` への手術は 3 回に分かれ、直列化が必須**です:
  `git-adapter`（git 関数の分離）→ `forge-records`（DTO・バリデータの分離）
  → `forge-protocol`（Forge クラス化）。
- **git 移行と forge 移行はモジュール群ごとに 1 Issue へ統合**します。
  両者は同じ IO 呼び出し箇所を書き換えるため、分離すると同じファイルを
  2 度触ることになり、直列化と手戻りが発生します。
- **`shared_contract` タグを 3 つ設定**しています。いずれも
  「まだ存在しないため、どの subtask の footprint にも literal では現れない
  共有拡張点」（architecture.md の Shared-Contract Gate が対象とするケース）です。

  | `shared_contract` | writer | pure consumer |
  | --- | --- | --- |
  | `domain-models` | `domain-models`, `forge-records` | `rewire-dispatch-imports`, `rewire-integrator-imports` |
  | `io-adapters` | `git-adapter`, `forge-records`, `forge-protocol`, `forge-cleanup` | `adapter-migrate-*` 4 件 |
  | `test-fixtures` | `test-fixtures` | `split-test-dispatcher`, `split-large-tests` |

- **`tests/conftest.py` は追加のみ**とします。当初案（17 ファイルの重複ヘルパを
  一括削除）はほぼ全テストファイルを footprint に含むため、他の全 Issue と
  衝突します。各ファイルの掃除はそのファイルを触る Issue に委ねます。

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

## 7. 決定事項

| # | 論点 | 決定 |
| --- | --- | --- |
| 1 | フェーズ全体の方針と実施順序 | 承認済み。安全網（#281）を最初に入れる |
| 2 | フェーズ 4 の案 A / 案 B | **案 B（`Forge` 抽象の全面採用）を採用** |
| 3 | Issue 分割の粒度 | footprint が互いに素になる単位（17 subtask）。§5 参照 |
| 4 | 実施方式 | **手作業。ディスパッチャー・Integrator は使用しない** |
| 5 | マージ方式 | **Issue ごとに `main` へ直接マージ**（二層ブランチモデルは使わない） |

### 起票済み Issue

親 Issue #280（トラッキング）+ 子 Issue #281〜#297（Sub-issue として紐付け済み）。
`decomposition_plan.md`（リポジトリルート・`.gitignore` 対象）に Issue 番号を
書き戻してあります。

GitHub ネイティブの `blocked_by` 依存は設定していません（起票に使用した
GitHub MCP がこのフィールドをサポートしないため）。依存関係は各 Issue 本文の
Footprint YAML の `depends_on` と冒頭の Issue 番号参照に記録されています。

### 次のアクション

§5 の順序に従って #281 から着手する。各 Issue につき 1 ブランチ・1 PR を作成し、
`./scripts/local-ci.sh` を通してから `main` へマージする。
