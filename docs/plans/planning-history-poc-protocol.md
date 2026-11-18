# 類似計画カード PoC 固定資料・検索契約・比較評価手順（Issue #1872）

親 EPIC: [#1842](https://github.com/Saltmu/orchestune/issues/1842) / 本契約 Issue: [#1872](https://github.com/Saltmu/orchestune/issues/1872) / 後続セレクタ実装: [#1873](https://github.com/Saltmu/orchestune/issues/1873) / 後続比較評価: [#1874](https://github.com/Saltmu/orchestune/issues/1874)

本ドキュメントは、#1842 の最小 PoC（過去の分解計画と記録された Outcome を根拠付きカードとして提示し、分解計画作成の品質とトークン消費を改善できるか検証する）における **固定資料 JSON・検索リクエスト JSON・出力カード JSON の型と不変条件、出典採取規則、BM25 検索と予算削減の決定論的仕様、合成 fixture の期待挙動、および 2 条件比較評価の手順と継続判定基準** を定義する。

#1873（`scripts/planning_history_poc.py` および `tests/test_planning_history_poc.py`）と #1874（`docs/plans/planning-history-poc-eval.md`）は本契約を唯一のデータ・評価仕様として参照し、追加の仕様解釈を行わない。

---

## 1. 責務と非対象（Scope & Non-Goals）

| 成果物・既存モジュール | 役割と境界 |
| --- | --- |
| `docs/plans/planning-history-poc-protocol.md`（本書） | 入出力 JSON スキーマ、ハッシュ算出、時刻・系列フィルタ、同点処理、合成 fixture の採否期待値、#1874 の比較評価プロトコルと継続基準を定義する。 |
| [`tests/fixtures/planning_history_poc_cases.json`](../../tests/fixtures/planning_history_poc_cases.json) | #1873 の単体・CLI テストが直接読み込む合成候補集合（`planning-history-corpus-v1`）を版管理する。 |
| `scripts/planning_history_poc.py`（#1873） | 本書の契約に従い、`--corpus` と `--request` を検証・フィルタ・BM25 順位付け・バイト予算調整して `planning-history-cards-v1` を標準出力へ出す読み取り専用 CLI。既存の `orchestune` サブコマンド登録は変更しない。 |
| `orchestune/context/search.py::search_artifacts` | 既存の公開 BM25 検索関数（`TOKENIZER_VERSION="char-bigram-v1"`, `BM25_PARAMS_VERSION="bm25-v1"`）をそのまま再利用する。独自トークナイザや別スコア関数を作らない。 |
| `orchestune/context/budget.py::fit_context_budget` | 既存の公開 UTF-8 バイト予算計測関数をそのまま再利用する。文字数やトークン推定で代替しない。 |
| `orchestune/provisioning/plan_restoration.py::restore_plan_from_parent` | 実資料作成時に親 Issue と子 Issue の計画構造を取得時点復元する参照仕様として用いる。PoC 用の自動収集デーモンや共有 SQLite 索引（`context-index.sqlite3`）への書込みは行わない。 |
| `orchestune/outcome_record.py::parse_from_comments` | 実資料作成時に子 Issue コメント列から最新の正規 Outcome Record（`done` / `not-needed` / `blocked`）を抽出する参照仕様として用いる。 |

### 明示的な非対象（Out of Scope）
- タスクの実開始順・実完了順・試行回数・並列 overlap 区間の推定。
- 根拠のない「成果の最新計画版への結合」や、未確認の PR マージ・CI 成否・親計画全体の受入達成の推測。
- `result=done` や成功回数・レビュー rounds による BM25 スコアへの加点・減点（ランキングは課題テキスト・変更範囲・シンボルの類似度のみで決定する）。
- Outcome のないタスク（`result="unknown"`）や `blocked` タスクをカードから除外すること（失敗や未完了の履歴も分解の参照根拠としてそのままカードに残す）。

---

## 2. 共通プリミティブ・文字コード・ハッシュ規則

すべての JSON 入出力（corpus、request、cards）は以下の共通規則に従う。違反する入力は **スキーマ検証エラー（終了コード `2`、標準出力は空 `0` bytes、標準エラー出力に理由を出力）** として拒否する。

1. **文字コードと JSON 構文**:
   - ファイルおよび標準出力はすべて **UTF-8（BOM なし）** の厳密な JSON オブジェクトとする。
   - 不正な UTF-8 バイト列、JSON 構文エラー、トップレベルが JSON オブジェクト（`{...}`）でない入力、`NaN` / `Infinity` を拒否する。
   - **重複 JSON キーの禁止**: 同一 JSON オブジェクト内に同一キーが複数回出現した場合（`json.loads(..., object_pairs_hook=...)` で検査）は後勝ちで上書きせず、検証エラー（終了コード `2`）とする。
   - **未知キーの禁止**: 本書で定義されたキー以外のフィールドが存在する場合は検証エラー（終了コード `2`）とする。
   - **真偽値と整数の厳密区別**: Python の `isinstance(True, int)` による誤通過を防ぐため、すべての整数フィールドで `bool`（`true` / `false`）を拒否する。
2. **正規化済み `repository`**:
   - `"owner/repo"` 形式の非空文字列。正規表現 `^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$` に一致し、`owner` と `repo` のいずれも `"."` または `".."` でないこと。前後の空白、URL プレフィックス、`.git` サフィックスを含めてはならない。
3. **UTC 時刻（`available_at` / `as_of`）**:
   - 秒精度の UTC ISO 8601 固定形式 `YYYY-MM-DDTHH:MM:SSZ`（正規表現 `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`）のみを許容する。
   - ミリ秒（`.123Z`）、タイムゾーンオフセット（`+09:00` や `+00:00`）、実在しない暦日・時刻（例: `2026-02-30T00:00:00Z`）は拒否する。
   - `YYYY-MM-DDTHH:MM:SSZ` に統一されているため、時刻の大小比較は UTC 時刻としての順序と ASCII 辞書順が完全に一致する。
4. **SHA-256 ダイジェスト（`digest` / `plan_revision_digest` / `corpus_digest` / `request_digest`）**:
   - 64 文字の 16 進小文字文字列（正規表現 `^[0-9a-f]{64}$`）。大文字 hex やプレフィックス（`sha256:` 等）は許容しない。
   - ファイルダイジェスト（`corpus_digest`, `request_digest`）および出典ダイジェスト（`source.digest`）は、対象データの **UTF-8 生バイト列（raw bytes）** に対する SHA-256 とする。

---

## 3. データ契約（JSON Schemas & Invariants）

### 3.1 固定資料スキーマ（`planning-history-corpus-v1`）

#### トップレベルオブジェクト

| フィールド | 必須 | 型 | null | 制約・不変条件 |
| --- | :---: | --- | :---: | --- |
| `schema_version` | 必須 | `string` | 不可 | 固定値 `"planning-history-corpus-v1"`。 |
| `repository` | 必須 | `string` | 不可 | 正規化済み `"owner/repo"`（§2.2）。`cases` 内の全 case はこの `repository` に属する。 |
| `cases` | 必須 | `array[Case]` | 不可 | `Case` オブジェクトの配列（空配列 `[]` も構文上有効）。 |

#### `Case` オブジェクト（11 必須フィールド）

| フィールド | 必須 | 型 | null | 制約・不変条件 |
| --- | :---: | --- | :---: | --- |
| `case_id` | 必須 | `string` | 不可 | 前後空白のない非空文字列（`^[A-Za-z0-9._:-]+$`）。**`corpus.cases` 内で一意**。 |
| `parent_issue` | 必須 | `integer` | 不可 | 正整数（`>= 1`）。同じ親 Issue の異なる計画版は、同一 `parent_issue` を持つ別 `Case`（別 `case_id`）として表現する。 |
| `plan_revision_digest` | 必須 | `string` | 不可 | 64 文字小文字 hex SHA-256。後述の **計画版ダイジェスト算出規則** の計算結果と完全一致しなければならない。 |
| `title` | 必須 | `string` | 不可 | 非空文字列（空白のみ不可）。親 Issue のタイトル。 |
| `purpose` | 必須 | `string` | 不可 | 非空文字列（空白のみ不可）。親計画の目的・概要。 |
| `acceptance_criteria` | 必須 | `array[string]` | 不可 | 1 件以上の非空文字列配列（各要素とも空白のみ不可）。 |
| `tasks` | 必須 | `array[Task]` | 不可 | 1 件以上の `Task` オブジェクト配列。 |
| `lineage_issue_numbers` | 必須 | `array[integer]` | 不可 | 重複のない正整数（`>= 1`）配列。**必須包含条件**: `case.parent_issue` および `case.tasks` 内のすべての非 null `task.issue_number` を含まなければならない（replan で置換された同系列の旧子 Issue 番号もここへ含める）。 |
| `pr_numbers` | 必須 | `array[integer]` | 不可 | 重複のない正整数（`>= 1`）配列（関連 PR がない場合は空配列 `[]`）。 |
| `sources` | 必須 | `array[Source]` | 不可 | 1 件以上の `Source` オブジェクト配列。少なくとも `kind == "plan"` を 1 件以上、かつ `kind == "task"` を 1 件以上含まなければならない。 |
| `available_at` | 必須 | `string` | 不可 | UTC 時刻 `YYYY-MM-DDTHH:MM:SSZ`。**必須不変条件**: `case.sources` 内の全 `source.available_at` の最大値（`max(s.available_at for s in case.sources)`）と完全一致しなければならない。 |

#### `Task` オブジェクト（9 必須フィールド）

| フィールド | 必須 | 型 | null | 制約・不変条件 |
| --- | :---: | --- | :---: | --- |
| `subtask_id` | 必須 | `string` | 不可 | 前後空白のない非空文字列。**同一 `case.tasks` 内で一意**。 |
| `issue_number` | 必須 | `integer \| null` | 可 | 起票済みは正整数（`>= 1`）、未起票タスクは `null`。非 null の場合、同一 `case.tasks` 内の非 null `issue_number` 間で一意であり、`case.parent_issue` と異なり、かつ `case.lineage_issue_numbers` に含まれなければならない。 |
| `description` | 必須 | `string` | 不可 | 非空文字列（空白のみ不可）。 |
| `depends_on` | 必須 | `array[string]` | 不可 | 重複のない非空文字列配列（依存なしは `[]`）。**参照整合性と DAG 不変条件**: 各要素は同一 `case.tasks` 内に実在する他の `subtask_id` を指し（自己依存・未定義 ID は不可）、`case.tasks` 全体の依存有向グラフに閉路（cycle）が存在してはならない。 |
| `footprint` | 必須 | `array[string]` | 不可 | 重複のない非空文字列配列（空配列 `[]` 可）。リポジトリ相対パス。 |
| `symbols` | 必須 | `array[string]` | 不可 | 重複のない非空文字列配列（空配列 `[]` 可）。対象シンボル名。 |
| `result` | 必須 | `string` | 不可 | `"done"` / `"not-needed"` / `"blocked"` / `"unknown"` のいずれか。記録された Outcome の要約であり、PR マージや CI 成否、親計画全体の受入達成を意味しない。 |
| `missing_reason` | 必須 | `string \| null` | 条件付 | **null 規則**: `result == "unknown"` のときは非空文字列（空白のみ不可）が **必須**。`result` が `"done"` / `"not-needed"` / `"blocked"` のときは **`null` でなければならない**（非 null 文字列は検証エラー）。 |
| `source_ids` | 必須 | `array[string]` | 不可 | 1 件以上の重複のない非空文字列配列。**参照整合性**: すべての要素が同一 `case.sources[*].source_id` に存在しなければならない。また `result` が `"done"` / `"not-needed"` / `"blocked"` の場合、参照先に少なくとも 1 件の `kind == "outcome"` source を含まなければならない。 |

#### `Source` オブジェクト（5 必須フィールド）

| フィールド | 必須 | 型 | null | 制約・不変条件 |
| --- | :---: | --- | :---: | --- |
| `source_id` | 必須 | `string` | 不可 | 前後空白のない非空文字列。**同一 `case.sources` 内で一意**（corpus 全体でも case 接頭辞により一意とすることを推奨）。 |
| `kind` | 必須 | `string` | 不可 | `"plan"` / `"task"` / `"outcome"` / `"pr"` のいずれか。 |
| `url` | 必須 | `string` | 不可 | `"https://github.com/"` で始まる非空 URL 文字列。 |
| `digest` | 必須 | `string` | 不可 | 採取した元データの UTF-8 bytes の 64 文字小文字 hex SHA-256。 |
| `available_at` | 必須 | `string` | 不可 | UTC 時刻 `YYYY-MM-DDTHH:MM:SSZ`。採用した版が存在したと証明できる時点。 |

#### 計画版ダイジェスト（`plan_revision_digest`）の算出規則
`case.plan_revision_digest` は、結果（`outcome`）や PR（`pr`）の更新時刻・有無に左右されない計画構造と子タスク定義の版識別子として、以下の手順で算出・検証する。

1. `case.sources` から `source.kind in ("plan", "task")` の要素のみを抽出する（`outcome` と `pr` は含めない）。
2. 抽出した source を `source_id` の昇順（UTF-8 コードポイント順）に並べる。
3. 各要素を 2 要素配列 `[source_id, digest]` とした配列 `[[source_id_1, digest_1], [source_id_2, digest_2], ...]` を作る。
4. この配列を UTF-8 のコンパクト JSON（`json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))`、ASCII エスケープなし、空白なし、**末尾改行なし**）に直列化する。
5. 直列化した UTF-8 バイト列の SHA-256（64 文字小文字 hex）を `plan_revision_digest` とする。

---

### 3.2 検索リクエストスキーマ（`planning-history-query-v1`）

| フィールド | 必須 | 型 | null | 制約・不変条件 |
| --- | :---: | --- | :---: | --- |
| `schema_version` | 必須 | `string` | 不可 | 固定値 `"planning-history-query-v1"`。 |
| `repository` | 必須 | `string` | 不可 | 正規化済み `"owner/repo"`（§2.2）。 |
| `as_of` | 必須 | `string` | 不可 | 評価基準（cutoff）の UTC 時刻 `YYYY-MM-DDTHH:MM:SSZ`。 |
| `query` | 必須 | `string` | 不可 | 現在の課題文（タイトル・目的・受入条件等、初期分解案を作る前の既知情報のみ）から構成した非空文字列（`query.strip() != ""` かつ `orchestune.context.search` のトークナイザで 1 トークン以上生成されること。空白や記号のみでトークンが 0 個になる文字列は終了コード `2` で拒否する）。 |
| `exclude_issue_numbers` | 必須 | `array[integer]` | 不可 | 重複のない正整数（`>= 1`）配列（空配列 `[]` 可）。対象課題自身、同一親計画の子、replan で置換された同系列 Issue の集合を資料作成側が確定して渡す。 |
| `exclude_pr_numbers` | 必須 | `array[integer]` | 不可 | 重複のない正整数（`>= 1`）配列（空配列 `[]` 可）。対象系列に関連する PR 番号の集合。 |

---

### 3.3 セレクタ CLI と出力カードスキーマ（`planning-history-cards-v1`）

#1873 で実装する CLI の呼び出しインターフェースと出力形式を以下のとおり固定する。

#### CLI 入口と引数・終了コード

```bash
uv run python -m scripts.planning_history_poc \
  --corpus <corpus.json> \
  --request <request.json> \
  [--limit <int>] \
  [--max-bytes <int>]
```

- `--corpus`: 必須。`planning-history-corpus-v1` JSON ファイルへのパス。
- `--request`: 必須。`planning-history-query-v1` JSON ファイルへのパス。
- `--limit`: 任意。`0` 以上の整数、既定値 `3`。負数や非整数は終了コード `2`。
- `--max-bytes`: 任意。正の整数（`>= 1`）、既定値 `12288`。`0` 以下や非整数は終了コード `2`。

| 終了コード | 条件 | 標準出力 (`stdout`) | 標準エラー (`stderr`) |
| :---: | --- | --- | --- |
| `0` | 正常完了（`status` が `"ready"` / `"no_matches"` / `"disabled"` / `"over_budget"` のいずれかで、包絡 JSON 全体が `--max-bytes` 以内） | `planning-history-cards-v1` のコンパクト UTF-8 JSON ＋ 末尾 LF 1 バイト（`\n`）をバイナリ出力 | 空（`0` bytes） |
| `2` | CLI 引数不正、UTF-8/JSON 不正、重複キー、未知キー、スキーマ・不変条件違反、または `cards: []` の最小包絡 JSON（末尾 LF 込み）ですら `--max-bytes` を超過した場合 | 空（`0` bytes） | 非空の診断メッセージ |
| `1` | `--corpus` または `--request` のファイル不在・ディレクトリ指定・権限エラー等のファイル I/O エラー | 空（`0` bytes） | 非空の診断メッセージ |

#### 出力トップレベルオブジェクト（キー順序固定）

出力 JSON は以下のフィールド順で構築し、`json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"` の **同一バイト列** を `fit_context_budget(rendered_bytes, max_bytes=max_bytes)` で計測して `sys.stdout.buffer.write(rendered_bytes)` で出力する（Windows 環境でも CRLF 変換が入らないようバイナリ標準出力を使う）。

| 順序 | フィールド | 型 | 説明・不変条件 |
| :---: | --- | --- | --- |
| 1 | `schema_version` | `string` | 固定値 `"planning-history-cards-v1"`。 |
| 2 | `status` | `string` | `"ready"` / `"no_matches"` / `"disabled"` / `"over_budget"` のいずれか。 |
| 3 | `corpus_digest` | `string` | `--corpus` ファイルの実バイト列の 64 文字小文字 hex SHA-256。 |
| 4 | `request_digest` | `string` | `--request` ファイルの実バイト列の 64 文字小文字 hex SHA-256。 |
| 5 | `returned_count` | `integer` | 出力された `cards` の要素数（`len(cards)`）。 |
| 6 | `excluded_counts` | `object` | BM25 実行前に除外された case 数の内訳（後述の 4 キー固定順序）。 |
| 7 | `budget_dropped` | `integer` | BM25 上位 `limit` 件のうち、`--max-bytes` 制限により末尾から落とされたカード枚数（`>= 0`）。 |
| 8 | `cards` | `array[Card]` | 採用されたカードの配列（BM25 順位順）。 |

`excluded_counts` オブジェクトのキー（固定順序）:
1. `repository_mismatch`: `integer`（`>= 0`）
2. `lineage_excluded`: `integer`（`>= 0`）
3. `after_as_of`: `integer`（`>= 0`）
4. `superseded_revision`: `integer`（`>= 0`）

#### `Card` オブジェクト（キー順序固定）

入力 `corpus` 内での `cases`、`tasks`、`sources`、`depends_on`、`footprint`、`symbols`、`source_ids` の並び順が入れ替わっても、入力ファイルの生バイト列 SHA-256 を記録する `corpus_digest`（および `request_digest`）を除き、**完全に同一の `cards` 配列（カード順位と内容）および `status`・`returned_count`・`excluded_counts`・`budget_dropped`** が再現されるよう（同一入力バイト列に対しては標準出力の全バイト列も完全一致するよう）、以下の正規化順で出力する。

| 順序 | フィールド | 型 | 正規化・出力規則 |
| :---: | --- | --- | --- |
| 1 | `case_id` | `string` | `case.case_id` |
| 2 | `parent_issue` | `integer` | `case.parent_issue` |
| 3 | `plan_revision_digest` | `string` | `case.plan_revision_digest` |
| 4 | `available_at` | `string` | `case.available_at` |
| 5 | `title` | `string` | `case.title` |
| 6 | `purpose` | `string` | `case.purpose` |
| 7 | `acceptance_criteria` | `array[string]` | `case.acceptance_criteria`（定義順を保持） |
| 8 | `tasks` | `array[CardTask]` | `case.tasks` を **`subtask_id` の昇順** にソートした配列。各 `CardTask` のキー順は `subtask_id`, `issue_number`, `description`, `depends_on`（昇順）, `footprint`（昇順）, `symbols`（昇順）, `result`, `missing_reason`, `source_ids`（昇順）。 |
| 9 | `declared_edges` | `array[DeclaredEdge]` | 各 task の `depends_on` から生成した宣言依存辺 `{"from_subtask": dep_id, "to_subtask": task.subtask_id}` を **`(from_subtask, to_subtask)` の昇順** にソートした配列（依存辺がない場合は `[]`）。 |
| 10 | `sources` | `array[CardSource]` | `case.sources` を **`source_id` の昇順** にソートした配列。各 `CardSource` のキー順は `source_id`, `kind`, `url`, `digest`, `available_at`。 |

---

## 4. フィルタ・親単位重複排除・BM25 順位付け・予算削減アルゴリズム

`select_cards` は以下の 4 段階を **この順番で決定論的に** 実行する。

### Step 1: BM25 前の適格性フィルタ（1 件 1 理由の優先順）
`corpus.cases` の各 `case` について、以下の優先順 **1 → 2 → 3** で検査し、最初に該当した理由 1 つだけを `excluded_counts` に加算して候補から除外する（BM25 の文書集合・IDF・平均文書長計算には一切含めない）。

1. **`repository_mismatch`（最優先）**:
   - 条件: `corpus.repository != request.repository`
   - 該当時はその `case` を `repository_mismatch` として除外する（`corpus.repository` が不一致の場合、`corpus.cases` の全件が `repository_mismatch` に計上され、以降の除外理由はすべて `0` となる）。
2. **`lineage_excluded`（第 2 優先）**:
   - `case_issues = set(case.lineage_issue_numbers) | {case.parent_issue} | {t.issue_number for t in case.tasks if t.issue_number is not None}`
   - `case_prs = set(case.pr_numbers)`
   - 条件: `bool(case_issues & set(request.exclude_issue_numbers))` または `bool(case_prs & set(request.exclude_pr_numbers))`
   - 該当時は `lineage_excluded` に `+1` して除外する。
3. **`after_as_of`（第 3 優先）**:
   - 条件: `case.available_at >= request.as_of`（適格条件は厳密不等号 `case.available_at < request.as_of` であり、**`available_at == as_of` の同時刻候補および `available_at > as_of` の未来候補はいずれも除外** する）。
   - 該当時は `after_as_of` に `+1` して除外する。

### Step 2: 同一親（`parent_issue`）の版重複排除（`superseded_revision`）
Step 1 を通過した適格 case 群を `case.parent_issue` ごとにグループ化する。
- 各 `parent_issue` グループ内で、以下の比較キーにより **1 件だけ** を代表版として残す:
  1. `case.available_at` が **最大（最新）** のものを優先する。
  2. `case.available_at` が同一時刻の場合は、`case.case_id` の **昇順（UTF-8 コードポイント辞書順で最小）** のものを優先する。
- 同一 `parent_issue` グループ内で代表版以外に落ちた case 数を `excluded_counts.superseded_revision` に加算する。

### Step 3: `search_artifacts` アダプタと BM25 順位付け・同点処理
- `--limit 0` の場合:
  - Step 1〜2 の `excluded_counts` を確定した上で BM25 検索を行わず、`cards = []`, `returned_count = 0`, `budget_dropped = 0`, `status = "disabled"` とする（Step 4 の包絡 JSON 予算検査は実施する）。
- `--limit > 0` の場合:
  - Step 2 で残った親ごとの代表 case（`parent_issue` 昇順で整列）から、1 計画 = 1 文書のアダプタオブジェクトを構成して `orchestune.context.search.search_artifacts(documents, request.query, limit=limit)` に渡す。

#### `search_artifacts` 用アダプタのフィールド対応表

| `search_artifacts` が参照する属性 | 設定値（正規化規則） |
| --- | --- |
| `title` | `case.title`（文字列） |
| `description` | `[case.purpose, *case.acceptance_criteria, *(t.description for t in sorted(case.tasks, key=lambda t: t.subtask_id))]` を `"\n"` で連結した文字列。`tasks` を `subtask_id` 昇順に並べることで入力順依存を排除する。 |
| `summary` | `None`（Outcome 結果や欠測理由を検索語に含めない） |
| `task_kind` | `""`（空文字列） |
| `footprint` | `case.tasks` 内の全 `t.footprint` の重複を除き、昇順ソートした `tuple[str, ...]` |
| `symbols` | `case.tasks` 内の全 `t.symbols` の重複を除き、昇順ソートした `tuple[str, ...]` |
| `issue_number`（tie-break 第 1 キー） | `case.parent_issue`（整数） |
| `source_comment_id`（tie-break 第 2 キー） | `case.case_id`（文字列） |
| `completion_id`（tie-break 第 3 キー） | `None` |

- **非索引化不変条件**: `task.result`（`done` / `not-needed` / `blocked` / `unknown`）、`task.missing_reason`、完了数・失敗数、source URL/digest は検索アダプタのどのテキストフィールドにも含めない。
- **同点処理（Tie-Breaking）**: `search_artifacts` の既定規則（`_tie_key`）により、正の BM25 スコアが同点の場合は `(-score, issue_number=parent_issue 昇順, source_comment_id=case_id 昇順, completion_id="")` で決定論的に順位が定まる。BM25 スコアが `0` 以下の候補は `search_artifacts` により自動的に除外される。
- `search_artifacts` の返却件数が `0` 件のときは、`cards = []`, `returned_count = 0`, `budget_dropped = 0`, `status = "no_matches"` とする。

### Step 4: `fit_context_budget` による UTF-8 バイト予算調整と `status` 決定
1. Step 3 で選ばれた `m` 件（`1 <= m <= limit`）の候補を順位順に `Card` オブジェクトへ変換する。
2. 現在の `cards`、`returned_count = len(cards)`、`budget_dropped`、`status`（`len(cards) > 0` なら `"ready"`、`0` なら `"over_budget"`）で包絡 JSON を直列化し、末尾 LF（`\n`）を付与した UTF-8 バイト列 `rendered_bytes` を作る。
3. `fit_context_budget(rendered_bytes, max_bytes=max_bytes).fits` を評価する:
   - `fits == True` であれば確定し、その `rendered_bytes` をバイナリ標準出力へ書き出して終了コード `0` で終了する。
   - `fits == False` かつ `len(cards) > 0` であれば、最下位（末尾）のカードを 1 枚丸ごと削除（`cards.pop()`）し、`budget_dropped += 1` として 2 へ戻る（カード内部のフィールドや出典の途中切り詰めは一切行わない）。
   - `cards` がすべて削除（`len(cards) == 0`, `budget_dropped == m`）された状態では `status = "over_budget"`, `returned_count = 0` として包絡 JSON を再計測する。この `over_budget` 包絡 JSON が `max_bytes` に収まれば標準出力へ出して終了コード `0`、**0 枚の包絡 JSON ですら `max_bytes` を超える場合は標準出力を空にして標準エラー出力へ理由を書き、終了コード `2`** とする（`no_matches` や `disabled` で 0 枚包絡 JSON が `max_bytes` を超えた場合も同じく終了コード `2`）。

---

## 5. 合成 Fixture（`tests/fixtures/planning_history_poc_cases.json`）の構成と期待採否

[`tests/fixtures/planning_history_poc_cases.json`](../../tests/fixtures/planning_history_poc_cases.json)（`repository: "Saltmu/orchestune"`, 全 9 cases）は、#1873 のテストで検証すべきすべての結果状態・版重複・同点・除外境界を網羅する。

### 5.1 基準検証リクエスト（Canonical Reference Query）

```json
{
  "schema_version": "planning-history-query-v1",
  "repository": "Saltmu/orchestune",
  "as_of": "2026-11-01T00:00:00Z",
  "query": "共有コンテキスト索引とBM25検索カードの抽出契約・予算制限を分解する",
  "exclude_issue_numbers": [1400, 1401, 1402, 1409],
  "exclude_pr_numbers": [1410]
}
```

### 5.2 各 Case の特徴・`plan_revision_digest`・基準リクエストでの期待採否

| `case_id` | `parent_issue` | `available_at` | `plan_revision_digest` | 表現するシナリオ・タスク結果 | 基準リクエスト（`limit=3`, `max_bytes=12288`）での期待採否と理由 |
| --- | ---: | --- | --- | --- | --- |
| `case-1100-rev1` | `1100` | `2026-10-01T10:00:00Z` | `058675f447d2d744fc77288adaa0ff34b91c48aa25f394dde5a73adaed019484` | 親 `#1100` の旧計画版（`#1101: done`, `#1102: blocked`） | **除外 (`superseded_revision`)**: 同一親 `#1100` に、より新しい適格版 `case-1100-rev2`（`2026-10-05T12:00:00Z`）が存在するため BM25 前に除外。 |
| `case-1100-rev2` | `1100` | `2026-10-05T12:00:00Z` | `ab490a047c1fe553be4e1873b36d4654354d243af37292e5e6dc04af12647202` | 親 `#1100` の改訂版（`#1101: done`, `#1103: done`, `#1104: not-needed`、旧子 `#1102` を `lineage_issue_numbers` に保持） | **採用（Rank 1 / BM25 `38.975463`）**: 親 `#1100` の最新適格版であり、共有コンテキスト索引・BM25 検索カード・抽出契約・予算制限の検索語に最も強く一致する。 |
| `case-1200-mixed-outcomes` | `1200` | `2026-10-12T15:30:00Z` | `204706a354ca1e25286febe645a1f9ea4599b352f645f9e133708dc6fdf63c65` | 結果混在・未起票タスク保持（`#1201: done`, `#1202: blocked`, `#1203: unknown` + `missing_reason`, 未起票 `history-rollout-notes: issue_number=null, result=unknown`） | **採用（Rank 2 / BM25 `7.152551`）**: `blocked`・`unknown`（理由付き）・未起票（`issue_number: null`）の各タスクが欠落せずカードに残ることを検証する。 |
| `case-1300-tie-a` | `1300` | `2026-10-20T09:00:00Z` | `5a7bfe857f4972111a9c7f23f9e3d0a392d59c4f7a8bca8879b380b5c43d48cf` | 同一親同時刻 tie-break 勝者、かつ `case-1350-tie-b` と同一索引テキストを持つ BM25 同点候補（`#1301: done`） | **採用（Rank 3 / BM25 `6.697718`）**: 同一親 `#1300`・同一 `available_at` の `case-1300-tie-z` に `case_id` 昇順で勝ち残り、さらに BM25 同点の `case-1350-tie-b` に `parent_issue` 昇順（`1300 < 1350`）で勝って `limit=3` の 3 枚目に入る。 |
| `case-1300-tie-z` | `1300` | `2026-10-20T09:00:00Z` | `526b45da54c0783c86daed90f9c68f6ae12d1a7a3f084d81b9df8caee83af837` | 親 `#1300` で `case-1300-tie-a` と同一 `available_at` を持つ同時刻別版 | **除外 (`superseded_revision`)**: 同一 `parent_issue: 1300` かつ同一 `available_at` のため、`case_id` 昇順（`"case-1300-tie-a" < "case-1300-tie-z"`）により除外。 |
| `case-1350-tie-b` | `1350` | `2026-10-20T09:00:00Z` | `264d3b5275cc2012177357da17217fdc39b64df8f863d27b929684a7b033a472` | `case-1300-tie-a` と同一索引テキストを持つ異親（`#1350`）の BM25 同点候補 | **BM25 Rank 4（`limit=3` では上限外、`limit>=4` で 4 枚目に採用 / BM25 `6.697718`）**: `case-1300-tie-a` と完全同点となり、`parent_issue` 昇順（`1300 < 1350`）で Rank 4 になる。 |
| `case-1400-target-lineage` | `1400` | `2026-10-15T10:00:00Z` | `b49fa7b0a7f8e2f051e5f041515e1697134b1ef9a0a7d3a49e30aee6fdcd041c` | 対象系列候補（`lineage_issue_numbers: [1400, 1401, 1402, 1409]`, `pr_numbers: [1410]`） | **除外 (`lineage_excluded`)**: `exclude_issue_numbers` / `exclude_pr_numbers` と交差するため、自己・同系列リーク防止として BM25 前に除外。 |
| `case-1500-at-cutoff` | `1500` | `2026-11-01T00:00:00Z` | `45049a90ad0862d351de0a7c8fae24e1d354de8f657e253771f3976787d5ebb0` | cutoff 境界同時刻候補（Outcome source が `2026-11-01T00:00:00Z == as_of`） | **除外 (`after_as_of`)**: `available_at < as_of` の厳密不等号を満たさない（同時刻）ため BM25 前に除外。 |
| `case-1600-after-cutoff` | `1600` | `2026-11-02T09:15:00Z` | `d35de1497982acac3ccded07e3996e25d4b54032d4305d4adf754123c8667529` | cutoff 事後更新候補（子 task source が `2026-11-02T09:15:00Z > as_of` に事後編集） | **除外 (`after_as_of`)**: 事後編集 source により `case.available_at > as_of` となるため未来情報として BM25 前に除外。 |

#### 基準リクエスト実行時の期待サマリー値
- `status`: `"ready"`
- `excluded_counts`: `{"repository_mismatch": 0, "lineage_excluded": 1, "after_as_of": 2, "superseded_revision": 2}`
- `returned_count`: `3`（`--limit 3` 既定時: `["case-1100-rev2", "case-1200-mixed-outcomes", "case-1300-tie-a"]`）、`--limit 4` 以上では `4`（末尾に `"case-1350-tie-b"` が追加）
- `budget_dropped`: `0`（`--max-bytes 12288` 既定時）

---

## 6. 実資料の採取・出典照合手順（Corpus Curation & Verification Protocol）

#1874 で実履歴から比較用 corpus を作成・固定する際は、以下の採取・照合手順を厳守する。

1. **対象と同系列集合の完全性確認**:
   - 各候補計画について、親 Issue、全子 Issue、replan で置換された旧子 Issue、および関連 PR の集合を列挙し、`lineage_issue_numbers` と `pr_numbers` に明記する。
   - ページネーション未完了、削除済み子 Issue、親子対応の不整合などにより **集合の完全性が確認できない計画資料は比較用 corpus に入れない**。
2. **利用可能時刻（`available_at`）の証明と事後編集の扱い**:
   - 各 `source.available_at` は「採用した本文の版がその時点で存在したと証明できる UTC 時刻」とする。
   - **事後編集の禁止規則**: GitHub Issue 本文やコメントに事後編集（`updated_at > created_at` や編集履歴）がある場合、**編集後の現在本文に対して過去の `created_at` を流用してはならない**。
   - 編集後の版を採用する場合はその編集完了が確認できる時刻（`updated_at` 等）を `available_at` とし、版の時点を証明できない記録や評価対象 `as_of` 以降の編集が混入した記録は実資料から除外する。
   - `case.available_at` は常に `max(source.available_at for source in case.sources)` とする。
3. **既存復元・Outcome 読解契約の参照**:
   - 計画構造の復元は `restore_plan_from_parent`（`orchestune/provisioning/plan_restoration.py`）の取得時点復元契約に準拠し、Outcome の要約は `parse_from_comments`（`orchestune/outcome_record.py`）のコメント解析契約に準拠する。
   - Outcome コメントが存在しない子タスクは `result: "unknown"` とし、`missing_reason` に具体的な不在理由（例: `"no_outcome_comment_recorded"`、`"unfiled_subtask_in_parent_manifest"`）を記録してカードに残す。
   - `result: "done"` は記録された Outcome コメントの `result` フィールド要約に留め、PR マージや CI 通過、親計画全体の受入達成は別個の検証済み source がある場合に限り記述する。
4. **原本バイト列の固定と SHA-256 記録**:
   - 採取した元データの UTF-8 バイト列ごとに SHA-256 を `source.digest` に記録し、`plan` / `task` source から §3.1 の規則で `plan_revision_digest` を算出する。
   - 評価実行前に、固定した `--corpus` ファイルと各 `--request` ファイルの **実バイトの SHA-256** を評価レポート（`docs/plans/planning-history-poc-eval.md`）へ事前登録する。

---

## 7. 2 条件比較評価手順と採点・継続判定基準（#1874 Evaluation Protocol）

#1874 では、本書と #1873 のセレクタを用いて、以下の手順で「類似計画カードなし（条件 A）」と「類似計画カードあり（条件 B）」の比較評価を実施する。

### 7.1 事前登録と環境・SHA 分離（Pre-Registration & Isolation）
1. **代表課題の選定（5〜10 件）**:
   - 共有契約先行・独立タスク・失敗／再開の参照例を含む代表課題 **5〜10 件** を試行開始前に選定し、課題ごとの完全な要件文、対象実装が含まれない開始前 `base_sha`、対象系列除外集合（`exclude_issue_numbers`, `exclude_pr_numbers`）、評価基準時刻 `as_of`、corpus/request ファイルの SHA-256、使用モデル・reasoning 設定・ツール・時間／トークン予算を `docs/plans/planning-history-poc-eval.md` に固定する。結果を見た後の課題差し替えは禁止する。
2. **実装前 `base_sha` の確認と作業場所の分離**:
   - 各課題の `base_sha` にその課題自身の事後実装コミットが含まれていないことを `git log` / `git merge-base` で確認する。
   - **2 つの checkout の分離**:
     - **カード生成用 checkout**: #1873（`scripts/planning_history_poc.py`）が存在する SHA の作業場所で固定 corpus と固定 request からカードを生成し、その SHA と出力カードの SHA-256 を記録する。
     - **分解試行用 worktree**: 評価対象課題の実装前 `base_sha` をチェックアウトした独立・初期状態の worktree／セッションで A・B の各試行を実行する。両方の SHA をレポートに明記する。
3. **2 条件（A / B）の入力同一性と交互実行（AB / BA）**:
   - **条件 A（カードなし）**: 共通の課題要件・開始前 `base_sha` のみを与える。
   - **条件 B（カードあり）**: 条件 A と **完全に同一の必須入力** に加え、#1873 が生成した `planning-history-cards-v1` カードのみを追加する（#1841 の Tier 1 注入有無など他の条件差を混ぜない）。
   - **リーク防止**: 両条件とも、評価対象課題の事後実装や `as_of` 以降の GitHub Issue/PR 本文・コメントへアクセスできない環境で実行する。条件 B で出典を確認する場合も固定資料の採用版のみを参照させる。
   - **順序の交互割当**: 各課題につき条件 A・条件 B を 1 回ずつ実行し、実行順は課題番号順に **AB, BA, AB, BA, ...** と交互に入れ替える。

### 7.2 盲検採点ルーブリック（4 項目 × 0〜2 点 ＝ 合計 8 点満点）

採点前に計画出力から条件名（A / B）やカード引用の明示ラベルなど **条件を推測できる情報を伏せた匿名化（ブラインド）状態** にし、両条件を同一のルーブリックで採点する。

| 評価項目 | `0` 点（欠落または重大な誤り） | `1` 点（部分的に妥当） | `2` 点（満たす） |
| --- | --- | --- | --- |
| **1. 必須要件網羅** (`requirement_coverage`) | 課題の必須要件・受入条件の脱落がある、または要件と矛盾する分解になっている。 | 主要要件は含むが、一部の境界条件やサブタスク受入条件の対応付けが曖昧。 | 課題のすべての必須要件・受入条件が漏れなく具体的なサブタスクへ割り当てられている。 |
| **2. 実コードと整合する変更範囲** (`footprint_alignment`) | `base_sha` 上の実在モジュール・責務境界と整合しない架空ファイルや誤った層への変更を前提としている。 | 変更対象の大枠は妥当だが、関連テスト・契約ファイルの一部漏れや過剰な予約がある。 | `base_sha` 上の実コード・層境界・共有契約と整合する過不足ない `footprint` / `symbols` が定義されている。 |
| **3. 根拠のある依存と DAG** (`dependency_dag_validity`) | 閉路（cycle）、未解決依存、共有契約の書込競合放置、または必要な先行依存の欠落がある。 | 有効な DAG だが、根拠のない過剰な直列化（不要な必須依存の追加）や説明不足の辺がある。 | 共有契約・データフローに基づく必要十分な依存辺のみで有効な DAG が構成され、並列性と競合回避の根拠が明示されている。 |
| **4. 検証計画** (`verification_plan`) | 検証コマンドやテスト観点が欠落している、または変更内容と無関係な検証になっている。 | 標準 CI やテスト実行は記載されているが、サブタスク固有の境界ケースや回帰確認対象が不足。 | 各サブタスクの成果物・境界条件に対応する具体的なテスト対象と標準 local CI 検証計画が揃っている。 |

- 上記 4 項目の合計点（`0〜8` 点）に加え、**① 自己・事後情報の混入（リーク）有無** と **② 条件 B での必須要件落ち有無** を別途監査フラグとして記録する。

### 7.3 測定項目・欠測処理（理由付き `null`）と指標別ペア分母

各試行について以下を記録する:
- 実行状態（成功 / 失敗 / 中断）、匿名試行 ID、DAG 検証結果
- 採点 4 項目の各点数（`0〜2`）と合計点（`0〜8`）、リーク有無、必須要件落ち有無
- 実測 `input_tokens`, `output_tokens`, `total_tokens`, `cached_tokens`, `cost_usd`, 計画作成時間 (`wall_ms`)
- セレクタ検索時間 (`search_ms`, 秒換算値も併記)、注入カード bytes (`injected_bytes`)、リポジトリ／出典取得回数、カードごとの採否と理由
- 実装後の競合・再レビュー・手戻り（計画生成のみの評価では実測しないため `null` とし、`missing_reason: "planning-only evaluation; post-implementation rework not executed"` を明記する）

#### 欠測処理の鉄則
- 取得できない値（例: CLI が `cost_usd` を出力しない、試行が技術的エラーで中断した等）を **`0` で補完したり、バイト数からトークン数を換算・推定してはならない**。必ず `null` とし、対応する欠測理由（`missing_reason`）を記録する。
- ペア比較（条件 B と条件 A の比較）は、**その指標について A・B 両腕の実測値が揃っている比較可能ペアだけ** を分母として集計し、指標ごとに「比較可能ペア数（分母）」と「欠測ペア数・理由」を併記する。

### 7.4 継続・再設計・見送りの判定基準（Continuation Criteria）

以下の **6 条件をすべて同時に満たす場合** に限り、基盤拡張（共有索引 `context-index-v2` への計画履歴統合と分解スキルへの任意接続）を **「継続（Continue）」** と判定する。

| # | 判定項目 | 継続（Continue）の必須達成基準 |
| :---: | --- | --- |
| 1 | **リークゼロ** | 全試行において、対象系列（自己・兄弟・置換 Issue/PR）および `as_of` 以降の事後情報の混入が **`0` 件** であること。 |
| 2 | **B の必須要件落ちゼロ** | 条件 B（カードあり）の全試行において、必須要件の脱落が **`0` 件** であること。 |
| 3 | **品質比較可能ペア数と非悪化** | 品質スコア（合計点）を比較可能なペアが **5 組以上** あり、その **すべての比較可能ペアで条件 B の合計点が条件 A を下回らない**（全ペアで `Score(B) - Score(A) >= 0`）こと。 |
| 4 | **品質または実測 Total Tokens の改善** | 比較可能ペアのうち **少なくとも 1 組以上** で、合計点の改善（`Score(B) > Score(A)`）または実測 `total_tokens` の削減（`TotalTokens(B) < TotalTokens(A)`）が観測されること。 |
| 5 | **実測 Total Tokens の中央値非増加** | 実測 `total_tokens` を比較可能なペアが **5 組以上** あり、ペア差 `TotalTokens(B) - TotalTokens(A)` の **中央値（median）が `0` 以下** であること。 |
| 6 | **検索レイテンシ上限** | 条件 B のすべての試行において、カード検索・生成時間（`scripts/planning_history_poc.py` の実行時間）が **各回 10 秒以内（`<= 10.0s`）** であること。 |

- **再設計／追加観測（Redesign / Observe More）**: 品質や時系列リークの問題、比較可能ペア数が 5 組未満、または品質改善とトークン増加（`B - A` 中央値 `> 0`）が混在して上記 6 条件の一部を満たさない場合。
- **見送り（Drop）**: 品質改善も実測 `total_tokens` 削減も観測されない場合。
