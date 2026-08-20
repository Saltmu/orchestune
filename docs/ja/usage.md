# 使用方法とコマンドリファレンス

Orchestuneの各CLIコマンド（`orchestune dag`、`orchestune provision`、`orchestune dispatch`）の使い方、およびタスクの分解計画ファイル（`decomposition_plan.md`）の記述仕様について説明します。

---

## 1. タスク分解計画（Decomposition Plan）の仕様

メインとなる大きな開発タスク（「大きな石」）を並列実行可能なサブタスクに分解するために、リポジトリのルートに `decomposition_plan.md` というファイルを配置します。
このファイルは、上部にYAMLフロントマター形式でメタデータを記述し、下部（ボディ）に補足説明を記載する構成をとります。

### フォーマット例

```markdown
---
title: "大きな石（開発対象全体）の一行要約"
parent_issue_number: null  # orchestune provision が親Issue作成後に書き戻す（起票済Issue起点の場合はその番号）
parent_issue_source: derived  # 起票済Issue採用時は "adopted"、新規EPIC作成時は "derived"
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
    issue_number: null  # orchestune provision がこのサブタスクのIssue作成後に書き戻す

  - id: user-auth
    description: "ユーザー認証エンドポイントの実装"
    footprint:
      - src/auth/routes.py
    symbols:
      - auth.login_user
    depends_on: [setup-database]
    shared_contract: db-connection
    issue_number: null
---
# タスク分解計画の説明
この計画は、構築に必要な手順をまとめたものです...
```

### フロントマターのスキーマ定義

トップレベルには以下のフィールドがあります：

- **`title`** (文字列, 必須): 「大きな石」全体を表す一行要約。`orchestune provision`（後述）が親Issue（`[EPIC] <title>`）の起票に使用します。
- **`parent_issue_number`** (整数または`null`, 任意, 既定値 `null`): 親Issueの番号。起票済みIssueを起点とする場合はその番号を指定します。手動で設定しない場合、`orchestune provision`が親Issue作成（または既存Issueの再利用）後にこのファイルへ書き戻します。部分失敗からの再実行時に、この値が設定済みであれば親Issueは重複作成されません。
- **`parent_issue_source`** (文字列, 任意, 既定値 `derived`): 親Issueの由来。`adopted`（既存Issueを採用）または `derived`（計画の `title` から自動生成・解決）のいずれか。`adopted` の場合、タイトル一致検証をスキップして親Issue番号と親マーカーで検証・再利用します。
- **`subtasks`** (サブタスクのリスト, 必須): 各サブタスクは以下のフィールドを持ちます。

各サブタスクは以下のフィールドを持ちます：

* **`id`** (文字列, 必須): サブタスクを一意に特定するための識別子。ブランチ名やIssueのタイトル等に使用されます。文字列である必要があり、YAMLの数値・真偽値・日付・null・リスト（例: `id: 123`、`id:`、`id: []`）を指定した場合はエラーになります。数字だけのIDを使いたい場合は `id: "123"` のように引用符で囲んでください。
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
* **`shared_contract`** (文字列, 任意, 既定値なし): レジストリやCLI配線のような共有拡張点を識別するタグ。ただしタグの一致だけでは警告されません。`orchestune-dag` が比較するのは、その共有ファイルへ実際に**書き込む**と判定されたサブタスク同士のみで、契約に `depends_on` するだけの消費者（読み取り・importのみ）は対象外です。書き込み者同士が順序付けられていない（DAG上でどちらもどちらへも到達不能な）場合に警告します。
* **`writes_shared_contract`** (真偽値, 任意, 既定値 `false`): このサブタスクが `shared_contract` のファイルへ書き込むことを明示します。書き込み者かどうかは、まず `footprint` のパスが以下の命名カテゴリに一致するかで自動判定されます。
    * `registry`: `registry` / `registration` / `registrar` を含むファイル名（例: `src/format_registry.py`）
    * `cli-wiring`: `cli.*` / `__main__.*` / `main.*`
    * `public-api`: `__init__.py` / `index.ts` / `index.js` / `index.tsx` / `index.jsx`
    * `dependency-manifest`: `pyproject.toml` / `package.json` / `poetry.lock` / `package-lock.json` / `yarn.lock` / `pnpm-lock.yaml` / `Cargo.toml` / `go.mod`

    上記に一致しない独自のファイル名（例: `src/db/connection.py`、`src/custom_hook.py`）へ書き込む場合は自動判定が働かないため、**`writes_shared_contract: true` の明示が必要です**。指定を怠ると、同じ `shared_contract` タグを付けていても双方が消費者と見なされ、警告は一切出ません。
