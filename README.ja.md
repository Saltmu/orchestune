# Orchestune

[English](README.md) | [日本語](README.ja.md)

Orchestuneは、並列開発タスクを協調して実行するためのマルチエージェント実装オーケストレーターです。DAG（有向非巡回グラフ）の構築、スケジューリング、ディスパッチサイクル、自己修復、およびプルリクエストの統合を自動化します。

Orchestuneは、**Agentic AI開発向けのスキル**（Claude Code、Antigravityなど）として提供されており、AIエージェントがタスクの分解からサブタスクのディスパッチ、結果の統合までを自律的に行えるようにします。

## 主な機能

1. **DAG構築とコンフリクト回避**
   - サブタスク間の依存関係をファイルやシンボルの重複度から静的に計算し、競合のない安全な並列実行順序をDAGとして構築します。
2. **インテリジェントなディスパッチとスケジューリング**
   - エージェントの実行先として、ローカル環境および Claude Code Cloud Routine へのディスパッチをサポートします。
3. **自己修復（ステートリカバリ）機能**
   - GitHub Actions などのステートレスなCI/CD環境に最適化されており、GitHubのPRやIssueから実行状態を自動復元します。
4. **統合およびリベースの調整**
   - サブタスクのPR完了を監視し、下流ブランチの自動マージやリベースを調整して競合を最小化します。

👉 詳細な仕組みについては [アーキテクチャと設計思想](docs/ja/architecture.md) を参照してください。

---

## インストール方法

Python 3.12以上、Poetry、GitHub CLIがインストールされていることを確認してください。

```bash
# グローバルにインストール（推奨・pipx使用）
pipx install git+https://github.com/Saltmu/orchestune.git
```

インストール後、以下のコマンドで各種AIアシスタント（Claude Code, Codex CLI, Antigravity）へのスキル登録を自動で行うことができます。

```bash
orchestune setup
```

👉 プロジェクト開発依存への追加や手動セットアップ、Cloud Routine の設定方法などの詳細は [セットアップガイド](docs/ja/setup.md) を参照してください。

---

## 使い方

Orchestuneの基本的な流れは以下の通りです。

1. **タスク分解**: AIエージェントに「この大きな機能を実装したい。`orchestune`で分解して」と指示し、`decomposition_plan.md` を作成・検証させます。
2. **ディスパッチ**: 計画が承認されたら、ディスパッチャーを起動してサブタスクをエージェントに割り振り、実装を開始します。

```bash
# 計画のDAG検証
orchestune dag --plan decomposition_plan.md

# ディスパッチャーの起動（ドライラン）
orchestune dispatch --no-apply

# ディスパッチャーの起動（実行）
orchestune dispatch
```

👉 各コマンドの全オプション、および `decomposition_plan.md` の記述スキーマ詳細については [使用方法とコマンドリファレンス](docs/ja/usage.md) を参照してください。

---

## コントリビュート

Orchestune自体を開発したい（テストスイートやローカルCIを実行したい）場合は、[CONTRIBUTING.ja.md](CONTRIBUTING.ja.md) を参照してください。
