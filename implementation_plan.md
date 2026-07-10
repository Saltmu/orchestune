# Issue #35 実装計画

## 目的

Issue番号との紐づけによって検出した既存PRについて、重複起動判定が実際の
ヘッドブランチの最新SHAを参照するよう修正し、別名ブランチ上の既存PRと
新規エージェントが競合することを防ぐ。

## 実装アプローチ

1. `tests/test_dispatcher.py` に、完了履歴があるIssueと想定名とは異なる
   `existing_pr.head_ref` の既存PRを使う回帰テストを追加する。
2. 修正前に、想定ブランチを問い合わせてタスクを再起動すること（Red）を確認する。
3. `orchestune/dispatcher.py` の `git ls-remote` 参照先を
   `existing_pr.head_ref` に変更し、起動が抑止されること（Green）を確認する。
4. 全テストと `./scripts/local-ci.sh` を実行し、ベースラインとの差分を確認する。
5. `walkthrough.md` を作成し、`Closes #35` を含むPRを作成する。

## 影響範囲

- 既存PR検出後の重複起動判定における `git ls-remote` の参照先のみ。
- 公開API・データモデル、既存のラベル遷移、コメント処理は変更しない。
- `git ls-remote` が例外で失敗した場合に安全側で起動を抑止する既存方針は維持する。

## 検証・受け入れ条件

- `git ls-remote origin refs/heads/<existing_pr.head_ref>` が呼ばれる。
- リモートSHAと完了履歴のSHAが異なる場合、タスクを起動せず
  `status:blocked-human-review` へ遷移する。
- 回帰テストが修正前に失敗し、修正後に成功する。
- ベースラインに対する新規失敗がなく、ローカルCIが成功する。
- PRに再現結果、ベースライン差分、Local CI結果、`Closes #35` を記載する。

## 前提

- Issue #35は起票済みのため、新規Issueは作成しない。
- 最新 `origin/main` を基点とする `fix/issue-35-existing-pr-head-ref` で作業する。