* **`issue_number`** (整数または`null`, 任意, 既定値 `null`): このサブタスクのIssue番号。**手動で設定しないでください** — `orchestune provision`がこのサブタスクのIssue作成（または既存Issueの再利用）後にこのファイルへ書き戻します。設定済みの場合、`orchestune provision`はそのサブタスクのIssueを再作成せず再利用します。

### 計画ファイルのライフサイクルと親Issueへの永続化（方針 (b)）

`decomposition_plan.md` は、計画作成・DAG検証・ユーザー承認（Stage 1〜3）の段階ではローカル（または作業worktree）上のドラフトファイルとして扱われます。
`orchestune provision`（Stage 4）を実行すると、親Issue（EPIC）の作成・採用とともに、親Issue本文の `<!-- orchestune:decomposition-plan -->` ブロックへ最新の計画内容（Frontmatter YAML）が自動的に埋め込まれ、同期・永続化されます。

- **親Issueが永続化の真実源（Source of Truth）**: AIエージェントの使い捨てworktreeが削除されてローカルの `decomposition_plan.md` が消失しても、親Issue本文に計画全体（各サブタスクの定義や起票された `issue_number`、説明文）が完全な形で記録として残ります。
- **計画ファイルを失った状態からの安全な復元と再実行**:
  1. `orchestune provision --restore-plan <親Issue番号>`（必要に応じて `--plan <出力先>`）を実行すると、親Issue本文から `decomposition_plan.md`（Frontmatter および元の説明文）が直接ファイルへ復元されます。
  2. 復元した状態で再度 `orchestune provision` を行う場合は、必ず `--parent-issue <親番号>` を指定し、まずは `--no-apply` でプレビューして既存の子Issueが正しく再利用されることを確認してください。
- **複数 big rock（計画）の並行運用**:
  複数の大きな石を並行して進める場合は、`orchestune provision --plan plans/rock-a.md` のように `--plan` オプションで個別パスを指定するか、別々のworktreeで作成してください。いずれの場合も `provision` 実行時に各big rockの親Issue本文へ個別に計画が永続化されるため、衝突することなく安全に分離・管理されます。
- **`orchestune-dispatch` は計画ファイルを参照しない**:
  `orchestune-dispatch` は、各サブタスクのGitHub Issue本文に埋め込まれた Footprint YAML（`subtask_id`, `depends_on`, `footprint` 等）から実行DAGを自律的に復元します。そのため、ローカルの `decomposition_plan.md` が存在しなくても、ディスパッチ・並列実行・自己修復・マージ統合は正常に動作します。

> [!NOTE]
> 必須フィールドは `id` のみです。`id` が欠落している、または空文字の場合はパース時にエラーで停止します。
> それ以外のフィールドは省略可能で、上記の既定値へフォールバックします。ただし `description` または `footprint` を省略した場合、
> パーサーは警告ログを出力します（リスク検知・フットプリント競合検知の精度が低下するため、実運用では両方の指定を推奨します）。

> [!NOTE]
> `orchestune provision` によるIssue番号の書き戻し（`parent_issue_number`・各サブタスクの `issue_number`）は、上記フォーマット例のような**標準的なブロックスタイルYAML**（各サブタスクを `- key: value` の複数行で記述し、キーは非クォートの識別子）を前提としています。フロースタイル（`- {id: task-a, ...}`）の単一行マッピングにも対応していますが、複数行にまたがるフローマッピングやクォート済みキー（`"id": task-a`）などの非標準的な記法はサポート対象外です。承認済みplanは上記の標準的な記法で記述してください。

---

## 2. Issue起票（orchestune provision）

承認済みの `decomposition_plan.md` から、`title` を親Issue、各サブタスクを子Issue（Sub-issue）としてGitHub上に起票します。`.github/issue_template.md` のプレースホルダー規則に沿って本文を生成し、`depends_on` のトポロジカル順で起票、`--parent`/`--blocked-by` 相当のnative関係を設定します。起票したIssue番号は都度 `decomposition_plan.md` のフロントマター（`parent_issue_number`、各サブタスクの `issue_number`）へ書き戻され、同時に親Issue本文の `<!-- orchestune:decomposition-plan -->` ブロックへも最新の計画YAMLが同期されます。そのため、**冪等**（既にIssueがあるサブタスクは再作成されない）かつ**部分失敗から再開可能**（N件目で失敗しても再実行時に1〜N-1件目は重複作成されない）です。

