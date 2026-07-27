# 使用方法とコマンドリファレンス

Orchestuneの各CLIコマンド（`orchestune dag`、`orchestune dispatch`）の使い方、およびタスクの分解計画ファイル（`decomposition_plan.md`）の記述仕様について説明します。

---

## 1. タスク分解計画（Decomposition Plan）の仕様

メインとなる大きな開発タスク（「大きな石」）を並列実行可能なサブタスクに分解するために、リポジトリのルートに `decomposition_plan.md` というファイルを配置します。
このファイルは、上部にYAMLフロントマター形式でメタデータを記述し、下部（ボディ）に補足説明を記載する構成をとります。

### フォーマット例

```markdown
---
subtasks:
  - id: setup-database
    description: "データベーススキーマとコネクションプールの初期化"
    priority: high
    footprint:
      - src/db/connection.py
    symbols:
      - db.get_connection
    depends_on: []
    overview: "アプリ全体が利用するDB接続基盤を用意する。"
    acceptance_criteria:
      - "コネクションプールの初期化テストが通ること"
    proposed_changes:
      - "src/db/connection.py に get_connection を追加"
    verification_plan:
      - "poetry run pytest tests/test_connection.py"
    shared_contract: db-connection
    writes_shared_contract: true

  - id: user-auth
    description: "ユーザー認証エンドポイントの実装"
    footprint:
      - src/auth/routes.py
    symbols:
      - auth.login_user
    depends_on: [setup-database]
    shared_contract: db-connection
---
# タスク分解計画の説明
この計画は、構築に必要な手順をまとめたものです...
```

### フロントマターのスキーマ定義
各サブタスクは以下のフィールドを持ちます：

* **`id`** (文字列, 必須): サブタスクを一意に特定するための識別子。ブランチ名やIssueのタイトル等に使用されます。
* **`description`** (文字列, 任意, 既定値 `""`): タスクが行う内容の短い説明。リスク検知の入力に使われます。
* **`footprint`** (ファイルパスのリスト, 任意, 既定値 `[]`): このサブタスクが変更・作成・削除する予定のファイルパス（リポジトリルートからの相対パス）。
* **`symbols`** (文字列のリスト, 任意, 既定値 `[]`): このサブタスクが作成または変更する関数名やクラス名。
* **`depends_on`** (サブタスクIDのリスト, 任意, 既定値 `[]`): このサブタスクが開始される前に完了していなければならない先行サブタスクの `id` リスト。依存がない場合は空配列 `[]` を指定します（省略した場合も依存なしとして扱われます）。
* **`priority`** (文字列, 任意, 既定値 `medium`): サブタスクの優先度。`high` / `medium` / `low` のいずれか。これ以外の値を指定した場合はエラーにはならず `medium` として扱われます。ディスパッチ時の選出スコアに影響します。
* **`overview`** (文字列, 任意, 既定値 `""`): 起票されるIssue本文の「概要」に転記される、`description` より詳細な説明。
* **`acceptance_criteria`** (文字列のリスト, 任意, 既定値 `[]`): 起票されるIssue本文の「受け入れ基準」に転記されるチェック項目。
* **`proposed_changes`** (文字列のリスト, 任意, 既定値 `[]`): 起票されるIssue本文の「変更内容」に転記される変更方針。
* **`verification_plan`** (文字列のリスト, 任意, 既定値 `[]`): 起票されるIssue本文の「修正・検証計画」に転記される検証手順。
* **`risk`** (真偽値, 任意, 既定値 `false`): `true` を指定すると、自動判定の結果によらずリスクありとして明示的にフラグを立てます（リスク理由に `explicit` が追加されます）。`false` を指定してもパスやキーワードによる自動判定は無効化されません。
* **`shared_contract`** (文字列, 任意, 既定値なし): レジストリやCLI配線のような共有拡張点を識別するタグ。同じタグを持つサブタスク同士が順序付けられていない場合に `orchestune-dag` が警告します。
* **`writes_shared_contract`** (真偽値, 任意, 既定値 `false`): `shared_contract` のファイルへ実際に書き込むサブタスクであることを明示します。通常は `footprint` から自動判定されるため指定不要です。

> [!NOTE]
> 必須フィールドは `id` のみです。`id` が欠落している、または空文字の場合はパース時にエラーで停止します。
> それ以外のフィールドは省略可能で、上記の既定値へフォールバックします。ただし `description` または `footprint` を省略した場合、
> パーサーは警告ログを出力します（リスク検知・フットプリント競合検知の精度が低下するため、実運用では両方の指定を推奨します）。

