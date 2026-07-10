# PRテンプレート準拠の強制 Walkthrough

## 変更内容

- `local-ci-developer` のPR作成手順で、`.github/pull_request_template.md` を作業用本文へコピーして全項目を記入することを必須化した。
- プレースホルダー、空の箇条書き、未判定チェックボックスを残さない確認手順を追加した。
- GitHub CLI、GitHub MCP、Web UIの各作成経路でテンプレート構造を維持するよう明記した。
- スキルのfrontmatterから現行仕様で許可されない旧メタデータを除去した。

## 検証

- `quick_validate.py skills/local-ci-developer`: passed (`Skill is valid!`)
- `git diff --check`: passed
- `./scripts/local-ci.sh`: passed
  - Ruff format/lint: passed
  - Mypy: passed
  - Pytest: 323 passed
  - Coverage: 91.72%
  - Gitleaks: ローカル未導入のためスキップ（CIで実行予定）

## 影響範囲

- ドキュメント化された開発ワークフローのみを変更する。
- アプリケーションコード、公開API、実行時動作は変更しない。
