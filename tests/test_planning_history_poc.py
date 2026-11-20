"""Unit and CLI contract tests for scripts/planning_history_poc.py."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from orchestune.context.budget import fit_context_budget

FIXTURE_CORPUS_PATH = Path("tests/fixtures/planning_history_poc_cases.json")

CANONICAL_REQUEST: dict[str, Any] = {
    "schema_version": "planning-history-query-v1",
    "repository": "Saltmu/orchestune",
    "as_of": "2026-11-01T00:00:00Z",
    "query": "共有コンテキスト索引とBM25検索カードの抽出契約・予算制限を分解する",
    "exclude_issue_numbers": [1400, 1401, 1402, 1409],
    "exclude_pr_numbers": [1410],
}


def _write_json(path: Path, data: Any) -> Path:
    path.write_bytes(
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    )
    return path


def _run_cli(
    corpus_path: Path,
    request_path: Path,
    *,
    limit: int | str | None = None,
    max_bytes: int | str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    cmd = [
        sys.executable,
        "-m",
        "scripts.planning_history_poc",
        "--corpus",
        str(corpus_path),
        "--request",
        str(request_path),
    ]
    if limit is not None:
        cmd.extend(["--limit", str(limit)])
    if max_bytes is not None:
        cmd.extend(["--max-bytes", str(max_bytes)])
    return subprocess.run(cmd, capture_output=True, check=False)


def _load_fixture_corpus() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE_CORPUS_PATH.read_text(encoding="utf-8"))
    return data


def _with_adopted_footprint(path: str) -> dict[str, Any]:
    corpus = _load_fixture_corpus()
    case = next(item for item in corpus["cases"] if item["case_id"] == "case-1100-rev2")
    case["tasks"][0]["footprint"] = [*case["tasks"][0]["footprint"], path]
    return corpus


def test_canonical_fixture_default_limit_and_budget(tmp_path: Path) -> None:
    request_path = _write_json(tmp_path / "request.json", CANONICAL_REQUEST)
    proc = _run_cli(FIXTURE_CORPUS_PATH, request_path)

    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    assert proc.stderr == b""
    assert proc.stdout.endswith(b"\n")
    assert not proc.stdout.endswith(b"\r\n")
    assert not proc.stdout.endswith(b"\n\n")
    assert fit_context_budget(proc.stdout, max_bytes=12288).fits

    payload = json.loads(proc.stdout.decode("utf-8"))
    assert list(payload.keys()) == [
        "schema_version",
        "status",
        "corpus_digest",
        "request_digest",
        "returned_count",
        "excluded_counts",
        "budget_dropped",
        "cards",
    ]
    assert payload["schema_version"] == "planning-history-cards-v1"
    assert payload["status"] == "ready"
    assert (
        payload["corpus_digest"]
        == hashlib.sha256(FIXTURE_CORPUS_PATH.read_bytes()).hexdigest()
    )
    assert (
        payload["request_digest"]
        == hashlib.sha256(request_path.read_bytes()).hexdigest()
    )
    assert payload["returned_count"] == 3
    assert payload["excluded_counts"] == {
        "repository_mismatch": 0,
        "lineage_excluded": 1,
        "after_as_of": 2,
        "superseded_revision": 2,
    }
    assert payload["budget_dropped"] == 0
    assert [c["case_id"] for c in payload["cards"]] == [
        "case-1100-rev2",
        "case-1200-mixed-outcomes",
        "case-1300-tie-a",
    ]

    mixed = payload["cards"][1]
    assert [t["subtask_id"] for t in mixed["tasks"]] == sorted(
        t["subtask_id"] for t in mixed["tasks"]
    )
    tasks_by_id = {t["subtask_id"]: t for t in mixed["tasks"]}
    assert tasks_by_id["history-rollout-notes"]["issue_number"] is None
    assert tasks_by_id["history-rollout-notes"]["result"] == "unknown"
    assert tasks_by_id["history-rollout-notes"]["missing_reason"]
    assert tasks_by_id["history-collector"]["result"] == "blocked"
    assert tasks_by_id["history-collector"]["missing_reason"] is None


def test_canonical_fixture_limit_4_includes_tie_b(tmp_path: Path) -> None:
    request_path = _write_json(tmp_path / "request.json", CANONICAL_REQUEST)
    proc = _run_cli(FIXTURE_CORPUS_PATH, request_path, limit=4)

    assert proc.returncode == 0
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert payload["status"] == "ready"
    assert payload["returned_count"] == 4
    assert [c["case_id"] for c in payload["cards"]] == [
        "case-1100-rev2",
        "case-1200-mixed-outcomes",
        "case-1300-tie-a",
        "case-1350-tie-b",
    ]


def test_permutation_invariance_preserves_cards_and_counts(tmp_path: Path) -> None:
    corpus = _load_fixture_corpus()
    corpus["cases"] = list(reversed(corpus["cases"]))
    for case in corpus["cases"]:
        case["tasks"] = list(reversed(case["tasks"]))
        case["sources"] = list(reversed(case["sources"]))
        for task in case["tasks"]:
            task["depends_on"] = list(reversed(task["depends_on"]))
            task["footprint"] = list(reversed(task["footprint"]))
            task["symbols"] = list(reversed(task["symbols"]))
            task["source_ids"] = list(reversed(task["source_ids"]))

    reordered_corpus_path = _write_json(tmp_path / "reordered.json", corpus)
    request_path = _write_json(tmp_path / "request.json", CANONICAL_REQUEST)

    orig_proc = _run_cli(FIXTURE_CORPUS_PATH, request_path, limit=4)
    reord_proc = _run_cli(reordered_corpus_path, request_path, limit=4)
    assert orig_proc.returncode == 0
    assert reord_proc.returncode == 0

    orig = json.loads(orig_proc.stdout.decode("utf-8"))
    reord = json.loads(reord_proc.stdout.decode("utf-8"))
    assert reord["corpus_digest"] != orig["corpus_digest"]
    assert reord["corpus_digest"] == (
        hashlib.sha256(reordered_corpus_path.read_bytes()).hexdigest()
    )
    assert reord["request_digest"] == orig["request_digest"]
    assert reord["status"] == orig["status"]
    assert reord["returned_count"] == orig["returned_count"]
    assert reord["excluded_counts"] == orig["excluded_counts"]
    assert reord["budget_dropped"] == orig["budget_dropped"]
    assert reord["cards"] == orig["cards"]


def test_limit_zero_returns_disabled(tmp_path: Path) -> None:
    request_path = _write_json(tmp_path / "request.json", CANONICAL_REQUEST)
    proc = _run_cli(FIXTURE_CORPUS_PATH, request_path, limit=0)

    assert proc.returncode == 0
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert payload["status"] == "disabled"
    assert payload["returned_count"] == 0
    assert payload["budget_dropped"] == 0
    assert payload["cards"] == []
    assert payload["excluded_counts"] == {
        "repository_mismatch": 0,
        "lineage_excluded": 1,
        "after_as_of": 2,
        "superseded_revision": 2,
    }


def test_no_matches_when_query_unmatched_or_repo_mismatch(tmp_path: Path) -> None:
    unmatched_req = deepcopy(CANONICAL_REQUEST)
    unmatched_req["query"] = "qqzxwv"
    req_path = _write_json(tmp_path / "req1.json", unmatched_req)
    proc = _run_cli(FIXTURE_CORPUS_PATH, req_path)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert payload["status"] == "no_matches"
    assert payload["returned_count"] == 0
    assert payload["cards"] == []

    mismatch_req = deepcopy(CANONICAL_REQUEST)
    mismatch_req["repository"] = "OtherOrg/other-repo"
    req2_path = _write_json(tmp_path / "req2.json", mismatch_req)
    proc2 = _run_cli(FIXTURE_CORPUS_PATH, req2_path)
    assert proc2.returncode == 0
    payload2 = json.loads(proc2.stdout.decode("utf-8"))
    assert payload2["status"] == "no_matches"
    assert payload2["excluded_counts"] == {
        "repository_mismatch": 9,
        "lineage_excluded": 0,
        "after_as_of": 0,
        "superseded_revision": 0,
    }


def test_budget_partial_drop_all_drop_and_envelope_exceeded(tmp_path: Path) -> None:
    request_path = _write_json(tmp_path / "request.json", CANONICAL_REQUEST)
    full_proc = _run_cli(FIXTURE_CORPUS_PATH, request_path, limit=3, max_bytes=65536)
    assert full_proc.returncode == 0
    full_size = len(full_proc.stdout)

    # Partial drop: budget smaller than 3 cards, larger than 1 card
    partial_budget = full_size - 500
    partial_proc = _run_cli(
        FIXTURE_CORPUS_PATH, request_path, limit=3, max_bytes=partial_budget
    )
    assert partial_proc.returncode == 0
    assert len(partial_proc.stdout) <= partial_budget
    partial = json.loads(partial_proc.stdout.decode("utf-8"))
    assert partial["status"] == "ready"
    assert partial["budget_dropped"] >= 1
    assert partial["returned_count"] == 3 - partial["budget_dropped"]

    # All dropped -> over_budget (envelope fits in 512 bytes)
    over_proc = _run_cli(FIXTURE_CORPUS_PATH, request_path, limit=3, max_bytes=512)
    assert over_proc.returncode == 0
    assert len(over_proc.stdout) <= 512
    over = json.loads(over_proc.stdout.decode("utf-8"))
    assert over["status"] == "over_budget"
    assert over["returned_count"] == 0
    assert over["budget_dropped"] == 3
    assert over["cards"] == []

    # Envelope itself does not fit -> exit 2, empty stdout
    tiny_proc = _run_cli(FIXTURE_CORPUS_PATH, request_path, limit=3, max_bytes=50)
    assert tiny_proc.returncode == 2
    assert tiny_proc.stdout == b""
    assert tiny_proc.stderr != b""


def test_outcome_text_is_not_indexed_for_bm25(tmp_path: Path) -> None:
    from scripts.planning_history_poc import select_cards

    corpus = _load_fixture_corpus()
    req = deepcopy(CANONICAL_REQUEST)
    req["query"] = "qqzxwv"
    corpus["cases"][0]["tasks"][0]["result"] = "unknown"
    corpus["cases"][0]["tasks"][0]["missing_reason"] = "qqzxwv"

    result = select_cards(
        corpus,
        req,
        corpus_digest="0" * 64,
        request_digest="1" * 64,
        limit=3,
        max_bytes=12288,
    )
    assert result.payload["status"] == "no_matches"
    assert result.payload["cards"] == []


@pytest.mark.parametrize(
    ("args_override", "expected_code"),
    [
        ({"limit": -1}, 2),
        ({"limit": "abc"}, 2),
        ({"max_bytes": 0}, 2),
        ({"max_bytes": -5}, 2),
    ],
)
def test_invalid_cli_arguments_exit_2(
    tmp_path: Path, args_override: dict[str, Any], expected_code: int
) -> None:
    request_path = _write_json(tmp_path / "request.json", CANONICAL_REQUEST)
    proc = _run_cli(FIXTURE_CORPUS_PATH, request_path, **args_override)
    assert proc.returncode == expected_code
    assert proc.stdout == b""
    assert proc.stderr != b""


def test_file_io_errors_exit_1(tmp_path: Path) -> None:
    request_path = _write_json(tmp_path / "request.json", CANONICAL_REQUEST)
    proc_missing = _run_cli(tmp_path / "missing.json", request_path)
    assert proc_missing.returncode == 1
    assert proc_missing.stdout == b""
    assert proc_missing.stderr != b""

    proc_dir = _run_cli(tmp_path, request_path)
    assert proc_dir.returncode == 1
    assert proc_dir.stdout == b""
    assert proc_dir.stderr != b""


def test_invalid_inputs_exit_2(tmp_path: Path) -> None:
    valid_req = _write_json(tmp_path / "valid_req.json", CANONICAL_REQUEST)

    # Duplicate JSON key
    dup_path = tmp_path / "dup.json"
    dup_path.write_text(
        '{"schema_version":"planning-history-query-v1","schema_version":"planning-history-query-v1"}',
        encoding="utf-8",
    )
    proc_dup = _run_cli(FIXTURE_CORPUS_PATH, dup_path)
    assert proc_dup.returncode == 2
    assert proc_dup.stdout == b""

    # UTF-8 BOM
    bom_path = tmp_path / "bom.json"
    bom_path.write_bytes(b"\xef\xbb\xbf" + valid_req.read_bytes())
    proc_bom = _run_cli(FIXTURE_CORPUS_PATH, bom_path)
    assert proc_bom.returncode == 2
    assert proc_bom.stdout == b""

    # Symbol-only query
    sym_req = deepcopy(CANONICAL_REQUEST)
    sym_req["query"] = "  --- ... !!!  "
    sym_path = _write_json(tmp_path / "sym_req.json", sym_req)
    proc_sym = _run_cli(FIXTURE_CORPUS_PATH, sym_path)
    assert proc_sym.returncode == 2
    assert proc_sym.stdout == b""

    # Boolean in integer field
    bool_req = deepcopy(CANONICAL_REQUEST)
    bool_req["exclude_issue_numbers"] = [True]
    bool_path = _write_json(tmp_path / "bool_req.json", bool_req)
    proc_bool = _run_cli(FIXTURE_CORPUS_PATH, bool_path)
    assert proc_bool.returncode == 2
    assert proc_bool.stdout == b""

    # Invalid calendar date
    bad_date_req = deepcopy(CANONICAL_REQUEST)
    bad_date_req["as_of"] = "2026-02-30T00:00:00Z"
    bad_date_path = _write_json(tmp_path / "bad_date.json", bad_date_req)
    proc_date = _run_cli(FIXTURE_CORPUS_PATH, bad_date_path)
    assert proc_date.returncode == 2
    assert proc_date.stdout == b""

    # Tampered plan_revision_digest
    bad_corpus = _load_fixture_corpus()
    bad_corpus["cases"][0]["plan_revision_digest"] = "0" * 64
    bad_corpus_path = _write_json(tmp_path / "bad_corpus.json", bad_corpus)
    proc_digest = _run_cli(bad_corpus_path, valid_req)
    assert proc_digest.returncode == 2
    assert proc_digest.stdout == b""

    # Dependency cycle in tasks
    cyc_corpus = _load_fixture_corpus()
    cyc_corpus["cases"][0]["tasks"][0]["depends_on"] = ["context-index"]
    cyc_path = _write_json(tmp_path / "cyc.json", cyc_corpus)
    assert _run_cli(cyc_path, valid_req).returncode == 2

    # Non-null missing_reason when result is done
    mr_corpus = _load_fixture_corpus()
    mr_corpus["cases"][0]["tasks"][0]["missing_reason"] = "unexpected"
    mr_path = _write_json(tmp_path / "mr.json", mr_corpus)
    assert _run_cli(mr_path, valid_req).returncode == 2

    # Missing outcome source when result is done
    no_out_corpus = _load_fixture_corpus()
    no_out_corpus["cases"][0]["tasks"][0]["source_ids"] = ["src-1100-r1-task-1101"]
    no_out_path = _write_json(tmp_path / "no_out.json", no_out_corpus)
    assert _run_cli(no_out_path, valid_req).returncode == 2

    # Case available_at != max(sources.available_at)
    avail_corpus = _load_fixture_corpus()
    avail_corpus["cases"][0]["available_at"] = "2026-10-01T09:00:00Z"
    avail_path = _write_json(tmp_path / "avail.json", avail_corpus)
    assert _run_cli(avail_path, valid_req).returncode == 2


@pytest.mark.parametrize(
    "footprint",
    [
        "/absolute/repro.txt",
        "C:\\absolute\\repro.txt",
        "C:/absolute/repro.txt",
        "\\\\server\\share\\repro.txt",
        "../outside.txt",
        "tasks/../../outside.txt",
        ".",
        "..",
        "foo/..",
    ],
)
def test_non_repo_relative_footprints_exit_2(tmp_path: Path, footprint: str) -> None:
    corpus_path = _write_json(
        tmp_path / "corpus.json", _with_adopted_footprint(footprint)
    )
    request_path = _write_json(tmp_path / "request.json", CANONICAL_REQUEST)
    proc = _run_cli(corpus_path, request_path)
    assert proc.returncode == 2
    assert proc.stdout == b""
    assert proc.stderr != b""


def test_lexical_repo_relative_footprints_stay_on_the_card(tmp_path: Path) -> None:
    relative = "dir/../orchestune/context/search.py"
    windows_relative = "orchestune\\context\\search.py"
    corpus = _with_adopted_footprint(relative)
    case = next(item for item in corpus["cases"] if item["case_id"] == "case-1100-rev2")
    case["tasks"][0]["footprint"].append(windows_relative)
    corpus_path = _write_json(tmp_path / "corpus.json", corpus)
    request_path = _write_json(tmp_path / "request.json", CANONICAL_REQUEST)
    proc = _run_cli(corpus_path, request_path)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    assert proc.stderr == b""
    payload = json.loads(proc.stdout.decode("utf-8"))
    card = next(
        item for item in payload["cards"] if item["case_id"] == "case-1100-rev2"
    )
    footprints = [fp for task in card["tasks"] for fp in task["footprint"]]
    assert relative in footprints
    assert windows_relative in footprints


def test_lone_surrogate_exits_2_without_traceback(tmp_path: Path) -> None:
    corpus = _load_fixture_corpus()
    case = next(item for item in corpus["cases"] if item["case_id"] == "case-1100-rev2")
    case["title"] = "broken-\ud800"
    corpus_path = tmp_path / "surrogate.json"
    corpus_path.write_bytes(json.dumps(corpus).encode("ascii") + b"\n")
    request_path = _write_json(tmp_path / "request.json", CANONICAL_REQUEST)
    proc = _run_cli(corpus_path, request_path)
    assert proc.returncode == 2
    assert proc.stdout == b""
    assert proc.stderr != b""
    assert b"Traceback" not in proc.stderr


def test_oversized_json_integer_exits_2_without_traceback(tmp_path: Path) -> None:
    encoded = json.dumps(CANONICAL_REQUEST)
    replacement = "[" + ("9" * 5000) + "]"
    encoded = encoded.replace(
        json.dumps(CANONICAL_REQUEST["exclude_issue_numbers"]),
        replacement,
        1,
    )
    huge_path = tmp_path / "huge.json"
    huge_path.write_bytes(encoded.encode("ascii") + b"\n")
    proc = _run_cli(FIXTURE_CORPUS_PATH, huge_path)
    assert proc.returncode == 2
    assert proc.stdout == b""
    assert proc.stderr != b""
    assert b"Traceback" not in proc.stderr
