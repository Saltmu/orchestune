# Orchestune

[English](README.md) | [日本語](README.ja.md)

Orchestuneは、並列開発タスクを協調して実行するためのマルチエージェント実装オーケストレーターです。DAG（有向非巡回グラフ）の構築、スケジューリング、ディスパッチサイクル、自己修復、およびプルリクエストの統合を自動化します。

Orchestuneは、**Agentic AI開発向けのスキル**（Claude Code、Antigravityなど）として提供されており、AIエージェントがタスクの分解からサブタスクのディスパッチ、結果の統合までを自律的に行えるようにします。

## 主な機能

1. **DAG構築とコンフリクト回避**
   - サブタスク間の依存関係を自動的に計算します。
   - 明示的な依存関係（`depends_on`）だけでなく、変更対象ファイル（`footprint`）やコードシンボル（`symbols`）の重複を類似度メトリクスを用いて分析し、コンフリクトのない安全な並列実行順序をDAGとして構築します。
   - 依存関係の循環参照エラー（`DagCycleError`）を検出し、セキュリティやリスクのある箇所（認証情報やサブプロセス記述など）を警告・フラグ立てします。

2. **インテリジェントなディスパッチとスケジューリング**
   - 専用のGit worktreeを切り出し、各サブタスク用の環境を構築してエージェントを起動します。
   - 最大同時実行数（`--max-concurrent`）を制限し、時間窓内の最大起動回数（`--max-launches-per-window` / `--window-seconds`）に基づいてAPIのバーストレートを制御します。
   - エージェントの実行先として、ローカル環境でのコマンド実行およびClaude Code Cloud Routineへのディスパッチ（`--dispatch-target`）をサポートしています。

3. **自己修復（ステートリカバリ）機能**
   - GitHub ActionsなどのステートレスなCI/CD環境（ローカルの状態ファイル `run_state.json` が消失する環境）に最適化されています。
   - 稼働中のGitHub IssuesやオープンなPRブランチから、現在の実行状態を動的かつ自動的に再構築します。

4. **統合およびリベースの調整**
   - サブタスクの完了（PRオープン）を監視します。
   - 下流のPRブランチのマージや自動リベースを調整し、コンフリクトを最小限に抑えます。
   - 統合PRに対するLLMによるセマンティックレビュー（コードレビューコメントの自動付与）機能と連携します。

---

## インストール方法

Python 3.12以上、Poetry、GitHub CLI（`gh auth status`）がインストールされていることを確認してください。

### 別のプロジェクトでOrchestuneを利用する場合
`orchestune-dag` / `orchestune-dispatch` を*別の*プロジェクト（例: `manuscriptune`というプロジェクト）内でエージェントに実行させたい場合は、以下の2ステップでセットアップします。

**ステップA: CLIのインストール**

```bash
# グローバルにインストール（推奨・pipx使用）
pipx install git+https://github.com/Saltmu/ochestune.git

# または導入先プロジェクトの開発依存として追加（Poetry）
poetry add --group dev git+https://github.com/Saltmu/ochestune.git
```

これにより、導入先プロジェクトのディレクトリから `orchestune-dag` / `orchestune-dispatch` を素のコマンドとして実行できるようになります。

**ステップB: エージェントへのスキル定義の登録**

エージェントに `orchestune` / `orchestune-dispatch` / `local-ci-developer` の各スキルの存在を認識させる必要があります。利用しているエージェントに応じて、以下のいずれかの方法を選んでください。

- **`.agents/skills.json`**（Antigravity向け）: 導入先プロジェクトの`.agents/skills.json`に、本リポジトリの`skills/`ディレクトリへのパスを指定します：
  ```json
  {
    "entries": [
      { "path": "../path/to/cloned/ochestune/skills" }
    ]
  }
  ```
- **プロジェクトスキル**（Claude Code、Codex CLI向け）: 両エージェントとも、`.claude/skills/<name>/`・`.codex/skills/<name>/`配下に置かれたスキルをネイティブに自動検出します（`SKILL.md`はエージェント間で共通のフォーマットなので、同じファイルがそのまま両方で使えます）。導入先プロジェクトで、スキルフォルダをコピーまたはシンボリックリンクしてください：
  ```bash
  ln -s ../path/to/cloned/ochestune/skills/orchestune .claude/skills/orchestune
  ln -s ../path/to/cloned/ochestune/skills/orchestune .codex/skills/orchestune
  ```
  本リポジトリ自身も同様の構成を採用しています。動作例として`.claude/skills/`・`.codex/skills/`を参照してください。
