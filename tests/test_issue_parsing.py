"""#485: ネイティブSub-issue関係が使えない環境向けフォールバックの単体テスト。"""

from __future__ import annotations

import pytest

from orchestune.forge import MetadataSearchUnavailableError
from orchestune.issue_parsing import (
    PARENT_MARKER,
    backfill_launch_history,
    backfill_parent_issue_number,
    backfill_recovery_counters,
    effective_parent_number,
    ensure_parent_marker,
    find_children_by_parent,
    launch_history_from_body,
    launch_history_in_window,
    parent_issue_number_from_body,
    recovery_counters_from_body,
)
from orchestune.models import IssueRecord


class TestParentIssueNumberFromBody:
    def test_parses_value_from_footprint_block(self):
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: 100\n```\n"
        assert parent_issue_number_from_body(body) == 100

    def test_returns_none_when_null(self):
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: null\n```\n"
        assert parent_issue_number_from_body(body) is None

    def test_returns_none_when_field_absent(self):
        body = "```yaml\nsubtask_id: task-a\n```\n"
        assert parent_issue_number_from_body(body) is None

    def test_returns_none_when_no_yaml_block(self):
        assert parent_issue_number_from_body("no yaml here") is None

    def test_returns_none_on_malformed_yaml(self):
        body = "```yaml\n: not valid: yaml: [\n```\n"
        assert parent_issue_number_from_body(body) is None

    def test_parses_quoted_integer_string(self):
        body = '```yaml\nsubtask_id: task-a\nparent_issue_number: "100"\n```\n'
        assert parent_issue_number_from_body(body) == 100

    def test_rejects_fractional_value_instead_of_truncating(self):
        """#485 review round 8 (P2): `int(100.9)` truncates to 100 rather
        than rejecting a malformed value — this must reject it instead,
        or a metadata-search substring match on '100' could wrongly treat
        the issue as a child of parent #100."""
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: 100.9\n```\n"
        assert parent_issue_number_from_body(body) is None

    def test_rejects_boolean_value(self):
        """`bool` is a subclass of `int` in Python; `int(True) == 1` would
        otherwise silently accept a YAML boolean as issue #1."""
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: true\n```\n"
        assert parent_issue_number_from_body(body) is None

    def test_rejects_non_numeric_string(self):
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: abc\n```\n"
        assert parent_issue_number_from_body(body) is None


class TestBackfillParentIssueNumber:
    def test_adds_missing_field_preserving_rest_of_body(self):
        body = (
            "# [FEAT] task-a: d\n\nSome human-written notes.\n\n"
            "```yaml\nsubtask_id: task-a\nfootprint: [src/foo.py]\n```\n"
        )
        result = backfill_parent_issue_number(body, 100)
        assert result is not None
        assert "Some human-written notes." in result
        assert parent_issue_number_from_body(result) == 100
        assert "subtask_id: task-a" in result

    def test_corrects_a_stale_value(self):
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: 999\n```\n"
        result = backfill_parent_issue_number(body, 100)
        assert result is not None
        assert parent_issue_number_from_body(result) == 100

    def test_returns_none_when_already_correct(self):
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: 100\n```\n"
        assert backfill_parent_issue_number(body, 100) is None

    def test_returns_none_when_no_footprint_block(self):
        assert backfill_parent_issue_number("no yaml here", 100) is None

    def test_rewrites_a_boolean_value_matching_via_python_equality(self):
        """#485 review round 9 (P2): `True == 1` in Python, so a raw `==`
        against the parsed YAML value would wrongly treat
        `parent_issue_number: true` as "already correct" for parent #1
        even though `parent_issue_number_from_body` rejects booleans. Must
        actually rewrite it instead of silently no-oping."""
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: true\n```\n"
        result = backfill_parent_issue_number(body, 1)
        assert result is not None
        assert parent_issue_number_from_body(result) == 1

    def test_rewrites_a_fractional_value_matching_via_python_equality(self):
        """`100.0 == 100` in Python, so a raw `==` would wrongly treat
        `parent_issue_number: 100.0` as already correct even though the
        strict parser rejects non-integral values."""
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: 100.0\n```\n"
        result = backfill_parent_issue_number(body, 100)
        assert result is not None
        assert parent_issue_number_from_body(result) == 100


class _NativeOnlyForge:
    """`find_issues_by_parent_metadata`を実装しない旧来のForge。"""

    def __init__(self, native: list[IssueRecord]):
        self._native = native

    def list_sub_issues(self, parent_issue_number):
        return self._native


class _MetadataAwareForge(_NativeOnlyForge):
    def __init__(self, native, metadata_candidates):
        super().__init__(native)
        self._candidates = metadata_candidates
        self.metadata_calls = 0

    def find_issues_by_parent_metadata(self, parent_issue_number):
        self.metadata_calls += 1
        return self._candidates


class _UnsupportedMetadataForge(_NativeOnlyForge):
    def find_issues_by_parent_metadata(self, parent_issue_number):
        raise MetadataSearchUnavailableError("MCP does not expose issue search")


class _FlakyMetadataForge(_NativeOnlyForge):
    def find_issues_by_parent_metadata(self, parent_issue_number):
        raise RuntimeError("gh: rate limit exceeded")


def _issue(number, parent_issue_number, subtask_id="task-a"):
    return IssueRecord(
        number=number,
        title=f"[FEAT] {subtask_id}",
        body=(
            f"```yaml\nsubtask_id: {subtask_id}\n"
            f"parent_issue_number: {parent_issue_number}\n```\n"
        ),
        labels=(),
        created_at="2026-01-01T00:00:00Z",
    )


