# Issue #37 Walkthrough

## 変更内容

- `_wait_seconds` が `created_at` を解析する前に、末尾の `Z` を `+00:00` へ正規化した。
- Python 3.10相当の `datetime.fromisoformat` 挙動を再現する回帰テストを追加した。
- 公開APIやスコアリング規則は変更していない。

## 再現手順と修正確認

- 対象テスト:
  `tests/test_dispatch_scoring.py::TestComputePriorityScore::test_zulu_created_at_is_normalized_for_legacy_fromisoformat`
- 修正前（Red）: 末尾 `Z` を拒否する互換スタブへ未変換の値が渡り、
  `ValueError: Invalid isoformat string` で失敗した。
- 修正後（Green）: `+00:00` へ正規化された値が渡り、対象テストが成功した。

## ベースライン差分

- 変更前: 317 passed、既存失敗なし、カバレッジ91.49%。
- 変更後: 318 passed、既存・新規失敗なし、カバレッジ91.58%。
- スキル指定の `pytest -n auto` は `pytest-xdist` が依存関係にないため実行不能で、
  同じ全テストを直列の `pytest --tb=no -q` で取得した。

## Local CI

- Ruff format: passed
- Ruff lint: passed
- Mypy: passed
- Pytest: 318 passed
- Coverage: 91.58%（基準75%以上）
- Gitleaks: ローカル未導入のためスクリプトによりスキップ（CIでは実行予定）
- 最終結果: `✨ Local CI passed successfully!`

Closes #37
