# セットアップガイド

Orchestuneのインストール方法、各種AIアシスタント（Claude Code, Codex CLI, Antigravity）へのスキル登録方法、およびクラウド実行（Claude Code Cloud Routine）の設定手順について説明します。

---

## 0. 導入要件（Prerequisites）

Orchestuneは「エージェントが標準開発ワークフローに従って実装し、CIが通ればそのまま自動マージされる」ことを前提に設計されています。導入先のリポジトリが以下を満たしていない場合、期待通りのトレーサビリティ・品質は得られません。導入前に必ず確認してください。

1. **(a) エージェント規律を定義したファイルが存在すること**
   `AGENTS.md` / `CLAUDE.md` など、対象リポジトリでエージェントに遵守させたい開発ワークフロー（TDD、Issue起票、PR作成規約等）を明文化したファイルを用意してください。Orchestuneディスパッチャーが送るエージェントへの指示は「標準開発ワークフローに従って実装してください」という一文のみで、その実体の定義は導入先リポジトリ側の責務です。ゼロから用意する場合は、下記2章の `orchestune setup --with-workflow-skill` で汎用テンプレートを配置できます。
2. **(b) 自動マージを任せられる厚さの品質ゲート（CI）が存在すること**
   Orchestuneの子タスクレベルには人間によるレビューゲートが存在せず、CIの合否が事実上唯一の品質ゲートになります（詳細は[Architecture & Design](architecture.md)を参照）。スモークテスト程度のCIしかないリポジトリに導入すると、無レビューのまま自動マージされるコードの品質を担保できません。
3. **(c) `ci_command` を自リポジトリのCIエントリーポイントに設定すること**
   Integratorが統合ブランチ上で実行するCIコマンドの既定値は `./scripts/local-ci.sh`（Orchestune自身のリポジトリ固有の値）です。導入先リポジトリのCIエントリーポイントが異なる場合（例: `make ci`、`npm run ci`）は、`orchestune dispatch --ci-command "..."` または `orchestune.toml`/`pyproject.toml` の `[tool.orchestune]` セクションで `ci_command` を明示的に設定してください。

```toml
# orchestune.toml の例（リポジトリルートの orchestune.toml.example も参照）
ci-command = "make ci"
```

---

## 1. インストール方法

OrchestuneはPython 3.12以上、Poetry、およびGitHub CLI（`gh auth status` で認証済みであること）が必要です。

### 別のプロジェクトでOrchestuneを利用する場合
`orchestune-dag` / `orchestune-dispatch` を別のプロジェクト（例: `manuscriptune` というプロジェクト）内でエージェントに実行させたい場合は、以下の2ステップでセットアップを行います。

#### ステップA: CLIのインストール

```bash
# グローバルにインストール（推奨・pipx使用）
pipx install git+https://github.com/Saltmu/orchestune.git

# または導入先プロジェクトの開発依存として追加（Poetry）
poetry add --group dev git+https://github.com/Saltmu/orchestune.git
```

これにより、導入先プロジェクトのディレクトリから、統一された `orchestune` コマンド、および個別の `orchestune-dag` / `orchestune-dispatch` コマンドを実行できるようになります。

#### Windows環境での動作サポート
OrchestuneはWindows NT/10/11環境をネイティブサポートしています:
- **排他ロック**: POSIX環境では `fcntl`、Windows環境では `msvcrt` を用いたクロスプラットフォームな排他ロック (`file_lock`) を提供します。
- **開発・ローカルCI**: Orchestune自体の開発時（クローンしたリポジトリ内）は、PowerShellから `.\scripts\setup-git-hooks.ps1` および `.\scripts\local-ci.ps1` を使用できます（詳細は [CONTRIBUTING.ja.md](../../CONTRIBUTING.ja.md) を参照）。

---

## 2. エージェントへのスキル定義の登録

AIエージェントに `orchestune` / `orchestune-provision` / `orchestune-dispatch` / `local-ci-developer` の各スキルの存在を認識させる必要があります。以下のいずれかの方法を選んでください。

### 方法A: 自動セットアップ（推奨）
セットアップコマンドを実行するだけで、サポートされているすべてのAIアシスタント（Claude Code、Codex CLI、Antigravity）のグローバル設定ディレクトリに対して、自動的にシンボリックリンクを作成します。`local-ci-developer` は自動リンクの対象外です。

```bash
orchestune setup
```

#### `--with-workflow-skill`: 汎用ワークフロースキルのプロジェクトローカル配置

上記の「0. 導入要件」(a)にある、エージェント規律を定義したファイルをゼロから用意したい場合は、`--with-workflow-skill` オプションを付けて実行します。

```bash
orchestune setup --with-workflow-skill
```

- `skills/workflow-template/SKILL.md`（`local-ci-developer` からPython/Poetry固有のコマンドを一般化したテンプレート）を、検出されたアシスタントごとに**プロジェクトローカル**な `.claude/skills/`・`.codex/skills/`・`.gemini/config/skills/` 配下へ**実体コピー**します（シンボリックリンクではありません。コピー元はOrchestuneパッケージ内にしか存在せず、対象プロジェクト内には存在しないため）。
- `workflow-template` は `local-ci-developer` と同様、この規律がプロジェクト固有であるべきという理由からグローバル自動リンクの対象外です。オプションを付けない通常の `orchestune setup` の挙動には影響しません。
- 配置後、テンプレート内の `<TEST_COMMAND>` / `<FORMAT_LINT_COMMAND>` / `<TYPE_CHECK_COMMAND>` / `<CI_ENTRYPOINT>` プレースホルダーを、対象プロジェクトの実際のコマンドに置き換えてから使用してください（`<CI_ENTRYPOINT>` は上記(c)の `ci_command` 設定と一致させることを推奨します）。フォルダ名・スキル名も自由に変更できます。