class TestFindChildrenByParent:
    def test_returns_native_only_when_forge_lacks_metadata_search(self):
        native = [_issue(1, 100)]
        forge = _NativeOnlyForge(native)

        result = find_children_by_parent(forge, 100)

        assert result.issues == native
        assert result.metadata_search_supported is False

    def test_merges_metadata_fallback_candidates_not_in_native_list(self):
        native = [_issue(1, 100)]
        # #485: issue 2 was created without a native sub-issue link (e.g. an
        # MCP without sub_issue_write) but carries the metadata field.
        candidates = [_issue(1, 100), _issue(2, 100, subtask_id="task-b")]
        forge = _MetadataAwareForge(native, candidates)

        result = find_children_by_parent(forge, 100)

        assert sorted(r.number for r in result.issues) == [1, 2]
        assert result.metadata_search_supported is True
        assert forge.metadata_calls == 1

    def test_rejects_candidates_whose_body_metadata_does_not_match(self):
        """gh --search is a substring match ('100' can hit '1000'); the
        result must be re-verified against the exact parsed value."""
        native = []
        candidates = [_issue(2, 1000, subtask_id="task-b")]
        forge = _MetadataAwareForge(native, candidates)

        assert find_children_by_parent(forge, 100).issues == []

    def test_rejects_candidates_whose_native_parent_disagrees_with_stale_body(self):
        """#485 review (P2): an issue natively reparented elsewhere, whose
        body still carries the old parent_issue_number (never rewritten),
        must not match the old parent via the metadata fallback — or it
        would indefinitely block that old parent's completion under the
        wrong scope."""
        native: list[IssueRecord] = []
        reparented = IssueRecord(
            number=2,
            title="[FEAT] task-b",
            body="```yaml\nsubtask_id: task-b\nparent_issue_number: 100\n```\n",
            labels=(),
            created_at="2026-01-01T00:00:00Z",
            parent={"number": 200},
        )
        forge = _MetadataAwareForge(native, [reparented])

        assert find_children_by_parent(forge, 100).issues == []

    def test_falls_back_to_native_only_when_metadata_search_is_unsupported(self):
        native = [_issue(1, 100)]
        forge = _UnsupportedMetadataForge(native)

        result = find_children_by_parent(forge, 100)
        assert result.issues == native
        assert result.metadata_search_supported is False

    def test_propagates_operational_metadata_search_failures(self):
        """#485 review (P1): a transient/operational failure (auth expired,
        rate limit, network) must not be silently downgraded to
        native-only — that can make a metadata-only child temporarily
        disappear from a parent-scoped cycle, and provisioning's own
        dedup could then recreate it as a duplicate."""
        native = [_issue(1, 100)]
        forge = _FlakyMetadataForge(native)

        with pytest.raises(RuntimeError):
            find_children_by_parent(forge, 100)

    def test_excludes_parent_issue_itself_from_children(self):
        """親Issue自身（EPIC本文にparent_issue_numberを持つ）がmetadata検索で
        ヒットしても、子Issueとして返されてはならない。"""
        native = [_issue(101, 100)]
        parent_as_candidate = IssueRecord(
            number=100,
            title="[EPIC] Parent Epic",
            body=(
                f"# [EPIC] Parent Epic\n\n{PARENT_MARKER}\n\n"
                "```yaml\nparent_issue_number: 100\n```\n"
            ),
            labels=(),
            created_at="2026-01-01T00:00:00Z",
            state="OPEN",
        )
        forge = _MetadataAwareForge(native, [parent_as_candidate])

        result = find_children_by_parent(forge, 100)

        assert [r.number for r in result.issues] == [101]

    def test_excludes_epic_issues_from_children(self):
        """EPICマーカーを持つ親Issueがmetadata検索でヒットした場合、
        子Issueとして扱われてはならない。"""
        native = [_issue(101, 100)]
        epic_candidate = IssueRecord(
            number=200,
            title="[EPIC] Another Epic",
            body=f"{PARENT_MARKER}\n```yaml\nparent_issue_number: 100\n```\n",
            labels=(),
            created_at="2026-01-01T00:00:00Z",
            state="OPEN",
        )
        forge = _MetadataAwareForge(native, [epic_candidate])

        result = find_children_by_parent(forge, 100)

        assert [r.number for r in result.issues] == [101]


