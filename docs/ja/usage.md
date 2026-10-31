# 使用方法とコマンドリファレンス

## 親スコープ内の補修タスクを追加する（remediate）

`remediate` はレビュー残件や統合バグを、既存EPICの要件を満たすための子Issueとして追加します。
未着手の分解世代を置き換える `replan` と異なり、完了済み・進行中の子Issueを保持します。
新しい要件は親の編集や別EPICで扱ってください。

frontmatter付きMarkdownのspecを作成します。`issue_number` を省略すると新規起票し、指定すると既存Issueを登録します。
`kind` は `review-followup` または `integration-bug` です。`parent_acceptance` は親本文のチェック項目か既存タスクの受け入れ条件と一致させ、
`origins` は同じ親の子Issue・親ブランチのPR・そのコメントを指定します。

```yaml
---
parent_issue_number: 693
subtasks:
  - id: repair-validation
    description: 統合レビューで見つかった検証漏れを修正する
    kind: review-followup
    # issue_number: 806  # 既存のフォローアップ・バグIssueを登録する場合
    footprint: [src/validation.py]
    depends_on: [existing-subtask-id]
    parent_acceptance: [親の受け入れ条件の文言]
    origins: ['#805']
    reason: 親の検証要件を完遂するため
---
```

```bash
orchestune remediate --spec remediation.md
orchestune remediate --spec remediation.md --apply --confirm-preview <表示されたtoken>
```

previewで根拠・変更するFootprint・依存関係を確認します。既存IssueのタイトルとFootprint以外の本文は保持し、補修の根拠を追記します。
#1277のフォローアップ（`depends_on` だけのFootprintや `blocked-by #N` を含む本文）も、specに完全なFootprintと解決できる依存を記載して登録できます。
起票した番号はspecに書き戻します。親計画の末尾へ追加し、openな最終PRを保留表示にし、親へ監査コメントを残します。
登録でDispatcherは起動しません。次の `orchestune dispatch -p 693` サイクルで依存・競合を評価します。

終了コード3はtokenの不一致です。書き込みは行いません。終了コード4は部分登録または並行更新です。
子Issueを取り消さず、同じspecで再previewし、`resume` と残りの段階を確認して新しいtokenでapplyしてください。
登録後に子IssueのFootprint等が変わっている場合は手動確認で止め、進行中のclaim情報や変更済みmetadataを上書きしません。
起票後の番号書き戻しに失敗した場合も、親metadataとsubtask_idで既存Issueを再発見します。
親metadata検索に対応しないForgeでは、親子関係を張る前の中断から再発見できない場合があります。重複起票を避けるため、作成済みIssueを確認してspecへ番号を記載してください。
全件登録済みは5、親・既存Issueの状態による拒否は6、スコープ確認が必要な場合は7、入力不備は2です。
closedな親や現在の親ブランチがマージ済みの場合は自動でreopenしません。

親本文は書き込み直前に再取得し、計画ブロックのfingerprintを照合します。計画以外の本文（Human Notesを含む）を保持し、書き込み後にも読み戻します。
GitHubのIssue本文APIにはcompare-and-swapがないため、最終照合と書き込みの間の競合は完全には防げません。
同じ親への登録・provision・replan・本文編集を直列に実行してください。Issue本文は既存Forgeモデルの改行正規化を経由します。

### クラウド起動の障害復旧

通常の`cloud-routine`・`codex-cloud`ワーカー起動では、起動前にタスクIssueへ
`orchestune:launch-attempt`のJSONブロックを保存します。親Issueのクオータ予約とは別の台帳です。

| 停止時のphase | 復旧方針 |
|---|---|
| 台帳なし | provider呼び出し前。新しい試行を開始する。 |
| `prepared` | providerは未呼び出し。同じ試行IDで再開する。 |
| `unknown` | 起動済みの可能性あり。対応providerで試行IDを照合し、照合不能なら人手確認へ保留する。queuedタスクとして再起動しない。 |
| `launched` | handle保存済み。PR・ローカル状態がなくても同じ試行ID・phase・開始時刻・ブランチ・handleを復元する。 |

組み込みクラウドアダプターは試行IDによる照会・冪等起動に未対応のため、通常ワーカーのRoutine POSTは通信エラー時に再送しません。
provider境界を越えた失敗ではworktreeとクオータ予約を維持します。ローカル起動とSemantic Reviewの`fire_text`の再試行契約は従来どおりです。

照合不能な試行はID・理由付きで`status:blocked-human-review`へ遷移します。
queuedへのラベル変更、ローカル状態消失、クオータ窓の経過だけでは新しい実行を許可しません。
手動復旧時はdispatcherを停止し、provider上の旧実行を確認してください。
同じ実行を追跡する場合は確認済みhandleと`launched`を台帳に復元し、
新しい実行が必要な場合は旧実行が動いていないことを確認してから台帳ブロックを退避・削除します。
推測で`unknown`を`prepared`へ戻してはいけません。回収済みクラウドタスクの意図的な再投入もこの手順に従います。

既存の単一dispatcherロックとIssue本文APIを使い、独立ランナー間のCASは提供しません。
同じタスクに複数の独立dispatcherを同時実行しないでください。
導入前の台帳がない実行は従来のPRによる復旧となり、失われた過去のhandleを推測して補完しません。

Orchestuneの各CLIコマンド（`orchestune dag`、`orchestune provision`、`orchestune dispatch`）の使い方、およびタスクの分解計画ファイル（`decomposition_plan.md`）の記述仕様について説明します。

---

## 1. タスク分解計画（Decomposition Plan）の仕様

メインとなる大きな開発タスク（「大きな石」）を並列実行可能なサブタスクに分解する際は、`.orchestune/tmp/decomposition-my-task-20260922T120000Z-550e8400/decomposition-plan.md` のような一意で Git 管理外のパスを作成します。セッションディレクトリは `<artifact>-<issue-or-task>-<UTC timestamp>-<random>` 形式とし、random には UUID 等を使い、生成したパスを `--plan` で明示してください。CLI の従来の `decomposition_plan.md` デフォルトは後方互換のため維持しますが、エージェントはリポジトリ直下や OS グローバルの `/tmp` に固定名の下書きを作成してはいけません。
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
      - "uv run pytest tests/test_connection.py"
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
* **`shared_contract`** (文字列, 任意, 既定値なし): レジストリやCLI配線のような共有拡張点を識別するタグ。`orchestune-dag` が比較するのは、その共有ファイルへ実際に**書き込む**と判定されたサブタスク同士のみで、契約に `depends_on` するだけの消費者（読み取り・importのみ）は対象外です。書き込み者pairはConflict Graphの排他制約となります。さらにPrecedence DAG上で順序付けられていない（どちらもどちらへも到達不能な）pairのうち、同じ契約の`shared-contract`排他辺で保護済みのpairを除いたものは、「先行関係が未指定で、所有者・順序要否の確認が必要」という警告として表示されます（排他と先行順序は別の事実で、排他辺は`depends_on`を生成しません）。
* **`writes_shared_contract`** (真偽値, 任意, 既定値 `false`): このサブタスクが `shared_contract` のファイルへ書き込むことを明示します。書き込み者かどうかは、まず `footprint` のパスが以下の命名カテゴリに一致するかで自動判定されます。
    * `registry`: `registry` / `registration` / `registrar` を含むファイル名（例: `src/format_registry.py`）
    * `cli-wiring`: `cli.*` / `__main__.*` / `main.*`
    * `public-api`: `__init__.py` / `index.ts` / `index.js` / `index.tsx` / `index.jsx`
    * `dependency-manifest`: `pyproject.toml` / `package.json` / `poetry.lock` / `uv.lock` / `package-lock.json` / `yarn.lock` / `pnpm-lock.yaml` / `Cargo.toml` / `go.mod`
    * 共有文書: リポジトリルート`docs/`配下の`.md`ファイルを`footprint`へ宣言している場合も書き込み者と判定されます（読むだけで`footprint`へ含めない消費者は書き込み者になりません）。この判定は`dag_ignore_patterns`では解除されません。

    上記に一致しない独自のファイル名（例: `src/db/connection.py`、`src/custom_hook.py`）へ書き込む場合は自動判定が働かないため、**`writes_shared_contract: true` の明示が必要です**。指定を怠ると、同じ `shared_contract` タグを付けていても双方が消費者と見なされ、警告は一切出ません。
* **`execution_profile`** (文字列または`null`, 任意, 既定値 `null`): サブタスクを実行するエージェントの抽象実行プロファイル名（例: `fast-code`、`deep-reasoning`）。英小文字・数字・ハイフン・アンダースコアで構成され、32文字以内である必要があります。
* **`issue_number`** (整数または`null`, 任意, 既定値 `null`): このサブタスクのIssue番号。**手動で設定しないでください** — `orchestune provision`がこのサブタスクのIssue作成（または既存Issueの再利用）後にこのファイルへ書き戻します。設定済みの場合、`orchestune provision`はそのサブタスクのIssueを再作成せず再利用します。

