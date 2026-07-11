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
| `--dispatch-target {local,cloud-routine,claude-cli,agy-cli}` | `local` | エージェントの起動先。ローカル環境で直接コマンドを実行するか、Claude Code Cloud Routineで実行するか、あるいはローカルの`claude`/`agy` CLIへ、許可プロンプトを毎回バイパスするフラグ付きの組み込みプリセットコマンドでディスパッチするかを指定（バイパスの安全境界はサブタスクごとのworktreeが担う）。 |
| `--local-cmd <template>` | - | `--dispatch-target local` の際に、ローカルのCLI（`agy` など）へディスパッチするためのコマンドテンプレート。使用可能な変数: `{issue_number}`, `{subtask_id}`, `{branch_name}`, `{worktree_path}`（例: `agy --issue {issue_number}`）。指定しない場合はデフォルトのダミー起動コマンドが使われます。`--dispatch-target claude-cli`/`agy-cli`使用時は省略可能で、指定した場合は組み込みプリセットを上書きします。 |
| `--parent-issue <int>` | - | 開発対象をまとめている親の GitHub Issue 番号を指定。起票される子Issueがすべてこの親Issueに紐付けられます。 |
| `--deviation-buffer-lines <int>` | `50` | ライブロックを防止するための、フットプリントから逸脱したファイルの変更行数の許容バッファ値。 |
| `--max-launches-per-window <int>` | `10` | 指定した時間窓（`--window-seconds`）内で最大何回エージェントを起動できるかを制限する、APIバースト制御用オプション。 |
| `--window-seconds <int>` | `3600` | バースト制限を適用する時間窓の秒数（デフォルトは1時間）。 |

---

## 4. 統合（Integration）と自動リベース

`orchestune-dispatch` コマンドは、**タスクの割り振りだけでなく、完了したタスクの統合処理も同時に行います。**

1. エージェントがタスクを完了してプルリクエスト（PR）を作成すると、ディスパッチャーはそれを検知します。
2. ディスパッチャーは自動的にPRブランチをマージした一時統合ブランチを作成し、CIテストを実行します。
3. CIテストが成功すれば、そのままマージ調整を進め、下流の依存タスクのブランチを自動で最新の main にリベースして競合を解消します。
