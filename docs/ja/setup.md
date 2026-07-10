# セットアップガイド

Orchestuneのインストール方法、各種AIアシスタント（Claude Code, Codex CLI, Antigravity）へのスキル登録方法、およびクラウド実行（Claude Code Cloud Routine）の設定手順について説明します。

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

---

## 2. エージェントへのスキル定義の登録

AIエージェントに `orchestune` / `orchestune-dispatch` / `local-ci-developer` の各スキルの存在を認識させる必要があります。以下のいずれかの方法を選んでください。

### 方法A: 自動セットアップ（推奨）
セットアップコマンドを実行するだけで、サポートされているすべてのAIアシスタント（Claude Code、Codex CLI、Antigravity）のグローバル設定ディレクトリに対して、自動的にシンボリックリンクを作成します。

```bash
orchestune setup
```

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

現時点で `--dispatch-target cloud-routine` が対応しているクラウド実行先は **Claude Code Cloud Routineのみ** です。

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
