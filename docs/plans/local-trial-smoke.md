# Local Trial 実エージェントスモークテスト証跡と計測・継続判断 (#1754)

## 1. 概要と検証目的

親課題 #1730（ローカル初回体験）および #1738 の受け入れ基準として要求された「実エージェント（Claude Code または Codex CLI）によるエンドツーエンドの初回体験スモークテスト」を実施し、その実行証跡、所要時間、手数、詰まり・復旧操作、および継続判断を記録する。
fake E2E による単体・統合テストの成功を実エージェント受け入れ条件の代用とせず、本番環境の認証済み実 CLI を用いた完全な自律サイクル（計画 → 明示承認 → 実装 → CI → 別 worktree レビュー → ready → 手動採用）を確認した。

---

## 2. 実行環境と利用 CLI

- **実行日時**: 2026-11-08
- **OS**: Linux (x86_64, Ubuntu kernel)
- **Python**: 3.13.15
- **Orchestune 対象コミット**: `e0ca1980` (先行修正 #1746, #1751, #1752, #1753 統合後の `parent/issue-1730` HEAD)
- **CLI 製品検証結果**:
  - **Claude Code**:
    - バージョン: `2.1.318 (Claude Code)`
    - 実行結果: `Claude Code can't use your Claude subscription, usually because a payment is past due` によりサブスクリプション支払い未完了エラーとなり、実実行不可を確認。
  - **OpenAI Codex**:
    - バージョン: `codex-cli 0.167.0` (model: `gpt-6.1-sol`)
    - 実行結果: 認証済み・通常利用者権限で正常稼働を確認。今回の実スモークテスト対象として採用。

---

## 3. 検証リポジトリ構成と依頼内容

Git 管理外の session directory 配下に、リモートを持たず初期コミット済みのスタンドアロン Python プロジェクトを作成した。

- **リポジトリパス (相対)**: `.orchestune/tmp/trial-smoke-1754-20261108T065220Z-d24180ef/sample-repo`
- **初期ファイル**:
  - `calc.py`: `add(a, b)` 関数
  - `test_calc.py`: `TestCalc` (`python3 -m unittest`)
  - `AGENTS.md`: テスト規律文書 (`python3 -m unittest test_calc.py`)
  - `.gitignore`: `__pycache__/`, `*.pyc`, `.serena/`
- **初期コミット (Base SHA)**: `cc741ca2f692cf28666ad3ccd33202e29b854a9f`
- **依頼内容 (`request.txt`)**:
  ```text
  Add a subtract function `subtract(a: int, b: int) -> int` to calc.py and add corresponding test to test_calc.py.
  ```
- **CI コマンド**: `python3 -m unittest test_calc.py`

---

## 4. 実行ログと自律サイクル証跡

### 4.1 試行時の障害と復旧操作 (詰まり・知見)

初回起動時（trial `d02aca4f77ec47278eff620d69fb80a4`）、Codex 実行時に環境の serena MCP が自動初期化され、作業ツリー内に未追跡ディレクトリ `.serena/` が生成された。
`verify_clean_plan` により計画フェーズでの未承認変更（`worktree_dirty`）として検知され安全に停止した。
- **復旧操作**:
  - `orchestune trial result ... --decision keep` で初回試行をクローズ
  - `.gitignore` に `.serena/` を追加コミット（コミット `cc741ca`）
  - 作業ツリーをクリーンにして再試行

### 4.2 本番スモーク実行 (`trial_id: 3d949b9c092444daaa66afe1497ea80c`)

1. **Trial Start**:
   ```bash
   orchestune trial start \
     --request-file .orchestune/tmp/.../request.txt \
     --repo .orchestune/tmp/.../sample-repo \
     --agent codex \
     --ci-command "python3 -m unittest test_calc.py"
   ```
2. **計画フェーズ (Plan Generation)**:
   - Codex がリクエストと `AGENTS.md` を精査し、計画を策定。
   - `goal`: `Add subtract(a: int, b: int) -> int to calc.py, returning a - b, with corresponding unit tests.`
   - `write_paths`: `['calc.py', 'test_calc.py']`
   - `discipline_status`: `compatible`
   - `plan_digest`: `de54bac92c4eb137e3cbce290c0116c565e948a7e9e5671e1183c96d6a04d861`
   - `effective_phase`: `planned (reason: approval_required)`
3. **明示承認 (Interactive Prompt / Resume)**:
   - 対話プロンプト `この計画で開始しますか? (yes/no) [no]: ` に対し `yes` を入力。
   - `approved_plan_digest` に `de54bac9...` が記録され、実装へ自動遷移。
4. **実装フェーズ (Implementation)**:
   - 実 Codex が `calc.py` に `subtract` 関数を追加し、`test_calc.py` に 6 パターンのテストケースを追加。
   - 変更が承認 write_paths 内であることを確認し、Orchestune supervisor がコミット `94656666bf79150f552ed2134665885b26fe253a` を作成。
   - `budget`: `cycles=1/3`
5. **CI 検証フェーズ (CI Verification)**:
   - コミット `9465666` 上で `python3 -m unittest test_calc.py` を実行。
   - 実行結果: `Ran 2 tests in 0.000s OK`。
   - `ci_evidence`: `passed (head=94656666bf79150f552ed2134665885b26fe253a)`
6. **別セッションレビュー (Separate Worktree Review)**:
   - 独立したレビュー用 worktree（`trial-review-3d949b9c092444daaa66afe1497ea80c`）を自動作成。
   - Codex を読み取り専用サンドボックスで起動し、Git diff と CI 結果を検証。
   - 判定結果: 指摘なし（`verdict: pass`, `findings: []`）。
   - `review_evidence`: `同一製品の別セッションレビュー passed (head=94656666bf79150f552ed2134665885b26fe253a)`
   - `budget`: `reviews=1/3`
7. **Ready 到達**:
   - `effective_phase`: `ready (ローカルの採用候補)`
   - 終了コード: `0`
8. **手動採用 (Manual Adoption Flow)**:
   - 提示されたコマンドに従い base ブランチへ fast-forward マージ:
     ```bash
     git -C .orchestune/tmp/.../sample-repo merge --ff-only trial/3d949b9c092444daaa66afe1497ea80c
     ```
   - 採用記録:
     ```bash
     orchestune trial result 3d949b9c092444daaa66afe1497ea80c --decision adopted --repo .orchestune/tmp/.../sample-repo
     ```
   - `effective_phase`: `closed (decision: adopted)`

---

## 5. 生成された差分 (Diff)

```diff
diff --git a/calc.py b/calc.py
index 1f383d8..84c5a94 100644
--- a/calc.py
+++ b/calc.py
@@ -1,3 +1,8 @@
 def add(a: int, b: int) -> int:
     """Return the sum of two integers."""
     return a + b
+
+
+def subtract(a: int, b: int) -> int:
+    """Return the difference of two integers."""
+    return a - b
diff --git a/test_calc.py b/test_calc.py
index aae98c0..f0e67e0 100644
--- a/test_calc.py
+++ b/test_calc.py
@@ -1,11 +1,19 @@
 import unittest
-from calc import add
+from calc import add, subtract
 
 
 class TestCalc(unittest.TestCase):
     def test_add(self) -> None:
         self.assertEqual(add(2, 3), 5)
 
+    def test_subtract(self) -> None:
+        self.assertEqual(subtract(5, 3), 2)
+        self.assertEqual(subtract(3, 5), -2)
+        self.assertEqual(subtract(3, 3), 0)
+        self.assertEqual(subtract(-5, -3), -2)
+        self.assertEqual(subtract(0, 3), -3)
+        self.assertEqual(subtract(3, 0), 3)
+
 
 if __name__ == "__main__":
     unittest.main()
```

---

## 6. 計測値とリソース消費

| 指標 | 計測結果 | 備考 |
|---|---|---|
| **計画フェーズ所要時間** | 約 28 秒 | Codex によるリポジトリ調査・規律照合・計画生成 |
| **承認〜実装〜CI〜レビュー所要時間** | 約 43 秒 | 実装コード生成、CI 実行、別 worktree レビュー実行 |
| **実稼働所要時間 (Active Time)** | 約 71 秒 (1分11秒) | 入力待機時間を除いた実処理時間 |
| **消費サイクル数** | 1 / 3 | 実装 1 回で完了、fix なし |
| **消費レビュー回数** | 1 / 3 | 1 回のレビューで pass |
| **利用トークン・コスト** | 非公開 (CLI 不出力) | `codex exec` の stdout schema ではトークン集計値が出力されないため未取得。疎通プローブ時は 10,766 トークン消費を確認。 |
| **手数** | 2 手 | 起動コマンド 1 回 + 承認入力 1 回（および採用コマンド 2 回） |

---

## 7. 評価と継続判断

### 7.1 評価
- **初回体験の成立性**: GitHub 設定やリモートリポジトリが一切存在しないローカル環境において、実エージェント CLI のみで計画立案・規律確認・ユーザー承認・自動実装・CI 検証・独立 worktree レビュー・手動採用まで完走できることを確認した。
- **安全性と分離性**:
  - 計画・レビュー時は読み取り専用サンドボックスで実行され、不要なファイル変更が防止された。
  - 実装時は計画された `write_paths` 内のみが更新され、Git コミットと CI は Orchestune 側で厳格に管理された。
  - レビューは別セッション・別 worktree で行われ、コンテキスト汚染が防止された。
- **課題と注意点**:
  - 対話型 TTY ではユーザープロンプト（`yes/no`）で標準入力を待機する一方、非対話環境（パイプやバックグラウンド実行）ではプロンプトは表示されず `planned` で終了する仕様（`stdin.isatty() and stdout.isatty()`）であるため、非対話自動実行時は明示的な `orchestune trial resume <ID> --approve-plan <DIGEST>` を呼び出す必要がある。
  - 開発環境のグローバルプラグイン（serena 等）が生成する未追跡ファイルを無視するため、対象リポジトリの `.gitignore` 設定が重要となる。

### 7.2 継続判断
- **判定**: **継続 (Continue / Adopt)**
- **根拠**: #1730 の要求仕様である「GitHub 設定不要で 1 件を完走できるローカル初回体験」が実エージェントを用いて証明され、予期せぬ中断や不正な差分も安全にハンドリングできることが確認された。

---

## 8. Claude Code スモーク再検証 (#1903) — 実 Claude スモーク結果は未確認

> **結論**: 実 Claude Code によるスモークは**未確認**である。計画生成の段階で Claude Code CLI が未ログインのため停止した。既存の Codex 成功記録（上記 §2–§7）および fake E2E の成功は、Claude の成功に数えない。

### 8.1 検証日・統合 SHA・バージョン

- **検証日**: 2026-11-25
- **統合 SHA**: `1471d87e`（`parent/issue-1891` HEAD。#1901 PR #1906 と #1902 PR #1905 の両方がマージ済み）
- **OS**: Linux (x86_64, Ubuntu 24.04, kernel 7.0.0-44-generic)
- **Python**: 3.12.3
- **Orchestune**: 0.5.0（上記統合 SHA の worktree コードで実行）
- **Claude Code**: `2.1.332 (Claude Code)`（検証当日に公式 npm ルート `npm install -g @anthropic-ai/claude-code` で導入）
- **認証・権限の前提**: claude.ai サインインまたは API キーが必要。通常権限で実行。認証・権限を回避するフラグは使っていない。
- **認証状態**: 未サインイン（`claude doctor` で "Not signed in to claude.ai"、"no API key or claude.ai sign-in" を確認。インストール自体に問題なし。API 接続は到達可）

### 8.2 試行先と依頼内容

- **試行先**: workspace 内の Git 管理外領域（相対パス `.orchestune/tmp/trial-smoke-1903-…/sample-repo`）に作った、リモートなし・初期コミット済み・小規模のサンプル（§3 と同一構成: `calc.py` の `add`、`test_calc.py`、`AGENTS.md`、`.gitignore` に `.serena/` を含む）
- **初期コミット (Base SHA)**: `fa2d615e5f7824510520403d24c507a83ff8999a`
- **依頼内容**: §3 と同一（`subtract(a: int, b: int) -> int` を `calc.py` に追加し、対応するテストを `test_calc.py` に追加）
- **CI コマンド**: `python3 -m unittest test_calc.py`
- **実行コマンド**:
  ```bash
  orchestune trial start \
    --request "Add a subtract function \`subtract(a: int, b: int) -> int\` to calc.py and add corresponding test to test_calc.py." \
    --repo <sample-repo> \
    --agent claude \
    --ci-command "python3 -m unittest test_calc.py"
  ```

### 8.3 到達した段階と停止理由

- **Trial ID**: `d9340de6b12b48e7a1a9e2067344fbd5`
- **到達段階**: 計画生成（checkpoint `plan`）で停止。`effective_phase: needs_attention`、`reason: agent_failed`。
- **エージェントの出力**: Claude Code CLI は起動し、約 4.5 秒で終了コード 1 を返した。出力は `Not logged in · Please run /login`（`terminal_reason: api_error`、`is_error: true`、0 トークン、コスト $0）。
- **未到達**: 計画・digest の確認、明示承認、実装・ローカルコミット、同一 head の CI、別 worktree レビュー、`result` による採用判断のいずれも未実施（`plan: null`、`approved_plan_digest: null`、budget `cycles=0/3, reviews=0/3`）。
- **試行の後始末**: `trial result <ID> --decision keep` で試行をクローズ済み（`effective_phase: closed`、`decision: keep`）。

### 8.4 #1901 の機能の副次的確認

本試行の過程で、#1901 で導入された機能が実動作で確認できた（本 Issue の主目的ではないが記録する）:

- **ID 省略**: `trial status --repo <sample-repo>`（ID なし）で active な試行 `d9340de6…` が解決された。
- **終了後の明示 ID 照会**: クローズ後に `trial result d9340de6… --repo <sample-repo>`（明示 ID）で終了済み試行が再表示された。
- **クローズ後の ID 省略**: クローズ後に ID なしの `status` は `Error (not_found): No active trial found` で終了コード 2 となった。

### 8.5 計測値

| 指標 | 計測結果 | 備考 |
|---|---|---|
| **試行開始〜停止まで** | 約 9 秒 | `trial start` コマンド全体（`uv run` を含む） |
| **エージェント呼び出し** | 約 4.5 秒 | 計画生成の 1 回の呼び出しで終了コード 1 |
| **消費サイクル数** | 0 / 3 | 計画が生成されず実装に進まなかった |
| **消費レビュー回数** | 0 / 3 | レビュー未実施 |
| **利用トークン・コスト** | 0 トークン / $0 | Claude Code の出力が 0 トークンを報告 |
| **手数** | 1 手 | 起動コマンド 1 回で停止（承認・採用の操作なし） |

### 8.6 再検証条件

実 Claude スモークを完了させるには、以下を満たしたうえで §8.2 と同一の手順を再実行すること:

1. 実行マシンで Claude Code にサインインする（claude.ai サブスクリプション）か、API キーを用意する。
2. `parent/issue-1891` が前進していたら、その時点の HEAD を新たな統合 SHA として記録する（本記録の統合 SHA `1471d87e` は 2026-11-25 時点のもの）。
3. 再実行時は本記録の Trial ID や sample-repo を使い回さず、新規の試行として開始する。

なお、#1754 時点の Claude Code の停止理由はサブスクリプションの支払い未完了エラーだったが、今回は未サインイン（未ログイン）であり、原因は異なる。どちらも「Claude Code が実行できなかった」点では同じだが、混同しないこと。

---

## 9. Codex CLI 実エージェントスモーク検証 (#1910)

> **結論**: `parent/issue-1891`（#1901・#1902 統合済み）において、実 Codex CLI を用いた初回体験（`start --request` → 計画・digest 確認 → `resume --approve-plan` → 実装・ローカルコミット → 同一 head CI → 別 worktree レビュー → `ready` → fast-forward マージ・`result` 採用記録、および ID 省略・終了後明示 ID 照会）が正常に完走した。なお、本記録の成功は #1903（Claude Code スモーク）の成功には数えず、#1903 は引き続き OPEN・未確認のままとする。

### 9.1 検証日・統合 SHA・バージョン・認証前提

- **検証日**: 2026-11-26 JST（UTC: 2026-11-25）
- **対象統合 SHA**: `1471d87e`（`parent/issue-1891` 上の #1901 PR #1906 と #1902 PR #1905 の統合時点。実行は同一コードに §8 のドキュメント追記 PR #1909 のみを乗せた `parent/issue-1891` HEAD `1b54b044` の worktree で実施）
- **OS**: Linux (x86_64)
- **Python**: 3.13.15
- **Orchestune**: 0.5.0（上記 worktree コードで実行）
- **Codex CLI**: `codex-cli 0.171.0`（model: `gpt-6-luna`）
- **認証・権限の前提**: ChatGPT アカウント連携（`codex login`）または API キーが必要。`codex doctor` にて `Logged in using ChatGPT`（認証済み・接続到達可）を確認のうえ、通常権限で `--agent codex` を指定して実行。認証・権限を回避するフラグは使用していない。

### 9.2 試行先と依頼内容

- **試行先**: workspace 内の Git 管理外領域（相対パス `.orchestune/tmp/task-1910-.../sample-repo`）に作成した、リモートなし・初期コミット済み・小規模のサンプル（§3・§8.2 と同一構成: `calc.py` の `add`、`test_calc.py`、`AGENTS.md`、`.gitignore` に `.serena/` を含む）
- **初期コミット (Base SHA)**: `062eda8def30de6ec40a64cdc75e9e41392cbfab`
- **依頼内容**: §8.2 と同一（`subtract(a: int, b: int) -> int` を `calc.py` に追加し、対応するテストを `test_calc.py` に追加）
- **CI コマンド**: `python3 -m unittest test_calc.py`

### 9.3 実行手順と証跡

- **Trial ID**: `e75db459dfdf4080b721fd511db74c50`
- **Head SHA**: `224ad1c5e5970396c318419df1eb3266500b7083`

1. **Trial Start (`--request`)**:
   ```bash
   orchestune trial start \
     --request 'Add a subtract function `subtract(a: int, b: int) -> int` to calc.py and add corresponding test to test_calc.py.' \
     --repo <sample-repo> \
     --agent codex \
     --ci-command "python3 -m unittest test_calc.py"
   ```
   - 計画が生成され、`goal`、`acceptance_criteria`、`write_paths: ['calc.py', 'test_calc.py']`、`discipline_status: compatible`、`plan_digest: efd8482799fed88da2d57451ec460a2f336cffca9bc6092e9ffeefd658960d6e`、`effective_phase: planned`（`reason: approval_required`）が表示された。
   - 対話プロンプト `この計画で開始しますか? (yes/no) [no]: ` に対して `no` を入力し、`planned` 状態で一旦終了した（終了コード 0）。
2. **ID 省略での状態確認**:
   ```bash
   orchestune trial status --repo <sample-repo>
   ```
   - Trial ID を省略して active な試行 `e75db459dfdf4080b721fd511db74c50` と `plan_digest` が解決・表示された（終了コード 0）。
3. **ID 省略での明示承認・実装・CI・別 worktree レビュー**:
   ```bash
   orchestune trial resume \
     --approve-plan efd8482799fed88da2d57451ec460a2f336cffca9bc6092e9ffeefd658960d6e \
     --repo <sample-repo>
   ```
   - `approved_plan_digest` が記録され、実装 worktree（`trial-e75db459...`）で `calc.py` と `test_calc.py` が更新・コミット（`224ad1c5e5970396c318419df1eb3266500b7083`）された。
   - **CI 結果**: `ci_evidence: passed (head=224ad1c5e5970396c318419df1eb3266500b7083)`（`python3 -m unittest test_calc.py`）。
   - **別 worktree・別セッションレビュー結果**: 独立したレビュー用 worktree（`trial-review-e75db459...`）で実行され、`review_evidence: 同一製品の別セッションレビュー passed (head=224ad1c5e5970396c318419df1eb3266500b7083)`。
   - `budget: cycles=1/3, reviews=1/3`、`effective_phase: ready (ローカルの採用候補)` で終了コード 0。
4. **採用・ID 省略での完了記録・終了後照会**:
   - Fast-forward マージ:
     ```bash
     git -C <sample-repo> merge --ff-only trial/e75db459dfdf4080b721fd511db74c50
     ```
     - マージ後 `python3 -m unittest test_calc.py` で `Ran 2 tests in 0.000s OK` を確認。
   - ID 省略での採用記録:
     ```bash
     orchestune trial result --decision adopted --repo <sample-repo>
     ```
     - `effective_phase: closed`、`decision: adopted` で終了コード 0。
   - 終了後の明示 ID 照会:
     ```bash
     orchestune trial result e75db459dfdf4080b721fd511db74c50 --repo <sample-repo>
     ```
     - 終了済み試行のサマリ・diff・`decision: adopted` が正常に表示された（終了コード 0）。
   - 終了後の ID 省略確認:
     ```bash
     orchestune trial status --repo <sample-repo>
     ```
     - `Error (not_found): No active trial found`（終了コード 2）となった。

### 9.4 計測値と観察事項

| 指標 | 計測結果 | 備考 |
|---|---|---|
| **計画生成（`trial start`）** | 約 6 秒（計画出力まで） | プロンプト待機を含めたコマンド全体は 201 秒 |
| **承認〜実装〜CI〜レビュー（`trial resume`）** | 39 秒 | 実装・コミット・CI・別 worktree レビュー・`ready` 到達まで |
| **実稼働所要時間 (Active Time)** | 約 45 秒 | 入力待機時間を除いた実処理時間 |
| **消費サイクル数** | 1 / 3 | 実装 1 回で完了 |
| **消費レビュー回数** | 1 / 3 | 1 回のレビューで pass |
| **操作数** | 3 手（`start` + `resume` + `result`、および採用 merge 1 回） | `status`・終了後照会等の確認コマンドを除く主手順 |

- **詰まった点・観察事項**:
  - PTY 接続環境で `trial start` を実行すると計画表示後に `この計画で開始しますか? (yes/no) [no]: ` で入力待機となる。`resume --approve-plan` を別コマンドとして試す場合はプロンプトに `no` を入力して `planned` で抜ける必要がある。
  - #1901 で追加された `--request` インライン入力、active trial の ID 省略（`status` / `resume` / `result`）、および終了後の明示 ID 照会がいずれも実 Codex CLI 経路で期待通り動作した。

