# Orchestuneへのコントリビュート

[English](CONTRIBUTING.md) | [日本語](CONTRIBUTING.ja.md)

このドキュメントでは、Orchestune自体の開発環境のセットアップ方法を説明します。別のプロジェクトでOrchestuneを*利用*したいだけの場合は、[README](README.ja.md)を参照してください。

## セットアップ

Python 3.12以上、uv、Node.js（[Node.js と Quint](#nodejs-と-quint) を参照）、GitHub CLI（`gh auth status`）がインストールされていることを確認し、依存関係をインストールします。

```bash
uv sync
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

## Node.js と Quint

Node.js は **local CI の必須依存**です。依存解決の Quint チェック（モデルのサンプリング探索と、生成したトレースの本番ハーネスでの再生。[verification-contracts.md](docs/ja/verification-contracts.md#quint-model) を参照）を実行するためです。Node.js がないと `./scripts/local-ci.sh` / `.\scripts\local-ci.ps1` は Exit 2 で止まり、この手順を表示します。チェックを skip することはありません。

[`package.json`](package.json) の `engines` に書かれた **Node.js の major 版**（現在は LTS の 24）を、npm とともに導入してください。

* **Linux**: パッケージマネージャーまたは公式ビルドを使い、リリースの `SHASUMS256.txt`（https://nodejs.org/dist/）に載っている SHA-256 を確認します。たとえばアーカイブを `~/.local` に展開し、`node`・`npm`・`npx` を `PATH` の通ったディレクトリへリンクします。
* **macOS**: `brew install node@24`、または https://nodejs.org/ のインストーラー。
* **Windows**: `winget install OpenJS.NodeJS.LTS`、または https://nodejs.org/ のインストーラー。導入後に PowerShell を開き直してください。
* **WSL**: WSL の中に導入します。`/mnt/c/...` 経由で見える Windows の Node.js は Linux の Node.js ではなく、当てにしないでください。
* **Claude Code on the web**: 操作は不要です。[`.claude/hooks/session-start.sh`](.claude/hooks/session-start.sh) が、Node.js がない、または major 版が違うときに、固定した版を公式配布物から導入し（SHA-256 はその `SHASUMS256.txt` と照合）、続けて下のチェックを実行します。hook は nodejs.org と npm registry に到達できる必要があります。Web 環境のネットワーク設定が両方を許可しているかは、ここでは確認していません。許可されていない場合、hook は非0の終了コードで目に見える形で失敗し、ホストを許可するまで local CI は上のメッセージで止まります。

そのあと、ロックしたツールを導入して確認します。

```bash
./scripts/quint-check.sh        # Windows: .\scripts\quint-check.ps1
```

`npm ci` を実行し（Quint は `package.json` と `package-lock.json` で exact に固定してあり、グローバルの Quint は使いません）、導入された版が固定した版であることを確認します。`local-ci` が自動で実行します。Exit 2 は Node.js がない、または major 版が違うこと、Exit 1 は導入または版の確認に失敗したことを表します。

## コード解析ツール（Serena MCP）

実装着手前の影響範囲調査に、[Serena](https://github.com/oraios/serena) をMCPサーバとして利用します（[#822](https://github.com/Saltmu/orchestune/issues/822)）。Pythonの言語サーバ（LSP）を介した型認識のシンボル・参照検索を提供するため、`depends_on` のように複数の型に同名で存在するフィールドを、テキスト検索と違って型ごとに区別して追跡できます。

接続設定はリポジトリ管理下で、バージョンは `serena-agent==1.7.0` に固定されています。JSON形式のMCP設定を読むクライアントは [`.mcp.json`](.mcp.json) を、Codexは [`.codex/config.toml`](.codex/config.toml) を読みます。両方の起動コマンドは同一に保ってください。**任意の導入**であり、未導入でも他の開発作業は行えます。

### 前提条件

[uv](https://docs.astral.sh/uv/getting-started/installation/) が必要です（`.mcp.json` と `.codex/config.toml` は `uvx` でSerenaを起動します）。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

導入後、リポジトリ（またはその配下の worktree）でエージェントのセッションを開始し直すと、プロジェクトスコープのMCPサーバとして読み込まれます。MCPサーバはセッション開始時にのみロードされるため、設定を変更した場合はセッションの再起動が必要です。Codexでは `codex mcp list`（TUIでは `/mcp`）で検出を確認してください。`.mcp.json` だけでは Codex は設定されません。

### worktree と索引の対応

`--project-from-cwd` を指定しているため、Serenaはカレントディレクトリから上位へ辿り、`.serena/project.yml` または `.git`（**worktreeのポインタファイルを含む**）を持つ最近傍のディレクトリをプロジェクトルートとして解決します。`worktree/<BRANCH_SLUG>/` で起動したセッションはその worktree 自身をルートとするため、worktree 間で索引が混ざりません。

シンボルキャッシュは `<プロジェクトルート>/.serena/cache/<言語>/` に置かれ、`ファイルの相対パス → (内容のハッシュ, シンボル)` の形で保持されます。ハッシュが一致しないエントリは破棄され言語サーバへ再問い合わせされるため、branch を切り替えても古いcommitのシンボルが返ることはありません。手動での再索引は不要です。結果が明らかにおかしい場合のみ、その worktree の `.serena/cache/` を削除してください。

`.serena/`（索引・キャッシュ・memories）は `.gitignore` 済みです。コミットしないでください。

### フォールバックと無効化

MCPサーバが起動しない、または言語サーバが応答しない場合は、`rg`（ripgrep）や `grep` による既存のテキスト検索へフォールバックし、その旨をセッション固有の implementation plan に明記して作業を継続してください。テキスト検索は同名シンボルを型で区別できないため、確認範囲を広めに取ります。ツールの不調でタスクを停止させないでください。

エージェントの一時成果物は Git 管理外の `.orchestune/tmp/` 配下に置きます。セッションごとに `<artifact>-<issue-or-task>-<UTC timestamp>-<random>`（UTC は `YYYYMMDDTHHMMSSZ`、random は UUID 等）というディレクトリを作り、計画、PR本文、レビュー返信を格納してください。リポジトリ直下や OS グローバルの `/tmp` に固定名で作成してはいけません。

恒久的に無効化する場合は、エージェントのクライアント側でMCPサーバを無効化してください（Claude Codeでは `claude mcp` の設定、または `/mcp` からの切断）。

影響範囲の列挙・仕分け・実装後の照合の手順は [`skills/local-ci-developer/references/impact-scope.md`](skills/local-ci-developer/references/impact-scope.md) を参照してください。

## テストの実行

`pytest`を使用して、ユニットテストを実行します。
```bash
uv run pytest
```

ローカルの開発ループを軽くするため、デフォルトの`pytest`実行にはカバレッジ計装を含めていません。カバレッジを確認する場合は、以下のように明示的にオプションを指定してください（`local-ci.sh`もこのオプション付きで実行します）。
```bash
uv run pytest --cov=orchestune --cov-branch --cov-report=term-missing
```

### 内部シンボルのモック化

`unittest.mock.patch(...)` / `patch.object(...)` の対象が `orchestune.` または `scripts.` 配下のシンボルである場合（`subprocess` や `time.time`、`os.kill` のようなプロセス・OS境界、あるいは `FakeForge` のようなテストダブルは対象外）、`autospec=True` を指定してください（[#829](https://github.com/Saltmu/orchestune/issues/829)）。指定しない場合でも、対象シンボルの**改名・削除**は `patch(...)` の既定動作（`AttributeError`）で検知できますが、**シグネチャの変更**（引数の追加・削除・並び替え）は検知できません。モックは呼び出し方に関わらず何でも受け付けてしまうため、実体側の引数が変わってもテストは無反応で通過します。

```python
# 改名は検知できるが、引数の変更は検知できない:
with patch("orchestune.dispatch.worktree._branch_exists") as mock_exists:
    ...

# 実シグネチャから生成されたモックのため、引数の変更も検知できる:
with patch("orchestune.dispatch.worktree._branch_exists", autospec=True) as mock_exists:
    ...
```

以下の場合は `autospec=True` は適用対象外であり、付けない:
- 対象が既存の除外基準に該当するプロセス・OS・ライブラリ境界（`subprocess`、`os.kill`/`environ`/`getpid`、`time.time`/`sleep`/`monotonic`、`shutil`、`urllib`、`fcntl`、`msvcrt`、`pathlib.Path.cwd`/`home`）またはテストダブル（`FakeForge`、`fake_forge_proxy.*`、`MagicMock` ベースのフィクスチャ）である場合 — これらは設計上の境界・フェイクであり、検証すべき内部契約ではない。
- `new=`/`new_callable=` で呼び出し側が自前の差し替えオブジェクトを渡している場合（autospecが参照する「実シグネチャ」が存在しない）。
- 対象が非callable属性（例: `__file__`）である場合 — autospecはcallable向け。
- 対象がbuiltinやプラットフォーム条件付き属性（例: `open`、Windows限定の`ctypes`ハンドル）でモジュール上に常時存在するとは限らず `create=True` が必要な場合 — autospecは属性の実在を前提に内省するため適用できない。

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
  Windows環境では既定で 2 ワーカーによる並列テスト（`-n 2`）が実行されます。環境変数 `ORCHESTUNE_TEST_WORKERS`（例: `$env:ORCHESTUNE_TEST_WORKERS = "4"` や `"0"`）または `PYTEST_ADDOPTS` を設定することで並列度を調整できます。
このスクリプトは以下のチェックを実行します。
1. **Ruff フォーマット & Lint チェック**: `ruff format` と `ruff check`
2. **Mypy 型チェック**: 型注釈の検証
3. **Quint ツール**: `scripts/quint-check.sh` / `.ps1` が `npm ci` を実行し、固定した Quint を確認します（[Node.js と Quint](#nodejs-と-quint) を参照）
4. **Pytest カバレッジチェック**: テストが通過し、カバレッジが90%以上であることを保証します。モデルの有界探索と、そのトレースの本番ハーネスでの再生も含み、seed・境界・ツールの版・件数をスクリプトが表示します
5. **シークレット・ローカルパススキャン**（`gitleaks`）: シークレットや `file:///home/<user>/...` のような絶対ローカルパスの漏洩を含むコミット・プッシュをブロックします。設定は[`.gitleaks.toml`](.gitleaks.toml)を参照してください。`local-ci.sh` / `.ps1`はgitleaksが未インストールの場合、自動インストールを試みます（`scripts/install-gitleaks.sh` / `.ps1`）。それでもインストールできない場合はスキップせずエラーで停止するため、push前に必ずこのチェックが実行されます。リモートCIでも念のため再検証されます。