- **グローバルスキルディレクトリ**: プロジェクトごとの設定なしにどこでも使えるようにしたい場合は、スキルフォルダをエージェントのグローバルスキルディレクトリに配置（またはシンボリックリンク作成）します（例: Claude Codeの場合 `~/.claude/skills/orchestune/`、Codex CLIの場合 `~/.codex/skills/orchestune/`）。

---

## 使い方

典型的な一連の流れは以下のようになります。
1. 人間がエージェントに「この大きな機能（大きな石）を実装したい。`orchestune`で分解して並列開発をセットアップして」と指示します。
2. エージェントがそれをトリガーに`orchestune`スキルを自動的にロードします。
3. エージェントが`decomposition_plan.md`を作成し、インストール済みのCLIを使ってDAGを検証します。
4. 人間が計画を承認すると、エージェントがGitHub Issueを起票し、ディスパッチャーを起動します。

### 1. タスク分解計画とDAGの検証（Decomposition Plan & DAG Validation）
メインタスク（「大きな石」）をより小さなサブタスクに分解するには、エージェント（Claude Code、Antigravityなど）に **`orchestune` コアスキル** をロードさせます。AIが自動的にコードベースの調査、`decomposition_plan.md` の作成・検証、およびFootprintメタデータや`status`/`priority`ラベルを含むGitHub Issueの起票までを一貫して行うため、計画を手動で書いたりIssueを手動で起票したりする必要はほぼありません。

参考として、`decomposition_plan.md` はYAMLフロントマター形式で記述されます：

```markdown
---
subtasks:
  - id: setup-database
    description: "データベーススキーマとコネクションプールの初期化"
    footprint:
      - src/db/connection.py
    symbols:
      - db.get_connection
    depends_on: []

  - id: user-auth
    description: "ユーザー認証エンドポイントの実装"
    footprint:
      - src/auth/routes.py
    symbols:
      - auth.login_user
    depends_on: [setup-database]
---
# タスク分解計画の説明
この計画は、構築に必要な手順をまとめたものです...
```

この計画はエージェント自身が検証しますが、同じチェックを手動で実行してDAGのトポロジー、循環参照、リスクフラグを確認することもできます：
```bash
orchestune-dag --plan decomposition_plan.md
# (本リポジトリ自身の開発環境内であれば: poetry run orchestune-dag --plan decomposition_plan.md)
```

Issueの起票は、ディスパッチャーが解析できるよう決まった形式（タイトル形式、`Footprint`のYAMLブロック、`status`/`priority`/`risk`ラベル）に従います。手動で起票する必要が生じた場合は[`skills/orchestune/SKILL.md`](skills/orchestune/SKILL.md)を参照してください。

### 2. ディスパッチャーコマンド
スケジューラ/ディスパッチャーを実行します。

```bash
# ドライラン（worktreeの作成やラベル更新を行わずに計画のみを表示）
orchestune-dispatch --no-apply

# 適用（ディスパッチサイクルを実行: worktree作成、ラベル更新、エージェント起動）
orchestune-dispatch
```

このコマンド1つで**統合およびリベースの調整**も行われます。サブタスクのPRがオープンされると、以降の`orchestune-dispatch`実行時にそれを検知し、下流ブランチへのリベース・マージやセマンティックレビューが自動的にトリガーされます。個別のコマンドは不要です。

`--dispatch-target cloud-routine`を使う場合は、事前に`ORCHESTUNE_ROUTINE_ID`と`ORCHESTUNE_ROUTINE_TOKEN`環境変数を設定してください。これによりディスパッチャーがClaude Code Cloud Routine経由でエージェントを起動できるようになります。

#### 主なオプション:
- `--apply` / `--no-apply`: 実際にアクションを実行するか、ドライラン（確認のみ）にするかを指定。
- `--max-concurrent <int>`: 同時に実行可能なサブタスクの最大数。
- `--parent-issue <int>`: サブタスク全体を統括する親GitHub Issueの番号。
- `--dispatch-target {local,cloud-routine}`: エージェントをローカルで起動するか、Claude Code Cloud Routineで起動するかを選択。
- `--deviation-buffer-lines <int>`: ライブロック防止のための、想定フットプリントからの変更行数の許容バッファ。

---

## コントリビュート

Orchestune自体を開発したい（テストスイートやローカルCIを実行したい）場合は、[CONTRIBUTING.ja.md](CONTRIBUTING.ja.md)を参照してください。