```bash
# プレビュー（GitHubへ書き込まず、生成される本文・ラベルのみ出力）
orchestune provision --plan decomposition_plan.md --no-apply

# 実際に起票する
orchestune provision --plan decomposition_plan.md
```

### 主要なオプション

| オプション | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `--plan <path>` | `decomposition_plan.md` | 起票元の分解計画ファイルのパス。 |
| `--template <path>` | `.github/issue_template.md` | Issue本文のテンプレートファイルのパス。 |
| `--apply` / `--no-apply` | `--apply` | 実際にGitHubへIssueを作成・書き戻しを行うか、プレビュー（ドライラン）のみにするかを選択。 |
| `--parent-issue <番号>` | なし | `title`から親Issueを新規作成/再利用する代わりに、既存の指定Issue番号をサブタスクの親EPICとして使う。詳細は下記「既存EPIC Issueへの紐付け」を参照。 |

### 既存EPIC Issueへの紐付け（`--parent-issue`）

EPIC Issueを先に（手動、またはOrchestuneを使わず普通に）起票しておき、サブタスクの分解・起票だけにOrchestuneを使いたい場合は、計画ファイルのフロントマターで `parent_issue_number: <番号>` と `parent_issue_source: adopted` を指定するか、CLIで `--parent-issue <番号>` を指定します。

```bash
orchestune provision --plan decomposition_plan.md --parent-issue 123
```

指定したIssueがまだOrchestune形式（タイトルが `[EPIC] ` で始まり、本文に親マーカーが埋め込まれている状態）になっていなければ、既存の内容は保持したままその場で正規化されます（タイトルへの `[EPIC] ` プレフィックス付与、本文へのマーカー追記）。`title` フロントマターとのタイトル一致チェックは行われません。

`--parent-issue` を指定して実行すると、計画ファイルのフロントマターへ `parent_issue_source: adopted` が自動的に永続化されます。そのため、**2回目以降の `orchestune provision` では `--parent-issue` を再指定しなくても自動的に同じ親Issueが採用・再利用されます**。もし採用済みの親Issueが存在しないか親マーカーが失われている場合は、重複起票を防ぐために新規作成へ倒れずエラーで停止します。

> [!NOTE]
> `orchestune-dispatch` の実行時には、対象親Issue配下の子ブランチを親ブランチ（`parent/issue-<番号>`）経由で二層マージさせるため、引き続き `--parent-issue <番号>` を指定してください。

### 起票ルール

* **ラベル**: `depends_on` が空、または依存先サブタスクが全て `status:done` なら `status:queued`、未解決の依存があれば `status:blocked`。`priority` に応じて `priority:high`/`medium`/`low`、`risk: true` なら `risk:flagged`。
* **冪等性の判定順**: (1) そのサブタスクの `issue_number` が設定済みならそれを再利用、(2) 未設定なら親Issue配下の既存子Issueの本文に埋め込まれたFootprint YAMLの `subtask_id` と照合して一致すれば再利用、(3) どちらもなければ新規作成。
* 実行には `gh` CLIのインストール・認証が必要です（`orchestune bootstrap` で事前確認）。`gh` が使えない環境でのフォールバックは [orchestune-provision スキル](../../skills/orchestune-provision/SKILL.md) を参照してください。

---

## 3. DAG検証（orchestune-dag）

`decomposition_plan.md` で定義されたタスク構成が正しいDAG（有向非巡回グラフ）になっているか、コンフリクトがないかを検証します。
通常、AIエージェントが自動でこのコマンドを実行して計画を修正しますが、手動で検証を行うこともできます。

```bash
# 素のCLIコマンドで検証
orchestune-dag --plan decomposition_plan.md

# またはラッパーコマンド
orchestune dag --plan decomposition_plan.md
```

### 主要なオプション

| オプション | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `--threshold <float>` | - | 類似度エッジの閾値（`[0, 1]`の範囲）。未指定時は、設定ファイルの`dag_similarity_threshold`（後述）が設定されていればその値、無ければ`0.2`（`orchestune.dag_similarity.DEFAULT_SIMILARITY_THRESHOLD`）にフォールバックする。`[0, 1]`の範囲外の値（`nan`/`inf`を含む）はエラーとして拒否される。 |

### 設定ファイルによる指定

