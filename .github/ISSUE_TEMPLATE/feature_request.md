---
name: 機能開発・改善 (Feature Request / Task)
about: 新機能の追加、仕様変更、改善タスク用テンプレート
title: '[FEAT] '
labels: enhancement
assignees: ''
---

## 概要・目的
<!-- 実装したい機能や改善の目的、背景について簡潔に説明してください。 -->

## ユースケース・メリット
<!-- この機能・改善が提供する価値や、想定される使い方を記述してください。 -->

## 設計案・実装アプローチ
<!-- 実装のアイディアや、想定される変更内容について記述してください。 -->

---

## Footprint (ディスパッチャー読み取り用)
<!-- 
Orchestuneによる並列開発・スケジューリングを行う場合、以下のFootprint YAMLブロックを記述してください。
ディスパッチャーが依存関係を自動計算する際に読み取ります。不要な場合はこのセクションを削除するか、空にしてください。
-->

```yaml
subtask_id: <subtask_id>
footprint:
  - <path/to/modified_file>
symbols:
  - <modified_class_or_function>
depends_on: []
```

## 影響範囲
<!-- 変更によって影響を受ける可能性があるコンポーネントや依存モジュールがあれば記載してください。 -->
