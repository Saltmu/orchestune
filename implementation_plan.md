# 実装計画書: Issue #997 [FEAT] complete-contract-scaffold

## 概要 (Overview)
- **対象Issue**: [#997](https://github.com/Saltmu/orchestune/issues/997) `[FEAT] complete-contract-scaffold: completeパッケージの共有入力・結果・エラー契約を定義する`
- **親Issue**: [#894](https://github.com/Saltmu/orchestune/issues/894) `[EPIC] feat(cli): タスク完了・Outcome宣言を一元管理する complete コマンドの導入`
- **ベースブランチ**: `parent/issue-822`（ユーザー指定）
- **作業ブランチ**: `claude/issue-997-complete-contract-scaffold`
- **GitHub 操作バックエンド**: `gh` CLI
- **PR レビュアー**: `codex` (Non-Interactive mode)

## 目的と受け入れ基準
1. `done` / `not-needed` / `blocked` の入力型と状態遷移が型安全に表現できること
2. 成功は GC 引渡し（`HANDED_OFF_TO_GC`）までを意味し、`CompletionReceipt`（`orchestune.dispatch.cycle_records.CompletionReceipt`）を含まないこと（CLIとGCの責任境界の厳格な分離）
3. `claim` パッケージ（#893）の所有者識別（`owner_token`, `owner_kind`, `claim_id`）と矛盾せず接続できること
4. `uv run pytest tests/test_complete_contracts.py` を含むローカル CI が通過すること

## 影響範囲と仕分け (Impact Scope Reconciliation)

| シンボル / 参照箇所 | 分類 | 根拠 |
| :--- | :--- | :--- |
| `orchestune/complete/__init__.py` | in scope (新規作成) | パッケージ公開シンボルの再エクスポート (`CompleteRequest`, `CompleteResult`, `CompleteFailure`, `CompleteStage`, バリデータヘルパー等) |
| `orchestune/complete/contracts.py` | in scope (新規作成) | `CompleteRequest`, `CompleteResult`, `CompleteFailure`, `CompleteStage`, `CompleteExitCode`, `CompleteFailureReason`, ペイロード型などのドメインモデル定義 |
| `tests/test_complete_contracts.py` | in scope (新規作成) | Result別入力型、状態遷移ルール、GC責任境界 (CompletionReceipt非依存)、claim互換性の網羅的契約テスト |
| `orchestune/outcome_record.py` | out of scope (参照のみ) | 既存の OutcomeRecord スキーマ・定数（`RESULT_*`, `REASON_*`, `MAX_REASON_LENGTH`）を利用し、無変更 |
| `orchestune/claim/contracts.py` | out of scope (参照のみ) | `OwnerKind`, `OwnerToken` 等の契約を利用し、無変更 |
| `orchestune/dispatch/cycle_records.py:CompletionReceipt` | out of scope (分離対象) | GC完了証跡であり、`CompleteResult` に含めないことをテストで検証 |

## #822 observation record

- Start date: 2026-09-22
- Environment: Linux, Python 3.13.15, uv 0.12.10, ruff 0.4.10, mypy 1.20.2
- Base SHA: `aabe267`
- Tool: Serena MCP (`find_symbol`), supplemented by `git grep`
- Actual use: Verified non-existence of `CompleteRequest`, `CompleteResult`, `CompleteFailure`; verified declaration and role of `CompletionReceipt` in `orchestune/dispatch/cycle_records.py`.
- Tokens: unavailable (no counter exposed).
- Scope snapshot permalink: https://github.com/Saltmu/orchestune/issues/997#issuecomment-5769967296

## 設計詳細

### 1. 状態遷移 (`CompleteStage`)
```python
class CompleteStage(str, Enum):
    INITIALIZING = "initializing"
    PREFLIGHT_VALIDATING = "preflight_validating"
    EVIDENCE_VERIFYING = "evidence_verifying"
    JOURNALING = "journaling"
    POSTING = "posting"
    HANDED_OFF_TO_GC = "handed_off_to_gc"
```
遷移順序: `INITIALIZING` -> `PREFLIGHT_VALIDATING` -> `EVIDENCE_VERIFYING` -> `JOURNALING` -> `POSTING` -> `HANDED_OFF_TO_GC`
ヘルパー: `can_transition(from_stage: CompleteStage, to_stage: CompleteStage) -> bool`

### 2. エラー分類とExitCode (`CompleteExitCode`, `CompleteFailureReason`)
`ClaimExitCode` / `ClaimFailureReason` と整合した設計:
- Preflight / Validation errors (10-19):
  `INVALID_REQUEST`, `CLAIM_NOT_FOUND`, `OWNER_TOKEN_MISMATCH`, `INVALID_RESULT_PAYLOAD`, `PR_REQUIRED`, `PR_PROHIBITED`, `REASON_REQUIRED`, `DIRTY_WORKTREE`, `EVIDENCE_MISSING`
- Concurrency / State errors (20-29):
  `STATE_LOCK_FAILED`, `CONCURRENT_COMPLETION`, `INVALID_STAGE_TRANSITION`
- Infrastructure / Forge errors (30-39):
  `FORGE_POST_FAILED`, `STATE_SAVE_FAILED`

### 3. Result別入力型 (`DonePayload`, `NotNeededPayload`, `BlockedPayload`)
- `DonePayload`: `pr: int` (非ブール正整数), `review: ReviewSummary` (rounds は None または非ブール正整数), `ci: str | None`, `baseline_regressions: tuple[str, ...]`
- `NotNeededPayload`: 空の dataclass（canonical OutcomeRecord スキーマに合わせた設計）
- `BlockedPayload`: `reason: str` (空白・制御文字サニタイズかつ `MAX_REASON_LENGTH` (100) でキャップ), `base_sha: str | None`, `attempt: int | None` (None または非ブール正整数), `review: ReviewSummary` (rounds は None または非ブール正整数), `ci: str | None`

### 4. `CompleteRequest`
- ファクトリメソッド `CompleteRequest.done()`, `CompleteRequest.not_needed()`, `CompleteRequest.blocked()` を提供
- バリデーション機能 `validate()` および `__post_init__` により、`issue_number` が非ブール正整数であること、done 時に valid な pr と valid な review があること、not-needed 時に NotNeededPayload または None であること、blocked 時に non-empty reason と valid attempt と valid な review であること等を厳格に事前検査

### 5. `CompleteResult`
- `success: bool`
- `issue_number: int` (非ブール正整数)
- `result: str` (`VALID_RESULTS` のいずれかであることを強制)
- `stage: CompleteStage`
- `claim_id: str | None`
- `owner_kind: OwnerKind | None`
- `pr: int | None` (None または非ブール正整数)
- `outcome_record: OutcomeRecord | None` (指定時は `issue`, `result`, `pr` がトップレベル属性と一致することを強制)
- `failure: CompleteFailure | None`
- `handed_off_to_gc: bool`
- **境界不変条件**: `__post_init__` で `success=True` の場合は `stage == HANDED_OFF_TO_GC` かつ `handed_off_to_gc=True` かつ `failure is None` を、`success=False` の場合は `stage != HANDED_OFF_TO_GC` かつ `handed_off_to_gc=False` かつ `failure is not None` を強制
- **重要**: `CompletionReceipt` を絶対に属性や依存関係に含めない

### 6. `CompleteFailure`
- `reason: CompleteFailureReason`
- `message: str`
- `issue_number: int | None`
- `conflicting_stage: CompleteStage | None`
- `next_actions: tuple[str, ...]`
- `@property exit_code -> CompleteExitCode`

## TDD・実装ステップ
1. **Red**: `tests/test_complete_contracts.py` を作成し、インポートエラーおよびアサーション失敗（Red）を確認
2. **Green**: `orchestune/complete/__init__.py` および `orchestune/complete/contracts.py` を実装し、全テスト合格（Green）を確認
3. **Refactor**: Ruff / Mypy / detect-bloat / local-ci.sh の検証
4. **PR & Review**: `parent/issue-822` をベースとする PR 作成および Claude による自動レビュー