`orchestune-dispatch`（§4）と同様に、`orchestune-dag`も`orchestune.toml` / `pyproject.toml`の`[tool.orchestune]`テーブルを読み込む（探索順序も同じ: `orchestune.toml`を先に、次に`pyproject.toml`）。

| 設定項目 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `dag_ignore_patterns`（または`dag-ignore-patterns`） | `[]` | 正規表現文字列のリスト。**`footprint`のパスに対してのみ**マッチする — `symbols`の項目はこの設定でフィルタされることは無く、常に類似度スコアの計算対象に含まれる。マッチしたfootprintパスは、組み込みの無視リスト（`pyproject.toml`、`poetry.lock`、`logging.py`、`logger.py`、`config.py`、`settings.py`）に加えて、類似度スコアの**計算入力**から除外される（＝そのペアの重なりスコアに寄与しなくなる）。ただし、そのペアが除外対象でない別のfootprintパスや共有する`symbols`の項目でも重なっている場合は、類似度エッジ自体は形成され続ける（または新たに形成される）ことがある。同様に、下記の「ファイル/シンボルの競合」チェックが必ず防げるとも限らない — このチェックは各subtaskの元の（フィルタ前の）footprintを見るため、同一カテゴリの書き込み者同士をそれまで繋いでいたエッジが除外によって消えた場合、この警告を防ぐのではなく**新たに発生させる**こともある。`DagCycleError`自体には影響しない（循環が全て明示的な`depends_on`エッジのみで構成される場合にのみ発生し、`dag_ignore_patterns`はそのケースに影響を及ぼせない） — 下記の実在検証・リスク検出という独立したチェックにも影響しない。空文字列は拒否される（空パターンはあらゆるfootprintパスに一致し、全てのfootprint項目をスコア計算から無診断で除いてしまうが、共有する`symbols`の項目があれば依然としてエッジが形成され得るため）。 |
| `dag_similarity_threshold`（または`dag-similarity-threshold`） | `0.2` | `--threshold`（前述）の永続的なフォールバック値。`[0, 1]`の範囲のfloat。同じ設定ファイルから`orchestune provision`側のDAG再計算にも読まれるため、ここで調整した閾値がそちらで黙って無視されることはない。注意: `orchestune-dag`と`orchestune provision`はいずれも共通の`resolve_repo_root()`関数を使ってリポジトリルートを解決しており、これは上位へ`.git`を探索してリポジトリルートを特定する。そのため`--plan`がリポジトリルートより下のネストしたファイルを指す場合でも、両ツールは一貫して同じリポジトリルートの設定を参照する。 |

#### 設定ファイルの記述例 (`orchestune.toml`)

```toml
dag_ignore_patterns = ['(^|/)package\.json$', '(^|/)generated/']
dag_similarity_threshold = 0.35
```

> [!WARNING]
> `dag_ignore_patterns`の各要素はTOMLから読み込まれる正規表現であり、パスの文字列そのものではありません。上記のようにTOMLの**リテラル文字列**（シングルクォート`'...'`）を使うことを推奨します: バックスラッシュはそのまま扱われるため、`\.`を意図した通りに書けます。
> 代わりにTOMLの**基本文字列**（ダブルクォート`"..."`）を使う場合、バックスラッシュはTOML自体のエスケープ文字も兼ねるため、正規表現側のバックスラッシュ1つごとに追加のエスケープが必要になります — 正規表現の`\.`は`"\\."`と書かなければなりません。`"(^|/)package\\.json$"`（基本文字列）と`'(^|/)package\.json$'`（リテラル文字列）は、全く同じ正規表現にコンパイルされます。基本文字列の中に裸の`"\."`を書くと、単に「正規表現として間違っている」のではなく、TOMLパーサー自体が不正なエスケープシーケンスとして拒否します。

