# Review Loop Reference (Step 11)

本ドキュメントは、LLM自動PRレビューと指摘対応サイクルの詳細手順を定めたリファレンスです。

---

## 11. LLM自動PRレビューループ (Review Cycle)

`scripts/wait_for_review.py` を同期実行し、レビュアーボット（Claude / Codex）へのレビュー依頼、完了待機、指摘解析を行います。二重送信防止およびラウンド数の管理はスクリプトが自動で行います。

### レビューループの制御構造（擬似コード）

```text
Loop (最大5ラウンド):
  1. レビュー待機コマンドを実行
     - 初回: poetry run python scripts/wait_for_review.py --pr <PR番号> --bot-name <bot>
     - 2周目以降: --body-file /tmp/review_reply.md を付与して再レビュー依頼
  2. 終了コード (exit code) に応じた分岐:
     - exit 0 (指摘なし / LGTM):
         -> ループ終了。ステップ12（完了宣言）へ進む。
     - exit 10 (修正すべき指摘あり):
         -> 指摘内容を精査しコード修正・テスト追加を実施。
         -> ローカルCIで検証後、コミット＆プッシュ。
         -> /tmp/review_reply.md を作成し、ループ先頭（1）へ戻る（後退辺）。
     - exit 11 (ボットがレビュー進行中):
         -> poetry run python scripts/wait_for_review.py --pr <PR番号> --bot-name <bot> --no-post で待機継続。
         -> ループ先頭（1）へ戻る（後退辺）。
     - exit 12 (ラウンド上限到達 / max-rounds):
         -> 自動反復を停止。理由を記録した outcome レコード (result: blocked) を投稿してエスカレーション。
     - exit 20 (タイムアウト):
         -> --no-post --timeout 300 で1回のみ再試行。解消しなければエスカレーション。
     - exit 30 (判定不能 / 手動判定フォールバック):
         -> 出力本文をLLMが精読。修正が必要なら対応してループ先頭へ戻る。問題なければステップ12へ進む。
     - exit 2 (内部エラー):
         -> 異常終了。エラー内容を記録して停止。
```

### レビュー返信ファイル (`/tmp/review_reply.md`) の作成
指摘に対応した後は、修正内容をまとめた返信ファイルを作成します：
```markdown
## レビュー指摘への対応

### 対応内容
- [対応] 指摘事項Aのバグを修正し、テストを追加しました（コミット: abc1234）
- [見送り] 指摘事項Bは仕様に基づく設計のため現状維持とします（理由: ...）

@claude review
```

返信ファイル作成後、`wait_for_review.py` を `--body-file /tmp/review_reply.md` 付きで実行して再レビューをトリガーします。
