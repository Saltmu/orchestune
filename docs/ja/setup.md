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

[統合パイプライン (architecture/integration.md)](architecture/integration.md#3-排他制御と設計前提)に記載の通り、ローカルのファイルロックは複数のCIランナー/マシンをまたいだ同時実行を守りません。このため、`orchestune dispatch` を定期実行する場合は、次の単一実行者契約を運用で満たしてください。

### 6.1 単一実行者契約（サポート範囲）

標準サポート構成は「対象リポジトリにつき同時に1つの制御実行者」です。対象の親Issueがどれであっても、同じリポジトリを扱う全dispatch入口を同一の直列化スコープへ入れます。親単位の並行制御とリポジトリ横断（アカウント共通）のquota保護は対象外です。

| 資源／処理 | 必須の運用条件 | 既存機構の保証範囲 |
| --- | --- | --- |
| 候補取得、依存・footprint判定、起動予約、ラベル／本文更新、起動・recovery・GC | 同一リポジトリの制御処理を直列化 | run-stateロックは同一ローカル資源のみ。別path・別clone・別マシンを保護しない |
| not-needed review、Integrator、Semantic Reviewの起動／復旧、親ブランチ更新、子／親Issue完了 | dispatch開始から後処理終了まで直列化を維持 | 各ローカルロック／ref競合検出は局所的な防御 |
| run-state、intent journal、review状態、worktree、実行中PID | 1つの運用所有者が継続して管理し、移管時に状態を引き継ぐ | 同時実行を止めるだけではローカル状態の消失・他マシンのPIDを復元できない |

- 「単一実行者」は制御処理の契約です。選ばれた子タスクの開発エージェントは並列に動いてかまいません。通常の `claim`／`complete` も禁止しません（それぞれの既存の所有権・状態ロック契約に従います）。
- 定期実行する `orchestune gc`（既定で予約解放を適用します）は、dispatchと同じ直列化スコープ（Actionsでは同じgroup）へ入れます。`orchestune recover --apply` などの一回限りの操作は、制御実行者の停止を確認してから行います。
- 契約外の構成（独立した状態からの同時実行）では、起動予約の上書きや同一タスクの重複選出が起こり得ます。
- run-stateロックは `execute_cycle` のサイクル部分だけを覆い、その後のnot-needed reviewのポーリング・Integrator（Semantic Reviewを含む）・親Issue完了・報告は覆いません。CLI全体の直列化は、単一所有者またはActionsのgroupという運用で担保します。ローカルロックがCLI全体を保護しているわけではありません。

### 6.2 所有者の単位と移管

- 所有者の単位は run-state の解決先です。同じcloneのlinked worktreeやサブディレクトリからの起動は、`claim/workspace.py:resolve_claim_workspace` によりprimary checkoutの `run_state.json`／`run_state.lock` へ解決されるため、同じ所有者として扱われます。dispatchに `--run-state` 引数はなく、配置は設定の `run_state_path`（相対パスはprimary checkout基準）で決まります。
- 別clone、`run_state_path` に別の絶対パスを指定した構成、別マシンは独立した所有者です。共有ファイルシステム上のロックは保証しません。
- ローカル運用では、1つの常駐所有者／スケジューラーからCLI全体（後処理を含む）を直列に起動します。サイクルロックがあることを理由に、複数CLIの同時起動をサポート構成にはしません。
- 移管手順: (1) 新規tickを止める → (2) 旧制御プロセスと後処理の停止を確認 → (3) 実行中タスク・予約・worktreeを確認して状態を引き継ぐ → (4) 新しい所有者を開始。停止を確認できない場合は、新しい所有者を自動で開始しません。
- ActionsとローカルCLI／Cloud Routine／外部cronの併用、forkなど別リポジトリのworkflowから同じ対象を更新する構成は、Actionsのgroupでは守れません（非保護）。

### 6.3 GitHub Actionsでの構成要件

標準のconcurrencyは次のとおりです（mapping形で、文字列の短縮形にはしません）。

```yaml
concurrency:
  group: orchestune-control-${{ github.repository }}
  cancel-in-progress: false
```

- groupに親Issue番号・`github.workflow`・branch/ref・run ID・runner名を入れません。`schedule` と `workflow_dispatch` は同じgroupにします。複数のworkflowが同じ対象を制御する場合も同じgroupにします。jobごとの独立groupや親ごとのgroupは標準構成にしません。文字列短縮形（`concurrency: <group>`）は `cancel-in-progress` を明示できないため使いません。
- workflowのトップレベルで、診断からdispatch・後処理の終了までを1つのrunに収めます。バックグラウンド化、子workflowへの非同期の引き渡し、matrixによる複製はしません。
- 待機中のrunは新しいrunに置き換えられ得ます（GitHubの既定のqueue挙動で、FIFOや全tickの実行は保証されません）。実行中のrunは `cancel-in-progress: false` により中断されませんが、手動cancel・timeout・runner消失は防げません。異常終了後は既存のrecovery手順で状態を確認してください。
- 待機runの置換を無害にするため、1つのrunが対象の全親を同じstepの中で順に処理します（親ごとにrunを分けると、親Aの待機runが親Bのrunに置き換えられて親Aが処理されない飢餓が起こります）。対象親は、`workflow_dispatch` の任意input `parent_issue` が指定されればその親だけ、それ以外（scheduleを含む）はリポジトリ変数 `ORCHESTUNE_PARENT_ISSUES`（空白区切りの正整数）の全親です。手動指定だけの親は、置き換えられると再実行されません。継続運用する親は変数へ登録し、完了したら外してください。
- 制御jobには `timeout-minutes` を明示します。1 runで全親を処理し、各親でIntegratorのCIも走るため、「親の数 × Integratorのtimeout」に余裕を持たせてください。timeoutは後処理の途中で止まり得るので、超過時はrecovery手順で確認します。
- Actions上の制御実行は外部target（`cloud-routine`／`codex-cloud`）だけにします。ローカルプロセスtarget（`local`／`claude-cli`／`agy-cli`／`codex-cli`／`auto`）は、ジョブ終了時にプロセスとworktreeが失われるため使いません。外部targetは資格情報が解決できないと警告だけでローカル起動へフォールバックするため、secretsから制御stepの `env:` へ渡します（`cloud-routine`: `ORCHESTUNE_ROUTINE_ID`／`ORCHESTUNE_ROUTINE_TOKEN`、`codex-cloud`: `ORCHESTUNE_CODEX_CLOUD_ENV`。IDとCodex環境は設定の `routine_id`／`codex_cloud_env` でもよいですが、tokenはenvだけです）。
- `inputs`／`vars` はstepの `env:` で受け取り、`run:` 本文へ `${{ }}` で直接展開しません（script injection対策）。

**runner上で失われるローカル状態**: 次の状態はキャッシュやartifactで永続化しません（`actions/cache` はキーが不変で後勝ちの書込ができず、所有権の継続を証明できないためです）。影響を許容できない場合は、永続するself-hosted runnerまたはローカルの単一所有者運用にしてください。

| ローカル状態（既定パス） | 失われたときの影響 |
| --- | --- |
| `run_state.json`（`run_state_path`）の `task_reclaim_counts` | 回収回数（`count`／`pending`）、早期終了の再投入回数（`early_death_retry_*`）、AIレビュー待機タイムアウトの再投入回数（`review_timeout_retry_*`）、claude-cliのセッション上限による再投入回数（`usage_limit_retry_*`）がrunごとに0へ戻り、`--max-task-reclaims` と早期終了・review timeout・セッション上限の再投入上限がrunをまたいで効かない。targetごとのセッション上限cooldown（`usage_limit_cooldowns`）も失われ、上限が解除される前にclaude-cliを再起動し得る。backoffの次回起動時刻と未確定の予約も失われる（上限超過で `status:blocked-human-review` になったタスクはラベルが残るので再投入されない。recompute・base-branch-redの回数はIssue側に残る） |
| 同 `active_worktrees`／`completed_worktrees`／`launch_history` | 毎回GitHubのラベル・PR・ブランチ・親Issue本文から再構成する。起動枠の予約履歴は親Issue本文側にも保存される |
| 同 `pending_lock_release_notices` | 未送信の外部ロック解除通知の再送が失われる |
| 同 `completion_journal`／`completion_reservations`／`completion_replay_receipts`／`recovery_receipts` | 進行中のcompleteの再開用journalと、GCが照合する予約・replay・復旧のreceiptが失われる。正規のOutcome RecordはIssueコメントに残るが、journalやreceiptからの冪等な再開・再発行は保証されないため、異常終了後は `orchestune recover` と[状態復旧](architecture/state-recovery.md)の手順で確認する（個別の復元可否は確認できていない） |
| `run_state.status-intents.json`（`run_state_path` と同じ場所） | 実行途中のstatus repairのintent journalが失われ、PLANNED／APPLIEDのintentを次サイクルで照合できない。ラベルの実状態はGitHubが真実なので再観測で再計画されるが、途中状態の突合せ記録は確認できていない |
| `not_needed_review_state.json`（`not_needed_review_state_path`） | 検証依頼済みのnot-needed reviewのpending一覧が失われ、`not-needed-review:passed`／`failed` ラベルのポーリングによるクローズが行われなくなる。依頼済みレビューの追跡が切れるため、該当Issueは人手で確認する |
| `events.jsonl`（`events_log_path`）・`logs/`（`log_dir`）・`.orchestune/reports/dispatch`（`report_dir`） | ログ・報告の履歴だけが失われる |
| `worktrees/`（`worktree_root`）と `worktrees/.holds/` | ローカルtargetのworktreeとhold記録が失われる（外部targetに限定する理由） |

#### 6.3.1 導入用workflow例

[`examples/dispatch-single-executor.yml`](../examples/dispatch-single-executor.yml) は上記の要件を満たすworkflow例です（schedule＋手動入口、トップレベルの共通group、単一の同期job、親供給と同一stepでの親の直列処理、自己診断ゲート）。

- 推奨コピー先は `.github/workflows/orchestune-dispatch.yml` です。`.github/workflows/` 直下であれば別名でも、ゲートが `GITHUB_WORKFLOW_REF` から自身のパスを導出するので動きます。dispatchの前に `orchestune doctor --execution-mode actions --workflow <自身のパス>` を1回実行し、診断のerrorや資格情報の空値（名前だけを出し値は出さない）があればdispatchを1件も開始せずjobを止めます。
- 有効化の前に置き換える箇所は `ORCHESTUNE_VERSION`、cron、`timeout-minutes`、secret名、dispatchのtargetです（`codex-cloud` を使う場合は、target・資格情報のenv名・ゲートの確認を替え、別途Codex CLIをrunnerへ導入して認証します）。
- 対象の親はリポジトリ変数 `ORCHESTUNE_PARENT_ISSUES`（空白区切りの正整数）へ登録し、完了した親は外します。手動の `parent_issue` input だけで指定した親は、待機runが置き換えられると再実行されません。不正値・重複はdispatchを開始せずrunが失敗し、空なら成功終了します。1つの親が失敗しても残りを処理し、最後に非0で終了します。
- Actions外の実行（ローカルCLI・Cloud Routine・外部cron）はこの設定では止められません。併用しないか、所有者を移管してください（6.2参照）。
- 本リポジトリ自身ではこの例を有効化していません。

### 6.4 既存導入先の移行

旧来の親単位group（`orchestune-integrate-…-${{ inputs.parent_issue }}`）は本契約では不合格です。group名の接頭辞も変わるため、移行中に旧workflowと新workflowが同じgroupに入ることはありません。次の順に切り替えてください。

1. 旧workflowを無効化（`gh workflow disable` 等）し、新規tickを止める。
2. 旧workflowの実行中・待機中のrunがないことを確認する。実行中のものは完了を待つ（cancelしない）。
3. 旧workflowファイルを `.github/workflows/` から削除する。有効／無効はオフライン診断で判定できないため、非標準groupで直接dispatchを含むファイルが残っていると、新workflowの自己診断が `dispatch.repository.other_entrypoints` のerrorで止まる。
4. 新workflowを追加し、`orchestune doctor` がerrorなしであることを確認してから有効化する。

### 6.5 `orchestune doctor` による診断

```bash
orchestune doctor --execution-mode actions --workflow .github/workflows/orchestune-dispatch.yml
orchestune doctor --execution-mode actions --workflow .github/workflows/orchestune-dispatch.yml --json
orchestune doctor --execution-mode local
```

`--workflow` は複数回指定できます。診断は常にオフラインで、GitHub APIの呼出し・認証確認・状態変更を行いません。

- 終了コード: `0` = 静的設定にerrorなし、`1` = 設定errorあり、`2` = 引数不正。warning／not_checkedだけなら `0` です。
- **設定診断は運用所有権の保証ではありません。** 終了コード0でも、別マシン・別clone・Actions外の入口との競合がないことは確認されていません。
- statusは `ok`／`warning`／`error`／`not_checked`（静的には判定できず、運用での確認が必要）です。

| code | 内容（error／warning／not_checkedになる条件の要約） |
| --- | --- |
| `dispatch.config.readable` | `orchestune.toml`／`pyproject.toml` の読込・検証に失敗するとerror（targetや資格情報も決められない） |
| `dispatch.workflow.readable` | 指定ファイルの欠落・読取不能・YAML不正・重複keyはerror |
| `dispatch.actions.group` | トップレベルgroupが標準式 `orchestune-control-${{ github.repository }}` と一致すればok。文字列短縮形・欠落・空・親／workflow／ref／run等による分割・workflow間の不一致はerror |
| `dispatch.actions.cancel` | 明示的な `cancel-in-progress: false` だけok。欠落・true・文字列・動的式はerror |
| `dispatch.actions.parallelism` | 直接dispatchを実行するjobのmatrix、job-level concurrency、複数制御job、バックグラウンド起動はerror |
| `dispatch.actions.entrypoint` | 同期dispatch入口を特定できればok。検出できなければnot_checked |
| `dispatch.actions.target` | 外部target（`cloud-routine`／`codex-cloud`）はok、ローカルプロセスtargetはerror、静的に決定できなければnot_checked |
| `dispatch.actions.credentials` | 外部targetに必要な資格情報が制御stepから見える `env:` で受け渡されていればok、欠落はerror |
| `dispatch.local.serialization` | localモードでは常にnot_checked（単一所有者とCLI全体の直列実行を運用で確認） |
| `dispatch.repository.other_entrypoints` | 指定外workflowの直接dispatch。標準groupならwarning、欠落・非標準groupはerror（旧workflowの残置を含む）。localモードでは直接dispatchを含むworkflowがあればwarning |
| `dispatch.repository.other_control_entrypoints` | 適用モードの `orchestune gc`／`orchestune recover --apply` を検出。標準groupならok、それ以外はwarning |
| `dispatch.external_ownership` | 別マシン・別clone・Actions外入口は常にnot_checked |
| `dispatch.state.continuity` | 永続状態・worktree／PIDの引継ぎは常にnot_checked（6.3の表を確認） |

直接dispatch入口として検出するのは、`orchestune dispatch`、`orchestune-dispatch`、`python -m orchestune.dispatch.dispatcher` です（`--no-apply` の入口は制御実行者に数えません）。コマンド名の変数展開、分割できない行、`uses:` のactionやreusable workflowの内部、呼び出し先のscriptは検出できないため、`not_checked` になります。その場合は実行入口と後処理までの保護区間を手動で確認してください。

なお、本リポジトリ自身は現時点でOrchestuneのdispatchをGitHub Actionsのスケジュール実行では回しておらず（Cloud RoutineまたはローカルCLIへのディスパッチが前提）、上記は導入先リポジトリ向けの設定です。本リポジトリで実際にcron定期実行を有効化する際は、上記のconcurrency設定を含むworkflowファイルを別途`.github/workflows/`へ追加してください。