class TestEffectiveParentNumber:
    def test_returns_none_when_self_parenting_in_body(self):
        issue = IssueRecord(
            number=100,
            title="Self-parenting issue",
            body="```yaml\nparent_issue_number: 100\n```\n",
            labels=(),
            created_at="2026-01-01T00:00:00Z",
        )
        assert effective_parent_number(issue) is None

    def test_returns_none_when_self_parenting_in_native_parent(self):
        issue = IssueRecord(
            number=100,
            title="Self-parenting issue",
            body="",
            labels=(),
            created_at="2026-01-01T00:00:00Z",
            parent={"number": 100},
        )
        assert effective_parent_number(issue) is None

    def test_returns_parent_number_when_different_from_self(self):
        issue = IssueRecord(
            number=101,
            title="Child issue",
            body="```yaml\nparent_issue_number: 100\n```\n",
            labels=(),
            created_at="2026-01-01T00:00:00Z",
        )
        assert effective_parent_number(issue) == 100

    def test_returns_none_when_parent_number_malformed(self):
        issue = IssueRecord(
            number=101,
            title="Child issue",
            body="```yaml\nparent_issue_number: abc\n```\n",
            labels=(),
            created_at="2026-01-01T00:00:00Z",
        )
        assert effective_parent_number(issue) is None


class TestEnsureParentMarker:
    def test_appends_marker_when_absent(self):
        body = "A human-written EPIC description."
        result = ensure_parent_marker(body)
        assert result.startswith(body)
        assert PARENT_MARKER in result

    def test_is_a_noop_when_marker_already_present(self):
        body = f"Some description.\n\n{PARENT_MARKER}\n"
        assert ensure_parent_marker(body) == body

    def test_preserves_existing_body_content_exactly(self):
        body = "Line one.\nLine two with *markdown*.\n"
        result = ensure_parent_marker(body)
        assert result.startswith("Line one.\nLine two with *markdown*.")


class TestRecoveryCountersFromBody:
    """#513: recompute_count/forced_serialのFootprint YAMLフェンス往復。"""

    def test_parses_values_from_footprint_block(self):
        body = (
            "```yaml\nsubtask_id: task-a\nrecompute_count: 2\n"
            "forced_serial: true\n```\n"
        )
        assert recovery_counters_from_body(body) == (2, True)

    def test_defaults_to_zero_and_false_when_fields_absent(self):
        """#513: 導入前に作られたIssue（フィールド欠落）への後方互換。"""
        body = "```yaml\nsubtask_id: task-a\n```\n"
        assert recovery_counters_from_body(body) == (0, False)

    def test_defaults_to_zero_and_false_when_no_yaml_block(self):
        assert recovery_counters_from_body("no yaml here") == (0, False)

    def test_defaults_to_zero_and_false_on_malformed_yaml(self):
        body = "```yaml\n: not valid: yaml: [\n```\n"
        assert recovery_counters_from_body(body) == (0, False)

    def test_forced_serial_defaults_to_false_when_only_recompute_count_present(self):
        body = "```yaml\nsubtask_id: task-a\nrecompute_count: 1\n```\n"
        assert recovery_counters_from_body(body) == (1, False)

    def test_rejects_non_integer_recompute_count(self):
        """#485レビューの`_validated_parent_issue_number`と同様、壊れた
        値を機械的にintへ丸めるのではなく0へフォールバックする。"""
        body = "```yaml\nsubtask_id: task-a\nrecompute_count: not-a-number\n```\n"
        assert recovery_counters_from_body(body) == (0, False)


