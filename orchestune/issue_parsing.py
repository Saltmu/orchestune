"""Issue本文からのタスク定義パース。"""

from __future__ import annotations

import math
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from orchestune.forge import MetadataSearchUnavailableError
from orchestune.models import IssueRecord, Task, normalize_newlines

if TYPE_CHECKING:
    from orchestune.forge import IssueForge

BASE_PRIORITY = {"low": 1.0, "medium": 2.0, "high": 3.0}

FOOTPRINT_BLOCK_PATTERN = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)

# Embedded in every parent (EPIC) issue `provisioning.py` creates, and required
# (in addition to an exact title match) before an orphan-recovery lookup is
# allowed to adopt an existing issue as "our" parent: an unrelated issue that
# coincidentally has the same title has no way to also have this exact literal
# string in its body. Lives here (L2) rather than in `provisioning.py` (L4) so
# that `dispatch_cycle.py` (L3) can validate a `--parent-issue` number against
# it without depending on the entrypoint-layer `provisioning` module.
PARENT_MARKER = "<!-- orchestune:decomposition-plan-parent -->"

# #514: 親(EPIC) Issue本文へ`launch_history`を永続化するブロックの目印。
# 子Issueと違い親Issueは`provisioning._parent_body`が示す通りFootprint YAML
# フェンスを持たないため、専用マーカーで自分のフェンスを識別する。マーカーを
# 必須にすることで、無関係な```yamlフェンス（人間が本文へ書いたもの等）を
# 誤って読み書きすることも防ぐ。
LAUNCH_HISTORY_MARKER = "<!-- orchestune:launch-history -->"
LAUNCH_HISTORY_BLOCK_PATTERN = re.compile(
    re.escape(LAUNCH_HISTORY_MARKER) + r"\r?\n```yaml\s*\n(.*?)```[ \t]*\r?\n?",
    re.DOTALL,
)

# #532: 親(EPIC) Issue本文へ分解計画（`decomposition_plan.md`のFrontmatter YAML）を
# 永続化するブロックの目印。ローカルの計画ファイルがworktree破棄等で消失しても、
# 親Issue本文から計画全体と各サブタスクのissue_number等のメタデータを復元できるようにする。
DECOMPOSITION_PLAN_MARKER = "<!-- orchestune:decomposition-plan -->"
DECOMPOSITION_PLAN_BLOCK_PATTERN = re.compile(
    re.escape(DECOMPOSITION_PLAN_MARKER)
    + r"\r?\n```yaml[^\n]*\n(.*?)\r?\n```[ \t]*(?:\r?\n|$)",
    re.DOTALL,
)


def _last_block_match(pattern: re.Pattern[str], body: str) -> re.Match[str] | None:
    """#664: マーカーブロックの**最後**の出現を返す。

    先頭固定（`search`）だと、過去の不具合で重複追記された本文から最も古い
    コピーを読んでしまう（`issue_number`未充填の計画を復元し、再provisionで
    Issueを重複作成する）。最後＝最新の状態を採用する。
    """
    matches = list(pattern.finditer(body))
    return matches[-1] if matches else None


def _replace_all_blocks(
    pattern: re.Pattern[str], body: str, new_block: str
) -> str | None:
    """#664: 本文中の該当ブロックを**すべて**取り除き、先頭があった位置へ
    `new_block`を1つだけ置いた本文を返す。ブロックが無ければNone。

    追記ではなく置換に倒すことで更新を冪等にし、既に重複してしまった本文
    （Issue #486は8個まで増殖した）も次回の書き込みで1つへ自己修復する。
    ブロック以外の本文は不変。重複ブロックの間にあった**空白のみ**の区切り
    （追記時に挿入された空行）はブロックと一緒に取り除くが、本文全体の空行を
    畳むことはしない（#666レビュー: 本文の他の場所にある3行以上の連続改行や
    コードフェンス内の空行まで巻き添えで潰してしまうため）。
    """
    matches = list(pattern.finditer(body))
    if not matches:
        return None
    pieces = [body[: matches[0].start()], new_block]
    cursor = matches[0].end()
    for match in matches[1:]:
        separator = body[cursor : match.start()]
        # 重複ブロック同士の区切りは、ブロックの一部として捨てる。散文が
        # 挟まっていた場合はそのまま残す（内容を失わない方を優先する）。
        if separator.strip():
            pieces.append(separator)
        cursor = match.end()
    pieces.append(body[cursor:])
    return "".join(pieces)


