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

## コード解析ツール（Serena MCP）

実装着手前の影響範囲調査に、[Serena](https://github.com/oraios/serena) をMCPサーバとして利用します（[#822](https://github.com/Saltmu/orchestune/issues/822)）。Pythonの言語サーバ（LSP）を介した型認識のシンボル・参照検索を提供するため、`depends_on` のように複数の型に同名で存在するフィールドを、テキスト検索と違って型ごとに区別して追跡できます。

接続設定はリポジトリ管理下の [`.mcp.json`](.mcp.json) にあり、バージョンは `serena-agent==1.7.0` に固定されています。**任意の導入**であり、未導入でも他の開発作業は行えます。

### 前提条件

[uv](https://docs.astral.sh/uv/getting-started/installation/) が必要です（`.mcp.json` は `uvx` でSerenaを起動します）。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

導入後、リポジトリ（またはその配下の worktree）でエージェントのセッションを開始し直すと、プロジェクトスコープのMCPサーバとして読み込まれます。MCPサーバはセッション開始時にのみロードされるため、`.mcp.json` を変更した場合はセッションの再起動が必要です。

### worktree と索引の対応

`--project-from-cwd` を指定しているため、Serenaはカレントディレクトリから上位へ辿り、`.serena/project.yml` または `.git`（**worktreeのポインタファイルを含む**）を持つ最近傍のディレクトリをプロジェクトルートとして解決します。`worktree/<BRANCH_SLUG>/` で起動したセッションはその worktree 自身をルートとするため、worktree 間で索引が混ざりません。

シンボルキャッシュは `<プロジェクトルート>/.serena/cache/<言語>/` に置かれ、`ファイルの相対パス → (内容のハッシュ, シンボル)` の形で保持されます。ハッシュが一致しないエントリは破棄され言語サーバへ再問い合わせされるため、branch を切り替えても古いcommitのシンボルが返ることはありません。手動での再索引は不要です。結果が明らかにおかしい場合のみ、その worktree の `.serena/cache/` を削除してください。

`.serena/`（索引・キャッシュ・memories）は `.gitignore` 済みです。コミットしないでください。

### フォールバックと無効化

MCPサーバが起動しない、または言語サーバが応答しない場合は、`rg`（ripgrep）や `grep` による既存のテキスト検索へフォールバックし、その旨を `implementation_plan.md` に明記して作業を継続してください。テキスト検索は同名シンボルを型で区別できないため、確認範囲を広めに取ります。ツールの不調でタスクを停止させないでください。

恒久的に無効化する場合は、エージェントのクライアント側でMCPサーバを無効化してください（Claude Codeでは `claude mcp` の設定、または `/mcp` からの切断）。

影響範囲の列挙・仕分け・実装後の照合の手順は [`skills/local-ci-developer/references/impact-scope.md`](skills/local-ci-developer/references/impact-scope.md) を参照してください。

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
