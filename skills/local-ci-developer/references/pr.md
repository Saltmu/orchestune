# Pull Request Reference (Step 10)

本ドキュメントは、ローカルCI通過後のPull Request（PR）作成手順を定めたリファレンスです。

---

## 10. PR作成手順 (Pull Request Finalization)

### ① PR本文ファイルの準備
リポジトリのPRテンプレート（`.github/pull_request_template.md`）を作業用ファイル（例: `/tmp/pr_body.md`）へコピーし、全項目を記入します。
- **Walkthrough**: 実施した設計変更・モジュール修正の概要。
- **再現手順と修正確認**: ステップ3のReproducerの検証結果（新規機能や軽微変更は「該当なし」と記載）。
- **ベースライン差分**: `scripts/ci_baseline.py` の結果（新規リグレッションなし、ベースブランチ由来の failure 有無）。
- **検証結果**: ローカルCIの合格エビデンス。

> [!NOTE]
> 軽微変更（typoやドキュメント修正のみ）の場合は、Reproducerおよびテスト項目欄に「軽微変更のため該当なし」と明記してスキップします。

### ② PRの送信
`gh` CLI を使用してPRを作成します。
```bash
gh pr create --title "PRのタイトル" --body-file /tmp/pr_body.md
```
*(注: `gh` CLI が利用できない環境では、GitHub MCP または Web UI から同内容で作成してください。)*

PR作成が完了したら、発行されたPR番号を記録し、ステップ11（レビューループ）へ進みます。