def decomposition_plan_from_parent_body(body: str) -> dict | None:
    """#532: 親Issue本文へ永続化された分解計画YAMLを読み取る。

    マーカー欠落・壊れたYAML・辞書形式でないものはNoneを返す。
    """
    match = _last_block_match(DECOMPOSITION_PLAN_BLOCK_PATTERN, body)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def embed_decomposition_plan_in_parent_body(body: str, plan_data: dict | str) -> str:
    """#532: 親Issue本文の`decomposition_plan`ブロックを書き換えて返す。
    ブロックが無ければ本文末尾へ追記する。

    plan_dataがdictの場合はyaml.dumpでシリアライズする。
    ブロック以外の本文はバイト単位で不変に保つ。
    """
    if isinstance(plan_data, dict):
        block_body = yaml.dump(
            plan_data,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
    else:
        block_body = plan_data.strip() + "\n"
    new_block = f"{DECOMPOSITION_PLAN_MARKER}\n```yaml\n{block_body}```\n"
    updated = _replace_all_blocks(DECOMPOSITION_PLAN_BLOCK_PATTERN, body, new_block)
    if updated is not None:
        return updated
    separator = "" if body.endswith("\n") else "\n"
    return f"{body}{separator}\n{new_block}"


def restore_plan_dict_to_markdown(
    plan_dict: dict, prose_body: str | None = None
) -> str:
    """#532: 分解計画dictを`decomposition_plan.md`形式のテキスト文字列へ変換する。"""
    frontmatter_yaml = yaml.dump(
        plan_dict,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    if prose_body is not None and prose_body.strip():
        body_content = prose_body.strip() + "\n"
    else:
        title = plan_dict.get("title", "Restored Decomposition Plan")
        description = plan_dict.get("description", "")
        body_content = f"# {title}\n"
        if description:
            body_content += f"\n{description}\n"
    return f"---\n{frontmatter_yaml}---\n\n{body_content}"


def restore_plan_markdown_from_parent_body(body: str) -> str | None:
    """#532: 親Issue本文から分解計画dictを抽出し、`decomposition_plan.md`形式のMarkdown文書として復元する。

    親Issue本文のマーカー前にあるProse本文（人間が記述した説明文・背景等）も引き継いで完全なMarkdownを復元する。
    """
    plan_dict = decomposition_plan_from_parent_body(body)
    if not plan_dict:
        return None
    # #664: 復元先はローカルの`decomposition_plan.md`なので、GitHub由来のCRLFを
    # 持ち込まずLFへ揃える。
    body = normalize_newlines(body)
    # Extract prose before the decomposition plan marker specifically
    parts = re.split(re.escape(DECOMPOSITION_PLAN_MARKER), body, maxsplit=1)
    prose_body = None
    if parts:
        raw_prose = parts[0]
        # Strip PARENT_MARKER and standard auto-generated boilerplate if present
        cleaned_prose = re.sub(re.escape(PARENT_MARKER), "", raw_prose)
        cleaned_prose = re.sub(
            r"配下のサブタスクはこのIssueのSub-issueとして紐付けられます。",
            "",
            cleaned_prose,
        ).strip()
        if cleaned_prose:
            prose_body = cleaned_prose
    return restore_plan_dict_to_markdown(plan_dict, prose_body=prose_body)


def _parse_footprint_block(body: str) -> dict | None:
    """Footprint YAMLフェンスをdictとして返す（存在しない/壊れている場合はNone）。"""
    match = FOOTPRINT_BLOCK_PATTERN.search(body)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


_INTEGER_STRING_PATTERN = re.compile(r"-?\d+")


def _validated_parent_issue_number(value: object) -> int | None:
    """Strictly coerce a raw (already YAML-parsed) `parent_issue_number`
    value, or reject it as `None` if it isn't unambiguously an integer.

    #485 review round 8 (P2): `int(100.9)` truncates instead of rejecting
    a malformed non-integral value, and `int(True)` == 1 would silently
    accept a YAML boolean as a valid issue number. Only accept an actual
    `int` (excluding `bool`, a subclass of `int`) or a digit-only string
    (rejecting a numeric-looking-but-fractional string like `"100.9"`).
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER_STRING_PATTERN.fullmatch(value.strip()):
        return int(value)
    return None


def parent_issue_number_from_body(body: str) -> int | None:
    """#485: ネイティブSub-issue関係が使えない環境向けに、Footprint YAML
    フェンスへ永続化された`parent_issue_number`を読み取るフォールバック。

    ネイティブの`issue.parent`が利用できる場合はそちらを優先すべきで、
    これは`gh`/GitHub MCPが関係操作を提供しない縮退時にのみ使われる。
    """
    data = _parse_footprint_block(body)
    if not data:
        return None
    return _validated_parent_issue_number(data.get("parent_issue_number"))


def effective_parent_number(issue: IssueRecord) -> int | None:
    """ネイティブSub-issue関係(`issue.parent`)を優先し、無い場合のみ本文
    metadataにフォールバックする（#485）。

    ネイティブ関係がある場合はそれを唯一の正とみなし、本文metadataは一切
    参照しない: Issueがネイティブに再親化された後も、本文の
    `parent_issue_number`が古いまま更新されていないケースがありうるため
    （#485 review P2）、本文metadataを「ネイティブが無い場合のみの
    フォールバック」以上の意味で使うと、旧親の完了判定やprovisioningの
    再利用インデックスが誤って古い親の下にこのIssueを含めてしまう。
    """
    if issue.parent and issue.parent.get("number") is not None:
        return issue.parent.get("number")
    return parent_issue_number_from_body(issue.body)


def backfill_parent_issue_number(body: str, parent_issue_number: int) -> str | None:
    """#485 review (P2): a reused Issue can predate this field (created by
    an older template, or by a prior run before `parent_issue_number` was
    added to `.github/issue_template.md`). Returns `body` with the
    Footprint YAML fence's `parent_issue_number` set/corrected, or `None`
    if it's already correct (nothing to write) or the fence can't be
    parsed at all (nothing safe to patch).

    Only the Footprint fence is touched — the rest of the body (which may
    carry human edits) is left byte-for-byte identical, unlike re-rendering
    the whole body from the template would.
    """
    match = FOOTPRINT_BLOCK_PATTERN.search(body)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    # #485 review round 9 (P2): compare via the same strict validator
    # `parent_issue_number_from_body` uses, not a raw `==` against the
    # freshly-parsed YAML value. A raw comparison would treat
    # `parent_issue_number: true` as "already correct" for parent #1
    # (`True == 1` in Python) or `100.0` as correct for #100, even though
    # the strict parser rejects both — silently skipping the backfill for
    # a body that isn't actually valid, discoverable metadata.
    if (
        _validated_parent_issue_number(data.get("parent_issue_number"))
        == parent_issue_number
    ):
        return None
    data["parent_issue_number"] = parent_issue_number
    new_block = yaml.dump(data, allow_unicode=True, default_flow_style=False)
    start, end = match.span(1)
    return body[:start] + new_block + body[end:]


def _validated_recompute_count(value: object) -> int:
    """#513: `_validated_parent_issue_number`と同様、`int()`による機械的な
    丸め（`int(2.9) == 2`）や`bool`の暗黙変換（`int(True) == 1`）を避け、
    実際に整数として書かれた値のみを受理する。壊れた値は「まだ0回」と
    同じ意味に倒す——過大なrecompute_countを誤って引き継ぐと、本来まだ
    再計算できるはずのタスクが即座にforced_serialへ落ちてしまうため。
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    return 0


def recovery_counters_from_body(body: str) -> tuple[int, bool]:
    """#513: Footprint YAMLフェンスへ永続化された`recompute_count`/
    `forced_serial`を読み取る。自己修復（`dispatch_recovery.py`）が
    `run_state.json`消失時にConflict Graph再計算のリトライ上限とforced_serial
    フォールバックを復元するために使う。

    フェンス欠落・壊れたYAML・フィールド欠落（本フィールド導入前に
    作られたIssue）は、いずれも`(0, False)`——「まだ一度も再計算して
    いない」と同じ意味——にフォールバックする。
    """
    data = _parse_footprint_block(body)
    if not data:
        return (0, False)
    recompute_count = _validated_recompute_count(data.get("recompute_count"))
    forced_serial = data.get("forced_serial") is True
    return (recompute_count, forced_serial)


def backfill_recovery_counters(
    body: str, recompute_count: int, forced_serial: bool
) -> str | None:
    """#513: Footprint YAMLフェンスの`recompute_count`/`forced_serial`を
    書き換えて返す。`backfill_parent_issue_number`と同じくフェンス以外の
    本文はバイト単位で不変に保つ。値が既に一致していれば`None`（無駄な
    `update_issue_body`呼び出しを避ける）。
    """
    match = FOOTPRINT_BLOCK_PATTERN.search(body)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    if recovery_counters_from_body(body) == (recompute_count, forced_serial):
        return None
    data["recompute_count"] = recompute_count
    data["forced_serial"] = forced_serial
    new_block = yaml.dump(data, allow_unicode=True, default_flow_style=False)
    start, end = match.span(1)
    return body[:start] + new_block + body[end:]


def launch_history_from_body(body: str) -> list[float]:
    """#514: 親Issue本文へ永続化された`launch_history`（起動タイムスタンプ列）を
    読み取る。自己修復（`dispatch_reconciliation.py`）が`run_state.json`消失時に
    `max_launches_per_window`を復元するために使う。

    マーカー欠落（本フィールド導入前に作られた親Issue）・壊れたYAML・
    数値化できない要素は、いずれも「起動履歴なし」＝空リストへ倒す:
    壊れた値で上限判定を誤らせるより、復元できなかった分だけ緩くなる方が
    安全側（既定の`max_concurrent`は別途効く）。

    #666レビュー: CRLF本文でブロックが重複追記されていた期間は、読み取りが
    毎回空を返していたため、追記された各ブロックはその時点の1回分しか
    持たない。最後のブロックだけを読むとウィンドウ内の他の起動を取りこぼし、
    次の永続化でその不完全な履歴が正本になって`max_launches_per_window`を
    超過し得る。全ブロックを`reconciliation`と同じ多重集合マージ
    （タイムスタンプ値ごとに最大個数を採用）で束ねてから返す。
    """
    merged: Counter[float] = Counter()
    for match in LAUNCH_HISTORY_BLOCK_PATTERN.finditer(body):
        merged |= Counter(_launch_history_from_block(match.group(1)))
    return sorted(merged.elements())


def _launch_history_from_block(raw_yaml: str) -> list[float]:
    """`launch_history`ブロック1個分のタイムスタンプ列を取り出す。"""
    try:
        data = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("launch_history")
    if not isinstance(raw, list):
        return []
    timestamps: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int | float):
            continue
        # #519レビュー6巡目(P2): 本文が手で編集・破損して`.inf`/`.nan`が
        # 入ると`yaml.safe_load`はPythonのfloatを返すため型チェックを通過する。
        # `.inf`は`quota_available`の`now - inf < window_seconds`が毎サイクル
        # 真になり、そのエントリが永久に1スロットを食い潰して親配下の
        # ディスパッチを止める。`.nan`は逆にあらゆる比較が偽になり、
        # ウィンドウ判定をすり抜けて記録が静かに失われる。
        if not math.isfinite(item):
            continue
        timestamps.append(float(item))
    return timestamps


