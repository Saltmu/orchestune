"""Tests for the read-only Jev outcome report (evaluation log x PR thread outcome)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import jev_outcome_report as report


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": "2026-10-01T00:00:00+00:00",
        "reviewer": "codex",
        "pr": 7,
        "path": "scripts/a.py",
        "line": 10,
        "comment": "Fix the bug",
        "validity": 0.9,
        "impact": "HIGH",
        "accepted": True,
        "bypassed": False,
        "schema_version": 2,
        "applicability": "APPLICABLE",
        "applicability_confidence": 0.8,
        "decision_reason": "accepted",
    }
    row.update(overrides)
    return row


def _thread(
    body: str = "Fix the bug",
    *,
    path: str = "scripts/a.py",
    line: int | None = 10,
    resolved: bool = False,
    outdated: bool = False,
    replies: int = 0,
) -> dict[str, Any]:
    comments = [{"body": body, "path": path, "line": line, "originalLine": line}] + [
        {"body": f"reply {i}", "path": path, "line": line} for i in range(replies)
    ]
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": {"nodes": comments},
    }


def _write_log(path: Path, rows: list[Any]) -> Path:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestLoadLog:
    def test_skips_broken_and_non_object_lines_and_counts_them(
        self, tmp_path: Path
    ) -> None:
        log = _write_log(
            tmp_path / "e.jsonl", [_row(), "{not json", "[1, 2]", "", _row(pr=8)]
        )

        loaded = report.load_log([log])

        assert [r["pr"] for r in loaded.records] == [7, 8]
        assert loaded.skipped_lines == 2

    def test_reads_multiple_files(self, tmp_path: Path) -> None:
        a = _write_log(tmp_path / "a.jsonl", [_row(pr=1)])
        b = _write_log(tmp_path / "b.jsonl", [_row(pr=2)])

        loaded = report.load_log([a, b])

        assert [r["pr"] for r in loaded.records] == [1, 2]

    def test_missing_file_is_reported_not_raised(self, tmp_path: Path) -> None:
        loaded = report.load_log([tmp_path / "nope.jsonl"])

        assert loaded.records == []
        assert loaded.missing_files == [str(tmp_path / "nope.jsonl")]


class TestDedupe:
    def test_keeps_latest_timestamp_per_finding(self) -> None:
        old = _row(timestamp="2026-10-01T00:00:00+00:00", validity=0.1)
        new = _row(timestamp="2026-10-02T00:00:00+00:00", validity=0.9)

        kept, collapsed = report.dedupe([new, old])

        assert kept == [new]
        assert collapsed == 1

    def test_whitespace_differences_are_the_same_finding(self) -> None:
        a = _row(comment="Fix  the\nbug ")
        b = _row(comment="Fix the bug", timestamp="2026-10-03T00:00:00+00:00")

        kept, collapsed = report.dedupe([a, b])

        assert kept == [b]
        assert collapsed == 1

    def test_different_pr_or_path_are_distinct(self) -> None:
        rows = [_row(pr=1), _row(pr=2), _row(path="other.py")]

        kept, collapsed = report.dedupe(rows)

        assert len(kept) == 3
        assert collapsed == 0


class TestClassify:
    @pytest.mark.parametrize(
        ("thread", "label"),
        [
            (_thread(resolved=True), "resolved"),
            (_thread(resolved=True, outdated=True, replies=2), "resolved"),
            (_thread(outdated=True), "outdated"),
            (_thread(outdated=True, replies=1), "outdated"),
            (_thread(replies=1), "replied"),
            (_thread(), "unresolved"),
        ],
    )
    def test_label_precedence(self, thread: dict[str, Any], label: str) -> None:
        parsed = report.parse_threads([thread])[0]

        assert report.classify(parsed) == label


class TestMatch:
    def test_matches_on_path_and_normalized_body(self) -> None:
        threads = report.parse_threads([_thread("Fix  the\nbug")])

        found = report.match_thread(_row(), threads)

        assert found is threads[0]

    def test_line_shift_still_matches(self) -> None:
        threads = report.parse_threads([_thread(line=42)])

        assert report.match_thread(_row(line=10), threads) is threads[0]

    def test_outdated_thread_with_null_line_still_matches(self) -> None:
        threads = report.parse_threads([_thread(line=None, outdated=True)])

        assert report.match_thread(_row(), threads) is threads[0]

    def test_different_path_does_not_match(self) -> None:
        threads = report.parse_threads([_thread(path="other.py")])

        assert report.match_thread(_row(), threads) is None

    def test_different_body_does_not_match(self) -> None:
        threads = report.parse_threads([_thread("Something else")])

        assert report.match_thread(_row(), threads) is None

    def test_duplicate_bodies_prefer_closest_line(self) -> None:
        threads = report.parse_threads([_thread(line=100), _thread(line=11)])

        assert report.match_thread(_row(line=10), threads) is threads[1]

    def test_identical_repeated_threads_prefer_the_latest_on_equal_distance(
        self,
    ) -> None:
        older = _thread(line=10, outdated=True)
        newer = _thread(line=10, resolved=True)
        threads = report.parse_threads([older, newer])

        assert report.match_thread(_row(line=10), threads) is threads[1]
        assert report.match_thread(_row(line=None), threads) is threads[1]


class TestBuildReport:
    def _fetcher(self, by_pr: dict[int, list[dict[str, Any]]]) -> Any:
        def fetch(pr: int) -> list[dict[str, Any]]:
            if pr not in by_pr:
                raise report.ThreadFetchError(f"no PR {pr}")
            return by_pr[pr]

        return fetch

    def test_cross_tab_by_decision_reason(self) -> None:
        rows = [
            _row(comment="a", decision_reason="accepted"),
            _row(comment="b", decision_reason="low_validity", validity=0.2),
            _row(comment="c", decision_reason="low_validity", validity=0.3),
        ]
        threads = [
            _thread("a", resolved=True),
            _thread("b", resolved=True),
            _thread("c"),
        ]

        result = report.build_report(rows, self._fetcher({7: threads}))

        assert result["by_decision_reason"]["accepted"] == {"resolved": 1}
        assert result["by_decision_reason"]["low_validity"] == {
            "resolved": 1,
            "unresolved": 1,
        }
        assert result["summary"]["evaluations"] == 3
        assert result["summary"]["matched"] == 3
        assert result["summary"]["unmatched"] == 0

    def test_fetch_failure_continues_and_is_reported(self) -> None:
        rows = [_row(pr=7), _row(pr=9, comment="x")]

        result = report.build_report(rows, self._fetcher({7: [_thread()]}))

        assert result["summary"]["matched"] == 1
        assert result["summary"]["unmatched"] == 1
        assert result["unmatched_reasons"] == {"fetch_failed": 1}
        assert result["fetch_errors"] == {"9": "no PR 9"}
        assert result["by_decision_reason"]["accepted"] == {
            "unresolved": 1,
            "unmatched": 1,
        }

    def test_unmatched_reasons_distinguish_no_match_and_no_pr(self) -> None:
        rows = [_row(comment="zzz"), _row(pr=None, comment="y")]

        result = report.build_report(rows, self._fetcher({7: [_thread()]}))

        assert result["unmatched_reasons"] == {"no_match": 1, "no_pr": 1}

    def test_fetches_each_pr_once(self) -> None:
        calls: list[int] = []

        def fetch(pr: int) -> list[dict[str, Any]]:
            calls.append(pr)
            return [_thread()]

        report.build_report([_row(comment="a"), _row(comment="b")], fetch)

        assert calls == [7]

    def test_validity_bands_and_missing_fields(self) -> None:
        rows = [
            _row(comment="a", validity=0.1),
            _row(comment="b", validity=0.5),
            _row(comment="c", validity=0.95),
            _row(comment="d", validity=None),
        ]
        threads = [_thread(c) for c in "abcd"]

        result = report.build_report(rows, self._fetcher({7: threads}))

        bands = result["by_validity_band"]
        assert bands["<0.4"] == {"unresolved": 1}
        assert bands["0.4-0.6"] == {"unresolved": 1}
        assert bands[">=0.8"] == {"unresolved": 1}
        assert bands["n/a"] == {"unresolved": 1}

    def test_old_rows_without_decision_reason_go_to_unknown(self) -> None:
        row = _row()
        del row["decision_reason"]

        result = report.build_report([row], self._fetcher({7: [_thread()]}))

        assert result["by_decision_reason"] == {"unknown": {"unresolved": 1}}

    def test_per_pr_breakdown(self) -> None:
        rows = [_row(pr=7, comment="a"), _row(pr=8, comment="b")]
        fetch = self._fetcher({7: [_thread("a", resolved=True)], 8: [_thread("b")]})

        result = report.build_report(rows, fetch)

        assert result["by_pr"]["7"] == {"resolved": 1}
        assert result["by_pr"]["8"] == {"unresolved": 1}


class TestRender:
    def test_markdown_contains_denominators_and_caveats(self) -> None:
        result = report.build_report([_row()], lambda pr: [_thread(resolved=True)])

        text = report.render_markdown(result)

        assert "decision_reason" in text
        assert "resolved" in text
        assert "1/1" in text
        assert "交絡" in text
        assert "助言" in text
        assert "有効性の証拠ではありません" in text

    def test_json_is_round_trippable(self) -> None:
        result = report.build_report([_row()], lambda pr: [_thread()])

        assert json.loads(json.dumps(result)) == result


class TestFetchThreads:
    def _completed(self, nodes: list[Any], *, has_next: bool, cursor: str | None):
        payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                            "nodes": nodes,
                        }
                    }
                }
            }
        }
        return subprocess.CompletedProcess([], 0, json.dumps(payload), "")

    def test_paginates_and_uses_read_only_graphql_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pages = [
            self._completed([_thread("a")], has_next=True, cursor="C1"),
            self._completed([_thread("b")], has_next=False, cursor=None),
        ]
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            commands.append(cmd)
            return pages[len(commands) - 1]

        monkeypatch.setattr(report.subprocess, "run", fake_run)

        nodes = report.fetch_threads(7)

        assert len(nodes) == 2
        assert commands[0][:3] == ["gh", "api", "graphql"]
        joined = " ".join(commands[0])
        assert "mutation" not in joined
        assert "number=7" in joined
        assert "cursor=C1" in " ".join(commands[1])

    def test_gh_failure_raises_thread_fetch_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            raise subprocess.CalledProcessError(1, cmd, "", "boom")

        monkeypatch.setattr(report.subprocess, "run", fake_run)

        with pytest.raises(report.ThreadFetchError):
            report.fetch_threads(7)

    def test_gh_missing_raises_thread_fetch_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            raise FileNotFoundError("gh")

        monkeypatch.setattr(report.subprocess, "run", fake_run)

        with pytest.raises(report.ThreadFetchError):
            report.fetch_threads(7)

    def test_malformed_response_raises_thread_fetch_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            report.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "{}", ""),
        )

        with pytest.raises(report.ThreadFetchError):
            report.fetch_threads(7)


class TestMain:
    def test_offline_threads_dir_markdown(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log = _write_log(tmp_path / "e.jsonl", [_row()])
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "7.json").write_text(
            json.dumps([_thread(resolved=True)]), encoding="utf-8"
        )

        code = report.main(["--log", str(log), "--threads-json", str(threads_dir)])

        out = capsys.readouterr().out
        assert code == 0
        assert "resolved" in out

    def test_json_format(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log = _write_log(tmp_path / "e.jsonl", [_row()])
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "7.json").write_text(json.dumps([_thread()]), encoding="utf-8")

        code = report.main(
            [
                "--log",
                str(log),
                "--threads-json",
                str(threads_dir),
                "--format",
                "json",
            ]
        )

        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert data["summary"]["evaluations"] == 1

    def test_missing_offline_file_counts_as_fetch_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log = _write_log(tmp_path / "e.jsonl", [_row()])
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        code = report.main(
            [
                "--log",
                str(log),
                "--threads-json",
                str(threads_dir),
                "--format",
                "json",
            ]
        )

        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert data["unmatched_reasons"] == {"fetch_failed": 1}

    def test_no_readable_log_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = report.main(["--log", str(tmp_path / "missing.jsonl")])

        assert code == 2
        assert "missing.jsonl" in capsys.readouterr().err

    def test_default_log_is_resolved_from_shared_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("JEV_LOG_PATH", raising=False)
        monkeypatch.chdir(tmp_path)
        log_dir = tmp_path / ".orchestune" / "jev"
        log_dir.mkdir(parents=True)
        _write_log(log_dir / "evaluations.jsonl", [_row()])
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "7.json").write_text(json.dumps([_thread()]), encoding="utf-8")

        code = report.main(["--threads-json", str(threads_dir), "--format", "json"])

        assert code == 0
        assert json.loads(capsys.readouterr().out)["summary"]["evaluations"] == 1