class TestBackfillRecoveryCounters:
    def test_adds_missing_fields_preserving_rest_of_body(self):
        body = (
            "# [FEAT] task-a: d\n\nSome human-written notes.\n\n"
            "```yaml\nsubtask_id: task-a\nfootprint: [src/foo.py]\n```\n"
        )
        result = backfill_recovery_counters(body, 2, True)
        assert result is not None
        assert "Some human-written notes." in result
        assert recovery_counters_from_body(result) == (2, True)
        assert "subtask_id: task-a" in result

    def test_updates_an_existing_value(self):
        body = (
            "```yaml\nsubtask_id: task-a\nrecompute_count: 1\n"
            "forced_serial: false\n```\n"
        )
        result = backfill_recovery_counters(body, 2, False)
        assert result is not None
        assert recovery_counters_from_body(result) == (2, False)

    def test_returns_none_when_already_correct(self):
        body = (
            "```yaml\nsubtask_id: task-a\nrecompute_count: 2\n"
            "forced_serial: true\n```\n"
        )
        assert backfill_recovery_counters(body, 2, True) is None

    def test_returns_none_when_no_footprint_block(self):
        assert backfill_recovery_counters("no yaml here", 1, False) is None

    def test_preserves_other_footprint_fields(self):
        body = (
            "```yaml\nsubtask_id: task-a\nparent_issue_number: 100\n"
            "footprint: [src/foo.py]\n```\n"
        )
        result = backfill_recovery_counters(body, 1, False)
        assert result is not None
        assert "parent_issue_number: 100" in result
        assert "subtask_id: task-a" in result


class TestLaunchHistoryFromBody:
    """#514: 親Issue本文へ永続化したlaunch_historyの読み取り。

    子Issue（`recovery_counters_from_body`）と違い、親Issue（EPIC）は
    `_parent_body`が示す通りFootprint YAMLフェンスを持たないため、
    専用マーカー付きのフェンスを使う。
    """

    def test_parses_timestamps(self):
        body = (
            "EPIC description\n\n"
            "<!-- orchestune:launch-history -->\n"
            "```yaml\nlaunch_history:\n- 1000.5\n- 2000.25\n```\n"
        )
        assert launch_history_from_body(body) == [1000.5, 2000.25]

    def test_returns_empty_when_marker_absent(self):
        """本フィールド導入前に作られた親Issueへの後方互換。"""
        body = "EPIC description\n\n<!-- orchestune:decomposition-plan-parent -->"
        assert launch_history_from_body(body) == []

    def test_returns_empty_on_malformed_yaml(self):
        body = "<!-- orchestune:launch-history -->\n```yaml\n: not: valid: [\n```\n"
        assert launch_history_from_body(body) == []

    def test_ignores_non_numeric_entries(self):
        """壊れた値で上限判定を誤らせないよう、数値化できない項目は捨てる。"""
        body = (
            "<!-- orchestune:launch-history -->\n"
            "```yaml\nlaunch_history:\n- 1000.0\n- not-a-number\n- 2000.0\n```\n"
        )
        assert launch_history_from_body(body) == [1000.0, 2000.0]

    def test_rejects_non_finite_timestamps(self):
        """#519レビュー6巡目(P2): 本文が手で編集・破損して`.inf`/`.nan`が
        入ると、`yaml.safe_load`はPythonのfloatを返すため型チェックを通過する。

        `.inf`は`quota_available`の`now - inf < window_seconds`が毎サイクル
        真になり、そのエントリが永久に1スロットを食い潰して親配下の
        ディスパッチを止めてしまう。`.nan`は逆にあらゆる比較が偽になり、
        ウィンドウ判定をすり抜けて記録が静かに失われる。どちらも捨てる。
        """
        body = (
            "<!-- orchestune:launch-history -->\n"
            "```yaml\nlaunch_history:\n- .inf\n- .nan\n- -.inf\n- 1000.0\n```\n"
        )
        assert launch_history_from_body(body) == [1000.0]

    def test_does_not_confuse_a_child_footprint_fence(self):
        """子IssueのFootprintフェンスを誤って読まないこと（マーカー必須）。"""
        body = "```yaml\nsubtask_id: task-a\nlaunch_history:\n- 1000.0\n```\n"
        assert launch_history_from_body(body) == []