def launch_history_in_window(
    timestamps: list[float], now: float, window_seconds: float
) -> list[float]:
    """#514: 起動タイムスタンプ列をウィンドウ内へ絞る（永続化側・復元側の
    両方が同じ意味論を使うための共通関数）。

    `now`を中心に前後1ウィンドウの帯だけを残し、値そのものは**変更しない**。

    - 下限（`now - window_seconds`）より古い: ウィンドウを抜けた記録
      （`prune_run_state`と同じ意味論）。
    - 上限（`now + window_seconds`）より先: 過去の起動の記録としてあり得ない
      値。`#519`レビュー7巡目(P2)の指摘どおり、下限だけを見ていると本文が
      手で編集されて`999999999999`のような有限の未来値が入った場合に
      `quota_available`の`now - t < window_seconds`が何年も真であり続け、
      その親配下のディスパッチが止まる（`math.isfinite`は`.inf`しか弾けない）。
      壊れた値は捨てるという既存方針（`.inf`/`.nan`/数値化できない要素）と
      同じ扱いにする。

    帯の内側にある「わずかに未来」の値は、ランナー間のクロックずれで書かれた
    正当な記録なので**そのまま残す**。捨てると起動数の過少カウント＝上限を
    緩める危険側へ倒れる（本ファイル群の非対称: 過少は危険、過大は安全）。
    期限切れも`now + window_seconds`までずれるだけで正常に訪れる。

    #519レビュー8巡目(P2): ここで`now`へクランプして正規化してはならない。
    復元側のマージはタイムスタンプ値を同一性のキーにした多重集合なので、
    クランプ先が毎サイクル動くと同じ1回の起動が別々のエントリとして増え続け、
    1回の起動が複数スロットを消費してしまう（`1005`が`[1000]`→
    `[1000, 1001]`→`[1000, 1001, 1002]`と増える）。値を変えないことが
    そのまま同一性の保存になる。
    """
    return sorted(
        timestamp
        for timestamp in timestamps
        if now - window_seconds <= timestamp <= now + window_seconds
    )


