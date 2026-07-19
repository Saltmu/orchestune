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
    footprint:
      - src/db/connection.py
    symbols:
      - db.get_connection
    depends_on: []

  - id: user-auth
    description: "ユーザー認証エンドポイントの実装"
    footprint:
      - src/auth/routes.py
    symbols:
      - auth.login_user
    depends_on: [setup-database]
---
# タスク分解計画の説明
この計画は、構築に必要な手順をまとめたものです...
```

### フロントマターのスキーマ定義
各サブタスクは以下のフィールドを持ちます：

* **`id`** (文字列, 必須): サブタスクを一意に特定するための識別子。ブランチ名やIssueのタイトル等に使用されます。
* **`description`** (文字列, 必須): タスクが行う内容の短い説明。
* **`footprint`** (ファイルパスのリスト, 必須): このサブタスクが変更・作成・削除する予定のファイルパス（リポジトリルートからの相対パス）。
* **`symbols`** (文字列のリスト, 任意): このサブタスクが作成または変更する関数名やクラス名。
* **`depends_on`** (サブタスクIDのリスト, 必須): このサブタスクが開始される前に完了していなければならない先行サブタスクの `id` リスト。依存がない場合は空配列 `[]` を指定します。

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
| `--deviation-buffer-lines <int>` | `50` | ライブロックを防止するための、フットプリントから逸脱したファイルの変更行数の許容バッファ値。 |
| `--max-launches-per-window <int>` | `10` | 指定した時間窓（`--window-seconds`）内で最大何回エージェントを起動できるかを制限する、APIバースト制御用オプション。 |
| `--window-seconds <int>` | `3600` | バースト制限を適用する時間窓の秒数（デフォルトは1時間）。 |

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

1. エージェントがタスクを完了してプルリクエスト（PR）を作成すると、ディスパッチャーはそれを検知します。
2. ディスパッチャーは自動的にPRブランチをマージした一時統合ブランチを作成し、CIテストを実行します。
3. CIテストが成功すれば、そのままマージ調整を進め、下流の依存タスクのブランチを自動で最新の main にリベースして競合を解消します。
