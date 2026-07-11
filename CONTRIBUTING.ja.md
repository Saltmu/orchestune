# Orchestuneへのコントリビュート

[English](CONTRIBUTING.md) | [日本語](CONTRIBUTING.ja.md)

このドキュメントでは、Orchestune自体の開発環境のセットアップ方法を説明します。別のプロジェクトでOrchestuneを*利用*したいだけの場合は、[README](README.ja.md)を参照してください。

## セットアップ

Python 3.12以上、Poetry、GitHub CLI（`gh auth status`）、[gitleaks](https://github.com/gitleaks/gitleaks#installing)がインストールされていることを確認し、依存関係をインストールします。

```bash
poetry install
```

続けて、以下を実行してGit hooksをローカルにインストールしてください。これにより `git push` の直前に `./scripts/local-ci.sh`（gitleaksスキャンを含む）が自動実行され、失敗時はpushがブロックされます。

```bash
./scripts/setup-git-hooks.sh
```

## テストの実行

`pytest`を使用して、ユニットテストとカバレッジ測定を実行します。
```bash
poetry run pytest
```

## ローカルCIスクリプト

コミットまたはプッシュする前に、ローカルCIスクリプトを実行してフォーマット、型チェック、およびテストを確認します。
```bash
./scripts/local-ci.sh
```
このスクリプトは以下のチェックを実行します。
1. **Ruff フォーマット & Lint チェック**: `ruff format` と `ruff check`
2. **Mypy 型チェック**: 型注釈の検証
3. **Pytest カバレッジチェック**: テストが通過し、カバレッジが75%以上であることを保証
4. **シークレット・ローカルパススキャン**（`gitleaks`）: シークレットや `file:///home/<user>/...` のような絶対ローカルパスの漏洩を含むコミット・プッシュをブロックします。設定は[`.gitleaks.toml`](.gitleaks.toml)を参照してください。gitleaksがローカルにインストールされていない場合、このスクリプトはスキップせずエラーで停止するため、push前に必ずこのチェックが実行されます。リモートCIでも念のため再検証されます。