---

## 2. DAG検証（orchestune-dag）

`decomposition_plan.md` で定義されたタスク構成が正しいDAG（有向非巡回グラフ）になっているか、コンフリクトがないかを検証します。
通常、AIエージェントが自動でこのコマンドを実行して計画を修正しますが、手動で検証を行うこともできます。

```bash
# 素のCLIコマンドで検証
orchestune-dag --plan decomposition_plan.md

# またはラッパーコマンド
orchestune dag --plan decomposition_plan.md
```

### 主なエラー・警告検出
* **`DagCycleError`**: 依存関係（`depends_on`）に循環参照がある場合にエラーを出力します。
* **ファイル/シンボルの競合**: 異なるサブタスクで `footprint` や `symbols` が競合し、依存関係が適切に定義されていない場合に警告またはエラーを出力します。
* **リスク検出**: 認証情報の露出や危険なコマンド実行の記述がある場合にフラグを設定します。

---

## 3. ディスパッチャーの実行（orchestune-dispatch）

準備が整い、計画が承認されたら、ディスパッチャーを起動してサブタスクをエージェントに割り振り、実装を開始します。

```bash
# ドライラン（影響を出さずに実行計画のプレビューのみを行う）
orchestune-dispatch --no-apply

# 実際に適用して並列ワークスペースを起動し、エージェントを起動する
orchestune-dispatch
```

### 主要なオプション

| オプション | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `--apply` / `--no-apply` | `--apply` | 実際にタスク割り当てやGitブランチ作成を実行するか、プレビュー（ドライラン）のみにするかを選択。 |
| `--max-concurrent <int>` | `2` | 同時に実行（起動）できるサブタスクエージェントの最大数。 |
| `--dispatch-target {local,cloud-routine,codex-cloud,claude-cli,agy-cli,codex-cli,auto}` | 自動選択（非CI: `auto` / GitHub Actions: `cloud-routine`） | エージェントの起動先。未指定時は実行環境（`GITHUB_ACTIONS`環境変数）から自動選択される。`auto`はPATH上のローカルCLIを検出する。ローカル CLI、Claude Code Cloud Routine、または `ORCHESTUNE_CODEX_CLOUD_ENV`（もしくは `--codex-cloud-env`）で指定した Codex Cloud environment を明示選択できる。`codex-cloud` はタスクブランチを `origin` へ push してから Codex Cloud に投入し、対象ブランチの open PR を完了シグナルにする。`local`を明示指定した場合のみ、後方互換のダミー起動（no-op、テスト・dry-run用途）になる。 |
| `--codex-cloud-env <id>` | - | `--dispatch-target codex-cloud` で利用する Codex Cloud environment ID。未指定時は `ORCHESTUNE_CODEX_CLOUD_ENV` 環境変数を使用。 |
| `--local-cmd <template>` | - | `--dispatch-target local` の際に、ローカルのCLI（`agy` など）へディスパッチするためのコマンドテンプレート。使用可能な変数: `{issue_number}`, `{subtask_id}`, `{branch_name}`, `{worktree_path}`（例: `agy --issue {issue_number}`）。指定しない場合はデフォルトのダミー起動コマンドが使われます。`--dispatch-target claude-cli`/`agy-cli`/`codex-cli`（`auto`がこれらに解決した場合を含む）使用時は省略可能で、指定した場合は組み込みプリセットを上書きします。 |
| `--parent-issue <int>` | - | 開発対象をまとめている親の GitHub Issue 番号を指定。起票される子Issueがすべてこの親Issueに紐付けられます。 |
| `--deviation-buffer-lines <int>` | `5` | ライブロックを防止するための、フットプリントから逸脱したファイルの変更行数の許容バッファ値。 |
| `--max-launches-per-window <int>` | `1` | 指定した時間窓（`--window-seconds`）内で最大何回エージェントを起動できるかを制限する、APIバースト制御用オプション。 |
| `--window-seconds <int>` | `3600` | バースト制限を適用する時間窓の秒数（デフォルトは1時間）。 |
| `--max-recompute-retries <int>` | `2` | フットプリント逸脱を検知した際のDAG再計算のリトライ上限。超過した場合は強制直列化（force-serial）へフォールバックする。 |
| `--run-state-path <path>` | `run_state.json` | ディスパッチサイクル間で引き継ぐ実行状態（起動中タスク・起動履歴等）の永続化先。 |

### 設定ファイルによるオプションの省略