def backfill_launch_history(body: str, launch_history: list[float]) -> str | None:
    """#514: 親Issue本文の`launch_history`ブロックを書き換えて返す。
    ブロックが無ければ本文末尾へ追記する（親Issueは既存フェンスを持たないため、
    `backfill_recovery_counters`と違い新規作成もこの関数の責務）。

    既に同じ値であれば`None`（無駄な`update_issue_body`呼び出しを避ける）。
    ブロック以外の本文はバイト単位で不変に保つ。
    """
    if launch_history_from_body(body) == launch_history:
        return None
    block_body = yaml.dump(
        {"launch_history": list(launch_history)},
        allow_unicode=True,
        default_flow_style=False,
    )
    new_block = f"{LAUNCH_HISTORY_MARKER}\n```yaml\n{block_body}```\n"
    updated = _replace_all_blocks(LAUNCH_HISTORY_BLOCK_PATTERN, body, new_block)
    if updated is not None:
        return updated
    separator = "" if body.endswith("\n") else "\n"
    return f"{body}{separator}\n{new_block}"


@dataclass(frozen=True)
class ChildDiscoveryResult:
    issues: list[IssueRecord]
    # #485 review round 7 (P2): whether `find_issues_by_parent_metadata`
    # actually works on this `forge` (as opposed to raising a
    # capability-absence signal). A body correctly carrying
    # `parent_issue_number` is worthless for discovery if nothing can ever
    # search for it — callers must not treat `has_parent_metadata` as
    # sufficient proof of discoverability unless this is also `True`.
    metadata_search_supported: bool


