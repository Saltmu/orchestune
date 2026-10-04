# セットアップガイド

Orchestuneのインストール方法、各種AIアシスタント（Claude Code, Codex CLI, Antigravity）へのスキル登録方法、およびクラウド実行（Claude Code Cloud Routine）の設定手順について説明します。

---

## 0. 導入要件（Prerequisites）

Orchestuneは「エージェントが標準開発ワークフローに従って実装し、CIと子レビュー証跡ゲート（既定）を通過した子タスクが自動で統合される」ことを前提に設計されています。導入先のリポジトリが以下を満たしていない場合、期待通りのトレーサビリティ・品質は得られません。導入前に必ず確認してください。

1. **(a) エージェント規律を定義したファイルが存在すること**
   `AGENTS.md` / `CLAUDE.md` など、対象リポジトリでエージェントに遵守させたい開発ワークフロー（TDD、Issue起票、PR作成規約等）を明文化したファイルを用意してください。Orchestuneディスパッチャーが送るエージェントへの指示は「標準開発ワークフローに従って実装してください」という一文のみで、その実体の定義は導入先リポジトリ側の責務です。ゼロから用意する場合は、下記2章の `orchestune setup --with-workflow-skill` で汎用テンプレートを配置できます。
2. **(b) 自動マージを任せられる厚さの品質ゲート（CI）が存在すること**
   Orchestuneの子タスクレベルには人間によるレビューゲートが存在しません。CIに加え、子レビュー証跡ゲートが既定の `required` モードで自動ゲートとして働き、親ブランチ更新前に各子のレビュー合格証跡（`verdict=pass`、マージ対象SHAとの一致）を検証します（詳細は[使い方 §4.5](usage.md#45-子レビュー証跡ゲート)および[Architecture & Design](architecture.md)を参照）。ただし、`child-review-gate = "off"` を明示すると証跡検証は省略され、ゲートが有効でもレビュー判断はLLMによるものです。機械的な正しさの担保はCIに依存するため、スモークテスト程度のCIだけでは、自動で統合されるコードの品質を十分に担保できません。
3. **(c) `ci_command` を自リポジトリのCIエントリーポイントに設定すること**
   Integratorが統合ブランチ上で実行するCIコマンドの既定値は `./scripts/local-ci.sh`（Orchestune自身のリポジトリ固有の値）です。導入先リポジトリのCIエントリーポイントが異なる場合（例: `make ci`、`npm run ci`）は、`orchestune dispatch --ci-command "..."` または `orchestune.toml`/`pyproject.toml` の `[tool.orchestune]` セクションで `ci_command` を明示的に設定してください。

### 設定ファイルの作成と編集

プロジェクト固有の `orchestune.toml` は、対話式ウィザードまたは手動コピーで作成できます。実ファイルはローカル設定や認証先を含み得るためGit管理外とし、共有する変更は設定例へ反映してください（別リポジトリへ導入する場合も `.gitignore` に追加してください）。

#### 方法A: 対話式設定ウィザード（推奨）

```bash
# 新規作成
orchestune config init

# 既存設定の対話式編集
orchestune config edit

# 対象ディレクトリを明示指定する場合
orchestune config init --project-dir /path/to/project
```

- **初期値の引き継ぎと優先順位**:
  - `orchestune.toml` がない場合、`pyproject.toml` の `[tool.orchestune]` から設定・コメント・テーブル構造を引き継いで新規作成します。
  - 保存後、新しく作成された `orchestune.toml` は `pyproject.toml` 側の設定全体に優先します。
- **安全な保存と競合防止**:
  - 保存直前にウィザード協調ロック（`.orchestune/config-write.lock`）を取得し、読み込み時点のファイルスナップショットと照合します。対話中に他プロセスによってファイルが変更・作成された場合は競合（終了コード3）として保存を中止し、元ファイルを保護します。
  - 既存ファイルを更新する際は、元の内容を `orchestune.toml.bak.<UTC timestamp>.<uuid>` として自動バックアップしてから、アトミックにファイルを置換します。
- **取消操作**:
  - プレビュー画面での取消、または対話中の `Ctrl+C`（終了コード130）や EOF では、設定ファイルおよびバックアップファイルは一切変更・作成されません。

#### 方法B: 手動コピー

```bash
cp orchestune.toml.example orchestune.toml
```

```toml
# orchestune.toml の例（リポジトリルートの orchestune.toml.example も参照）
ci-command = "make ci"
```

---

## 1. インストール方法

OrchestuneはPython 3.12以上、uv、およびGitHub CLI（`gh auth status` で認証済みであること）が必要です。

### 別のプロジェクトでOrchestuneを利用する場合
`orchestune-dag` / `orchestune-dispatch` を別のプロジェクト（例: `manuscriptune` というプロジェクト）内でエージェントに実行させたい場合は、以下の2ステップでセットアップを行います。

#### ステップA: CLIのインストール

```bash
# グローバルにインストール（推奨・uv tool使用）
uv tool install "orchestune==<RELEASE_VERSION>"

# またはpipxを使用
pipx install "orchestune==<RELEASE_VERSION>"

# または導入先プロジェクトの開発依存として追加（uv）
uv add --dev orchestune
```

これにより、導入先プロジェクトのディレクトリから、統一された `orchestune` コマンド、および個別の `orchestune-dag` / `orchestune-dispatch` コマンドを実行できるようになります。

#### Windows環境での動作サポート
OrchestuneはWindows NT/10/11環境をネイティブサポートしています:
- **排他ロック**: POSIX環境では `fcntl`、Windows環境では `msvcrt` を用いたクロスプラットフォームな排他ロック (`file_lock`) を提供します。
- **開発・ローカルCI**: Orchestune自体の開発時（クローンしたリポジトリ内）は、PowerShellから `.\scripts\setup-git-hooks.ps1` および `.\scripts\local-ci.ps1` を使用できます（詳細は [CONTRIBUTING.ja.md](../../CONTRIBUTING.ja.md) を参照）。

---

## 2. エージェントへのスキル配布と管理

AIエージェントに `orchestune` / `orchestune-provision` / `orchestune-dispatch` の各スキルの存在を認識させる必要があります。Orchestuneはサポートされる各AIコーディングエージェント向けに専用のインストーラー（`orchestune skills`）を提供しています:
- **Codex CLI**: `.agents/skills/`（プロジェクト）または `~/.agents/skills/`（ユーザー）
- **Antigravity IDE**: `.agents/skills/`（プロジェクト）または `~/.gemini/config/skills/`（ユーザー）
- **Antigravity CLI**: `.agents/skills/`（プロジェクト）または `~/.gemini/antigravity-cli/skills/`（ユーザー）
- **Claude Code**: `.claude/skills/`（プロジェクト）または `~/.claude/skills/`（ユーザー）

> [!NOTE]
> Codex、Antigravity IDE、Antigravity CLI はプロジェクトスコープにおいて `.agents/skills/` ディレクトリを共有します。Orchestune は同一物理ディレクトリへの重複配置を自動で集約します。

### スキルのインストール (`orchestune skills install`)

```bash
# 変更内容を事前に確認（書き込みなし）
orchestune skills install --target all --scope project --dry-run

# プロジェクトへインストール（Gitでのチーム共有推奨）
orchestune skills install --target all --scope project

# ユーザー設定ディレクトリへグローバルにインストール
orchestune skills install --target all --scope user

# 特定のアシスタントのみを対象にインストール
orchestune skills install --target codex --scope project
```

プロジェクトスコープのスキル（`.agents/skills/` および `.claude/skills/`）は、チーム全体で共有するためにGitへコミットできます。ローカルのトランザクション状態やロックファイルは `.orchestune-installer/` 配下で管理され、`.gitignore` に含めて除外します。

#### `--with-workflow-skill`: 汎用ワークフロースキルのプロジェクトローカル配置

Python以外のリポジトリや、プロジェクト固有のエージェント規律をゼロから作成したい場合:

```bash
orchestune skills install --target all --scope project --with-workflow-skill
```

これにより、`workflow-template` がプロジェクトのスキルディレクトリへ実体コピーされます。規律ファイルはプロジェクト固有であるため、`workflow-template` をグローバル（`--scope user`）へ配置することはできません。

### スキルの管理と診断

```bash
# インストール済みスキルの状態確認
orchestune skills status --target all --scope project

# Orchestuneパッケージ更新後のスキル同期
orchestune skills update --target all --scope project

# スキルのアンインストール
orchestune skills uninstall --target all --scope project

# システム環境と設定の自己診断
orchestune skills doctor --target all --scope project --offline
```

> [!TIP]
> **旧セットアップからの移行**: 以前の `orchestune setup` コマンドや手動シンボリックリンクで配置していた場合は、`orchestune skills install --target all --scope user --migrate-legacy` を実行することで、管理された新形式へ安全に移行できます。旧 `orchestune setup` コマンドは非推奨となり、内部で `orchestune skills install` へ委譲されます。

### 作業用セッションディレクトリの作成 (`orchestune scratch create`)

サブエージェントやスキルが一時的な計画・分解・レビュー成果物を作成する際は、`orchestune scratch` コマンドを使用します:

```bash
# .orchestune/tmp/<artifact>-<issue-or-task>-<timestamp>-<uuid>/ を作成してパスを出力
orchestune scratch create plan 1191
```

このコマンドは対象プロジェクトの `.gitignore` に `.orchestune/tmp/` が指定されていることを確認し、一時ファイルの誤コミットを未然に防ぎます。

---

## 3. Claude Code Cloud Routine のセットアップ手順

> [!NOTE]
> `--dispatch-target` を明示指定しない場合、GitHub Actions実行環境（`GITHUB_ACTIONS=true`）では本セクションの `cloud-routine` が自動的に選択されます。GitHub Actions上でディスパッチャーを動かす場合は、以下の手順で事前に環境変数（Actions Secrets）を設定しておいてください。

> [!IMPORTANT]
> ディスパッチャーはルーチンをfireする前に、task branch（stacked/parent baseの内容を含む）を`origin`へpushし、到達性を検証するようになりました。これはクラウドセッションがリポジトリのdefault branchではなく正しいbaseから作業を開始できるようにするためです。そのため、ディスパッチャープロセスが使用するgit資格情報（ワークフロー内のcheckoutトークン等）には、リポジトリへの**push権限**（`contents: write`）が必要です。多くのCIワークフロー（本リポジトリ自身の`ci.yml`を含む）が既定で使う`permissions: contents: read`だけでは不足します。権限不足でpushが失敗した場合、対象タスクは`status:blocked`のままとなり、Issueへのコメントとしてgitのエラー内容が添付されます。

`--dispatch-target cloud-routine` は **Claude Code Cloud Routine** 用の実行先です。

1. **ルーチンの新規作成**:
   [claude.ai/code/routines](https://claude.ai/code/routines) を開き、「New routine」からルーチンを新規作成します。プロンプト本文は簡単な説明で構いません（実際の作業指示はディスパッチャーが起動のたびに都度送信します）。
2. **リポジトリの追加**:
   「Repositories」に、ディスパッチ対象のGitHubリポジトリを追加します（ルーチンは実行のたびにデフォルトブランチからこのリポジトリをcloneします）。
3. **APIトリガーの追加**:
   「Select a trigger」→「Add another trigger」から **API** トリガーを追加し、ルーチンを保存します。
4. **情報の取得**:
   保存後、同じ画面に表示されるURL（`https://api.anthropic.com/v1/claude_code/routines/<routine_id>/fire`）から `routine_id` を控え、「Generate token」でAPIトークンを発行します。
5. **環境変数の設定**:
   控えた `routine_id` とトークンを環境変数として設定します。GitHub ActionsなどのCI環境で実行する場合は、リポジトリの Actions Secrets に登録してください：
   ```bash
   export ORCHESTUNE_ROUTINE_ID="<routine_id>"
   export ORCHESTUNE_ROUTINE_TOKEN="<token>"
   ```

> [!NOTE]
> ディスパッチャーが生成するブランチ名は常に `claude/issue-<Issue番号>-<subtask_id>` という `claude/` プレフィックス付きの形式です。これはルーチン側のデフォルトのブランチpush制限（`claude/` プレフィックスのみpush許可）と一致するため、別途ブランチ制限を解除する必要はありません。

---

## 4. Codex Cloud のセットアップ手順

`--dispatch-target codex-cloud` は、Codex CLI を通じて設定済みの Codex Cloud environment にサブタスクを投入します。

1. [Codex Cloud](https://chatgpt.com/codex) で対象リポジトリを接続し、environment を作成します。
2. ローカルの `codex` CLI を同じ ChatGPT アカウントで認証します。
3. environment ID を環境変数または CLI オプションで渡します。

   ```bash
   export ORCHESTUNE_CODEX_CLOUD_ENV="<environment_id>"
   orchestune dispatch --dispatch-target codex-cloud
   # または
   orchestune dispatch --dispatch-target codex-cloud --codex-cloud-env "<environment_id>"
   ```

起動前にタスク用ブランチを `origin` へ push し、`codex cloud exec --env <environment_id> --branch <branch>` を非対話で実行します。投入後は実タスク ID / URL を追跡し、Cloud 上の実タスク状態（failed / cancelled 等の早期検知）および対象ブランチの PR / outcome record を用いて完了を判定します。environment ID が未設定の場合は、警告の上で安全なダミー起動へフォールバックします。

---

## 5. ローカルの`claude` / `agy` / `codex` CLIへのディスパッチ設定

> [!NOTE]
> `--dispatch-target` を明示指定しない場合、GitHub Actions以外（ローカル/対話実行）では `auto` が自動選択され、PATH上にインストールされている `claude`/`agy`/`codex` のいずれか（`claude`優先、次点`agy`、`codex`）へ自動的にディスパッチされます。いずれもインストールされていない場合は警告を出した上でダミー起動（no-op）にフォールバックします。特定のCLIに固定したい場合は、本セクションの `claude-cli`/`agy-cli`/`codex-cli` を明示指定してください。

### 前提: `claude` CLI（Claude Code）のインストール

本セクションのプリセットは、ローカルに `claude` コマンド（Claude Code CLI）がインストール済みでPATH上にあることを前提とします。未インストールの場合は、以下のいずれかの方法でインストールしてください（詳細は[公式ドキュメント](https://docs.claude.com/)を参照）：

```bash
# npm経由でグローバルインストール
npm install -g @anthropic-ai/claude-code
```

インストール後、`claude --version` でCLIが認識されることを確認してください。

`--local-cmd` テンプレートを手書きせずに、ローカルの`claude`・`agy`(Antigravity)・`codex`(Codex CLI) いずれかのCLIセッションへサブタスクをディスパッチするには、組み込みのプリセットを使用します：

```bash
orchestune dispatch --dispatch-target claude-cli
# または
orchestune dispatch --dispatch-target agy-cli
# または
orchestune dispatch --dispatch-target codex-cli
# インストール済みのCLIを自動検出させたい場合は --dispatch-target を省略するか auto を指定
orchestune dispatch --dispatch-target auto
```

これは各サブタスクの専用worktree内で `claude -p "..." --permission-mode bypassPermissions` / `agy -p "..." --add-dir . --print-timeout 60m --dangerously-skip-permissions` / `codex exec "..." --dangerously-bypass-approvals-and-sandbox`（非対話・print/execモード）を実行します。いずれのプリセットも、許可プロンプトのバイパスフラグを毎回付与することで無人実行がブロックされないようにしています。

既定では、解決済みの実行ターゲットからベンダークロスレビューの担当も決定します。`claude-cli`と`cloud-routine`はCodex、`codex-cli`・`codex-cloud`・`agy-cli`はClaudeへレビューを依頼します。この選択は`--dispatch-target auto`が具体的なターゲットへ解決された後に行われます。設定ファイル（`orchestune.toml`）の `reviewer-bot = "claude"` または `reviewer-bot = "codex"` で上書きできます。カスタム `local-cmd` では `{reviewer_bot}` プレースホルダーを利用でき、それ以外の任意コマンドにはレビュー指示を自動追記しません。

> [!IMPORTANT]
> **信頼モデルとセキュリティ上の危険性について**
> 
> これらのローカルCLIターゲットは、承認やサンドボックスをバイパスする完全権限で起動されます。暗黙的な完全権限実行を防ぐため、実行の際は明示的に `--allow-unsafe-agent-execution` CLIオプションを指定してオプトインする必要があります（安全のため設定ファイルでの指定は禁止されています）。オプトインがない場合は、起動時に設定エラーとなり実行が拒否されます（Fail-Closed）。
> 
> また、サブタスクごとの `git worktree` はソースコードの差分を分離するための境界であり、OSレベルのセキュリティ境界（サンドボックス）ではありません。完全権限で起動されたCLIプロセスは、実行ユーザーがアクセス可能な範囲のホームディレクトリ、認証情報、他のプロジェクトフォルダ、ネットワーク等に自由にアクセスできます。信頼できないリポジトリやIssueを処理する場合、または本番・共有環境で実行する場合は、コンテナや仮想マシン（VM）などのOSレベルの隔離層を併用することを強く推奨します。

別途、許可設定ファイルを準備するステップは不要です。`orchestune bootstrap`は必須のGitHubラベルの起票のみを行います。

---

## 6. GitHub Actions上での定期実行とcross-runner直列化

`orchestune dispatch` をGitHub Actionsのcron等で定期実行するワークフローを組む場合、`concurrency`グループの設定を強く推奨します。[統合パイプライン (architecture/integration.md)](architecture/integration.md#3-排他制御と設計前提)に記載の設計前提（#377）の通り、Integratorの排他は同一マシン上のファイルロック（`orchestune/infra/process_utils.py`の`file_lock`）でのみ成立しており、複数のCIランナー/マシンをまたいだ同時実行には効きません。`concurrency`グループを使えば、コード変更なしに、リポジトリ全体（＝全ランナー）で親Issue単位の直列化が得られます。

```yaml
concurrency:
  # 必須の親Issue単位でグループ化する。
  group: orchestune-integrate-${{ github.repository }}-${{ inputs.parent_issue }}
  # 必須: trueにするとCI実行中のIntegratorが中断され、temp branchとworktreeが
  # 残留する（`dispatch_gc`側の回収対象は増えるが、中断タイミング次第で親ブランチが
  # 中途半端に進む可能性がある）。
  cancel-in-progress: false
```

> [!NOTE]
> GitHub Actionsの`concurrency`は「実行中1本 + 待機1本」しか保持せず、3本目以降にトリガーされた待機中のrunはキャンセルされます。本設計ではこれは無害です。理由は、Dispatcherが毎サイクルGitHub（Issueラベル/PR/ブランチ）から状態を再構成する自己修復設計であるため、キューでキャンセルされたrunは次回のcron tickと状態的に等価だからです。「サイクルが失われて処理が止まる」ことを意味するものではなく、次のトリガーで同じ状態から処理が再開されます。

なお、本リポジトリ自身は現時点でOrchestuneのdispatchをGitHub Actionsのスケジュール実行では回しておらず（Cloud RoutineまたはローカルCLIへのディスパッチが前提）、上記は導入先リポジトリ向けの設定例です。本リポジトリで実際にcron定期実行を有効化する際は、上記`concurrency`設定を含むワークフローファイルを別途`.github/workflows/`へ追加してください。