`model_tier` は廃止され、計画でサポートされるフィールドではありません。既存計画から削除し、モデルの選択をターゲット別execution profileへ移行してください（[廃止されたモデルランクからの移行](#廃止されたモデルランクからの移行)を参照）。

### 計画ファイルのライフサイクルと親Issueへの永続化（方針 (b)）

`decomposition_plan.md` は、計画作成・DAG検証・ユーザー承認（Stage 1〜3）の段階ではローカル（または作業worktree）上のドラフトファイルとして扱われます。
`orchestune provision`（Stage 4）を実行すると、親Issue（EPIC）の作成・採用および全サブタスクの起票・再利用完了後に、親Issue本文の `<!-- orchestune:decomposition-plan -->` ブロックへ目次形式（`plan_format: manifest-v1`）に射影された計画が1回だけ同期・永続化されます。親計画から起票済みエントリの長文・Footprintが除外されるため、大規模な分解計画でもGitHub本文上限（65,536文字）を超過しません。事前検査により、射影後の親Issue本文が上限に収まることをForge書き込み前に検証します。旧形式（全文章を含む計画）の親Issueは、次回以降の `provision`、`replan`、`remediation` の書き込み時に自動的に manifest-v1 形式へ移行されます。

- **親Issueへの永続化と起票済みサブタスクの正本（Source of Truth）**:
  AIエージェントの使い捨てworktreeが削除されてローカルの `decomposition_plan.md` が消失しても、親Issue本文に計画（各サブタスクの定義や起票された `issue_number`、説明文）が記録として残ります。ただし、起票済みのサブタスクについては、その文章（`overview`, `proposed_changes`, `acceptance_criteria`, `verification_plan`）および `footprint`, `symbols` の正本は子Issue本文となります。
- **計画ファイルを失った状態からの安全な復元と再実行**:
  1. `orchestune provision --restore-plan <親Issue番号>`（必要に応じて `--plan <出力先>` や `--template <テンプレートパス>`）を実行すると、親計画と各子Issue本文から完全な `decomposition_plan.md` が復元されます。起票済みエントリは各子Issue本文から最新の文章とFootprintが復元され（親に残った古い文章へはフォールバックしません）、未起票エントリは親計画の値が維持されます。復元後のローカル計画は全エントリがインライン展開された形式となるため、親計画が目次形式（`plan_format: manifest-v1`）であっても `plan_format` は付与されません。
  2. 親の構造と子の本文・メタデータ（`id`, `description`, `depends_on`, 共有メタデータ等）は厳密に照合され、不整合や子Issueの欠落がある場合は fail closed として復元を拒否します。
  3. 復元処理の最終段階で親および取得済みの子Issueを再取得して並行編集を検知した場合は出力を中止し再実行を促します（分散トランザクションは行わず、完全な同時点 snapshot は保証しません）。出力先ファイルは一時ファイルを経由してアトミックに置き換えられるため、復元失敗時に既存の出力ファイルが破壊されることはありません。
  4. 既存Issueを補修タスクとして登録した等の理由で本文がテンプレート構造に適合しない場合は、子Issue本文をテンプレートの形式に手動で整えてから再実行してください。なお、`orchestune provision` 実行時には起票前バリデーション（`validate_reversible_subtask`）が走り、箇条書きの空行・末尾空白・改行混入・予約語「特になし」を事前検出して復元不可能な計画の起票を未然に防止します。
  5. 復元した状態で再度 `orchestune provision` を行う場合は、必ず `--parent-issue <親番号>` を指定し、まずは `--no-apply` でプレビューして既存の子Issueが正しく再利用されることを確認してください。
- **古いローカル計画で親計画を上書きしない**: 親計画に `issue_number` を持つsubtaskがあり、同じ `id` のsubtaskがローカルの計画にない場合（親計画へ直接追加されたタスクがある場合など）、`orchestune provision` は子Issueを1件も作らずに終了コード3で止まり、親Issue本文も書き換えません。`orchestune provision --restore-plan <親番号>` で計画を復元してから再実行してください。`issue_number` を持たない親計画のエントリでは止まりません。
- **複数 big rock（計画）の並行運用**:
  複数の大きな石を並行して進める場合は、`orchestune provision --plan plans/rock-a.md` のように `--plan` オプションで個別パスを指定するか、別々のworktreeで作成してください。いずれの場合も `provision` 実行時に各big rockの親Issue本文へ個別に計画が永続化されるため、衝突することなく安全に分離・管理されます。
- **`orchestune-dispatch` は計画ファイルを参照しない**:
  `orchestune-dispatch` は、各サブタスクのGitHub Issue本文に埋め込まれた Footprint YAML（`subtask_id`, `depends_on`, `footprint`, `symbols`, `shared_contract`, `writes_shared_contract` 等）からPrecedence DAGとConflict Graphを自律的に復元します。そのため、ローカルの `decomposition_plan.md` が存在しなくても、ディスパッチ・並列実行・自己修復・マージ統合は正常に動作します。

> [!NOTE]
> 必須フィールドは `id` のみです。`id` が欠落している、または空文字の場合はパース時にエラーで停止します。
> それ以外のフィールドは省略可能で、上記の既定値へフォールバックします。ただし `description` または `footprint` を省略した場合、
> パーサーは警告ログを出力します（リスク検知・フットプリント競合検知の精度が低下するため、実運用では両方の指定を推奨します）。

> [!NOTE]
> `orchestune provision` によるIssue番号の書き戻し（`parent_issue_number`・各サブタスクの `issue_number`）は、上記フォーマット例のような**標準的なブロックスタイルYAML**（各サブタスクを `- key: value` の複数行で記述し、キーは非クォートの識別子）を前提としています。フロースタイル（`- {id: task-a, ...}`）の単一行マッピングにも対応していますが、複数行にまたがるフローマッピングやクォート済みキー（`"id": task-a`）などの非標準的な記法はサポート対象外です。承認済みplanは上記の標準的な記法で記述してください。

---

## 2. Issue起票（orchestune provision）

承認済みの `decomposition_plan.md` から、`title` を親Issue、各サブタスクを子Issue（Sub-issue）としてGitHub上に起票します。`.github/issue_template.md` のプレースホルダー規則に沿って本文を生成し、`depends_on` のトポロジカル順で起票、`--parent`/`--blocked-by` 相当のnative関係を設定します。起票したIssue番号は都度 `decomposition_plan.md` のフロントマター（`parent_issue_number`、各サブタスクの `issue_number`）へ書き戻され、全サブタスクの番号確定後に親Issue本文の `<!-- orchestune:decomposition-plan -->` ブロックへ目次形式（`plan_format: manifest-v1`）に射影された計画YAMLが一度に同期されます。そのため、**冪等**（既にIssueがあるサブタスクは再作成されない）かつ**部分失敗から再開可能**（N件目で失敗しても再実行時に1〜N-1件目は重複作成されない）です。

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

`--parent-issue` を指定して実行すると、計画ファイルのフロントマターへ `parent_issue_source: adopted` が自動的に永続化されます。そのため、**2回目以降の `orchestune provision` では `--parent-issue` を再指定しなくても自動的に同じ親Issueが採用・再利用されます**。もし採用済みの親Issueが存在しない場合は、重複起票を防ぐために新規作成へ倒れずエラーで停止します。

> [!NOTE]
> `orchestune-dispatch` の実行時には、対象親Issue配下の子ブランチを親ブランチ（`parent/issue-<番号>`）経由で二層マージさせるため、引き続き `--parent-issue <番号>` を指定してください。

### 起票ルール

* **ラベル**: `depends_on` が空、または依存先サブタスクが全て `status:done` なら `status:queued`、未解決の依存があれば `status:blocked`。`priority` に応じて `priority:high`/`medium`/`low`、`risk: true` なら `risk:flagged`。
* **冪等性の判定順**: (1) そのサブタスクの `issue_number` が設定済みならそれを再利用、(2) 未設定なら親Issue配下の既存子Issueの本文に埋め込まれたFootprint YAMLの `subtask_id` と照合して一致すれば再利用、(3) どちらもなければ新規作成。
* 実行には `gh` CLIのインストール・認証が必要です（`orchestune bootstrap` で事前確認）。`gh` が使えない環境でのフォールバックは [orchestune-provision スキル](../../skills/orchestune-provision/SKILL.md) を参照してください。

---

## 3. DAG検証（orchestune-dag）

`decomposition_plan.md` から、明示的な`depends_on`だけを含むPrecedence DAGが非巡回であることを検証し、`footprint`・`symbols`・shared-contractから得た対称なConflict Graphを別に表示します。
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
| `--threshold <float>` | - | 類似度に基づく競合辺の閾値（`[0, 1]`の範囲）。未指定時は、設定ファイルの`dag_similarity_threshold`（後述）が設定されていればその値、無ければ`0.2`（`orchestune.dag.similarity.DEFAULT_SIMILARITY_THRESHOLD`）にフォールバックする。`[0, 1]`の範囲外の値（`nan`/`inf`を含む）はエラーとして拒否される。 |

### 設定ファイルによる指定

`orchestune-dispatch`（§4）と同様に、`orchestune-dag`も`orchestune.toml` / `pyproject.toml`の`[tool.orchestune]`テーブルを読み込む（探索順序も同じ: `orchestune.toml`を先に、次に`pyproject.toml`）。

| 設定項目 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `dag_ignore_patterns`（または`dag-ignore-patterns`） | `[]` | 正規表現文字列のリスト。**`footprint`のパスに対してのみ**マッチし、`symbols`は常に類似度スコアの入力に残る。マッチしたパスは、組み込みの無視リスト（`pyproject.toml`、`poetry.lock`、`uv.lock`、`logging.py`、`logger.py`、`config.py`、`settings.py`）に加えて、類似度Conflict Edgeのスコア入力、ヒューリスティックなshared-contract hotspot競合、および`shared-document`競合の自動検出から除外される。ただし、別の非除外パスや共有`symbols`があればsimilarity競合は残り、明示的な`shared_contract` writer競合と独立したwriter警告もこの設定では消えない。Precedence DAGは明示的な`depends_on`だけから成るため、`DagCycleError`にも影響しない。空文字列は全パスに一致するため拒否される。 |
| `dag_similarity_threshold`（または`dag-similarity-threshold`） | `0.2` | `--threshold`（前述）の永続的なフォールバック値。`[0, 1]`の範囲のfloat。同じ設定ファイルから`orchestune provision`側のConflict Graph計算にも読まれるため、ここで調整した閾値がそちらで黙って無視されることはない。注意: `orchestune-dag`と`orchestune provision`はいずれも共通の`resolve_repo_root()`関数を使ってリポジトリルートを解決しており、これは上位へ`.git`を探索してリポジトリルートを特定する。そのため`--plan`がリポジトリルートより下のネストしたファイルを指す場合でも、両ツールは一貫して同じリポジトリルートの設定を参照する。 |

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
* **競合辺**: `footprint` / `symbols` の類似度、shared-contract writer判定、およびルート`docs/`配下の`.md`文書の正規化パス完全一致（`reason=shared-document`、`resources`は共有文書パス）から、priorityやIDに依存しない対称な排他制約を生成します。別文書（同じディレクトリの別ファイルや日英の別ファイル）は文書検出だけでは排他になりません。同時に更新させたくない契約は同じ`shared_contract`を宣言してwriterとして明示し、先行順序まで必要なら`depends_on`を指定してください。文書競合から`depends_on`は自動生成されず、類似度閾値も変わりません。テキスト出力では依存辺と競合辺を1つの`Edges`表に並べ、`Kind`列（`dependency`＝依存辺、`conflict`＝競合辺）で区別します。競合辺の`From`/`To`は順序に意味のない対称な組です。`--json`では`precedence_edges`と`conflict_edges`として分離されます（後方互換の`edges`はprecedenceだけです）。
* **Shared-contract writer警告**: writer同士がPrecedence DAGで順序付けられていない場合は、Conflict Edgeに加えて非ブロッキング警告を表示します。
* **実在検証（`footprint`/`symbols`）**: 宣言された `footprint` のパスや `symbols` のエントリが、現在のコードベース上に実在すると確認できない場合に警告します（例: `<subtask-id>: footprintに実在しないパスがあります` / `<subtask-id>: symbolsが実コードベースに見つかりません`）。これは必ずしもエラーではありません — ただし挙動は`footprint`と`symbols`で異なります: これから新規作成する`footprint`パスは常にこの警告が出ますが、新規追加予定の`symbols`エントリが警告されるのは検証が実際に実行された場合のみです。検証の実行には、footprint中に実在しparseに成功した`.py`ファイルが少なくとも1つあり、かつfootprint中の既存`.py`ファイルにparse失敗（構文エラー・エンコーディングエラー）が1件も無いことの両方が必要です（1件でもparse失敗ファイルがあると、そのsubtask全体で検証自体がスキップされます）。検証が実行されなかった場合、`symbols`の警告は一切出ません。警告が出ないことを「確認済み」と読み替えないでください。typo・パス誤りなのか、`footprint` の記載漏れ（衝突検知の見逃し）を疑うべきかの判断基準は [`orchestune` スキル](../../skills/orchestune/SKILL.md) のStage 2を参照してください。
* **リスク検出**: 認証情報の露出や危険なコマンド実行の記述がある場合にフラグを設定します。

---

## 4. ディスパッチャーの実行（orchestune-dispatch）

準備が整い、計画が承認されたら、ディスパッチャーを起動してサブタスクをエージェントに割り振り、実装を開始します。

```bash
# ドライラン（対象へ適用せず、ローカル結果を保存する）
orchestune-dispatch --no-apply

# 実際に適用して並列ワークスペースを起動し、エージェントを起動する
orchestune-dispatch
```

### 進捗と結果ファイル

stdoutはrun ID・親Issue・apply/dry-run・phase付きの進捗を改行・flush付きで表示します。pipeでも表示されます。最終JSONはatomicに保存され、絶対パスの `report saved` で確認できます。`report target` は予定パスです。`dispatch | jq` やstdoutのJSONリダイレクトは結果ファイル読取りへ移行してください。JSONスキーマとGitHub Step Summaryは維持します。

TOML `report-dir` の既定は `.orchestune/reports/dispatch` で、linked worktreeでもprimary checkout基準です。実行ごとに `parent-<N>/<UTC YYYYMMDDTHHMMSSZ>-<UUID>/result.json` へ保存します。環境変数 `ORCHESTUNE_DISPATCH_REPORT_PATH` は未使用ファイルへの明示指定で優先され、相対値は呼出し時cwd基準、絶対値はそのままです。既存結果・symlink・空値・ディレクトリ・業務状態/lockと重なる指定は拒否します。再実行は新しいパスを使い、最新mtime検索や前回結果の流用はしません。

`--no-apply` はdispatch対象へ適用せず、ローカル結果と報告用ディレクトリ/lockを作ります。`planned` はdry-runの選定、`launched` は起動手順の成立で、タスク完了を意味しません（`local` はダミー起動）。進捗表示障害でも保存を継続します。

専用の `<result-name>.report.lock` は明示パスでも結果の隣に残ります。排他の保証は協調するdispatch間に限り、無関係な外部writerは対象外です。他の実行が利用する可能性のあるlockは削除しないでください。

機械処理では終了コードを先に保持して今回のファイルを確認します。以下はBashの `set -e` とPowerShellのnativeエラー昇格でも非ゼロ終了後の結果を読める例です。

```bash
session_dir=$(./scripts/create-session-dir.sh dispatch-result 100)
result_path="$session_dir/dispatch-result.json"
dispatch_code=0
ORCHESTUNE_DISPATCH_REPORT_PATH="$result_path" orchestune-dispatch -p 100 || dispatch_code=$?
if [ -f "$result_path" ]; then jq . "$result_path"; else echo "report not created" >&2; fi
# dispatch_code remains available under set -e; file existence alone is not success.
```

```powershell
$sessionDir = .\scripts\create-session-dir.ps1 dispatch-result 100
$resultPath = Join-Path $sessionDir 'dispatch-result.json'
$env:ORCHESTUNE_DISPATCH_REPORT_PATH = $resultPath
$savedPreference = $PSNativeCommandUseErrorActionPreference
try {
    $PSNativeCommandUseErrorActionPreference = $false
    orchestune-dispatch -p 100
    $dispatchCode = $LASTEXITCODE
} finally {
    $PSNativeCommandUseErrorActionPreference = $savedPreference
    Remove-Item Env:ORCHESTUNE_DISPATCH_REPORT_PATH
}
if (Test-Path -LiteralPath $resultPath -PathType Leaf) { Get-Content -Raw $resultPath | ConvertFrom-Json }
else { Write-Warning 'report not created' }
```

非ゼロ終了でも失敗を含むJSONが保存される場合があります。引数/設定不正、出力予約失敗、完全なcycle reportが返る前の例外ではファイル未生成（`report not created`）となり得ます。終了コード（0:成功、1:fatal/保存失敗、2:retryableまたは引数/設定エラー）とJSONを併用し、ファイルの存在だけで成功と判断しないでください。自動削除は行わず、このリポジトリでは `.orchestune/reports/` をignoreします。

### 主要なオプション

日常のディスパッチ実行で使用するCLIオプションは以下の7つに集約されています。詳細な動作パラメータ（レート制限、トークン予算、タイムアウト、パス等）は設定ファイル（`orchestune.toml`）または環境変数で設定します。

| オプション | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `--parent-issue <int>` / `-p <int>` | - | 開発対象をまとめている親の GitHub Issue 番号を指定。未指定時は現在のGitブランチ名（`parent/issue-<N>`）から推論されます。どちらからも特定できない場合は起動エラーとなります。起票される子Issueがすべてこの親Issueに紐付けられます。 |
| `--apply` / `--no-apply` | `--apply` | 実際にタスク割り当てやGitブランチ作成を実行するか、プレビュー（ドライラン）のみにするかを選択。 |
| `--dispatch-target {local,cloud-routine,codex-cloud,claude-cli,agy-cli,codex-cli,auto}` | 自動選択（非CI: `auto` / GitHub Actions: `cloud-routine`） | エージェントの起動先。未指定時は設定ファイルの値、または実行環境（`GITHUB_ACTIONS`環境変数）から自動選択されます。`auto`はPATH上のローカルCLIを検出します。`local`は後方互換のダミー起動（no-op、テスト・dry-run用途）になります。 |
| `--max-concurrent <int>` | `2` (設定ファイル未指定時) | 同時に実行（起動）できるサブタスクエージェントの最大数。設定ファイルの値よりもCLI引数が優先されます。 |
| `--profile <name>` | - | この実行全体で使用するタスクプロファイル（例: `balanced`, `fast-code`, `deep-reasoning`）をオーバーライドします。タスクメタデータのプロファイルより優先されます。 |
| `--child-review-gate {required,off}` | - | 子サブIssueのレビュー合否検証モード（`required` または `off`）。未指定時は設定ファイル（`child-review-gate`）または環境変数（`ORCHESTUNE_CHILD_REVIEW_GATE`）、既定値は `required`。`off` 指定時は検証をスキップし警告を出力。[§4.5](#45-子レビュー証跡ゲートと第2層セマンティックレビューゲート)を参照。 |
| `--semantic-review-mode {required,advisory,off}` | - | 統合PRの第2層セマンティックレビューモード（`required`、`advisory`、または `off`）。未指定時は設定ファイル（`semantic-review-mode`）または環境変数（`ORCHESTUNE_SEMANTIC_REVIEW_MODE`）、既定値は `required`。`off` でスキップ。[§4.5](#45-子レビュー証跡ゲートと第2層セマンティックレビューゲート)を参照。 |
| `--force-integration-review` | - | 単一タスクFast-Pathをバイパスし、子タスク1件の場合でも統合PRセマンティックレビュー（第2層）を強制実行するフラグ。 |
| `--allow-unsafe-agent-execution` | `False` | ローカルCLI（`claude-cli`、`agy-cli`、`codex-cli`）に対する承認・サンドボックスのバイパス（完全権限実行）を明示的に許可するフラグ。安全のためCLI引数でのみ指定可能（設定ファイルでの指定は禁止）です。未指定でローカルCLIターゲットを実行しようとした場合は設定エラーで拒否されます（Fail-Closed）。 |

### 設定ファイル (`orchestune.toml`) による詳細設定

日常オプション以外の設定（ストレージパス、レート制限、タイムアウト、レビュアー、整合性ループ等）は、リポジトリの設定ファイル（`orchestune.toml` または `pyproject.toml` の `[tool.orchestune]` セクション）に集約して定義します。`cp orchestune.toml.example orchestune.toml` でテンプレートをコピーして使用してください。実ファイルはローカル設定としてGit管理外にし、共有する変更は `orchestune.toml.example` へ反映します。

#### 廃止されたモデルランクからの移行

`model_tier` は廃止されました。計画から削除し、代わりに `execution_profile` を選択してください。モデルは `[execution_profiles.<profile>.<target>]` の下で、ディスパッチ先ごとに設定します。

- **モデルランクのみを指定していた場合:** 以前のランクに対応する専用プロファイル（例: `tier-strong`）を作り、タスクを実行する各ターゲットに意図した `model` を設定して、タスクの `execution_profile` にそのプロファイルを指定します。以前のターゲット別デフォルトは[廃止前の `model_tiers` テーブル](https://github.com/Saltmu/orchestune/blob/95822058eec1243bdec3d13b710a64dee05597ab/orchestune.toml.example#L145-L164)で確認できます。
- **プロファイルとモデルランクを併用していた場合:** 他タスクの設定を変えないよう既存プロファイルを複製し、専用プロファイルを作ります。各ターゲットで `model` だけを以前のランクに対応するモデルへ置き換え、既存の `reasoning_effort` はそのまま維持します。タスクの `execution_profile` に新しいプロファイルを指定します。

dry-run dispatchを使って各ターゲットの解決結果を確認します。たとえば `orchestune-dispatch -p <N> --no-apply --dispatch-target claude-cli` を実行し、その実行で保存された `result.json` の `execution_selections` を開きます。対象タスクの `profile`、`model`、`reasoning_effort` を確認し、計画で使う各ターゲットについて繰り返します。

#### 設定ファイル項目一覧

| 設定キー | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `reviewer-bot` | `"auto"` | 実装後に依頼するレビュアー（`"auto"`, `"claude"`, `"codex"`）。`auto`はターゲットから判定し、Claude系にはCodex、Codex/agy系にはClaudeを割り当てます。 |
| `ci-command` | `"./scripts/local-ci.sh"` | Integratorが統合ブランチ上で実行するCIコマンド（shlex構文の文字列または文字列リスト。例: `"make ci"`）。導入先リポジトリのCIエントリーポイントが異なる場合は必ず設定してください。 |
| `child-review-gate` | `"required"` | 子タスクのレビュー合格証跡（`verdict=pass`、SHA一致）を検証するゲート（`"required"`, `"off"`）。`"off"`で検証をスキップし警告を出力。 |
| `semantic-review-mode` | `"required"` | 統合PRの第2層セマンティックレビューモード（`"required"`, `"advisory"`, `"off"`）。既定値は`"required"`。 |
| `force-integration-review` | `false` | 単一タスクFast-Pathをバイパスし、子タスク1件の場合でも統合PRセマンティックレビュー（第2層）を強制実行するかどうか。 |
| `semantic-review-provider` | `"auto"` | 第2層セマンティックレビューのレビュアープロバイダ（`"auto"`, `"claude"`, `"codex"`）。 |
| `semantic-fix-provider` | `"auto"` | 第2層セマンティック修正のプロバイダ（`"auto"` または設定されたディスパッチターゲット）。 |
| `semantic-review-trigger` | `"pr-comment"` | 第2層セマンティックレビューのトリガー方式（`"pr-comment"`）。 |
| `max-semantic-review-rounds` | `3` | 第2層セマンティックレビューの最大ラウンド数（1〜5）。超過時はエスカレーション。 |
| `max-semantic-fix-retries` | `2` | 第2層セマンティック修正の最大リトライ数（0で自動修正無効化）。 |
| `semantic-review-timeout-seconds` | `1800` | 第2層セマンティックレビュー1回のタイムアウト秒数（既定30分）。 |
| `semantic-review-ack-timeout-seconds` | `600` | レビュー依頼から reviewer の活動が一切ない状態を許す秒数。超えると `review_not_acknowledged` で停止します（既定10分。`semantic-review-timeout-seconds` を超えません）。 |
| `semantic-review-auto-retry` | `false` | 誰も応答していない依頼に限り、追加で1回だけレビューを再依頼します（明示 opt-in）。reviewer の活動が一度でもあれば再依頼しません。 |
| `semantic-review-retry-backoff-seconds` | `300` | ack 期限から opt-in の再依頼を投稿するまでの待ち秒数（0 なら期限と同時）。 |
| `semantic-fix-timeout-seconds` | `1800` | 第2層セマンティック修正1回のタイムアウト秒数（既定30分）。 |
| `semantic-ci-timeout-seconds` | `1800` | 修正後HEADに束縛したローカルCI検証のタイムアウト秒数（既定30分）。 |
| `semantic-attempt-timeout-seconds` | `7200` | 第2層統合PRの試行全体（全ラウンド・修正・CI）のタイムアウト秒数（既定2時間）。超過時はエスカレーション。 |
| `max-launches-per-window` | 未設定（上限なし） | 時間窓（`window-seconds`）あたりの起動数上限。未設定: 時間単位の上限なし（並行数 `max-concurrent` が主軸。トークン予算・競合などの制約は維持）。`0`: 起動禁止（ラベル更新・GCなどの処理は行う）。`1`以上: 時間窓あたりの起動数上限。TOMLにnullはないため、未設定にするにはキーを省略します。 |
| `window-seconds` | `7200` | 起動数上限・`max-tokens-per-window`の集計・aging正規化・起動履歴の保持に使う時間窓の秒数（既定は2時間）。 |
| `deviation-buffer-lines` | `5` | ライブロックを防止するための、フットプリントから逸脱したファイルの変更行数の許容バッファ値。 |
| `max-recompute-retries` | `2` | フットプリント逸脱を検知した際のruntime Conflict Graph再計算のリトライ上限。超過した場合は強制直列化（force-serial）へフォールバックします。 |
| `task-timeout-seconds` | `7200` | dispatch起動タスクをタイムアウトとみなすまでの秒数。`0`でタイムアウト回収を無効化します。対話型claimはタイムアウトの対象外です（GCのゾンビ回収が除外）。ローカル実行はWIP退避後に回収し、停止を確認できない外部（クラウド）実行は`active_worktrees`の枠と実行ハンドルを保持したまま`status:blocked-human-review`へ送ります。2時間を超える正常なタスクも回収・再投入（`max-task-reclaims`超過で`status:blocked-human-review`）の対象になるため、長時間タスクがある環境では`orchestune.toml`で延長してください。 |
| `max-task-reclaims` | `3` | ゾンビ・タイムアウトGCが同一タスクを`status:queued`へ差し戻せる回数の上限。超過したタスクは`status:blocked-human-review`へ遷移します。 |
| `early-death-window-seconds` | `120` | 起動からこの秒数以内にローカルプロセスがコミットなしで終了した場合、一時的な起動障害として扱います。 |
| `max-early-death-retries` | `2` | 一時的な起動障害を自動で再キューイングする上限。次のコミットなし終了は`status:blocked-human-review`へエスカレーションします。 |
| `early-death-backoff-seconds` | `60` | 起動直後の異常終了を再キューイングする際の基準待機秒数。再試行ごとに待機時間を2倍にします。 |
| `max-usage-limit-retries` | `2` | `claude-cli` がセッション上限（`You've hit your session limit` / `usage_limit_reached`）で終了した後に、追加で起動する最大回数。通常の回収・early-death・review-timeoutの回数とは別枠です。`0` は自動再投入なし。使い切ると使用上限による停止として`status:blocked-human-review`へエスカレーションします。 |
| `usage-limit-backoff-seconds` | `900` | リセット時刻が確定できない場合の基準待機秒数（再試行ごとに2倍）。1以上。 |
| `usage-limit-reset-grace-seconds` | `30` | 確定したリセット時刻に加える猶予秒数。この後にタスクを再投入します。 |
| `usage-limit-timezone` | `None` | `resets 1pm` のような壁時計表記を解釈するIANAタイムゾーン（例: `Asia/Tokyo`）。未指定の場合、この表記はUTCとは解釈せず「リセット不明」としてバックオフを適用します。オフセット付き時刻があれば常にそれを優先します。上限が有効な間は、同じ台帳の `claude-cli` タスクを新規に起動せず、他のtargetには影響しません。 |
| `zombie-gc` | `true` | ゾンビプロセスの検出・回収を有効にするフラグ。 |
| `max-tokens-per-window` | `None` | 指定した時間窓内で消費できるトークン数の総上限。累計消費量が上限に達した場合、新規タスクの起動を一時停止します。 |
| `max-tokens-per-task` | `None` | 単一サブタスクが消費できるトークン数の上限。完了時にこの上限を超過していた場合、自動完了を見送りエスカレーションします。 |
| `local-cmd` | `None` | ローカルターゲットへディスパッチするコマンドテンプレート。使用可能な変数: `{issue_number}`, `{subtask_id}`, `{branch_name}`, `{worktree_path}`, `{model}`, `{reasoning_effort}`, `{profile}`, `{reviewer_bot}`。 |
| `routine-id` | `None` | Cloud Routine ターゲットで使用するルーチンID。環境変数 `ORCHESTUNE_ROUTINE_ID` が優先されます。 |
| `codex-cloud-env` | `None` | Codex Cloud ターゲットで使用する環境ID。環境変数 `ORCHESTUNE_CODEX_CLOUD_ENV` が優先されます。 |
| `consistency-mode` | `"off"` | 追加のrepository-wide整合性loop（`"off"`, `"shadow"`, `"repair"`）。 |
| `consistency-repair-code` | `[]` | 追加の`repair` loopで許可するfinding codeまたはcommand codeのリスト。 |
| `consistency-max-repair-passes` | `1` | dispatch cycleあたりのguarded repair／再観測pass上限（1〜5）。 |
| `report-dir` | `.orchestune/reports/dispatch` | primary checkout基準の自動結果保存root。実行単位の `ORCHESTUNE_DISPATCH_REPORT_PATH` が優先。 |
| `run-state-path` | `"run_state.json"` | ディスパッチサイクル間で引き継ぐ実行状態の永続化先。相対パスはprimary checkoutルート基準で解決されます。 |
| `worktree-root` | `"worktrees"` | agent worktreeのルートディレクトリ。相対パスはprimary checkoutルート基準で解決されます。 |
| `log-dir` | `"logs"` | エージェント実行ログの出力先ディレクトリ。 |
| `events-log-path` | `"events.jsonl"` | ディスパッチイベントログの出力先パス。 |
| `not-needed-review-state-path` | `"not_needed_review_state.json"` | Cloud Routine の not-needed レビュー状態記録パス。 |
| `not-needed-review-timeout-seconds` | `86400` | not-needed レビューのタイムアウト秒数。 |
| `integration-dependency-timeout-seconds` | `600` | Integratorの依存準備（`uv sync`）1回の待機上限（秒）。正の整数のみ。 |
| `integration-ci-timeout-seconds` | `1800` | IntegratorのCIコマンド1回の待機上限（秒）。実際の上限は`min(この値, サイクルの残り時間)`。 |
| `integration-cycle-timeout-seconds` | `3600` | 親Issue1件の統合サイクル全体（lock待機・準備・仮マージ・依存準備・CI・PR処理・finalization）の実行予算（秒）。サイクル開始時に単調時計で1回だけ作られます。 |
| `integration-cleanup-timeout-seconds` | `30` | 期限超過後のプロセス停止・出力回収・rollback・記録に使う独立した総予算（秒）。段階ごとに取り直しません。 |
| `integration-command-timeout-seconds` | `60` | サイクル内の補助`git`/`gh`コマンド1回の待機上限（秒）。 |
| `max-integration-timeout-retries` | `2` | **確認済み**の統合timeout後に許可する自動再試行回数（既定で合計3試行）。`0`なら最初のtimeoutが終端です。 |
| `integration-timeout-backoff-seconds` | `60` | 1回目の再試行までの待ち（秒）。2回目はその2倍（既定で60秒、次に120秒）。 |
| `default_execution_profile` | `"balanced"` | タスクでプロファイルが指定されていない場合に使用するデフォルトプロファイル名。 |

default self-healing allowlistは`consistency-repair-code`から意図的に分離されています。内容は`status.blocked-with-resolved-dependencies`、`status.primary-status-conflict`、`execution.requeue`、`execution.update-bookkeeping`、`execution.reclaim`であり、追加loopより前から存在するstatus promotion／reconciliation、state recovery、GCの動作を維持します。組み込みrepair passへ到達したcodeを後段のrepository-wide repair loopが再試行することはなく、Planner候補に現れただけのcommandはuser allowlistの対象に残ります。opt-inしたexecution commandは、組み込み境界と同じguard付きGC／recovery handlerを使用します。

既存動作を保つ場合は`off`、開始／終了findingを追加確認する場合は`shadow`、新規policyを有効にせず最終dispositionを確認する場合はrepair codeなしの`repair`、有効化する場合は限定した`consistency-repair-code`を使用します。`--apply`は既存修復とopt-in policyの変更を許可し、`--no-apply`は外部または永続的な修復副作用を許可しません（GC出力はpreviewとなり、recoveryは一時的なmemory上のpreview bookkeepingだけを更新する場合があります）。

保存したdispatch JSONまたは`events.jsonl`の`consistency.scans`、`consistency.repair_passes`、`consistency.repair_outcomes`を確認してください。Outcomeは`resolved`、`unresolved`、`deferred`、`failed`、`observation-unknown`を区別します。unknown／staleな観測とnon-repairable findingは変更されずreportに残ります。dry-runまたはlive precondition不成立によるcommand単位のskipped resultはfindingの最終dispositionで表され、旧phase所有の修復経路へfallbackすることはありません。status遷移が途中で失敗した場合はIntent journalが`run_state.json`の隣に残り、次cycleが外部副作用を重複させず再開できます。

### クラウド環境変数とシークレット

クラウドターゲット連携用の認証情報および環境識別子は、環境変数経由での設定を基本とします。

| 環境変数名 | 用途 | 優先順位・制約 |
| :--- | :--- | :--- |
| `ORCHESTUNE_ROUTINE_TOKEN` | Claude Code Cloud Routine の API 認証トークン | **環境変数のみ**（セキュリティ保護のため設定ファイルへの記述は厳格に禁止） |
| `ORCHESTUNE_ROUTINE_ID` | Claude Code Cloud Routine のルーチンID | 環境変数 > 設定ファイル（`routine-id`） |
| `ORCHESTUNE_CODEX_CLOUD_ENV` | Codex Cloud の環境識別子 | 環境変数 > 設定ファイル（`codex-cloud-env`） |

### CLI引数から設定ファイル／環境変数への移行対応表

以前のバージョンでCLI引数として提供されていた非日常オプションは、以下のように設定ファイル項目または環境変数へ集約されました。従来のCLIオプションを指定した場合は起動時エラーとなります。

| 旧CLIオプション | 移行先設定 | 備考 |
| :--- | :--- | :--- |
| (旧) `--parent-issue <int>` | `-p <int>` / `--parent-issue <int>` または ブランチ推論 | CLIオプションとして存続（`-p` 短縮形追加、設定ファイルへの記述は禁止） |
| (旧) `--model <name>` | `[execution_profiles.<name>.<target>] model` | プロファイル配下のターゲット別テーブルに定義 |
| (旧) `--reasoning-effort <effort>` | `[execution_profiles.<name>.<target>] reasoning_effort` | プロファイル配下のターゲット別テーブルに定義 |
| (旧) `--reviewer-bot <bot>` | 設定ファイル `reviewer-bot = "..."` | 設定ファイルのみに集約 |
| (旧) `--local-cmd <cmd>` | 設定ファイル `local-cmd = "..."` | 設定ファイルのみに集約 |
| (旧) `--routine-id <id>` | `ORCHESTUNE_ROUTINE_ID` または 設定ファイル `routine-id` | 環境変数優先 |
| (旧) `--routine-token <token>` | `ORCHESTUNE_ROUTINE_TOKEN` | 環境変数のみ（設定ファイルへの記述禁止） |
| (旧) `--codex-cloud-env <id>` | `ORCHESTUNE_CODEX_CLOUD_ENV` または 設定ファイル `codex-cloud-env` | 環境変数優先 |
| (旧) `--ci-command <cmd>` | 設定ファイル `ci-command = "..."` | 設定ファイルのみに集約 |
| (旧) `--max-launches-per-window` | 設定ファイル `max-launches-per-window` | 設定ファイルのみに集約 |
| (旧) `--window-seconds` | 設定ファイル `window-seconds` | 設定ファイルのみに集約 |
| (旧) `--max-tokens-per-window` | 設定ファイル `max-tokens-per-window` | 設定ファイルのみに集約 |
| (旧) `--max-tokens-per-task` | 設定ファイル `max-tokens-per-task` | 設定ファイルのみに集約 |
| (旧) `--deviation-buffer-lines` | 設定ファイル `deviation-buffer-lines` | 設定ファイルのみに集約 |
| (旧) `--max-recompute-retries` | 設定ファイル `max-recompute-retries` | 設定ファイルのみに集約 |
| (旧) `--task-timeout-seconds` | 設定ファイル `task-timeout-seconds` | 設定ファイルのみに集約 |
| (旧) `--max-task-reclaims` | 設定ファイル `max-task-reclaims` | 設定ファイルのみに集約 |
| (旧) `--early-death-window-seconds` | 設定ファイル `early-death-window-seconds` | 設定ファイルのみに集約 |
| (旧) `--max-early-death-retries` | 設定ファイル `max-early-death-retries` | 設定ファイルのみに集約 |
| (旧) `--early-death-backoff-seconds` | 設定ファイル `early-death-backoff-seconds` | 設定ファイルのみに集約 |
| (旧) `--zombie-gc` | 設定ファイル `zombie-gc` | 設定ファイルのみに集約 |
| (旧) `--consistency-mode` | 設定ファイル `consistency-mode` | 設定ファイルのみに集約 |
| (旧) `--consistency-repair-code` | 設定ファイル `consistency-repair-code` | 設定ファイルのみに集約 |
| (旧) `--consistency-max-repair-passes` | 設定ファイル `consistency-max-repair-passes` | 設定ファイルのみに集約 |
| (旧) `--run-state-path` | 設定ファイル `run-state-path` | 設定ファイルのみに集約 |
| (旧) `--worktree-root` | 設定ファイル `worktree-root` | 設定ファイルのみに集約 |
| (旧) `--log-dir` | 設定ファイル `log-dir` | 設定ファイルのみに集約 |
| (旧) `--events-log-path` | 設定ファイル `events-log-path` | 設定ファイルのみに集約 |
| (旧) `--not-needed-review-state-path` | 設定ファイル `not-needed-review-state-path` | 設定ファイルのみに集約 |
| (旧) `--not-needed-review-timeout-seconds` | 設定ファイル `not-needed-review-timeout-seconds` | 設定ファイルのみに集約 |
| (旧) `--allow-unsafe-agent-execution` | `--allow-unsafe-agent-execution` | CLIオプションとして存続（設定ファイルへの記述は禁止） |

#### 設定ファイルの記述例 (`orchestune.toml`)
```toml
max-concurrent = 2
dispatch-target = "claude-cli"
reviewer-bot = "auto"
consistency-mode = "shadow"
consistency-repair-code = []
consistency-max-repair-passes = 1
run-state-path = "run_state.json"
default_execution_profile = "balanced"

[execution_profiles.balanced.claude-cli]
model = "sonnet"
reasoning_effort = "medium"

[execution_profiles.balanced.codex-cli]
model = "gpt-5.6-terra"
reasoning_effort = "medium"

[execution_profiles.deep-reasoning.claude-cli]
model = "opus"
reasoning_effort = "high"

[execution_profiles.deep-reasoning.codex-cli]
model = "gpt-5.6-sol"
reasoning_effort = "high"

[execution_profiles.deep-reasoning.cloud-routine]
model = "claude-opus-5"

[execution_profiles.fast-code.claude-cli]
model = "haiku"

[execution_profiles.fast-code.codex-cli]
model = "gpt-5.6-luna"
reasoning_effort = "medium"
```

#### 設定ファイルの記述例 (`pyproject.toml`)
```toml
[tool.orchestune]
max-concurrent = 2
dispatch-target = "claude-cli"
reviewer-bot = "auto"
consistency-mode = "shadow"
consistency-repair-code = []
consistency-max-repair-passes = 1
run-state-path = "run_state.json"
default_execution_profile = "balanced"

[tool.orchestune.execution_profiles.balanced.claude-cli]
model = "sonnet"
reasoning_effort = "medium"

[tool.orchestune.execution_profiles.balanced.codex-cli]
model = "gpt-5.6-terra"
reasoning_effort = "medium"

[tool.orchestune.execution_profiles.deep-reasoning.claude-cli]
model = "opus"
reasoning_effort = "high"

[tool.orchestune.execution_profiles.deep-reasoning.codex-cli]
model = "gpt-5.6-sol"
reasoning_effort = "high"
```

> [!NOTE]
> 設定項目名は、CLI オプションに対応するケバブケース（例: `max-concurrent`）と、内部変数名に対応するスネークケース（例: `max_concurrent`）のどちらの形式でも記述可能です。
> コマンドライン引数で明示的にオプションが指定された場合は、設定ファイルの値よりもコマンドライン引数の値が優先されます。
> 未知のキーや不正な値がある場合は、既定値へフォールバックせず起動時にエラーで停止します。親Issue（`parent_issue`）、安全バイパス（`allow_unsafe_agent_execution`）、ルーチントークン（`routine_token`）、およびトップレベルの `model`/`reasoning_effort` は設定ファイルへの記述が禁止されています。真偽値は TOML の bool、パス・文字列の設定は文字列、整数の設定は TOML の整数で指定してください。`consistency-repair-code`は空でない文字列のlistです。`max-concurrent`、`max-launches-per-window`、`deviation-buffer-lines`、`max-recompute-retries`、`task-timeout-seconds`、`max-task-reclaims`、`early-death-window-seconds`、`max-early-death-retries`、`early-death-backoff-seconds`、`not-needed-review-timeout-seconds`、`max-integration-timeout-retries` は `0` 以上、`window-seconds` と`integration-*-seconds`の各設定（`integration-timeout-backoff-seconds`を含む）は `1` 以上、`consistency-max-repair-passes`は`1`～`5`です。
>
> `[execution_profiles]`（または `[tool.orchestune.execution_profiles]`）では、各プロファイル名（例: `balanced`, `deep-reasoning`, `fast-code`）配下にターゲット名（`claude-cli`, `agy-cli`, `codex-cli`, `cloud-routine`, `codex-cloud`）別のテーブルを定義します。各ターゲット設定では `model`（文字列）および `reasoning_effort`（`"low"` / `"medium"` / `"high"`）が指定可能です。`execution_profiles` テーブルを定義する場合、`default_execution_profile`（未指定時は `"balanced"`）のエントリが必ず含まれている必要があります。

---

## 5. 統合（Integration）と自動リベース

`orchestune-dispatch` コマンドは、**タスクの割り振りだけでなく、完了したタスクの統合処理も同時に行います。**

### 4.1 共通の統合サイクル

1. エージェントがタスクを完了してプルリクエスト（PR）を作成し、Issueに `status:done` ラベルが付くと、ディスパッチャー（Integrator）がそれを検知します。
2. Integratorは統合先ブランチ（後述の base ブランチ）から一時統合ブランチを作成し、対象の子ブランチを順にマージした上でローカルCI（既定では `./scripts/local-ci.sh`）を実行します。
3. CIが成功すれば一時統合ブランチを `origin` へpushし、base ブランチへの統合PRを作成（または既存PRを再利用）します。
4. 統合対象として取り込まれた子Issueには `integration:included` ラベルが付与されます。

必須の `--parent-issue` が統合先（base）と一時統合ブランチを決めます。

| `--parent-issue` | base ブランチ | 一時統合ブランチ |
| :--- | :--- | :--- |
| `N` | `origin/parent/issue-{N}` | `integration/temp-parent-issue-{N}` |

### 4.2 親ブランチによる二層統合

統合は「子ブランチ → 親ブランチ」「親ブランチ → main」の二層構造です。

1. **子ブランチ → 親ブランチ（自動）**: 子PRは `parent/issue-{N}` ブランチへ自動的に統合されます。CIを通過した統合PRは人間の確認を待たずに自動マージされ、対象の子Issueは自動的にクローズされます。したがって、エージェントが作成した個別の子PRを人間がマージする必要はありません（レビュー用の記録として残ります）。
   - 自動マージに失敗した場合（ブランチ保護・権限設定など）は、対象Issueへその旨がコメントされ、次のディスパッチサイクルで自動的に再試行されます。
2. **親ブランチ → main（人間がマージ）**: 親Issue配下の全子Issueがクローズされると、`parent/issue-{N}` から `main` への最終統合PRが自動的に用意されます。**この最終PRをマージするかどうかの判断とマージ操作は、常に人間が行います。** 最終PRのマージが検知されると、親Issueは自動的にクローズされます。

### 4.3 自動リベース

下流の依存タスクのブランチは、依存先タスクの完了状況に応じて自動でリベースされます。リベース先は「最新の main」ではなく、**CIを通過済みの依存先タスクのブランチ**です（スタッキング）。依存先が単一に絞り込めない場合や、依存先がまだCIを通過していない場合、自動リベースは行われません。

### 4.4 IssueとPRの相互リンク通知

GitHubの `Closes #N` による自動リンクとIssueサイドバーの「Development」欄は、PRのbaseが既定ブランチ（`main`）の場合にのみ機能します。そのため親ブランチ運用では、子Issueだけを見てもどのPRで作業されたのかを辿れません。Orchestuneはこれを次のコメントで補完します。

1. **PR作成時**: ディスパッチャーがオープンPRを検知すると、対応する子Issueへ「PR #XXX が作成されました」という通知コメントを投稿します。対象Issueは、PR本文の `Closes #N` 参照とheadブランチ名（`claude/issue-{N}-{subtask_id}`）の双方から解決され、**PRのbaseがそのIssue自身の親ブランチ（`parent/issue-{親Issue番号}`）と完全に一致する場合にのみ**通知します（別の親ブランチ宛てのPRが `Closes` でこのIssueを参照しているだけの場合は通知しません）。また、upstreamリポジトリ上のブランチを head とするPRに限られ、forkからのPRおよびheadの出所を確認できないPRは、なりすまし防止のため通知しません。エージェントが自分で起票したPRも同じ経路で通知されます。
2. **PRマージ時**: Integratorが統合PRを親ブランチへマージして子Issueをクローズする際、クローズの**直前**に「PR #XXX が親ブランチにマージされました」という完了通知コメントを投稿します。クローズと同じコメントに載せないのは、クローズだけが失敗した場合に次サイクルの再試行が統合PR番号を復元できず、リンクが恒久的に失われるためです。

いずれのコメントにも `<!-- orchestune:pr-link:{created|merged}:{PR番号} -->` 形式のマーカーが埋め込まれます。Orchestuneはマーカー文字列の一致だけでなく、その投稿者が正規の実行者（`GET /user` による自己 identity）またはリポジトリのコラボレーター（`triage`/`push`/`maintain`/`admin` 権限保持者）であることを検証します（[Issue #686](https://github.com/Saltmu/orchestune/issues/686)）。
第三者による未認証マーカーや、API障害等で投稿者・権限の検証が解決できない場合（`UNKNOWN`）、および通知コメントの投稿に失敗した場合は、安全のためIssueのクローズを保留（defer）し、次サイクルの再試行に委ねます（クローズコメントへの同梱フォールバックは廃止されました）。統合receiptにPR番号が永続化されているため、マージ後の再試行サイクルでもリンクを復元して安全にクローズ処理が行われます。

---

<a id="45-子レビュー証跡ゲート"></a>
### 4.5 子レビュー証跡ゲートと第2層セマンティックレビューゲート

Orchestuneでは、子タスクから親ブランチ（`parent/issue-{N}`）への統合において、**第1層（子PRレビュー証跡ゲート）**と**第2層（統合PRセマンティックレビュー・修正ループゲート）**の二段構えゲートを採用しています。

- **第1層（子PRレビュー証跡ゲート）**: Integratorが統合一時ブランチを作成する前に、統合対象の各子が合格したレビュー証跡（`verdict=pass`、HEAD SHA一致）を持つことを検証します（既定: `required`）。
- **第2層（統合PRセマンティックレビューゲート）**: 複数タスクの統合PRに対し、結合特化のセマンティックレビュー・修正ループ（レビュー収集 → 判断兼修正ジョブ → claim付き昇格 → ローカルCI → 再レビュー）を実行します（既定: `required`）。子タスクが1件のみで第1層を通過している場合は、単一タスクFast-Path（`fast_path_passed`）により第2層レビューをバイパスして即時統合されます（`--force-integration-review` で強制可能）。詳細なアーキテクチャは [architecture/integration.md](architecture/integration.md) を参照してください。
  > [!NOTE]
  > `semantic_review_mode = "required"` のもとでは、Integratorの第2層ゲート検証はディスパッチターゲット種別（クラウド／ローカル）や旧環境フラグによらず常に有効です。第2層レビューの実行（ループ進行）はRunnerが担当し、第2層が完了（`passed` または `skipped_by_human`）するまでIntegratorによる親ブランチ確定は保留されます。

**人間による2段階のスキップ方法**

1. **実行全体スキップ**: `--semantic-review-mode off`（設定キー `semantic-review-mode = "off"`、環境変数 `ORCHESTUNE_SEMANTIC_REVIEW_MODE=off`）を指定すると、第2層セマンティックレビューをオフにして統合を進めます。
2. **個別統合PRの明示スキップ**: `status:blocked-human-review` やエスカレーション（`escalated`）状態の統合PRに対し、コラボレーター権限を持つ人間が有効なJSONペイロードを含むoverrideコメント（`<!-- orchestune:semantic-review-override:v1 {"attempt_id": "<attempt_id>", "head_sha": "<head_sha>", "reason": "人間による確認完了"} -->`）をPRに投稿することで、試行を `skipped_by_human` に遷移させて安全に親ブランチへ確定・統合できます。

**第1層の責務分担**

| 担当 | 責務 |
| :--- | :--- |
| 開発スキル（レビューループ・Step 11） | 対話モードでは着手依頼の直後（claim前）に明示的なレビュアー選択（`claude` / `codex` / `skip`）を求めて記録し、PR作成後は再質問せずその選択で子PR上のレビューを実行し、指摘ごとにLLMの判断（`adopt` / `decline` / `already_addressed` / `needs_information` / `duplicate`）を判断表へ記録する。 |
| `orchestune complete --issue <N> --pr <PR> --result done --reviewer <bot> --review-reply <file>` | PRのレビュー状態を取得し直し、判断表が現在の全指摘を網羅していること、`unresolved`が残っていないこと、`needs_information`と必須指摘（`adopt`）の`deferred`は最終巡（`MAX_REVIEW_ROUNDS`＝5巡以上）に限り、`evidence`がこのリポジトリのOPENなフォローアップIssue（PR・CLOSED・別リポジトリ・タスク自身は不可）を指す場合だけ許可されること（`required_deferred`として件数を記録。参照の取得に失敗した場合は`evidence_missing`）、レビュー対象SHAがPRのheadおよびローカルHEADと一致することを確認する。`verdict`・`reviewed_head_sha`・判断表のdigestをdone Outcome Recordへ保存する。不一致の場合はcompleteを拒否し（`review_evidence_invalid`・`review_head_mismatch`・`evidence_missing`）、何も投稿しない。`skip`は`verdict=skipped`として記録され、合格にはならない。 |
| Integrator | 保存済みの証跡を検証するだけで、レビュー自体は実行しない。 |

**合格条件（子ごと）**: 子Issueの最新のOutcome Recordが`result=done`かつ`verdict=pass`で、`head_sha`と`reviewed_head_sha`の両方がマージ対象のコミットSHAと一致すること。レビュー後にリベースやpushを行うとSHAが変わるため、再レビューが必要です。

**ゲートが統合を停止したとき**: 親ブランチは更新されず、子Issueのクローズも子ブランチの削除も行われません。**親Issue**が`status:blocked-human-review`へ遷移し、各子と理由を列挙したコメント1件（マーカー`<!-- orchestune:child-review-gate digest=… -->`）が付きます。同じ失敗の組み合わせは以降のサイクルで再コメントされず、ラベルが外れている場合に限り復元されます。ゲートは統合サイクルごとに再評価されるため、証跡が整えば同じ統合がそのまま進みます。このゲート自身は、親Issueの`status:blocked-human-review`を外しません（[status-labels.md](status-labels.md)を参照）。

| 理由 | 意味 | 再開方法 |
| :--- | :--- | :--- |
| `legacy` | Outcomeにレビュー証跡がない（ゲート導入前に記録された） | 下記「再開」を参照 |
| `skipped` | レビューが明示的にスキップされた | 下記「再開」を参照 |
| `not_pass` | `verdict`が`pass`でない、または`result`が`done`でない | 指摘を解消してレビューを合格させ、下記「再開」を参照 |
| `sha_mismatch` | `complete`後に子のheadが動いた（リベース・push） | 新しいheadを再レビューし、下記「再開」を参照 |
| `absent` | 子IssueにOutcome Recordがない | 子について`orchestune complete`を実行する |
| `lookup_unknown` | 子Issueのコメント取得に失敗した（API障害等） | dispatchを再実行する（子側の対応は不要） |
| `integration_evidence_missing` | 統合証跡または子との対応がない | 統合証跡を復旧して再実行する |

**再開**

- **完了がhandoffされる前**（`complete`が拒否され、何も投稿されていない場合）: 原因（現在のheadの再レビュー、判断表の補完）を解消して`orchestune complete`を再実行します。証跡が記録され、次のサイクルで子が統合されます。
- **証跡が不足したままcompleteがhandoff済みの場合**（`legacy`・`skipped`・`not_pass`・`sha_mismatch`）: 同じclaimで`complete`を再実行しても証跡の追加・差し替えはできません。同一リクエストは保存済みの結果を再生するだけで、レビュー引数を変えたリクエストは`request_fingerprint_mismatch`で拒否されます。進める必要がある実行に限り、ゲートを明示的にOFFにして再開します（下記）。OFFはその実行の**すべての**子で検証を行わないため、レビュー証跡なしで受け入れる子に限って使ってください。
- **親Issueのラベルの解除（どちらの経路でも）**: ゲートは親Issueの`status:blocked-human-review`を外さないため、証跡を整えた、または受け入れた後に自分で外してください。再実行の前に外しても安全で、ゲートが再び統合を停止した場合は、同じコメントを重複投稿せずにラベルだけを復元します。再実行前に外さない場合は、統合の成功後、親Issueがクローズされる前に外してください。

**設定**: `--child-review-gate {required,off}`、設定キー`child-review-gate`、環境変数`ORCHESTUNE_CHILD_REVIEW_GATE`。既定値は`required`です。`off`はその実行のすべての子で検証をスキップし、警告を出力します。明示的なオプトアウトであり、自動で選ばれることはありません。

**移行**: このゲート導入前に投稿されたOutcome Recordにはレビュー証跡がなく、`legacy`として停止します。移行期間中は、(a) 進行中の子の統合が終わるまで明示的に`off`を指定する、または (b) 停止を前提に、まだhandoffされていない子は子PRで再レビューして`orchestune complete`を再実行し、すでにhandoff済みの子は (a) を使います。legacyの子の統合が済んだら`required`へ戻してください。

**第2層ゲートの移行と注意点**: 第2層セマンティックレビューは既定で `required` です。セマンティックレビューbotやワークフローが未導入のリポジトリでは、複数タスクの統合PRが `review_not_acknowledged` でエスカレーションします。未導入環境では `--semantic-review-mode advisory` または `--semantic-review-mode off` を指定して運用してください。なお、統合PRのCI検証はリポジトリの `ci-command`（ローカルCI）によって検証され、修正出力は一時ブランチ（`integration/semantic-fix/*`）へ安全に退避・昇格されます。トークン上限管理は本レイヤのスコープ外（未対応）です。

#### 応答しない第2層 reviewer への対応（#1436）

- **どの reviewer に依頼するか**: `semantic-review-provider = "claude"` / `"codex"` は明示指定です。`"auto"` は dispatch target から別ベンダーを選びます（`claude-cli`・`cloud-routine` → `codex`、`codex-cli`・`codex-cloud`・`agy-cli` → `claude`。汎用の `local` は解決できません）。`issue_comment` で動くワークフローは常に**デフォルトブランチ**の定義で動くため、そこでワークフローや App が有効でない reviewer は応答しません。
- **早期停止**: 依頼から `semantic-review-ack-timeout-seconds` の間、reviewer の活動がなければ、`semantic-review-timeout-seconds` を待たずに `review_not_acknowledged` で停止します。PR と親 Issue の通知に、attempt・head・round・reviewer とその決まり方・依頼コメント・経過時間と期限・再開コマンドを載せます。通知は attempt と理由ごとに1回だけです。
- **`orchestune review`**（`--pr` は統合 PR）:
  - `orchestune review status --pr <PR> [--json]` は読み取り専用で、同じ情報をいつでも表示します。
  - `orchestune review retry --pr <PR> --reason "<理由>"` は停止した attempt を再開して reviewer に再依頼し、`orchestune review override --pr <PR> --reason "<理由>"` はこの head のゲートをスキップします。どちらも `--apply` を付けない限りマーカーの**プレビューだけ**で、`--apply` で1回だけ投稿します。理由が空、書き込み権限を持つ人間ではない実行者、head の移動、invalidated／終了済みの attempt、複数の open attempt、読めない state は拒否し、投稿の直前にも再検証します。override は人間の判断に限り、自動では使いません。
- **opt-in の再依頼**: `semantic-review-auto-retry = true` のとき、誰も応答していない依頼を `semantic-review-retry-backoff-seconds` 後に1回だけ繰り返します。再依頼にも独自の ack 期限があり、超えれば同じ停止になります。回数はその round・head で PR 上に既にある依頼の数なので、再起動や同時実行でも繰り返されません。別 bot への自動切替は未対応です。`semantic-review-provider` を変えたうえで `retry` を使ってください。

#### 無効化された第2層 attempt の後始末（#1440）

親ブランチが進む、または子の集合が変わると、runner は旧 attempt を `invalidated` として保存し、再利用しません。post-cycle はこの attempt を、再開できる段階に分けて片付けます（結果は semantic review レポートの `cleanup` リスト。`--no-apply` は計画の報告だけです）。

1. **通知**: 旧 PR に1回だけコメントします。attempt・理由・構築時の base・現在の親ブランチ先頭・この attempt の head を示し、レビューとマージの対象外になったと伝えます。後続の有無は書きますが、PR 番号は推測しません。
2. **クローズ**: PR を閉じます（REST。冪等で、結果を読み戻します）。merged の PR は破壊的な清掃を止め、閉じた PR を再オープンすることもありません。
3. **後続の案内**: 同じ親に正当な置き換え attempt がちょうど1つ確定したら、旧 PR に別のコメントで1回だけ案内します。旧 PR が閉じられブランチが消えた後でも追記し、重複させません。候補が複数なら案内しません。
4. **ブランチ**: `integration/temp-parent-issue-<N>-<id>` を、信頼済みの `invalidated` 状態に記録された head を lease にして削除します。対象は、attempt ID から導いた名前が信頼済み state と一致し、本リポジトリ内で、PR が閉じており、他の open PR が使っていないブランチだけです。tip が進んでいる、fork、信頼できない state の場合は残します。

各段階は毎サイクル、通知コメント・PR の状態・リモートのブランチから導き直すので、どこで中断しても最初の未完了の段階から再開し、人が先に閉じた PR の取り残しブランチも回収します。旧 fix ジョブの停止を（ポーリングで）確認してから、semantic claim の下で書き込みます。失敗した段階は `failed` として報告され（post-cycle の phase は警告）、次のサイクルで再試行します。新しい attempt の作成は止めず、レビュー結果も変えません。status は `cleaned`・`planned`・`pending`（claim 保持中・ジョブ実行中・クローズ未確認）・`blocked`（merged・tip 移動・共有ブランチ）・`failed` です。

### 4.6 統合実行の期限とtimeoutからの復旧

依存準備（`uv sync`）・CIコマンド・親Issue1件の統合サイクル全体に期限を設けています（#820）。7つの設定（`integration-*`と`max-integration-timeout-retries`）は[設定表](#設定ファイル-orchestunetoml-による詳細設定)にあります。専用のCLIフラグや環境変数はなく、`0`が「無期限」を意味することもありません。

**1サイクルの流れ**: 親単位の実行ロック（`worktrees/.locks/integration-parent-issue-<N>-execution.lock`。結果の記録まで保持）を取得 → 子を一時ブランチへマージ → 親Issueへ`reserved`イベントを保存して読み戻し → 依存準備とCIを各1回だけ実行 → timeoutなら管理下のプロセス群全体を停止し、マージ前SHAへrollbackして`HEAD`を確認し、`finished`イベントを記録します。サイクル期限は親のサイクル開始時に単調時計を1回読んだ値で、各段階は`min(段階の上限, サイクルの残り時間)`だけ待ち、期限後に新しい処理は始めません。停止・出力回収・rollback・記録は`integration-cleanup-timeout-seconds`の単一の予算を共有します。

| status | 意味 | 次の動作 |
| :--- | :--- | :--- |
| `execution_timed_out` | 段階がtimeout（または期限超過）し、停止とrollbackが**確認済み**。 | push・included付与・Issueクローズ・ブランチ削除は行わず、子Issueは`status:done`のままです（再投入は**しません**）。バックオフ（`integration-timeout-backoff-seconds`、次回は2倍）後のサイクルで、`max-integration-timeout-retries`まで自動再試行します。 |
| `execution_retry_exhausted` | 許可された最後の試行（既定で3回目）もtimeout。 | `terminal`イベントを保存し、**親Issue**を`status:blocked-human-review`へ送ります。ラベル更新が失敗してもCIは起動せず、ラベルと通知だけを再試行します。 |
| `execution_cleanup_failed` | 停止・rollback・`HEAD`確認・cleanup予算のいずれかを確認できない。 | worktreeと診断情報を**保持**（再利用・削除・回収しない）し、新しいCIを起動せず、親を人間確認へ送ります。 |
| `execution_indeterminate` | push/PRの書き込みがtimeoutして結果不明、履歴の読み取り失敗・不整合、予約や結果の保存を確認できない、または結果が未記録の過去の試行がある。 | 推測・再試行・リモートの巻き戻しは行わず、結果不明の書き込みは照合のため保持します。 |

通常の非ゼロ終了や起動失敗はtimeoutでは**ありません**。既存どおり（CI出力付きで子を再投入）に処理し、timeout回数へ加算も消去もしません。通常成功が確認できると現在のgenerationが閉じて回数が戻ります。回数は親Issueとgenerationに結び付き、run ID・タスク集合・CIコマンド・設定には依存しないため、ランナーの再作成や上限の引き下げで消えません。最終レポートとDispatcherの結果には、原因・対象・段階・設定上限と有効上限・試行・次回可能日時・停止/rollback/書き込みの確認状態・出力末尾が残ります。Dispatcherが再試行するのは`execution_timed_out`だけで、残る3つは人間確認の警告として扱います。

**holdや終端状態の解除（運用者の手順）**

1. そのrunのCIプロセスが残っていないことを確認し、保持されたworktree（`worktrees/integration-temp-*`）とリモートref（`parent/issue-<N>`、`integration/temp-*`）を点検します。結果不明の書き込みは、実際のrefと既存の統合証跡に照らして先に照合してください。
2. **親Issue**へ、理由付きの**reset**コメントを1件投稿します（実行主体、または`write`/`maintain`/`admin`権限のユーザー）。`generation`は現在のgeneration+1、`references`は過去のイベントコメントにあるterminalまたは未確定の`attempt_id`（GitHub上の試行を持たないholdなら`local-hold:<holdファイル名から.jsonを除いたもの>`）です。

<!-- orchestune:integration-execution:v1 -->
```json
{
  "parent_issue_number": 123,
  "generation": 2,
  "attempt_id": "reset-2026-01-01-ops",
  "event": "reset",
  "targets": [],
  "executed_at": "2026-01-01T00:00:00Z",
  "reason": "Processes, worktree and remote refs verified by <operator>",
  "references": "<attempt_id of the terminal or unconfirmed attempt, or local-hold:<hold file name without .json>>"
}
```

3. worktreeとholdの記録（`worktrees/.holds/<key>.json`）は運用者が削除します。holdは、resetで新しいgenerationが開かれるまで親の新しいCIを止め、自動では回収されません。

`status:blocked-human-review`を外すだけでは何もリセットされません。回数はラベルではなくイベントコメントに保存されているためです。

**保証と限界。** 待機は有界で、timeoutの再試行には停止の確認が前提です。Linux/macOSでは専用のsession/process groupで起動し（`SIGTERM`、最大5秒の猶予、`SIGKILL`）、Windowsでは suspended で生成してkill-on-closeのJob Objectへ割り当ててからresumeします（Job割り当てに失敗したらコマンドは実行しません）。出力は並行して有限の末尾バッファへ読み出し、回収待ちにも同じcleanup予算を使います。補助`git`/`gh`の呼び出しは直接の子プロセスのtimeoutだけで有界化され、スコープ内`git`のhook・SSH・credential helperが起こす子孫プロセスは所有も停止もしません。対象外: OSのプロセス生成APIと割り込み不能なカーネルI/O（厳密な壁時計上限は保証しません）、プロセスグループから意図的に離脱するPOSIXのdaemon、別ホストから同じ親への同時apply（GitHubコメントに原子的なcompare-and-swapはなく、競合を検出したら停止します）、そしてワーカーの`task-timeout-seconds`（別の実行予算で、Integratorを有界にはしません）。LinuxとWindowsはCIマトリクスで検証し、macOSはPOSIX実装を共有します。

## 6. 未着手の分解世代を置き換える（`orchestune replan`）

`orchestune provision` は計画からIssueを**初回作成**するためのコマンドです。親Issue
の要件を維持したまま未着手の分解が陳腐化した場合は、旧Issueを履歴として残して
世代を置き換える `orchestune replan` を使用します。既定のpreviewは必ずread-onlyです。

```bash
orchestune replan --plan decomposition_plan.md --parent-issue 123
```

previewは新世代の `create` / `reuse` と旧世代の `retire` / `manual-review` /
`conflict` / `no-op` を対象Issueとともに表示し、snapshotに束縛されたtokenを出力します。
applyには、その時点のtokenを明示します。

```bash
orchestune replan --plan decomposition_plan.md --parent-issue 123 \
  --apply --confirm-preview replan-preview-v1:sha256:<token>
```

`in-progress`、`done`、closed、status競合、またはmerged成果物を持つ旧Issueは自動で
置き換えません。applyは新planを埋め込んだ親Issue本文がGitHubの本文上限
（65,536文字）を超えないことを最初の書き込みより前に検証し、超える場合はIssueの作成・
関係変更・旧世代の廃止を一切行わずexit code `3` で停止します。部分失敗後は新しい
previewとtokenが必要で、完了済みの再実行はGitHubを変更しません。exit codeは `0`（安全なpreview/成功）、`2`（設定不備）、`3`（承認不足・token
不一致）、`4`（部分適用）、`5`（active世代のno-op）、`6`（競合またはmanual-reviewを含むpreview）です。

---

## 7. タスクの着手とワークツリー準備（`orchestune claim`）

起票されたサブタスクに着手する際は、`orchestune claim` コマンドを使用します。タスクの前提条件（依存タスクの完了など）の事前検証、ベースブランチの取得、作業用 worktree の作成、進行中台帳の記録、および GitHub Issue のステータスラベル更新を安全に一括実行します。

```bash
# 対象のIssue番号を指定して着手
orchestune claim 123
```

成功すると、Issue番号、Claim ID、ブランチ名、作成された worktree のパス、ベースブランチ等が表示され、終了コード `0` を返します。出力された worktree パスへ移動（`cd <worktree_path>`）して実装作業を開始します。

### 主要なオプション

| オプション | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `--no-apply` | 無効 | Git、GitHub、台帳の変更を行わず、事前検証と予定値の表示のみを行うプレビュー（ドライラン）モード。 |
| `--resume <claim_id>` | なし | 途中停止した既存の claim を、期待する世代と登録worktreeを照合して再開する。 |
| `--adopt-pr <番号>` | なし | 検証済みの孤立 PR を `status:blocked-human-review` から claim する。`--resume`、`--amend-footprint` とは併用不可。 |
| `--amend-footprint` | 無効 | 保持中の claim のファイル予約を拡張する。`--resume` とは併用不可。詳細は後述。 |
| `--state <path>` | `run_state.json` | 実行状態台帳ファイルのパスを指定。 |
| `--timeout <seconds>` | なし | 台帳ロックのタイムアウト秒数。 |

### 孤立 PR の復帰

ワーカーが claim を完了せずに終了すると、GC が `completed_orphaned_pr` と報告することがあります。Issue を再キューせず既存 PR を引き継ぐには、主チェックアウトで次を実行します。

```bash
orchestune claim <issue> --adopt-pr <pr> --no-apply
orchestune claim <issue> --adopt-pr <pr>
cd <表示されたworktree>
./scripts/local-ci.sh
orchestune complete --issue <issue> --pr <pr> --result done --reviewer <bot> --review-reply <file>
```

旧実行の記録と停止状態、信頼できる対応するエスカレーション通知、同一リポジトリの一意な open PR、正規 head ブランチ、親 base、リモート HEAD の一致を確認します。証跡の欠落・曖昧さ、他の claim、未完了の completion、fork、HEAD の変更があれば拒否します。PR 番号と HEAD は claim に結び付けられ、その後 HEAD が変わると完了できません。通常のローカル CI とレビュー証跡のゲートも必要です。手動マージは Layer-1 ゲートを通らない代替手段です。

孤立通知の後に `status:blocked-human-review` に言及するコメントがあると、最新のエスカレーション理由を特定できず、引き継ぎが拒否されることがあります。

### 失敗時の対応

前提条件の未達（先行タスク未完了など）や競合、環境エラーが発生した場合は、非ゼロの終了コードとともにエラー理由と推奨される次のアクションが標準エラー出力に表示されます。指示に従って競合を解消するか、中断された claim を `--resume` で復旧してください。なお、すでに着手済みで作業ツリー内にいる場合は、再度の claim は不要です。

すでに保持している Issue に対して `orchestune claim <N>` を再実行すると `existing_claim_unrecovered` で失敗し、次のアクションとして worktree のパス、`--resume` コマンド、`--amend-footprint` コマンドが表示されます。

GitHub GraphQL が使えない環境（例: Claude Code のクラウドセッションは HTTP 403 を返す。古い `gh` は `parent` / `blockedBy` フィールドを受け付けない）では、claim は REST API（`gh api`）で Issue を読み書きします。対象は Issue の本文・状態・ラベル・ネイティブの親と blocked-by 依存、リポジトリ名です。依存は、REST API が件数を返し、取得した一覧がその件数と一致した場合にだけ受け入れます。確認できない場合は「依存なし」とはみなさず、`forge_unavailable`（終了コード `35`）で止まります。それ以外の想定外のエラーは従来どおり詳細を出さずに `generic_error` と表示し、例外のクラス名だけを `error_type=` として出します。`orchestune complete` も、PR の証跡（head・base・状態・マージコミット・変更ファイル・CI のチェック）を REST で取得します。REST API は「その PR が閉じる Issue」とレビュー判定を返さないため、レビュー判定は記録されません。マージ済みの PR で完了する場合は、対象の Issue のタイムラインで、そのマージが Issue を閉じたことを確かめます。条件は、PR 本文の closing keyword（`Closes #N` など、同じリポジトリの `#N` の形）がその Issue を指し、GitHub がその PR と Issue を結び付けていて（`cross-referenced`）、マージした人がマージ直後にその Issue を閉じ、その後に再オープンされていないことです。確かめられない場合（サイドバーでの手動リンク、既定以外のブランチ宛ての PR、マージから時間がたってから手で閉じたなど）は拒否されるので、GitHub GraphQL が使える環境で完了してください。なお、REST の `closed` イベントは誰が閉じたか（PR かどうか）を返さないため、マージした人がマージ直後に手で閉じた場合は、自動クローズと区別できません。

### footprint 重複の警告

interactive の claim では、他の **interactive** な予約と `footprint` が重なるだけなら claim は成功します。その場合、相手の Issue 番号・ブランチ・worktree・重なったファイルが `Warning:` として標準エラー出力に表示されます（`--no-apply` のプレビューでも表示されます）。警告が出たら、相手のタスクとマージの順序を決め、後からマージする側がリベース時にコンフリクトを解消してください。

次の場合は、interactive の claim でも従来どおり `claim_conflict` で拒否されます。

- 重なる相手が dispatch（Dispatcher が起動した自律エージェント）の予約である場合
- どちらかがリポジトリ予約である場合（`repository_reservation`）
- 重なる相手が `forced_serial` の場合
- 同じ `shared_contract` への書き込み同士の場合（`shared_contract`）

dispatch の claim は、footprint が重なれば相手の種類に関わらず拒否されます。

### 予約の拡張（`--amend-footprint`）

保持中のファイル予約の外にあるファイルの変更が必要になった場合は、Issue 本文の `footprint` に追記してから次を実行します。

```bash
orchestune claim <N> --amend-footprint --no-apply  # プレビュー
orchestune claim <N> --amend-footprint
```

新しい footprint は、保持中の footprint・Issue の footprint・claim の基点以降に worktree で変更済みのすべてのファイル（コミット済み・未コミット・未追跡）の和集合です。縮小はしません。他のすべての active 予約との衝突を再判定し、衝突した場合は何も変更せずに相手の Issue を表示します。interactive な予約との footprint 重複は、新規 claim と同じく警告として表示し、拡張は行います。成功すると不足分を Issue の footprint に追記して台帳を更新します。worktree・ブランチ・claim ID・ラベルは変わりません。対象は、claim を作成したワークスペース（対応する claim marker がある場所）で完了済みの interactive なファイル予約のみです。リポジトリ予約への切り替えはサポートしません。

### レビュートリガーの再開と置換

同じラウンド・reviewer・HEAD の待機は、`scripts/wait_for_review.py --pr <PR> --bot-name <bot> --round <n> --no-post` で再開します。`--no-post` は常に読み取り専用です。投稿モードでも既存トリガーを再利用し、mention が欠落していても再投稿しません。Round 2 以降の再開では投稿済み本文の判断表を検証します。新ラウンドや置換では `--body-file` で判断表を渡してください。

HEAD 変更後に現在のラウンドを置換するには、投稿モードでその `--round <n>` を指定します。reviewer の変更には人間の明示指示と `--switch-reviewer` も必要です。新コメントの `<!-- orchestune:review-replaces trigger=<旧id> -->` から、同じ投稿者による一意な置換チェーンを検証します。新コメントを再取得して確認した後、旧コメントの trigger marker だけを `superseded-trigger` に変更し、判断表・round・HEAD を保持します。置換しても5ラウンド上限はリセットされません。チェーン内の過去と同じ reviewer・HEAD に戻る再投稿は、実行を区別できないため拒否します。

POST の成否が不明なら再取得し、未確認のまま再送しないでください。投稿確認後の旧 marker 編集に失敗した場合は、確認済みトリガーの待機を再開するか、`--round <n> --repair-trigger-markers` で旧 marker の編集だけを再試行します。このオプションは `--no-post` と併用できず、HEAD・reviewer の置換もしません。投稿状態の永続記録は `.orchestune/ci/review-replacements/` に保存します。

MCP の `--validate-request` は、`request_operation: replace`、置換元の id・reviewer・HEAD・投稿者、置換先の HEAD・reviewer・round、および結合済みの `trigger_body` を返します。置換 snapshot は置換元・置換先の `user.login` を保持してください。置換元の投稿者が欠けていれば投稿前に Exit 30 を返します。外部クライアントが同じ投稿・再取得・旧 marker 編集手順を実行し、offline 評価自体は POST/PATCH しません。無関係な重複、分岐、参照先欠落、別投稿者からの置換は停止します。置換後の結果は時刻だけでは現実行の証跡にせず、要求したコミットとの明示的な一致を必要とします。旧 HEAD の結果は履歴に残します。

### レビュー待機の timeout と stall

オンライン待機は必ず有限時間で終わります。Exit 20 は `--timeout`（既定1800秒）以内に結果が得られなかったこと、Exit 21 は現在のラウンド（同じ round・要求 HEAD）の進行中 tracker が `--stall-grace`（既定600秒）のあいだ同じ署名のままだったことを示します。tracker に変化があれば stall の猶予は計り直しますが、全体の timeout は計り直しません。前のラウンドや別 HEAD の tracker は stall とみなさないため、応答のないトリガーは Exit 20 のままです。どちらの期限も単調時計で測るため、壁時計の補正で短くも長くもなりません。sleep は残り時間を超えません。期限を超えうるのは実行中の1回の poll（`gh` 呼び出しはそれぞれ30秒まで）と、最後の HEAD 再取得（30秒まで）だけです。Exit 30（Completed だがレビュー本文なし）と Exit 11（単一 snapshot の評価）の意味は変わりません。

`--output-file` を指定すると、Exit 20/21 でもこの実行の診断でファイルを置き換えます。内容は `acquisition_status: unavailable`、`reason_code`（`review_timeout` / `review_stalled`）、`exit_code`、repository・PR・reviewer・round・trigger の ID と時刻・要求 HEAD、再取得した `current_head_sha`（取得失敗時は null。要求 HEAD で代用しません）、および `wait_diagnostics`（経過時間、timeout と猶予の設定、現在ラウンドで最後に見た tracker の ID・status・更新時刻・本文 SHA-256、トリガー以降に観測した bot 項目の出所情報）です。`reviewed_head_sha` と `review_target_sha` は null なので、`orchestune complete` のレビュー証跡にはなりません。各 section を `complete` とするのは最後の poll で取得できた場合だけです。ファイルを書き込めない場合は Exit 2 で終了し、以前の成功結果を残さないよう旧ファイルを削除します。

復旧も同じ単一の待機経路で行います。

1. 終了コード、診断 JSON、対象のトリガーと HEAD を確認します。
2. Exit 20: 同じ reviewer で `--no-post --round <n>` を1回だけ実行して待機を再開します。Round 2 以降はそのラウンドの `--body-file` も渡します。再開では投稿も新ラウンドの採番もしません。
3. Exit 21: tracker の最後の進行と、見える場合は関連する workflow run / job を確認します。Codex の内部 job は見えないため「不明」と記録し、GitHub Actions の実行がある前提にしません。その後は手順2と同じく再開します。
4. Exit 30: `--no-post` で再取得します。tracker の表はレビュー結果ではありません。
5. reviewer の切り替えは、人間が明示的に選んだ場合だけ `--switch-reviewer` で行います（検証付きの置換、または次のラウンド）。自動復旧の既定にはしません。Claude が起動しない場合は、再投稿せずに workflow の actor・`allowed_bots`・trigger marker・job の conclusion を確認します。
6. 有限回の再開でも完了しなければ、`orchestune complete --issue <N> --result blocked --reason review-timeout`（または `review-stalled`）で停止します。timeout をレビューの pass や skip に読み替えません。

## 8. ローカルCI証跡の保存と完了処理 (`orchestune complete`)

`orchestune complete` の成功は、固定したOutcome Recordの投稿、結果に対応するIssueラベル（`status:done` / `status:blocked` / `status:not-needed`）の確認、および共有台帳へのhandoffとreplay receiptの永続保存が成立したことを意味します。PRマージ、対応不要の独立レビュー承認、worktree回収の完了までは意味しません。`done` は公開前にローカルCI、PR/head、子レビュー証跡（[§4.5](#45-子レビュー証跡ゲートと第2層セマンティックレビューゲート)）、トークン上限の証跡を検証し、GCはその証跡を消費します。予約なしの旧完了経路ではGC側のトークン上限判定を維持します。

claim済みworktreeから `orchestune complete --issue <N> --pr <PR> --result done` を実行します。`blocked` は `--reason` が必要です。`not-needed` は未claimのIssueにもworktreeを作らず予約できます。コマンドは外部操作前にcompletion IDを表示します。途中から再開するには、対応する claim marker を保持したまま、同一引数に `--completion-id <ID>` を付けて再実行してください。引数・所有者・generationが変わると固定済み要求を上書きできません。handoff後の再実行は、後続ポリシーがIssueをqueuedにした後やactive回収後でも保存結果を返し、古いラベルへ戻しません。

### CI証跡の保存場所とGit管理
- **既定保存先**: 各worktree内の `.orchestune/ci/ci_evidence.json`
- **Git ignore**: `.orchestune/ci/` は `.gitignore` に登録されており、証跡ファイルや一時ファイル（`.tmp.*`）の生成によってGitのclean判定が汚されることはありません。
- **環境変数による上書き**: `ORCHESTUNE_CI_EVIDENCE_PATH` を指定することで、任意のファイルパスへ保存先を変更できます。
- **権限分離とサンドボックス対応**: Git metadata directory（`.git` や `.git/worktrees/<name>`）が読み取り専用のサンドボックス環境やlinked worktreeであっても、worktree内が書き込み可能であれば証跡の無効化・保存・検証が正常に動作します。
- **移行時の注意**: 旧バージョンで `.git` 配下に保存されていた古い証跡は新しい既定経路では再利用されません。移行後はローカルCI（`./scripts/local-ci.sh` または `.\scripts\local-ci.ps1`）を再実行して新たな証跡を生成してください。

## 9. handoff-readyタスクのGC（`orchestune gc`）

`orchestune complete` がOutcome Recordを記録し、対象PRがマージされた後、primary checkoutからGC専用コマンドを実行して予約を解放できます。親Issueなしの対話タスクにも使用できます。

```bash
orchestune gc --no-apply  # 判定を確認する（状態・worktree・lockは変更しない）
orchestune gc             # 確認後に適用する
```

Dispatcherと `orchestune gc` の両方が、Issueの `status:in-progress` 一覧に依存せず、確定journalとpendingの後続ポリシーを探索します。新形式はラベル確認済みhandoffとreplay receiptの整合が必要です。公開途中、証跡不一致・取得不能、未検証の旧handoffは保留します。予約なしの従来PR/cloud/Outcome検出経路は維持します。

独立GCはinteractive所有、Dispatcherはdispatch所有のworktreeを共通の保護付き回収処理で扱います。`done` は同一Outcomeコメント、マージ済みPRのhead/base、merge commitの到達可能性、worktreeのheadと所有者を確認します。`blocked` / `not-needed` のcleanなworktreeは削除し、dirtyなworktreeは保持します。これらにdone履歴やGC CompletionReceiptは作りません。worktreeなし予約は後続ポリシーだけの対象であり、worktree削除へ渡しません。

active回収後もreplay receiptと後続処理の対象・設定情報を保持します。各ポリシーは `(repository, generation, completion_id, policy_kind)` の固定operation IDを持ち、判断・再試行回数を副作用前に保存します。ラベルとcloseはlive照合、コメントはmarkerで復旧します。review-timeoutのpending/count/backoff、base-branch-redのattempt/marker/エスカレーションを維持します。ローカルの対応不要はcloseし、cloud・未claimの対象は独立レビュー承認までclose・依存完了を保留します。

`--no-apply` はlockも作らない読み取り専用プレビューです。applyモードではIssueのラベル・コメント・closeを更新し、独立レビューを起動する場合があります。Dispatcher不在でも `orchestune gc` を再実行してpending処理を進められます。cloudレビューには `ORCHESTUNE_ROUTINE_ID` / `ORCHESTUNE_ROUTINE_TOKEN` が必要です。provider不在・起動結果不明ならレビューを保留します。保存済みlaunch/attempt IDをproviderのlookupで照合できる場合は復旧し、不明な起動を二重実行しません。設定されたレビューtimeoutまで起動結果を確認できない場合は人間の確認へ移行し、policyと依存は保留を維持します。レビュー結果コメントには当該operationのmarkerが必要で、一般的な結果ラベルだけではgenerationの承認にしません。

同じ解決済みstate pathを使うwriterは、上限付き外部操作と保存を含めて共通の再入可能な台帳lockを保持し、物理回収はworktree別claim lockも保持します。異なるstate path間にはこの排他は成立しません。実行中/current worktree、所有者不一致の保護は維持します。`current_worktree` の場合はprimary checkoutから再実行してください。旧 `handed_off_to_gc` は自動昇格せず証跡移行まで保留します。再開可能な公開処理は元のcompletion IDと対応するclaim markerで `complete` から再開してください。

| オプション | デフォルト | 説明 |
| :--- | :--- | :--- |
| `--no-apply` | 無効 | 変更せずに判定を表示する。 |
| `--state <path>` | primary checkoutの `run_state.json` | 台帳パスを指定する。相対パスはprimary checkout基準。 |
| `--timeout <seconds>` | `0` | 台帳とclaim lockの取得待ち秒数。負数・非有限値は引数エラー。 |

## ローカルclaimの復旧（`orchestune recover`）

ローカルのclaim再開・footprint変更・completeはowner tokenファイルを読み書きしません。
呼び出し元のclaim markerにある世代、Git common dir、登録worktree、checkout中のbranchを検証します。
旧tokenファイルは残っていても支障ありません。旧state/journalの`owner_token_digest`は
互換用メタデータとして読めるため、一括移行やstate全削除は不要です。Routine API認証は従来どおりです。

markerはworktreeの世代を識別し、OS processの認証は行いません。同じworktreeを再利用する前に
古いagentを停止してください。置換後のmarkerを読み直すprocessは、新agentと区別できません。
サービス呼び出し側は起動時に取得した期待claim IDを保持してください。古いIDによる操作は再割当て後も拒否します。

primary checkoutから診断してください。既定は変更を行わないpreviewです。

```bash
orchestune recover --issue <N>
orchestune recover --issue <N> --claim-id <ID> --reason "worker停止を確認" --apply
# marker欠損時は修復し、未完了のcompleteを再開する:
orchestune recover --issue <N> --claim-id <ID> --reason "marker消失" --restore-marker --apply
```

診断で確認したclaim IDを指定します。適用時も共有state lockとworktree lock内で再検証します。
稼働中worker、不確かな起動・外部起動、世代変更、別repository、未完了のcompletion公開は保留します。
起動状況が不確かな場合はDispatcherで照合してください。公開途中ならmarkerを修復し、
元のcompletion IDでcompleteを再開します。停止確認後のinteractive / dispatch双方を扱えます。
`--state <path>`はDispatcherと同じ台帳を選択するために使用します。

解放は対象active予約だけを除去し、世代と理由を復旧receiptへ保存します。
dirtyな変更・commit・worktree・branch、他claim、回数、intent、completion証拠を保持します。
GitHubのラベルやIssue状態は変更しません。再queue・完了・closeは既存のengine / Outcome経路で行います。
Dispatcherは明示解放された旧世代を復元しません。同じ解放の再実行は冪等です。
`run_state.json`全体を削除する必要はありません。

merge済みPRも通常のcompleteコマンドで事後完了できます。Issue、claim作成時刻、head、予定base、
repository、merge commitの到達性、Issue再open時刻を照合します。親branchへのmergeも対象です。
CIとOutcome公開の要件は維持し、必要証拠が不足する場合は保留します。


### 操作者が停止を確認した外部実行の復旧

provider 側で当該実行を停止し、実行 ID・終端状態・成果物を確認して、再開しない状態にします。
primary checkout から台帳に記録された ID でプレビューしてください。

```bash
orchestune recover --issue <N> --claim-id <CLAIM> --external-id <EXECUTION> \
  --launch-attempt-id <ATTEMPT> --confirm-external-stopped \
  --reason "確認した実行URLと停止結果"
# 表示を確認後、同じ引数に --apply を追加して適用します。
```

claim/external ID と空白だけでない理由はプレビューでも必須です。active に attempt ID があれば
完全一致する値を指定し、なければオプション自体を省略します。このコマンドは停止・取消要求や PID kill を
行いません。`--restore-marker` と併用できず、worktree・branch・marker・GitHub は保持します。
`--state` は既存の共有ワークスペースのパス解決に従います。

`runtime_state`、`stop_evidence_source`、`provider_observation_reason` で根拠を確認してください。
fresh な `running` は必ず拒否し、`stopped` は provider、`unknown` は操作者の申告を根拠にします。
認証不足・状態取得非対応・provider 対応不明は unknown ですが、不正設定は `provider_config_invalid` で拒否します。
apply はプレビューを再利用せず、lock 下で世代・PID・marker・completion・runtime を再検証します。

completion がなければ停止確認と release receipt の保存・active 除去を原子的に行い、
`external_stop_confirmed_released` を返します。pending／handed_off／当該世代の journal があれば
停止確認だけを保存し、`external_stop_confirmed_active_retained` として全記録を保持します。
未完了なら既存 completion を再開し、handed_off 済みなら GC へ引き継ぎます。
journal だけの場合は `completion_resume_required` を表示します。`completion_state_invalid` は
既存の出版不整合調査へ進み、台帳直接編集・ラベル変更・force 解放で代替しないでください。
枠解放後は通常の再キュー、または完成成果物の complete／PR 統合手順へ進みます。

プレビューは `would_external_stop_confirm_release`／`would_external_stop_confirm_active_retained` です。
再送は初回の理由・日時・snapshot を保持し、`already_external_stop_confirmed_released`、
`already_external_stop_confirmed_active_retained`、または後続 GC 等で active が消えた場合の
`already_external_stop_confirmed_active_absent` を返します。最後の結果は recover による解放の証明ではありません。
新世代や曖昧・破損した証拠は拒否します。成功は終了コード 0、拒否は 43、不正な外部引数の組み合わせは引数エラーです。

GC は unknown の場合だけ、同じ repository と固定実行 identity に限定された操作者 receipt を使えます。
completion の進行では失効せず、所有権・claim 時刻・branch・attempt・起動時刻が変われば使用できません。
TTL は設けず、fresh な running を覆しません。Outcome・マージ・所有権・completion・WIP 保全の条件も維持します。

## 起動制御の既定値と移行（#1154）

起動制御の主軸は並行数 `max-concurrent`（既定`2`。対話型claimを含む`active_worktrees`の件数）です。
`max-launches-per-window` は既定で未設定となり、「1時間に1起動」には制限されません。未設定でも
起動履歴は記録され続けるため、後から`0`や正数を設定すると直近の起動が即座に判定へ反映されます
（短いwindowで刈り込まれた履歴は、windowを戻しても復元されません）。

従来の挙動（1時間に1起動・トークン集計1時間・タイムアウト無効）を維持するには、次の3つを明示します。

```toml
max-launches-per-window = 1
window-seconds = 3600
task-timeout-seconds = 0
```

`max-launches-per-window`を明示して`window-seconds`を省略すると「2時間あたり」に変わります。
`window-seconds`は`max-tokens-per-window`の集計、aging正規化、起動履歴の保持にも使われます。
上限`0`は新規起動だけを止める設定で、GC・タイムアウト・ラベル同期は止まりません。完全に止める場合は
dispatch自体を止めてください。

### 外部実行は停止を確認するまで枠を保持

外部（クラウド）実行の枠を解放するのは、provider が当該実行の再開不能な終端状態を返した場合、または
unknown に対して同じ実行世代の有効な操作者停止確認 receipt がある場合です。
PR/Outcomeの完了判定（MERGED/closed PR、handoff-ready）は成果物の状態であり、クラウド側の実行が
まだコードを実行し得るかの証拠ではありません。実行中・状態不明・未対応・状態取得失敗の場合、GCは
`active_worktrees`と実行ハンドルを保持し、Issueを`status:blocked-human-review`へ送って自動再投入しません。
これはタイムアウト、`status:in-progress`除去に伴う古い台帳エントリの後始末、完了回収のすべてに適用されます。
成果物の完了（マージ済みPRやOutcome）を検出したが停止を確認できない場合は、完了結果のラベルを変えず、
枠を保持している理由をIssueへコメントします（理由が変わったときだけ再投稿します）。
現在、停止状態を返せるのはCodex Cloudのみです（`codex cloud list`の`ready`・`applied`・`error`を停止、
`pending`を実行中と扱います）。それ以外の外部ターゲット（Cloud Routine等）は、操作者の停止確認がなければ終了後も保持されます。
クラウド側の実行状態と成果物を確認し、必要なら停止して、上記の停止確認付き `recover` を使ってください。
