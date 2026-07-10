# Walkthrough: orchestune bootstrap（gh認証確認 + 必須ラベル起票）

## 背景

`orchestune-dispatch`スキルの「ステージA: Issue起票」では、`status:queued`等のラベルを`gh issue create --label ...`で付与するが、対象リポジトリにそのラベルが未作成だと`gh`はエラーになる。従来はこの初期セットアップ（gh認証確認・ラベル事前作成）がコード化されておらず、エージェント(LLM)がエラーに気づいて都度`gh label create`を実行する暗黙の運用だった。本PRはこれを決定論的なPythonコードに置き換える。

## 変更内容

- `orchestune/forge.py`（新規）: `Forge`ABC（`check_auth()` / `ensure_labels()`）、`GitHubForge`実装、`LabelSpec`/`BootstrapResult`/`ForgeAuthError`、および`REQUIRED_LABELS`（`status:*`, `priority:*`, `risk:flagged`, `progress:partial`, `not-needed-review:*`の17個の正規ラベル定数）を追加。将来GitHub以外（GitLab等）のフォージに対応する余地を残す軽量な抽象。
  - `check_auth()`: `shutil.which("gh")`でCLI未インストールを検出、`gh auth status`の終了コードで未認証を検出し、いずれも`ForgeAuthError`で明確なメッセージを返す。
  - `ensure_labels()`: `gh label list --json name --limit 100`を1回だけ呼び出し既存ラベル集合を取得、未存在分のみ`gh label create`（`--force`は使わず既存ラベルを保護）。ラベル名は既存の`orchestune/github.py`の`_validate_label`で検証。
- `orchestune/bootstrap.py`（新規）: `run_bootstrap(forge=None)`とCLI `main()`。デフォルトで`GitHubForge`を使用し、認証失敗時はexit 1、成功時は作成/既存件数のサマリを出力してexit 0。
- `orchestune/cli.py`: `orchestune bootstrap`サブコマンドを追加。
- `skills/orchestune-dispatch/SKILL.md`: 「前提」に一行追記、「ステージA: Issue起票」の手順1として`orchestune bootstrap`実行を明記（既存の手順は1つずつ繰り下げ）。

## テスト

- `tests/test_forge.py`（新規、13ケース）: `check_auth`（gh未インストール/認証成功/認証失敗/`check=True`不使用の確認）、`ensure_labels`（list呼び出しが1回のみ/未存在分のみ作成/不正ラベル名の事前検証/コマンド形状/冪等性）、`REQUIRED_LABELS`（17個の正規ラベル名の完全一致・validate_label通過・color/description妥当性）、`BootstrapResult`のfrozen dataclass確認。
- `tests/test_bootstrap.py`（新規、4ケース）: 認証失敗時のexit 1とエラーメッセージ、成功時のexit 0とサマリ出力、デフォルトで`GitHubForge`が使われること、`main()`の終了コード伝播。
- `tests/test_cli.py`（追記、1ケース）: `bootstrap`サブコマンドが`orchestune.bootstrap.main`に委譲されること。

新規機能のため、再現手順（Reproducer）は該当なし。

## ローカルCI実行結果

- [x] Ruff による自動フォーマット/Lintチェックに合格
- [x] Mypy による型チェックに合格
- [x] Pytest のすべてのテストに合格
- [x] テストカバレッジが75%以上（合格基準）に達している（91.55%、新規モジュール`forge.py`/`bootstrap.py`はいずれも100%）

## ベースライン差分（Baseline-aware Test Evaluation）

- 変更前ベースライン: 289 passed, 0 failed（`poetry run pytest --tb=no -q`）
- 変更後: 307 passed, 0 failed
- [x] 新規のリグレッションテスト失敗は存在しない。
- 既存の未解決失敗テスト一覧: なし
