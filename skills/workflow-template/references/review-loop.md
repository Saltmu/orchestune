# Review Loop Reference (Step 11)

本ドキュメントは、LLM自動PRレビューと指摘対応サイクルの詳細手順を定めたリファレンスです。

---

## 11. LLM自動PRレビューループ (Review Cycle)

PR作成後、客観的な品質検証のためLLM自動PRレビューを実施し、指摘事項の解消まで改善サイクルを回します。

### レビューループの制御構造（擬似コード）

```text
Loop (最大5ラウンド):
  1. レビュー待機コマンドを実行
     - wait_for_review.py 環境:
         初回: poetry run python scripts/wait_for_review.py --pr <PR番号> --bot-name <bot>
         2周目以降: --body-file /tmp/review_reply.md を付与して再レビュー依頼
     - 縮退環境（スクリプト無し）:
         PRコメント（例: @claude review）投稿後、Web UI / 通知でレビュー完了を確認
  2. 出力結果の確認と文脈判定:
     - タイムアウト / 無応答の場合:
         -> 1回のみ再試行。解消しなければエスカレーション（outcome: blocked）。
     - レビュー結果取得の場合:
         -> 最新本文およびインライン指摘をLLMが精読。
         -> (a) 修正すべき指摘がある場合:
             指摘内容に合わせてコード修正・テスト追加を実施。
             ローカルCI（<CI_ENTRYPOINT>）で検証後、コミット＆プッシュ。
             /tmp/review_reply.md（Round X/5 表記付き）を作成し、ループ先頭（1）へ戻る。
         -> (b) 指摘なし（LGTM / All checks passed / No blocking issues）の場合:
             ループ終了。ステップ12（完了宣言）へ進む。
     - ラウンド上限（5ラウンド）到達時:
         -> 自動反復を停止。論点と対応状況をPRコメントに整理してエスカレーション。
```

### レビュー返信ファイル (`/tmp/review_reply.md`) の作成
指摘に対応した後は、修正内容をまとめた返信ファイルを作成します：
```markdown
## レビュー指摘への対応 (Round 2/5)

### 対応内容
- [対応] 指摘事項Aのバグを修正し、テストを追加しました（コミット: abc1234）
- [見送り] 指摘事項Bは仕様に基づく設計のため現状維持とします（理由: ...）

@claude review
```

返信ファイル作成後、`wait_for_review.py`（または手動PRコメント）経由で再レビューを依頼します。