プロジェクトディレクトリに設定ファイルを配置することで、上記オプションの指定を省略し、デフォルト値として優先適用できます。

設定ファイルは以下の順序で探索され、最初に見つかったものがロードされます：
1. プロジェクトルートの `orchestune.toml`
2. プロジェクトルートの `pyproject.toml` の `[tool.orchestune]` セクション

#### 設定ファイルの記述例 (`orchestune.toml`)
```toml
max-concurrent = 2
dispatch-target = "claude-cli"
parent-issue = 181
run-state-path = "run_state.json"
```

#### 設定ファイルの記述例 (`pyproject.toml`)
```toml
[tool.orchestune]
max-concurrent = 2
dispatch-target = "claude-cli"
parent-issue = 181
run-state-path = "run_state.json"
```

> [!NOTE]
> 設定項目名は、CLI オプションに対応するケバブケース（例: `max-concurrent`）と、内部変数名に対応するスネークケース（例: `max_concurrent`）のどちらの形式でも記述可能です。
> コマンドライン引数で明示的にオプションが指定された場合は、設定ファイルの値よりもコマンドライン引数の値が優先されます。
> 未知のキーや不正な値がある場合は、既定値へフォールバックせず起動時にエラーで停止します。真偽値は TOML の bool、パス・文字列の設定は文字列、整数の設定は TOML の整数で指定してください。`max-concurrent`、`max-launches-per-window`、`deviation-buffer-lines`、`max-recompute-retries` は `0` 以上、`window-seconds` と `parent-issue` は `1` 以上です。

---

## 4. 統合（Integration）と自動リベース

`orchestune-dispatch` コマンドは、**タスクの割り振りだけでなく、完了したタスクの統合処理も同時に行います。**

### 4.1 共通の統合サイクル

1. エージェントがタスクを完了してプルリクエスト（PR）を作成し、Issueに `status:done` ラベルが付くと、ディスパッチャー（Integrator）がそれを検知します。
2. Integratorは統合先ブランチ（後述の base ブランチ）から一時統合ブランチを作成し、対象の子ブランチを順にマージした上でローカルCI（既定では `./scripts/local-ci.sh`）を実行します。
3. CIが成功すれば一時統合ブランチを `origin` へpushし、base ブランチへの統合PRを作成（または既存PRを再利用）します。
4. 統合対象として取り込まれた子Issueには `integration:included` ラベルが付与されます。

統合先（base）と一時統合ブランチの名前は、`--parent-issue` の指定有無によって次のように変わります。

| `--parent-issue` | base ブランチ | 一時統合ブランチ |
| :--- | :--- | :--- |
| 指定あり（`N`） | `origin/parent/issue-{N}` | `integration/temp-parent-issue-{N}` |
| 指定なし | `origin/main` | `integration/temp-main` |

### 4.2 `--parent-issue` 指定時（親ブランチによる二層統合）

親Issue番号を指定した場合、統合は「子ブランチ → 親ブランチ」「親ブランチ → main」の二層構造になります。

1. **子ブランチ → 親ブランチ（自動）**: 子PRは `parent/issue-{N}` ブランチへ自動的に統合されます。CIを通過した統合PRは人間の確認を待たずに自動マージされ、対象の子Issueは自動的にクローズされます。したがって、エージェントが作成した個別の子PRを人間がマージする必要はありません（レビュー用の記録として残ります）。
   - 自動マージに失敗した場合（ブランチ保護・権限設定など）は、対象Issueへその旨がコメントされ、次のディスパッチサイクルで自動的に再試行されます。
2. **親ブランチ → main（人間がマージ）**: 親Issue配下の全子Issueがクローズされると、`parent/issue-{N}` から `main` への最終統合PRが自動的に用意されます。**この最終PRをマージするかどうかの判断とマージ操作は、常に人間が行います。** 最終PRのマージが検知されると、親Issueは自動的にクローズされます。

### 4.3 `--parent-issue` 未指定時

親Issueを指定しない場合、Integratorは `main` を base とする統合PRを作成するところまでを担当します。**この統合PRの自動マージは行われず、`main` へのマージは人間がレビューして行います。**

### 4.4 自動リベース

下流の依存タスクのブランチは、依存先タスクの完了状況に応じて自動でリベースされます。リベース先は「最新の main」ではなく、**CIを通過済みの依存先タスクのブランチ**です（スタッキング）。依存先が単一に絞り込めない場合や、依存先がまだCIを通過していない場合、自動リベースは行われません。