class TestLaunchHistoryInWindow:
    """#519レビュー7巡目(P2): ウィンドウ正規化の共通意味論（永続化側の
    `_persist_launch_history`とSupervisorのdesired-state導出が共有する）。"""

    def test_drops_timestamps_older_than_the_window(self):
        assert launch_history_in_window(
            [900.0, 800.0], now=1000.0, window_seconds=100
        ) == [900.0]

    def test_keeps_a_slightly_future_timestamp_unchanged(self):
        """クロックずれ由来の数秒未来は正当な記録なので捨てない（捨てると
        起動数の過少カウント＝上限が緩む危険側）。

        #519レビュー8巡目(P2): かつ`now`へクランプもしない。復元側のマージは
        タイムスタンプ値を同一性のキーにしているため、正規化で値が動くと
        同じ1回の起動が毎サイクル別エントリとして増えてしまう。
        """
        assert launch_history_in_window([1002.0], now=1000.0, window_seconds=100) == [
            1002.0
        ]

    def test_is_stable_across_repeated_normalization(self):
        """#519レビュー8巡目(P2)の核心: 同じ入力を異なる`now`で正規化しても、
        出力の値が変わらないこと（値が動くとマージがエントリを増殖させる）。"""
        persisted = [1005.0]
        assert (
            launch_history_in_window(persisted, now=1000.0, window_seconds=3600)
            == launch_history_in_window(persisted, now=1001.0, window_seconds=3600)
            == launch_history_in_window(persisted, now=1002.0, window_seconds=3600)
            == [1005.0]
        )

    def test_discards_beyond_one_window_into_the_future(self):
        """過去の起動の記録としてあり得ない値は捨てる（`math.isfinite`は
        `.inf`しか弾けず、有限の遥か未来値は永久にスロットを食い潰す）。"""
        assert (
            launch_history_in_window(
                [999_999_999_999.0, 1101.0], now=1000.0, window_seconds=100
            )
            == []
        )

    def test_keeps_both_boundary_values_inclusive(self):
        assert launch_history_in_window(
            [900.0, 1100.0], now=1000.0, window_seconds=100
        ) == [900.0, 1100.0]

    def test_preserves_duplicates_as_a_multiset(self):
        """同一サイクルの複数起動は同じ`now`を持つ正当なデータ（畳まない）。"""
        assert launch_history_in_window(
            [950.0, 950.0], now=1000.0, window_seconds=100
        ) == [950.0, 950.0]


class TestBackfillLaunchHistory:
    def test_creates_the_block_when_absent_preserving_body(self):
        body = "EPIC description\n\n<!-- orchestune:decomposition-plan-parent -->"
        result = backfill_launch_history(body, [1000.0, 2000.0])
        assert result is not None
        assert result.startswith("EPIC description")
        assert PARENT_MARKER in result
        assert launch_history_from_body(result) == [1000.0, 2000.0]

    def test_replaces_an_existing_block(self):
        body = (
            "EPIC\n\n<!-- orchestune:launch-history -->\n"
            "```yaml\nlaunch_history:\n- 1000.0\n```\n"
        )
        result = backfill_launch_history(body, [3000.0])
        assert result is not None
        assert launch_history_from_body(result) == [3000.0]
        assert "1000.0" not in result

    def test_returns_none_when_already_equal(self):
        body = (
            "<!-- orchestune:launch-history -->\n"
            "```yaml\nlaunch_history:\n- 1000.0\n```\n"
        )
        assert backfill_launch_history(body, [1000.0]) is None

    def test_writes_an_empty_list(self):
        """ウィンドウ外へ全て抜けた場合、空リストとして書ける。"""
        body = (
            "<!-- orchestune:launch-history -->\n"
            "```yaml\nlaunch_history:\n- 1000.0\n```\n"
        )
        result = backfill_launch_history(body, [])
        assert result is not None
        assert launch_history_from_body(result) == []

    def test_round_trips_through_the_parser(self):
        body = "EPIC"
        result = backfill_launch_history(body, [1.5, 2.5, 3.5])
        assert result is not None
        assert launch_history_from_body(result) == [1.5, 2.5, 3.5]


