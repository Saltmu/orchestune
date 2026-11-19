# #1882 適格比較バンドル

親 EPIC [#1842](https://github.com/Saltmu/orchestune/issues/1842)。比較契約は [`../planning-history-poc-protocol.md`](../planning-history-poc-protocol.md)。判定の本文は [`../planning-history-poc-eval.md`](../planning-history-poc-eval.md) の §8。ここにあるファイルが、レビュアーが checkout だけで照合する固定資料である。

#1874 の探索バッチ（プロセス上限 300 秒の 4 試行と、その後の 12 試行）は本ディレクトリに含まれない。探索記録が指す `.orchestune/tmp/eval-1874-20261119T013848Z-36cc12e0/` は checkout に無く、再検証時の作業環境にも残っていなかった。

## 置き場所・取得・保持

| 項目 | 内容 |
| --- | --- |
| 置き場所 | `docs/plans/planning-history-poc-1882/` |
| 取得 | この PR の head を checkout する。カード再生成は下のコマンドで、コミット済みの `corpus.json` と `requests/task-<N>.json` を渡す。 |
| 保持 | リポジトリの Git 履歴に残る間、このツリーと一緒に残る。作業用の一時ディレクトリへ移す保持期限は置かない。 |
| カード生成 SHA | `3b974a82fc723b1d391f71b91a2c522ab17714a2` |
| セレクタ blob | 上記 SHA の `scripts/planning_history_poc.py` は blob `a08f217fc937851d2063e84e91b1150066502e08`。本 PR はそのファイルを変更しない。 |
| 事前登録 digest | `pre_registration.json` の SHA-256 `178b4f58f131ff8e62641cd87c5f152e98e46685c2a912fba47f84fb4bba2769`。試行前に固定し、試行後に書き換えていない。 |

ログ・計画・盲検計画の SHA-256 は、各 JSONL レコードの `text` または `jsonl` フィールド（UTF-8）をハッシュした値であり、レコード内の `sha256` と一致する。公開前に環境固有の絶対パスを `LOCAL_HOME/` へ置換したバイト列が対象である。置換前のログはこのバンドルから復元しない。

公開ファイルに API トークンは無く、ユーザーホーム直下の絶対パスも残していない。PR 除外集合は凍結時の `closedByPullRequestsReferences` だけを記録する。5 課題は空で、`#1841` だけ `[1865]` を持つ。自己系列の除外は `exclude_issue_numbers` が担う。

## 再生成

```bash
for n in 1421 1588 1589 1590 1730 1841; do
  uv run python -m scripts.planning_history_poc \
    --corpus docs/plans/planning-history-poc-1882/corpus.json \
    --request "docs/plans/planning-history-poc-1882/requests/task-${n}.json" \
    --limit 3 --max-bytes 12288 | sha256sum
done
```

期待値は `cards/task-<N>.json` の次の SHA-256 である。

| 課題 | カード SHA-256 | 状態 |
| ---: | --- | --- |
| 1421 | `171bf94f480f414c7866f785c49d51325a39d1c9dc63dd9acff92a82a46942b5` | `no_matches`（`after_as_of` 4） |
| 1588 | `87bac5471f0d634275b9ea4d0f2bf2b890687e0a82f800854775145a6d2028df` | `over_budget`（`budget_dropped` 1、`lineage_excluded` 1、`after_as_of` 2） |
| 1589 | `39076a092396f68098f5415f4347335bc29f75cc1dd41138fd68b0acb3ec5769` | `over_budget`（`budget_dropped` 2） |
| 1590 | `2ff083aaffd10e1216d0245e3321c458d8f5d6fc675056ddbcf06265c823116e` | `over_budget`（`budget_dropped` 3） |
| 1730 | `aca329f81cf5104d4a2f409b140dd01d6c492c44632971ee6ebb4f244729980c` | `over_budget`（`budget_dropped` 3） |
| 1841 | `0a81cc713a1c999e050f1909c7bc07ef2e4d7eb993ad9cb12b560c91e99afa90` | `over_budget`（`budget_dropped` 3） |

6 件とも `returned_count` は 0 で、`cards` は空配列である。条件 B が足したのはこの封筒だけで、履歴カード本文は注入されていない。

## ファイル digest

| パス | bytes | SHA-256 |
| --- | ---: | --- |
| `pre_registration.json` | 258145 | `178b4f58f131ff8e62641cd87c5f152e98e46685c2a912fba47f84fb4bba2769` |
| `corpus.json` | 112060 | `834b528fdfa2f96966c3d5547a3f0b565b6e651f6041185e0db1837274960da5` |
| `raw-sources.jsonl` | 272593 | `2244b0478cd58bc3a651417d42c1dfb3a5318252f023741b9a60414dcad747d5` |
| `prompts/common.txt` | 866 | `8d681c7bb605bca995de9af41c5ab1fb73658fb0ebdc7bfa74268a29bb47fa3b` |
| `prompts/arm-a.txt` | 972 | `a592655170595b7015af2f380cc54c8cba980906f2c74e388b4e1f00fb913aab` |
| `prompts/arm-b.txt` | 1219 | `f1a6a0c1b85885df6dc5e420db566f27c96922dd609329a345799a33d3575e2d` |
| `requests/task-1421.json` | 21093 | `c228832d76044db9c58c83e9ba683c3747c0f3b35de2435c71234887288b572b` |
| `requests/task-1588.json` | 9229 | `b7e4edcba97377a4f2f6a1b62b9d4eb51332e6c2216421ce93030b1d85c88547` |
| `requests/task-1589.json` | 9918 | `f5c361bef39eefefcd990748fc3d6857e54f8304d9cc8f67d9aeed2fc94dab70` |
| `requests/task-1590.json` | 7351 | `82f6fd74cbf9739c932a7d64ba514a105baaa4e2e91ace9ed61a26c56346782b` |
| `requests/task-1730.json` | 30246 | `4ff92e2a3e2de2208a8312cc6dec6f8eff12ced3a6776d81ba73308ead5d1ddc` |
| `requests/task-1841.json` | 22870 | `b3c89d3a82c3b5e9efd209655d8d9736b0729e787e9512f293da2ab5c1ec2d17` |
| `trials/measurements.json` | 13083 | `d2e66c940a15f4c4b4e37b2748255c99a6969be7b293b3dbe65201a8a9108d80` |
| `trials/logs.jsonl` | 938575 | `64a59a7dfe797fb5a3d9fda36c84f5bacec27290538262b590c15b582f56ed5d` |
| `trials/plans.jsonl` | 185234 | `fa41874f78d74304bd7fa643a339bc3a64a48c4d4411c5a8f8cef77df41d6837` |
| `trials/blind-plans.jsonl` | 182878 | `13adf19e62f25a2742490fba74ed7275edfb52fedeca6c7e3825528944f6c2e1` |
| `trials/scores.json` | 11232 | `77756eb6115acedc85b437d6b96e7748c857a93d85408a2ce17d28a7b4380370` |
| `aggregation.json` | 5333 | `dde2becd78e265f0c02cc881a4df90d13146f70b6031720acff01cddfceafa15` |

`raw-sources.jsonl` は 60 行（`plan` 4、`task` 25、`outcome` 25、`requirement` 6）。各行は `source_id`, `kind`, `url`, `available_at`, `digest`, `text` を持つ。`digest` は `text` の SHA-256 である。要件行 `target-<issue>-requirement` の `available_at` と `digest` は、`pre_registration.json` の当該ターゲットの `as_of` と `requirement_digest` に一致する。

## 試行と匿名化計画の対応

採点は盲検 ID ごとに独立して行い、12 件の点を固定したあとで `trials/scores.json` に腕と `trial_id` を対応付けた。盲検計画 JSONL には `trial_id` と腕を入れていない。公開後、`blind-05` の採点用本文だけが「同梱 JSON」と条件を示していた。その句を採点用本文から外し、別の採点者がその本文だけを採点し直した。生計画 `trials/plans.jsonl` の当該試行は元の文のままである。他の 11 件の採点用本文には条件を示す句が無く、採点し直していない。

| 順 | trial_id | 盲検 | プロンプト SHA-256 | 計画 SHA-256 | ログ SHA-256 |
| ---: | --- | --- | --- | --- | --- |
| 1 | `trial-01-task-1421-A` | `blind-10` | `84ddc0a00181c2a32d634246c7f41827779fefef63cb211b89ca35f0f5b40e30` | `3dd2cdee61efe2cb33149007b339364343795c93075d96c1c97e06a65f6f2c68` | `3f103f62cc20fc31580fabcca9a5c27a2fc0729c430bf9b002855d840a5bebbc` |
| 2 | `trial-02-task-1421-B` | `blind-08` | `f60aa04aff83a053380fa39312c8990cd93aab4322fc4df296dd144bb48047df` | `b3d690e05fd5d9551c33ec889b9543a75bbb3fdf0b159be2dd910828f3b81242` | `832689b852be4daed82ed0c4bfa1befad35e6e4864231d15b7731ccba29f0266` |
| 3 | `trial-03-task-1588-B` | `blind-03` | `a9ba9eeb9b90f66cf0c8f5d8d2b1b6371e3c151e19e6061770c5e5613d42b0b4` | `870fc0575aa3361ae4079be5c25f5835320586db6a9a9c8eb4ec6348dad5ed0c` | `4bd365759acacaf95927aab427f7f3321b2f61a2ac1e19ae6be625dbe67dfc17` |
| 4 | `trial-04-task-1588-A` | `blind-01` | `2ee419bac1342afe8038a7e979b52b667616a96328cc4ef3f9b9b5ca55009b3e` | `3ee00e11f779bac64ce8ade0ec085de04450a7eedf5236ae0a9f09ee86cec38a` | `e167bb51020645d92a2de4f92b551d0462bbc878afc16f39b69bf26684092bca` |
| 5 | `trial-05-task-1589-A` | `blind-07` | `080d73dc7ffec920985aefd77849107365958f8ebd5bfcc410f6119bb6639468` | `2a4ec969fa820d13d9e8b31c6c91bcd36fb5c60a2c3381b87e451bee7eabf001` | `630ef00eb2c0452375e08c8c18e06ba5d38c50541444bc0f3c4b5c04dee727bb` |
| 6 | `trial-06-task-1589-B` | `blind-06` | `fb13ae9ff40120d6d5765a45a4ae4699c47c7cf7e44a5f5b0b762275017c2069` | `4200fb88a6c3696944870d56392f5b9d384167acd7996d91c8fd1b08468beb52` | `61966fd4048a3c2450b65eaab6e28d3fc562127c62b76e8257eeaa83464d98bc` |
| 7 | `trial-07-task-1590-B` | `blind-09` | `b48095f447b35edb46dbab2a40900ce48f3b86e4773f95345cec66163cf22b5d` | `e40415b151f6e7c0962186f8ee2c81e95f160cf2b2efd5f69c63665b93e2ad5f` | `a57a8bad2ccb04eb76a396ae7788b63205577e3eed8a1c35be7e8d241351c1be` |
| 8 | `trial-08-task-1590-A` | `blind-12` | `16952508032073260c361acac883cdbf0b3d1b239775654ad0f9a9f1df334595` | `18b371707d6dd46d0dc5f2b3a8dad8c10d5fc096c02e533b756a2c0a19e0fe8b` | `5f3f073c29fc4300f59a45d8275c59ddb6ed0bc236e47c40ad9e18b1757194d0` |
| 9 | `trial-09-task-1730-A` | `blind-02` | `26ec87aaf8b845b71ab767e6f89050fab1d7e3b98f2fee31c5fb213a85cc8eeb` | `aa3ff259d02b8ab80c7bb78317d9c7e0e5391bccbba776b88f03c6f1040a4401` | `62344a91a769083f595bee7143309b31944b1cf678c3a06876a3a5159856710f` |
| 10 | `trial-10-task-1730-B` | `blind-05` | `3b8640f83884ec0c8904403ee5778b8229b6e286a49a3d84020c8064a9d91de9` | `784c2044ca80898f40afde1d7990da8397a65efce202dbd593de46c70c3d6b9f` | `3881213b80e022424946076bf74b98b36b33413f0105a936a494480a0bd27142` |
| 11 | `trial-11-task-1841-B` | `blind-11` | `0d4c1d2a94dfdf4b76c037dc25c06de7946414f638d9aab8ddb8df79ed4b702c` | `7e7f80a297aa5e7426ef1119028ee96ca8f22a87a1657e80805dda5af0a2f750` | `00fd8ee78e672eafdf48b3e60b0ea5231d2d4694aee8d34b60ff34493aafd9fe` |
| 12 | `trial-12-task-1841-A` | `blind-04` | `79ab5b8da5adaecd781864e781e23e4a9095b967eb37d075281f37d48b2e59ef` | `52a92c9ff55645b88ad75d14a9f3b581005b8d900f8945bba942f167853b9e2b` | `54f69e198d3b9c37c933f9040f876e775a5581ee94e6b95ad30c5b9dab55331d` |

プロンプトの組み立ては、腕テンプレートの末尾改行を除いたバイト列に、空行、見出し `## Task`、空行、要件本文、末尾改行を足す。条件 B は続けて空行、見出し `## Cards`、空行、json フェンス、カードファイルのバイト列、閉じフェンスと改行を足す。カードファイルは末尾改行を含む。この規則と 12 個の組み立て済み digest は `pre_registration.json` の `prompt_assembly` と `assembled_prompt_digests` にある。