def find_children_by_parent(
    forge: IssueForge, parent_issue_number: int | str
) -> ChildDiscoveryResult:
    """`parent_issue_number`配下の子Issueを、ネイティブSub-issue関係を起点に、
    本文metadataフォールバックで補完して返す（#485）。

    `forge`が構造的にこの検索をサポートしない場合（`MetadataSearchUnavailableError`、
    または後方互換のため未実装のforge実装が送出する`AttributeError`/
    `NotImplementedError`）は、ネイティブの結果だけを黙って返す（既存の
    `gh`ベース運用は完全動作のまま変わらない）。それ以外の失敗（`gh`認証切れ・
    レート制限・ネットワーク瞬断などの一時的なもの）は伝播させ、呼び出し元の
    再試行に委ねる — 黙って握りつぶすと、metadataでしか発見できないIssueが
    一時的に消え、`provisioning.py`のdedup fallbackが誤って重複作成しうる。
    """
    native = forge.list_sub_issues(parent_issue_number)
    seen = {issue.number for issue in native}

    try:
        candidates = forge.find_issues_by_parent_metadata(parent_issue_number)
    except (MetadataSearchUnavailableError, AttributeError, NotImplementedError) as e:
        print(
            f"Warning: this forge does not support parent-metadata search; "
            f"falling back to native sub-issue relationships only for "
            f"#{parent_issue_number}: {e}",
            file=sys.stderr,
        )
        return ChildDiscoveryResult(issues=native, metadata_search_supported=False)

    # #485 review (P2): re-verify with `effective_parent_number`, not a raw
    # body-metadata read — a candidate returned by `gh search`'s substring
    # match could have since been natively reparented elsewhere while its
    # body still carries the old `parent_issue_number` (never rewritten).
    # `effective_parent_number` treats a present native `parent` as
    # authoritative, so such a stale-body candidate is correctly rejected
    # here instead of indefinitely blocking the old parent's completion.
    target_number = int(parent_issue_number)
    extra: list[IssueRecord] = [
        candidate
        for candidate in candidates
        if candidate.number not in seen
        and effective_parent_number(candidate) == target_number
    ]
    return ChildDiscoveryResult(issues=native + extra, metadata_search_supported=True)


