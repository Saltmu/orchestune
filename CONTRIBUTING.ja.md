# Orchestuneへのコントリビュート

[English](CONTRIBUTING.md) | [日本語](CONTRIBUTING.ja.md)

このドキュメントでは、Orchestune自体の開発環境のセットアップ方法を説明します。別のプロジェクトでOrchestuneを*利用*したいだけの場合は、[README](README.ja.md)を参照してください。

## セットアップ

Python 3.12以上、Poetry、GitHub CLI（`gh auth status`）がインストールされていることを確認し、依存関係をインストールします。

```bash
poetry install
```

続けて、以下を実行してGit pre-commitフックをローカルにインストールしてください。これにより `.gitignore` 対象ファイルの誤コミットが自動的にブロックされます（また、過去にインストールされた古い `pre-push` フックがあれば自動的に削除されます）。

* **POSIX (Linux / macOS)**:
  ```bash
  ./scripts/setup-git-hooks.sh
  ```
* **Windows (PowerShell)**:
  ```powershell
  .\scripts\setup-git-hooks.ps1
  ```

`setup-git-hooks`は、[gitleaks](https://github.com/gitleaks/gitleaks#installing)がまだ`PATH`上に無ければ`~/.local/bin`へ自動インストールします（`scripts/install-gitleaks.sh` / `.ps1`を参照）。`local-ci.sh` / `.ps1`側でも同様の自動リトライを行うため、新規環境で`gitleaks`が未インストールであることがローカルCI実行の妨げにはなりません。自動インストールに失敗した場合（ネットワーク未接続、未対応のOS/アーキテクチャ等）は、上記リンクから手動でインストールしてください。

## テストの実行

`pytest`を使用して、ユニットテストを実行します。
```bash
poetry run pytest
```

ローカルの開発ループを軽くするため、デフォルトの`pytest`実行にはカバレッジ計装を含めていません。カバレッジを確認する場合は、以下のように明示的にオプションを指定してください（`local-ci.sh`もこのオプション付きで実行します）。
```bash
poetry run pytest --cov=orchestune --cov-branch --cov-report=term-missing
```

## ローカルCIスクリプト

コミットまたはプッシュする前に、ローカルCIスクリプトを実行してフォーマット、型チェック、およびテストを確認します。
* **POSIX (Linux / macOS)**:
  ```bash
  ./scripts/local-ci.sh
  ```
* **Windows (PowerShell)**:
  ```powershell
  .\scripts\local-ci.ps1
  ```
このスクリプトは以下のチェックを実行します。
1. **Ruff フォーマット & Lint チェック**: `ruff format` と `ruff check`
2. **Mypy 型チェック**: 型注釈の検証
3. **Pytest カバレッジチェック**: テストが通過し、カバレッジが90%以上であることを保証
4. **シークレット・ローカルパススキャン**（`gitleaks`）: シークレットや `file:///home/<user>/...` のような絶対ローカルパスの漏洩を含むコミット・プッシュをブロックします。設定は[`.gitleaks.toml`](.gitleaks.toml)を参照してください。`local-ci.sh` / `.ps1`はgitleaksが未インストールの場合、自動インストールを試みます（`scripts/install-gitleaks.sh` / `.ps1`）。それでもインストールできない場合はスキップせずエラーで停止するため、push前に必ずこのチェックが実行されます。リモートCIでも念のため再検証されます。