### 方法B: 手動セットアップ（プロジェクト単位またはグローバル）

* **`.agents/skills.json`** （Antigravity向け）:
  導入先プロジェクトの `.agents/skills.json` に、本リポジトリの `skills/` ディレクトリへのパスを指定します：
  ```json
  {
    "entries": [
      { "path": "../path/to/cloned/orchestune/skills" }
    ]
  }
  ```

* **プロジェクトローカルスキル** （Claude Code、Codex CLI向け）:
  両エージェントとも、`.claude/skills/<name>/`・`.codex/skills/<name>/` 配下に置かれたスキルを自動検出します。導入先プロジェクトで、スキルフォルダをシンボリックリンクまたはコピーしてください：
  ```bash
  ln -s ../path/to/cloned/orchestune/skills/orchestune .claude/skills/orchestune
  ln -s ../path/to/cloned/orchestune/skills/orchestune .codex/skills/orchestune
  ```

* **グローバルスキルディレクトリ**:
  プロジェクトごとの設定なしにどこでも使えるようにしたい場合は、スキルフォルダをエージェントのグローバルスキルディレクトリに配置（またはシンボリックリンク作成）します：
  * **Claude Code**: `~/.claude/skills/orchestune/`
  * **Codex CLI**: `~/.codex/skills/orchestune/`
  * **Antigravity**: `~/.gemini/config/skills/orchestune/`

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

既定では、解決済みの実行ターゲットからベンダークロスレビューの担当も決定します。`claude-cli`と`cloud-routine`はCodex、`codex-cli`・`codex-cloud`・`agy-cli`はClaudeへレビューを依頼します。この選択は`--dispatch-target auto`が具体的なターゲットへ解決された後に行われます。`--reviewer-bot claude`または`--reviewer-bot codex`（TOMLでは`reviewer-bot = "..."`）で上書きできます。カスタム`--local-cmd`では`{reviewer_bot}`プレースホルダーを利用でき、それ以外の任意コマンドにはレビュー指示を自動追記しません。

> [!IMPORTANT]
> **信頼モデルとセキュリティ上の危険性について**
> 
> これらのローカルCLIターゲットは、承認やサンドボックスをバイパスする完全権限で起動されます。暗黙的な完全権限実行を防ぐため、実行の際は明示的に `--allow-unsafe-agent-execution` オプションを指定するか、設定ファイル（`orchestune.toml`等）で `allow_unsafe_agent_execution = true` を指定してオプトインする必要があります。オプトインがない場合は、起動時に設定エラーとなり実行が拒否されます（Fail-Closed）。
> 
> また、サブタスクごとの `git worktree` はソースコードの差分を分離するための境界であり、OSレベルのセキュリティ境界（サンドボックス）ではありません。完全権限で起動されたCLIプロセスは、実行ユーザーがアクセス可能な範囲のホームディレクトリ、認証情報、他のプロジェクトフォルダ、ネットワーク等に自由にアクセスできます。信頼できないリポジトリやIssueを処理する場合、または本番・共有環境で実行する場合は、コンテナや仮想マシン（VM）などのOSレベルの隔離層を併用することを強く推奨します。

別途、許可設定ファイルを準備するステップは不要です。`orchestune bootstrap`は必須のGitHubラベルの起票のみを行います。

---

## 6. GitHub Actions上での定期実行とcross-runner直列化

`orchestune dispatch` をGitHub Actionsのcron等で定期実行するワークフローを組む場合、`concurrency`グループの設定を強く推奨します。[統合パイプライン (architecture/integration.md)](architecture/integration.md#3-排他制御と設計前提)に記載の設計前提（#377）の通り、Integratorの排他は同一マシン上のファイルロック（`orchestune/infra/process_utils.py`の`file_lock`）でのみ成立しており、複数のCIランナー/マシンをまたいだ同時実行には効きません。`concurrency`グループを使えば、コード変更なしに、リポジトリ全体（＝全ランナー）で親Issue単位の直列化が得られます。

```yaml
concurrency:
  # 親Issue単位でグループ化する。--parent-issueを指定しないフラットモードでは
  # integration/temp-mainが共有資源になるため、'flat'という固定キーで直列化する。
  group: orchestune-integrate-${{ github.repository }}-${{ inputs.parent_issue || 'flat' }}
  # 必須: trueにするとCI実行中のIntegratorが中断され、temp branchとworktreeが
  # 残留する（`dispatch_gc`側の回収対象は増えるが、中断タイミング次第で親ブランチが
  # 中途半端に進む可能性がある）。
  cancel-in-progress: false
```

> [!NOTE]
> GitHub Actionsの`concurrency`は「実行中1本 + 待機1本」しか保持せず、3本目以降にトリガーされた待機中のrunはキャンセルされます。本設計ではこれは無害です。理由は、Dispatcherが毎サイクルGitHub（Issueラベル/PR/ブランチ）から状態を再構成する自己修復設計であるため、キューでキャンセルされたrunは次回のcron tickと状態的に等価だからです。「サイクルが失われて処理が止まる」ことを意味するものではなく、次のトリガーで同じ状態から処理が再開されます。

なお、本リポジトリ自身は現時点でOrchestuneのdispatchをGitHub Actionsのスケジュール実行では回しておらず（Cloud RoutineまたはローカルCLIへのディスパッチが前提）、上記は導入先リポジトリ向けの設定例です。本リポジトリで実際にcron定期実行を有効化する際は、上記`concurrency`設定を含むワークフローファイルを別途`.github/workflows/`へ追加してください。