def is_epic_issue(issue: IssueRecord) -> bool:
    """`issue`がEPIC（親）Issueと構造的に一致するかを判定する。

    `provisioning.py`の持続済みparent番号検証は、特定のplanの`metadata.title`との
    厳密な一致まで要求するが、dispatch実行時（`--parent-issue`検証）にはどのplanの
    親かという情報がなく番号しか分からないため、「本物のEPICらしい構造を持つか」
    という緩い判定に留める。
    """
    return issue.title.startswith("[EPIC] ") and PARENT_MARKER in issue.body


def ensure_parent_marker(body: str) -> str:
    """`body`に`PARENT_MARKER`が無ければ追記して返す（既にあれば無変更）。

    `provision --parent-issue`が人間の手で起票済みのEPIC Issueを正規化する
    際に使う。既存の本文（人間が書いた説明文）はそのまま保持し、マーカーの
    追記のみ行う。
    """
    if PARENT_MARKER in body:
        return body
    return f"{body.rstrip()}\n\n{PARENT_MARKER}\n"


def _native_depends_on(
    issue: IssueRecord, issue_to_subtask_id: dict[int, str] | None
) -> tuple[str, ...]:
    if issue_to_subtask_id is None or not issue.blocked_by:
        return ()
    return tuple(
        issue_to_subtask_id[num]
        for num in issue.blocked_by
        if num in issue_to_subtask_id
    )


def _body_depends_on(match: re.Match | None, yaml_error: bool) -> tuple[str, ...]:
    if not match or yaml_error:
        return ()
    try:
        data = yaml.safe_load(match.group(1))
        if isinstance(data, dict):
            return tuple(str(d) for d in (data.get("depends_on") or []))
    except Exception:
        pass
    return ()


