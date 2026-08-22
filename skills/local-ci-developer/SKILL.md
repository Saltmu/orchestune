---
name: "local-ci-developer"
description: "設計・実装プラン作成、Issue起票、TDD、ローカルCI、PR作成、自動レビュー、完了宣言（outcome）までを統制するルータスキル。"
---

# Local CI & TDD Developer Skill

本スキルは、開発タスクにおける設計からTDD実装、ローカルCI検証、PR作成、自動レビュー、完了宣言に至る標準ワークフローを統制するルータです。

## 実行モード別の既定動作

| 項目 | 対話モード | 非対話モード（自動ディスパッチ等） |
| :--- | :--- | :--- |
| **プラン承認 (Step 1)** | ユーザーへ提示し承認を待つ | `implementation_plan.md` 作成後、直ちに実装へ進む |
| **Issue 起票 (Step 2)** | 必要に応じて手動/CLIで起票 | プロンプトで渡された Issue 番号を使用（起票スキップ） |
| **レビュアー選択 (Step 11)** | ユーザーへ依頼先（Claude/Codex）を確認 | 実装と異なるモデル（クロスモデル）を自動選択 |
| **エスカレーション** | ユーザーへ判断を問い合わせ | outcome レコード (`blocked`) を残して安全に終了 |

## 軽微変更（Typo / Docs）の短経路
コードロジックの変更を伴わないドキュメント修正や typo 修正では、**ステップ3〜8（TDD）をスキップ**し、ステップ9でフォーマット・Lint（`poetry run ruff check`）を実施した上でステップ10（PR作成）へ進んで構いません。

## 開発ステップ一覧

| Step | 項目 | 概要 / 実行コマンド | 参照先 |
| :--- | :--- | :--- | :--- |
| **0** | **要件充足チェック** | 既に `main` で要件が満たされている場合は、PRを作らず Issue に outcome レコード (`result: not-needed`) を投稿して終了。 | - |
| **1** | **設計・実装プラン作成** | `implementation_plan.md` を作成し、方針を整理。 | - |
| **2** | **GitHub Issue 起票** | プロンプトで Issue 番号が渡されている場合はスキップ。新規起票時は `gh issue create --title "..." --body "..."` を実行。 | - |
| **3〜9** | **TDD & ローカルCI** | 再現テスト作成、ベースライン記録、テスト駆動実装、ローカルCI検証（`./scripts/local-ci.sh` / `.\\scripts\\local-ci.ps1`）。 | [references/tdd.md](references/tdd.md) |
| **10** | **Pull Request 作成** | `.github/pull_request_template.md` を記入し、`gh pr create` でPR送信。 | [references/pr.md](references/pr.md) |
| **11** | **LLM自動PRレビュー** | `scripts/wait_for_review.py` によるレビュー依頼・待機・指摘対応ループ。 | [references/review-loop.md](references/review-loop.md) |
| **12** | **完了宣言 (Outcome)** | PR/Issue コメントに outcome レコード (`result: done`) を投稿して作業終了。 | - |

### 完了宣言 (Outcome Record) のフォーマット
タスク完了時は、PR（またはIssue）のコメントに以下の機械可読マーカーを投稿します：
```markdown
<!-- orchestune:outcome -->
```json
{
  "result": "done",
  "issue": 123,
  "pr": 456
}
```
```