class TestExecutionProfileFromBody:
    """Footprint YAMLからのexecution_profile抽出とバリデーション。"""

    def test_parses_valid_execution_profile(self):
        from orchestune.issue_parsing import parse_task_from_issue
        from orchestune.models import IssueRecord

        issue = IssueRecord(
            number=1,
            title="[FEAT] task-a: test",
            body=(
                "```yaml\n"
                "subtask_id: task-a\n"
                "footprint: [src/foo.py]\n"
                "symbols: [Foo]\n"
                "depends_on: []\n"
                "execution_profile: fast-code_1\n"
                "```\n"
            ),
            labels=(),
            created_at="2026-01-01T00:00:00Z",
        )
        task = parse_task_from_issue(issue)
        assert task.execution_profile == "fast-code_1"

    def test_execution_profile_defaults_to_none_when_absent_or_null(self):
        from orchestune.issue_parsing import parse_task_from_issue
        from orchestune.models import IssueRecord

        issue_absent = IssueRecord(
            number=1,
            title="[FEAT] task-a: test",
            body="```yaml\nsubtask_id: task-a\n```\n",
            labels=(),
            created_at="2026-01-01T00:00:00Z",
        )
        assert parse_task_from_issue(issue_absent).execution_profile is None

        issue_null = IssueRecord(
            number=2,
            title="[FEAT] task-b: test",
            body="```yaml\nsubtask_id: task-b\nexecution_profile: null\n```\n",
            labels=(),
            created_at="2026-01-01T00:00:00Z",
        )
        assert parse_task_from_issue(issue_null).execution_profile is None

    @pytest.mark.parametrize(
        "invalid_profile",
        [
            "Fast-code",
            "fast code",
            "fast.code",
            "fast@code",
            "",
            "a" * 33,
            123,
            True,
            ["list"],
            {"dict": "val"},
        ],
    )
    def test_invalid_execution_profile_falls_back_to_none_safely(self, invalid_profile):
        import yaml

        from orchestune.issue_parsing import parse_task_from_issue
        from orchestune.models import IssueRecord

        data = {
            "subtask_id": "task-a",
            "execution_profile": invalid_profile,
        }
        body = f"```yaml\n{yaml.dump(data)}```\n"
        issue = IssueRecord(
            number=1,
            title="[FEAT] task-a: test",
            body=body,
            labels=(),
            created_at="2026-01-01T00:00:00Z",
        )
        task = parse_task_from_issue(issue)
        assert task.execution_profile is None
        assert task.yaml_error is False


class TestModelTierFromBody:
    """Footprint YAMLからのmodel_tier抽出とバリデーション。"""

    @pytest.mark.parametrize("tier", ["weak", "middle", "strong"])
    def test_parses_valid_model_tier(self, tier: str):
        from orchestune.issue_parsing import parse_task_from_issue
        from orchestune.models import IssueRecord

        issue = IssueRecord(
            number=1,
            title="[FEAT] task-a: test",
            body=(
                "```yaml\n"
                "subtask_id: task-a\n"
                "footprint: [src/foo.py]\n"
                "symbols: [Foo]\n"
                "depends_on: []\n"
                f"model_tier: {tier}\n"
                "```\n"
            ),
            labels=(),
            created_at="2026-01-01T00:00:00Z",
        )
        task = parse_task_from_issue(issue)
        assert task.model_tier == tier

    def test_model_tier_defaults_to_none_when_absent_or_null(self):
        from orchestune.issue_parsing import parse_task_from_issue
        from orchestune.models import IssueRecord

        issue_absent = IssueRecord(
            number=1,
            title="[FEAT] task-a: test",
            body="```yaml\nsubtask_id: task-a\n```\n",
            labels=(),
            created_at="2026-01-01T00:00:00Z",
        )
        assert parse_task_from_issue(issue_absent).model_tier is None

        issue_null = IssueRecord(
            number=2,
            title="[FEAT] task-b: test",
            body="```yaml\nsubtask_id: task-b\nmodel_tier: null\n```\n",
            labels=(),
            created_at="2026-01-01T00:00:00Z",
        )
        assert parse_task_from_issue(issue_null).model_tier is None

    @pytest.mark.parametrize(
        "invalid_tier",
        [
            "ultra",
            "low",
            "high",
            "Strong",
            "weak-tier",
            "",
            123,
            True,
            ["list"],
            {"dict": "val"},
        ],
    )
    def test_invalid_model_tier_falls_back_to_none_safely(self, invalid_tier):
        import yaml

        from orchestune.issue_parsing import parse_task_from_issue
        from orchestune.models import IssueRecord

        data = {
            "subtask_id": "task-a",
            "model_tier": invalid_tier,
        }
        body = f"```yaml\n{yaml.dump(data)}```\n"
        issue = IssueRecord(
            number=1,
            title="[FEAT] task-a: test",
            body=body,
            labels=(),
            created_at="2026-01-01T00:00:00Z",
        )
        task = parse_task_from_issue(issue)
        assert task.model_tier is None
        assert task.yaml_error is False
