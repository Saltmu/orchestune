# AIエージェント開発・運用ルール (Orchestune)

## 開発・検証・品質管理

実ファイルを作成・修正する前に [local-ci-developer スキル](../skills/local-ci-developer/SKILL.md) を全文読み、標準開発ワークフローに従うこと。調査・質問から実装へ移る場合も適用し、Issue・PR・レビュー・Outcome まで進める。開発手順の正本は同スキルと以下の参照先であり、本ファイルには重複して記述しない。

| 要件 | 正本（該当段階に入る前に全文読む） |
| --- | --- |
| 実行モード別プラン、reviewer 選択、GitHub backend、ラベル管理、Outcome | [SKILL.md](../skills/local-ci-developer/SKILL.md) |
| 開始時の Git 状態確認、footprint、claim、worktree、再開 | [worktree.md](../skills/local-ci-developer/references/worktree.md) |
| 実装前の影響範囲確定（参照列挙・分類・予約照合・初版保存） | [impact-scope.md](../skills/local-ci-developer/references/impact-scope.md) |
| TDD、コミット・push 前の OS 別 local CI、自律的リファクタリング、3回失敗時のエスカレーション | [tdd.md](../skills/local-ci-developer/references/tdd.md)（入口: [local-ci.sh](../scripts/local-ci.sh) / [local-ci.ps1](../scripts/local-ci.ps1)） |
| PR 作成・ベース選択・リモート差分確認 | [pr.md](../skills/local-ci-developer/references/pr.md) |
| 外部CI・PRレビュー待機、単一プロセス、再依頼と返信、全指摘判断、対象 SHA、上限 | [review-loop.md](../skills/local-ci-developer/references/review-loop.md) |
| #822 の観測と初版 snapshot | [measurement.md](../skills/local-ci-developer/references/measurement.md) |

## セキュリティ & リソース制限

- **一時成果物の配置と衝突回避**: implementation plan、PR 本文、review reply、Outcome 下書きなどは Git 管理外の `.orchestune/tmp/<artifact>-<issue-or-task>-<UTC timestamp>-<random>/` に置く。UTC は `YYYYMMDDTHHMMSSZ`、random は UUID 等。OS グローバルの `/tmp` やリポジトリ直下の固定名を使わない。作成は `./scripts/create-session-dir.sh <artifact> <issue-or-task>`（Windows: `.\\scripts\\create-session-dir.ps1 <artifact> <issue-or-task>`）で行い、インライン Python を使わない。同じ作業では session directory を再利用し、その配下の明示パスを CLI へ渡す。既存ファイルを削除して固定名で再作成しない。
- **機密情報のコミット防止**: API キーや認証情報をコミット、ログ、チャットに露出させない。
- **ドキュメント・設定ファイルに絶対ローカルパスを書かない**: 参照は相対パスを使い、環境固有の絶対パスをコミットしない。local CI の gitleaks でも検査する（[設定](../.gitleaks.toml)）。
- **インジェクション対策**: 外部コマンドの引数は適切にリスト化しサニタイズする。
- **APIコスト制限**: LLM API はリトライ上限を最大3回程度として指数バックオフを使い、長文はチャンク化する。