def _resolve_depends_on(
    issue: IssueRecord,
    issue_to_subtask_id: dict[int, str] | None,
    match: re.Match | None,
    yaml_error: bool,
) -> tuple[str, ...]:
    """#485 review (P1): a task can have some dependencies linked via native
    `blocked_by` and others not (e.g. one `set_blocked_by` call raised
    `RelationshipUnavailableError` after an earlier one succeeded for the
    same issue). Treating a non-empty native list as fully authoritative
    then silently drops the unlinked dependency, letting the task be
    promoted the moment only the linked one finishes. The body's
    `depends_on` is always the complete, authoritative list (rendered
    directly from `subtask.depends_on` at creation time), so union it in
    rather than discarding it whenever any native entry exists.
    """
    depends_on = _native_depends_on(issue, issue_to_subtask_id)
    for dep in _body_depends_on(match, yaml_error):
        if dep not in depends_on:
            depends_on += (dep,)
    return depends_on


def _extract_footprint_metadata(
    issue: IssueRecord,
) -> tuple[
    str,
    tuple[str, ...],
    tuple[str, ...],
    str | None,
    bool,
    bool,
    re.Match[str] | None,
]:
    subtask_id = ""
    footprint: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    shared_contract: str | None = None
    writes_shared_contract = False
    yaml_error = False
    match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    if match:
        try:
            data = yaml.safe_load(match.group(1))
            if isinstance(data, dict):
                subtask_id = str(data.get("subtask_id", ""))
                footprint = tuple(str(f) for f in (data.get("footprint") or []))
                symbols = tuple(str(s) for s in (data.get("symbols") or []))
                if data.get("shared_contract"):
                    shared_contract = str(data["shared_contract"])
                writes_shared_contract = data.get("writes_shared_contract") is True
        except yaml.YAMLError as e:
            print(
                f"Warning: Failed to parse YAML from issue #{issue.number}: {e}",
                file=sys.stderr,
            )
            yaml_error = True
    return (
        subtask_id,
        footprint,
        symbols,
        shared_contract,
        writes_shared_contract,
        yaml_error,
        match,
    )


def _extract_labels_metadata(
    labels: Sequence[str], issue_number: int
) -> tuple[str, bool, bool]:
    priority = "medium"
    has_unknown_priority_label = False
    risk = False
    progress_partial = False
    for label in labels:
        if label.startswith("priority:"):
            candidate = label.split(":", 1)[1]
            if candidate in BASE_PRIORITY:
                priority = candidate
            else:
                has_unknown_priority_label = True
                print(
                    f"Warning: Unknown priority label '{label}' on issue "
                    f"#{issue_number}; falling back to 'medium'.",
                    file=sys.stderr,
                )
        elif label == "risk:flagged":
            risk = True
        elif label == "progress:partial":
            progress_partial = True

    if has_unknown_priority_label:
        priority = "medium"
    return priority, risk, progress_partial


def _extract_parent_metadata(issue: IssueRecord) -> tuple[int | None, str | None]:
    if issue.parent:
        return issue.parent.get("number"), issue.parent.get("state")
    return parent_issue_number_from_body(issue.body), None


def parse_task_from_issue(
    issue: IssueRecord,
    issue_to_subtask_id: dict[int, str] | None = None,
) -> Task:
    (
        subtask_id,
        footprint,
        symbols,
        shared_contract,
        writes_shared_contract,
        yaml_error,
        match,
    ) = _extract_footprint_metadata(issue)
    depends_on = _resolve_depends_on(issue, issue_to_subtask_id, match, yaml_error)
    priority, risk, progress_partial = _extract_labels_metadata(
        issue.labels, issue.number
    )
    parent_number, parent_state = _extract_parent_metadata(issue)

    return Task(
        issue_number=issue.number,
        subtask_id=subtask_id,
        footprint=footprint,
        symbols=symbols,
        risk=risk,
        priority=priority,
        progress_partial=progress_partial,
        status_labels=tuple(issue.labels),
        created_at=issue.created_at,
        depends_on=depends_on,
        yaml_error=yaml_error,
        parent_number=parent_number,
        issue_state=issue.state,
        parent_state=parent_state,
        shared_contract=shared_contract,
        writes_shared_contract=writes_shared_contract,
    )
