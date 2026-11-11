"""Read-only census of blocked-human-review issue comments."""

import json

import pytest

import orchestune.blocked_cli as blocked_cli
from orchestune.blocked_cli import main
from orchestune.blocked_report import CATEGORIES
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord

_LABEL = StatusLabel.BLOCKED_HUMAN_REVIEW
_SECRET = "secret comment body must stay hidden"


def _issue(number: int, *, state: str = "OPEN", labels: tuple[str, ...] | None = None):
    return IssueRecord(
        number=number,
        title="t",
        body="issue body",
        labels=(_LABEL,) if labels is None else labels,
        created_at="2026-01-01T00:00:00Z",
        state=state,
    )


class _Forge:
    def __init__(self, issues, comments):
        self.issues = issues
        self.comments = comments
        self.calls: list[tuple] = []

    def list_issues_by_label(self, label, state="open", limit=1000):
        self.calls.append(("list_issues_by_label", str(label), state, limit))
        if isinstance(self.issues, Exception):
            raise self.issues
        return self.issues

    def list_all_issue_comments(self, number):
        self.calls.append(("list_all_issue_comments", number))
        found = self.comments[number]
        if isinstance(found, Exception):
            raise found
        return found

    def add_comment(self, *args):
        self.calls.append(("add_comment", args))

    def add_label(self, *args):
        self.calls.append(("add_label", args))

    def remove_label(self, *args):
        self.calls.append(("remove_label", args))

    def list_comments(self, *args):
        self.calls.append(("list_comments", args))


def _install(monkeypatch, forge: _Forge) -> None:
    monkeypatch.setattr(blocked_cli, "GitHubForge", lambda **kwargs: forge)


def _reads(forge: _Forge) -> list[tuple]:
    return [call for call in forge.calls if call[0].startswith("list_")]


def test_help_does_not_construct_a_forge(monkeypatch, capsys):
    def explode(**kwargs):
        raise AssertionError(kwargs)

    monkeypatch.setattr(blocked_cli, "GitHubForge", explode)
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "--json" in capsys.readouterr().out


def test_unknown_argument_exits_two():
    with pytest.raises(SystemExit) as exc:
        main(["--apply"])
    assert exc.value.code == 2


def test_counts_open_labeled_issues_in_number_order(monkeypatch, capsys):
    forge = _Forge(
        [
            _issue(9, state="CLOSED"),
            _issue(8, labels=()),
            _issue(4),
            _issue(4),
            _issue(2),
        ],
        {
            4: [{"body": f"<!-- orchestune:outcome -->\n{_SECRET}"}],
            2: [],
            9: [{"body": "<!-- orchestune:outcome -->"}],
            8: [{"body": "<!-- orchestune:outcome -->"}],
        },
    )
    _install(monkeypatch, forge)
    assert main([]) == 0
    assert capsys.readouterr().out == "2 コメントなし\n4 outcome 1\n"
    assert _reads(forge) == [
        ("list_issues_by_label", _LABEL, "open", 1000),
        ("list_all_issue_comments", 2),
        ("list_all_issue_comments", 4),
    ]


def test_json_is_category_counts_only(monkeypatch, capsys):
    forge = _Forge(
        [_issue(3)],
        {
            3: [
                {
                    "body": (
                        "<!-- orchestune:outcome -->\n"
                        "<!-- orchestune:semantic-review-state:v1 {} -->"
                    )
                },
                {"body": "no marker"},
            ]
        },
    )
    _install(monkeypatch, forge)
    assert main(["--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        **{category: 0 for category in CATEGORIES},
        "outcome": 1,
        "semantic-review": 1,
        "unrecognized": 1,
    }
    assert list(json.loads(captured.out)) == list(CATEGORIES)
    assert _SECRET not in captured.out
    assert "add_comment" not in {call[0] for call in forge.calls}


def test_no_issues_is_distinct_from_no_comments(monkeypatch, capsys):
    forge = _Forge([], {})
    _install(monkeypatch, forge)
    assert main([]) == 0
    assert capsys.readouterr().out == "対象の Issue はありません\n"
    assert main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        category: 0 for category in CATEGORIES
    }


def test_failures_print_nothing_on_stdout(monkeypatch, capsys):
    limited = _Forge([_issue(index) for index in range(1000)], {})
    _install(monkeypatch, limited)
    assert main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "1000" in captured.err
    assert _reads(limited) == [("list_issues_by_label", _LABEL, "open", 1000)]

    broken = _Forge(RuntimeError("list down"), {})
    _install(monkeypatch, broken)
    assert main(["--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "list down" in captured.err

    missing = _Forge([_issue(5), _issue(6)], {5: [{"body": "ok"}], 6: [{"body": None}]})
    _install(monkeypatch, missing)
    assert main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "6" in captured.err
    assert "ok" not in captured.err

    failed = _Forge([_issue(7)], {7: RuntimeError("comments down")})
    _install(monkeypatch, failed)
    assert main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "comments down" in captured.err
    assert "7" in captured.err
