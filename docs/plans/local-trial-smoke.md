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
  - 対話型 TTY ではユーザープロンプト（`yes/no`）で標準入力を待機するため、バックグラウンド自動実行時は `--approve-plan <DIGEST>` を用いた非対話 resume を利用するか、stdin へのパイプが必要である。
  - 開発環境のグローバルプラグイン（serena 等）が生成する未追跡ファイルを無視するため、対象リポジトリの `.gitignore` 設定が重要となる。

### 7.2 継続判断
- **判定**: **継続 (Continue / Adopt)**
- **根拠**: #1730 の要求仕様である「GitHub 設定不要で 1 件を完走できるローカル初回体験」が実エージェントを用いて証明され、予期せぬ中断や不正な差分も安全にハンドリングできることが確認された。