### 主なエラー・警告検出
1回の`Warnings:`出力に、以下の複数種類の警告が同時に含まれることがあります。各行の文言に応じて種類を判別してください。
* **`DagCycleError`**: 依存関係（`depends_on`）に循環参照がある場合にエラーを出力します。
* **ファイル/シンボルの競合**: 異なるサブタスクで `footprint` や `symbols` が競合し、依存関係が適切に定義されていない場合に警告またはエラーを出力します。
* **実在検証（`footprint`/`symbols`）**: 宣言された `footprint` のパスや `symbols` のエントリが、現在のコードベース上に実在すると確認できない場合に警告します（例: `<subtask-id>: footprintに実在しないパスがあります` / `<subtask-id>: symbolsが実コードベースに見つかりません`）。これは必ずしもエラーではありません — ただし挙動は`footprint`と`symbols`で異なります: これから新規作成する`footprint`パスは常にこの警告が出ますが、新規追加予定の`symbols`エントリが警告されるのは検証が実際に実行された場合のみです。検証の実行には、footprint中に実在しparseに成功した`.py`ファイルが少なくとも1つあり、かつfootprint中の既存`.py`ファイルにparse失敗（構文エラー・エンコーディングエラー）が1件も無いことの両方が必要です（1件でもparse失敗ファイルがあると、そのsubtask全体で検証自体がスキップされます）。検証が実行されなかった場合、`symbols`の警告は一切出ません。警告が出ないことを「確認済み」と読み替えないでください。typo・パス誤りなのか、`footprint` の記載漏れ（衝突検知の見逃し）を疑うべきかの判断基準は [`orchestune` スキル](../../skills/orchestune/SKILL.md) のStage 2を参照してください。
* **リスク検出**: 認証情報の露出や危険なコマンド実行の記述がある場合にフラグを設定します。

---

## 4. ディスパッチャーの実行（orchestune-dispatch）

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
| `--ci-command <cmd>` | `./scripts/local-ci.sh`（Orchestune自身のリポジトリ固有の値） | Integratorが統合ブランチ上で実行するCIコマンド（shlex構文のシェル風文字列。例: `'make ci'`）。導入先リポジトリのCIエントリーポイントが異なる場合は必ず設定してください（[セットアップガイドの「導入要件」](setup.md#0-導入要件prerequisites)参照）。`orchestune.toml`/`pyproject.toml`の`[tool.orchestune]`セクションでは`ci-command`キーとして指定できます。 |
| `--deviation-buffer-lines <int>` | `5` | ライブロックを防止するための、フットプリントから逸脱したファイルの変更行数の許容バッファ値。 |
| `--max-launches-per-window <int>` | `1` | 指定した時間窓（`--window-seconds`）内で最大何回エージェントを起動できるかを制限する、APIバースト制御用オプション。 |
| `--window-seconds <int>` | `3600` | バースト制限およびトークン消費上限を適用する時間窓の秒数（デフォルトは1時間）。 |
| `--max-tokens-per-window <int>` | - | 指定した時間窓（`--window-seconds`）内で消費できるトークン数の総上限。累計消費量が上限に達した場合、新規タスクの起動を一時停止する。未指定時は無制限。 |
| `--max-tokens-per-task <int>` | - | 単一サブタスクが消費できるトークン数の上限。完了時にこの上限を超過していた場合、自動完了を見送り `status:blocked-human-review` へエスカレーションする。未指定時は無制限。 |
| `--max-recompute-retries <int>` | `2` | フットプリント逸脱を検知した際のDAG再計算のリトライ上限。超過した場合は強制直列化（force-serial）へフォールバックする。 |
| `--task-timeout-seconds <int>` | `0` | タスクをタイムアウトとみなしてGCで回収するまでの秒数。`0`（既定）ではタイムアウトによる回収を行わず、ゾンビ検知のみ実行する。無人運転時は正の値を設定することを推奨。 |
| `--max-task-reclaims <int>` | `3` | ゾンビ・タイムアウトGCが同一タスクを`status:queued`へ差し戻せる回数の上限。超過したタスクは`status:blocked-human-review`へ遷移し、以降は再投入されない。`0`は「1回目の回収で即エスカレーション」を意味する（無制限にする設定値は存在しない）。 |
| `--not-needed-review-timeout-seconds <int>` | `86400` | `status:not-needed`判定の独立検証レビュー（Cloud Routineターゲット使用時）が、どちらの結果ラベルも返さないまま保持され続ける秒数の上限。超過したエントリは`status:blocked-human-review`へエスカレーションする（無制限にする設定値は存在しない）。 |
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
> 未知のキーや不正な値がある場合は、既定値へフォールバックせず起動時にエラーで停止します。真偽値は TOML の bool、パス・文字列の設定は文字列、整数の設定は TOML の整数で指定してください。`max-concurrent`、`max-launches-per-window`、`deviation-buffer-lines`、`max-recompute-retries`、`task-timeout-seconds`、`max-task-reclaims`、`not-needed-review-timeout-seconds` は `0` 以上、`window-seconds` と `parent-issue` は `1` 以上です。

---

## 5. 統合（Integration）と自動リベース

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


