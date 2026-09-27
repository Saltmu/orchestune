# 移動前のmainで確定した状態JSON

`2a4775a13ff58d018627080ea543d5fbe3c22597` の状態永続化実装を実行し、
読み込み可能な `input` と固定時刻 `now` で保存した `normalized` を記録した。
親ブランチの移動前実装との相違はenumのimport先のみであり、処理本体は同一。
期待値はledgerへの移動前に確定し、移動後の実装で再生成しない。

- `minimal`: 必須の所有権フィールドを持ち、省略可能なフィールドの既定値を検証。
- `completing`: completion journal、投稿evidence、handoffフラグ、実行profileを保存。
- `retention`: launch保持期間の境界、古い完了履歴の削除、回収カウンタ保持、
  通知キューの重複・無効値の除去、Usageの復元を検証。

回帰テストはJSON全体の一致と再読込を確認し、別途同じsibling lockの必須性と
不正な所有権・completion型の拒否も検証する。
