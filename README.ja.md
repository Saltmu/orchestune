# Orchestune

**並列に動くAIコーディングエージェントの競合を、コードを書き始める前に検出し、すべてのタスクと結果をGitHub Issueで追跡するツールです。**

[English](README.md) | [日本語](README.ja.md)

Orchestuneは、承認された分解計画を依存関係付きのGitHub Issueへ変換し、分離されたworktreeで動くエージェントの作業を調整します。ブランチ、プルリクエスト、CI結果は元のタスクに紐づきます。Claude Code、Codex CLI、AntigravityなどのAIコーディングアシスタント向けスキルとして動作します。

## 向いている用途

複数モジュールにまたがる機能、共有インターフェースや依存関係のある変更、エージェント用スキル自体の開発に向いています。席を外している間もまとまった作業を進めたい個人開発者や少人数チームが対象です。

簡単なWebアプリや、1回のプロンプトに収まる小さなタスクなら、エージェントに直接頼んだほうが速く、消費クオーターも少なくて済みます。作業同士の調整が必要になったときに、分解とディスパッチが役立ちます。

## 実際の開発例

Orchestune自身の[親Issue #1368](https://github.com/Saltmu/orchestune/issues/1368)では、CLIへのRich表示導入を**8つの子タスク**に分解しました。共通表示部品、status/doctor、dispatch進捗、provision、ライフサイクルコマンド、セットアップ、DAG/recovery/scratch、ルートhelp/READMEを分担しています。

[最初の実装PR #1419](https://github.com/Saltmu/orchestune/pull/1419)の作成から[最終統合PR #1439](https://github.com/Saltmu/orchestune/pull/1439)のマージまでは、**約12時間41分**でした（UTCで2026-10-25 11:21〜2026-10-26 00:02）。待機を含む経過時間であり、エージェントの実行時間ではありません。

通常のワークフローで人が判断するのは、分解計画の承認と最終PRのマージの2か所です。この実行では[統合中に人による復旧操作](https://github.com/Saltmu/orchestune/issues/1368#issuecomment-6398337132)も必要でした。

## 最初の計画を試す

Python 3.12以上、uv、認証済みのGitHub CLIを用意し、作業対象のリポジトリで実行します。

```bash
uv tool install "git+https://github.com/Saltmu/orchestune.git"
orchestune skills install --target all --scope project
orchestune config init
```

コーディングアシスタントに「この機能をorchestuneで分解し、計画の保存先を教えて」と依頼します。Issue起票やディスパッチを承認する前に、返された保存先で下の例のパスを置き換えて計画を検証します。

```bash
orchestune dag --plan "<saved-plan-path>"
```

### ローカル初回体験（GitHub設定不要）

試用に必要なのは Orchestune、Git、`python` として実行できる対応 Python、PATH 上の認証済み Codex CLI または Claude Code です。モデル通信はオンラインです。GitHub の認証、`gh`、remote、ラベル、dispatch 用の設定は試用には使いません。これらは既存プロジェクトへ GitHub 連携を入れるときの前提です。

試用で確認できるのは、サンプルの計画を承認し、同じ head の CI と別セッションのレビューが通った差分を見て、保持するか採用するかを決めるところまでです。

手順は同言語の[セットアップガイド](docs/ja/setup.md#ローカル初回体験のセットアップ)にあります。コマンドの詳細は[利用手引き](docs/ja/usage.md#10-ローカル自律試行-orchestune-trial)にもあります。

既存プロジェクトでは、上のコマンドの前に[セットアップガイド](docs/ja/setup.md)の GitHub 導入の前提を確認してください。他のインストール方法やエージェントの設定は、後述の[インストール方法](#インストール方法)を参照してください。

⚠️ **β版**: 主要ワークフローは利用可能ですが、安定版までにAPI、CLIコマンド、ワークフローが変更される可能性があります。

## 主な機能

1. **大きなタスクを追跡可能な作業単位へ分解**
   - 各サブタスクの実装範囲、受け入れ基準、検証手順、依存関係、変更予定のファイルやシンボルを含む、レビュー可能な分解計画を作成します。
2. **実装開始前に依存関係と競合を検証**
   - 計画からDAGを構築し、循環依存、ファイルやシンボルの重複、共有拡張点のリスク、危険な並列実行を、AIがコードを変更する前に検出します。
3. **実行計画をGitHub上の追跡可能なIssueへ変換**
   - 承認された計画から、依存関係、ステータスラベル、実装に必要な文脈を持つ親Issueと子Issueを起票し、各作業の目的と検証方法を記録します。
4. **AIによる自律的な実装と統合を協調**
   - 着手可能なIssueをローカルまたはクラウドのAIコーディングエージェントへ割り当て、GitHub IssueとPRから進捗を復元しながら、下流ブランチのリベースと統合を調整します。

👉 詳細な仕組みについては [アーキテクチャと設計思想](docs/ja/architecture.md) を参照してください。
👉 各`status:*`ラベルの意味とライフサイクルについては [status:* ラベルのライフサイクル](docs/ja/status-labels.md) を参照してください。

---

## 設計思想: クオーター効率

Orchestuneは、AIコーディングエージェントを使う個人開発者と少人数チームのためのものです。ここでの希少資源は実時間ではなく、**AI利用クオーター（サブスクリプションのセッション/週次の使用枠）と、人間が席にいられる時間**です。

DAG検証も、GitHubからの状態自己修復も、人間の判断を2点に限る承認モデル（[アーキテクチャと設計思想](docs/ja/architecture.md)参照）も、すべて「無人で放置しても壊れない」ためだけに存在します。

そしてパイプラインを実際に進めるのは決定論的なPythonです。LLM呼び出しはクオーターを消費する希少な操作なので、判断が代替不能な箇所にのみ使い、共有状態の自動遷移はPython側が担います（[2.2 決定論](docs/ja/architecture.md#determinism)）。

---

## インストール方法

👉 導入前に、対象リポジトリが満たすべき前提条件（エージェント規律の定義・CIの厚さ・`ci_command`設定）を[セットアップガイドの「0. 導入要件」](docs/ja/setup.md#0-導入要件prerequisites)で確認してください。

Python 3.12以上、uv、GitHub CLIがインストールされていることを確認してください。

```bash
# uv toolによるCLIのグローバルインストール（推奨）
uv tool install "orchestune==<RELEASE_VERSION>"
# またはGitリポジトリから最新を導入:
uv tool install "git+https://github.com/Saltmu/orchestune.git"
```

インストール後、以下のコマンドで各種AIアシスタント（Claude Code, Codex CLI, Antigravity IDE, Antigravity CLI, Grok CLI）へのスキル配布を行います。

```bash
# プロジェクトへインストール（チーム共有推奨）:
orchestune skills install --target all --scope project

# またはユーザー設定ディレクトリへグローバルにインストール:
orchestune skills install --target all --scope user
```

👉 状態確認・診断（`orchestune skills doctor`）、更新・削除、プロジェクト開発依存への追加などの詳細は [セットアップガイド](docs/ja/setup.md) を参照してください。

---

## 使い方

実際には、以下のコマンドを人間が自分で打つことはありません。「この機能をorchestuneで分解して」のように自然言語でAIエージェントへ依頼すると、`orchestune`スキルが`orchestune-provision`や`orchestune-dispatch`への引き継ぎも含めてパイプライン全体を内部で駆動し、これらのCLIコマンドをツール呼び出しとして代行実行します。以下はその各段階を具体的に示すためのものであり、また状態ファイル消失後の手動再開など例外的なケースで、いずれかのステップを人間が直接実行したくなる場合もあるためです。

1. **分解と検証**: あなたのエージェントが大きなタスクをレビュー可能な`decomposition_plan.md`へ分解し、依存DAGと競合リスクを検証します。あなたはその計画をレビューして承認します。
2. **Issue起票**: 承認後、あなたのエージェントが依存関係で結ばれたGitHubの親Issueと子Issueを作成します。
3. **ディスパッチ**: あなたのエージェントが、着手可能なサブタスクを分離されたworktreeでローカルまたはクラウドのAIコーディングエージェントへ割り当てます。
4. **追跡と統合**: 各サブタスクのIssue、ブランチ、プルリクエスト、CI結果を追跡しながら、Orchestuneが依存タスクと統合を調整します。

```bash
# 1. 設定ファイルの作成・編集（対話式ウィザード）
orchestune config init
# 既存設定の対話式編集: orchestune config edit

# 2. 計画のDAG検証
orchestune dag --plan decomposition_plan.md

# 3. 承認した計画からGitHub Issueをプレビューし、その後起票する
orchestune provision --plan decomposition_plan.md
orchestune provision --plan decomposition_plan.md --apply

# 4. ディスパッチャーの起動（ドライラン、その後実行）
orchestune dispatch
orchestune dispatch --apply
```

👉 各コマンドの全オプション、および `decomposition_plan.md` の記述スキーマ詳細については [使用方法とコマンドリファレンス](docs/ja/usage.md) を参照してください。

---

## CLIの表示

Orchestuneは対話的な端末では、人向けの出力にRichの表示を使います。パイプやリダイレクトへの出力、CI環境、`TERM=dumb`では自動的にplain表示を選びます。環境にかかわらずplain表示にするには、`ORCHESTUNE_CLI_PLAIN=1`を設定してください。

`NO_COLOR`に空でない値を設定すると、色だけを無効にできます。TTYでは色なしでも表やライブ進捗を利用できます。

既存の機械可読出力はスクリプトで使える状態を保ちます。JSON応答や`orchestune scratch create`が出力するパスには装飾や進捗表示が混ざりません。`orchestune status --watch`はTTYでは表示を更新します。plain表示または非TTYでは画面を消去せず、watchの各間隔で静的なスナップショットを追記します。

## コントリビュート

Orchestune自体を開発したい（テストスイートやローカルCIを実行したい）場合は、[CONTRIBUTING.ja.md](CONTRIBUTING.ja.md) を参照してください。
