# TDD & Local CI Reference (Steps 3–9)

本ドキュメントは、テスト駆動開発（TDD）による実装とローカルCI検証の具体手順を定めたリファレンスです。

---

## 3. 再現手順の確立 (Reproducer)
- **バグ修正の場合**: 実装前に不具合を再現する最小限のテスト（`tests/`配下）を作成し、失敗（Red）することを確認します。
- **新規機能の場合**: 本ステップをスキップし、ステップ4へ進みます。

## 4. ベースライン取得 (Baseline Record)
- 変更前のコード状態でベースラインを記録します。
```bash
poetry run python scripts/ci_baseline.py record
```
- この記録は、ステップ9で新規リグレッションとベースブランチ由来の既存失敗を自動判別するために使用されます。

## 5. 実装前のテスト作成 (テストファースト)
- 新規機能や改修仕様（正常系・主要シナリオ）を満たすテストを `tests/` 配下に記述します。
- `poetry run pytest` を実行し、追加したテストが期待通り失敗（Red）することを確認します。

## 6. 機能の実装とテスト通過
- テストをパスさせる最小限のコードを実装します。
- `poetry run pytest` を実行し、すべてのテストがパス（Green）することを確認します。

## 7. Failure Analyst (連続失敗時の根本原因分析)
- 同一のテスト失敗が2回以上連続した場合、闇雲な再修正を止め、以下を分析します：
  1. 失敗の直接原因（スタックトレース・diff該当箇所）
  2. 想定修正で解消しなかった理由の仮説
  3. 次に試す具体的な修正方針
- `scripts/ci_baseline.py` が連続失敗カウントを自動追跡します。3回分析しても解消しない場合は作業を中断し、エスカレーションします。

## 8. エッジケースと異常系のカバレッジ補強
- 境界値・異常系・エラーハンドリングのテストを追加し、カバレッジを向上させます。
```bash
poetry run pytest --cov=orchestune --cov-branch --cov-report=term-missing
```

## 9. ローカルCIの一括実行とエラー解消
- ベースライン対応のCI検証を実行します。
```bash
poetry run python scripts/ci_baseline.py check
```
- またはOS標準のCIスクリプトを実行します（Linux/macOS: `./scripts/local-ci.sh`、Windows: `.\\scripts\\local-ci.ps1`）。

### 各種エラーの解消手順
1. **Ruff Format/Lint**:
   ```bash
   poetry run ruff format
   poetry run ruff check --fix
   ```
2. **Mypy 型チェック**:
   ```bash
   poetry run mypy orchestune tests
   ```
3. **Pytest テスト失敗**:
   - `scripts/ci_baseline.py check` が新規失敗とベースライン由来失敗を自動分類します。
4. **Detect Bloat 警告**:
   ```bash
   poetry run python scripts/detect_bloat.py --warn-only
   ```
   - ファイルサイズ（コード: 1000行、スキル計: 500行）や関数長（50行）の超過警告を検知した場合は、リファクタリング計画を検討してください。
