# Issue #37 実装計画

## 目的

`dispatch_scoring._wait_seconds` が GitHub の ISO 8601 時刻（末尾 `Z`）を
Python 3.11 未満相当のパーサーでも扱えるようにし、ディスパッチャーの
`ValueError` を防止する。

## 実装アプローチ

1. `tests/test_dispatch_scoring.py` に回帰テストを追加する。
   - 試行履歴がないタスクの `created_at` に `...Z` を指定する。
   - Python 3.10 相当として末尾 `Z` を拒否する `fromisoformat` 互換スタブを使い、
     修正前はIssue記載どおり `ValueError` になること（Red）を確認する。
   - 修正後はパーサーへ `...+00:00` が渡り、優先度計算が完了すること（Green）を確認する。
2. `orchestune/dispatch_scoring.py` の `_wait_seconds` で、既存の
   `dispatcher.py` と同様に `task.created_at.replace("Z", "+00:00")` を
   `datetime.fromisoformat` へ渡す。
3. 変更前の全テスト結果をベースラインとして記録し、修正後との差分を確認する。
4. 対象テスト、全テスト、`./scripts/local-ci.sh` を実行し、フォーマット・Lint・
   型検査・カバレッジ・秘密情報検査に新規の失敗がないことを確認する。
5. `walkthrough.md` を作成し、Issue #37を閉じるPR本文を準備する。作業ブランチを
   pushして `Closes #37` を含むPRを作成し、レビューを依頼する。

## 影響範囲

- 公開API・データモデルの変更なし。
- `created_at` が末尾 `Z` の場合のみ入力を等価なUTCオフセット表現へ正規化する。
- 既存の `+00:00` などオフセット付き時刻と、試行履歴を優先するスコアリング動作は維持する。

## 検証・受け入れ条件

- 互換スタブを用いた回帰テストが修正前に `ValueError` で失敗し、修正後に成功する。
- 既存の優先度計算テストが成功する。
- ベースラインに対して新規テスト失敗がない。
- ローカルCIが `✨ Local CI passed successfully!` で完了する。
- PRに再現結果、修正確認、ベースライン差分、Local CI結果、`Closes #37`を記載する。

## 前提・留意点

- 現行プロジェクトのPython要件は3.12だが、Issue #37の要求に従い旧Python互換の
  入力正規化を維持する。
- Issue #37は起票済みのため、新しいIssueは作成しない。
- 現在のローカル`main`は`origin/main`より2コミット遅れているため、実装開始時に
  最新`origin/main`を基点とする作業ブランチを作成する。
