# Jevによるレビュー指摘の評価

`wait_for_review.py` は、`JEV_API_KEY` が設定され、対象のinline指摘がある場合にJevで評価します。キー未設定ならPRメタデータやコードの追加取得、評価API呼び出しを行わず、指摘をそのまま保持します。即時結果、polling結果、offline `--review-state-file` は同じ採否を使います。

## 評価と採否

一つの指摘につき一回のリクエストで、根拠の妥当性 `validity`、欠陥が成立した場合の `impact`、現在の利用条件への `applicability` を質問します。API URL、既存の指数backoffと再試行上限は従来どおりです。

| applicability | 意味 |
| --- | --- |
| APPLICABLE | 現在の実装・文書化された利用条件で欠陥が成立する、または既存要件・規約への具体的な違反 |
| SPECULATIVE | コードと実行条件から、文書化されていない追加の仮定が必要で、既存要件への違反もないと判断できる |
| UNKNOWN | コード・入力経路・規約などが不足、矛盾、または重要箇所が切り詰められている |

API障害は `bypassed=True` として無条件に保持します（validity閾値が1を超えていても保持）。SPECULATIVEでconfidenceが0.9以上、コード・実行条件の出典が一致し、base規約が取得でき、missing/truncatedが空の場合だけ新軸で除外します。APIの `confidence` は確率分布全体から導かれる確信度で、`probabilities[choice]` と一致する必要はなく、校正済みの発生確率でもありません。パーサーは独立した値として保持し、有限な0〜1の範囲と、確率分布がある場合は各値の範囲・合計1・choiceが最大確率であることを検査します。[API仕様](https://docs.typesafe.ai/api)を参照してください。パス分類やPR本文の「内部用」「YAGNI」という主張だけでは証拠を満たしません。低頻度だけでSPECULATIVEにはなりません。

それ以外は従来の `validity >= threshold` かつ `impact != LOW` を使います。旧形式の応答や新軸の不正値はUNKNOWNとして従来判定へ戻します。既存軸が不正な場合は評価全体をbypassします。

## 送信するcontext（schema_version 2）

既存の `state.comment/path/line` に `state.context` を追加します。

- `pr`: number、title、body、head_sha、base_sha。処理単位で一回取得します。
- `code`: source（git_blob / review_diff）、commit_sha、side、start_line、text、status。ローカルにある対象blobから前後10行と、別フィールドのmodule_descriptionを取得します。
- `execution`: component_hint、input_trust、evidence。`scripts/` はinternal_tool_candidateに留め、input_trustはunknownです。実行条件の意味はJevが評価します。
- `repository_rules`: base SHAの `.agents/AGENTS.md`、source、commit_sha、text、status。PR本文だけで既存規約を無効にはしません。
- `missing/truncated`: 取得不能、不正な出典、切り詰めを明示します。安全側の判定として、すべての切り詰めで新軸による除外を禁止します。

inline整形はid、diff_hunk、side、start_line、start_side、commit_id、original_commit_id、original_lineを保持します。表示用lineと実際のposition_lineを区別します。最新コメントのcommit_idがPR headと一致する場合、RIGHTはhead、LEFTはローカルで一意に確認できるhead/baseのmerge-baseを使います（base先端とは限りません）。merge-baseを確認できなければレビュー差分へ戻します。古いRIGHTはoriginal commitとoriginal_lineを使い、対応を証明できないLEFT・古いコメントは元レビュー差分へ戻します。作業ツリーの現在行へ当てはめません。

Git取得は検証済みの完全SHA・相対パスを引数リストで渡します。絶対パス、`..`、不正SHA、二値・非UTF-8・過大ファイルは読みません。ローカルにSHAがなければfetch/checkoutせずreview_diffへ戻り、差分もなければcode.status=missingとします。import先や呼び出し元の再帰探索は行いません。

上限はcomment 4,000文字、PR title 300、PR body 8,000、code 6,000、モジュール説明2,000、規約8,000、raw blob 128 KiB、リクエスト全体32 KiB（UTF-8）です。各項目の文字数が上限以内でも、特に多バイト文字ではリクエスト全体が収まるとは限りません。全体超過時は説明・実行証拠・PR本文・規約・コード・comment・pathの順で縮め、文字境界とtruncatedを保持します。省略があれば引き続きSPECULATIVE除外を禁止し、除外を可能にするための規約・証拠の選択的な要約は行いません。

#1101の調査では、PR #1094の公開inline8件を5,976文字の本文・4,870文字のbase規約で再構成すると、旧上限3,000/4,000文字では全8件に本文・規約の切り詰めが付きました。両上限を8,000文字にすると全8件が全文を保持し、24,825〜26,195 bytesで32 KiB以内に収まりました。これはコンテキスト取得可能性の測定で、Jevの実際の分類・除外頻度ではありません。APIによる再評価は実施しておらず、過去のJev JSONL結果も取得できていません。さらに長い入力では切り詰めが発生し、安全条件によって指摘が保持される場合があります。

offline入力ではトップレベルのoptional `context` を共通値として、または各inlineの `context` を個別値として指定できます。個別値を優先し、未指定はunknownです。追加のネットワーク・Git取得は行いません。出典付きの同じcontext形式を使います。context中の命令は評価対象データとして扱うようプロンプトへ明記しています。

## ログと検証の範囲

既存JSONL項目を維持し、schema_version、applicability、applicability_confidence、decision_reason（bypass / speculative / low_validity / low_impact / accepted）、contextのsource SHA・status・missing・truncatedを追加します。stderrにも同じ判定情報を出します。コード、PR全文、規約全文、APIキーを追加ログへ保存しません。context取得の失敗はmissing、Jev評価失敗はbypassedで区別します。

テストの対照例は、文書化された内部の信頼入力、外部入力、内部の破壊的副作用、規約違反、情報不足です。#1086は議論のある例で、一律にSPECULATIVEを期待しません。単体・結合テストはモックによる採否と契約の検証です。実Jevの精度改善を証明するものではありません。実API比較を行う場合はモデル・対象例・結果・保持すべき指摘の誤除外を別途記録してください。
