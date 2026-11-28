"""Read-only census of blocked-human-review issue comments."""

import io
import json

import pytest

import orchestune.blocked_cli as blocked_cli
from orchestune.blocked_cli import main
from orchestune.blocked_report import CATEGORIES
from orchestune.infra.cli_guidance import quote_command
from orchestune.infra.cli_output import make_output
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord

_LABEL = StatusLabel.BLOCKED_HUMAN_REVIEW
_SECRET = "secret comment body must stay hidden"


def _issue(
    number: int,
    *,
    state: str = "OPEN",
    labels: tuple[str, ...] | None = None,
    title: str = "t",
):
    return IssueRecord(
        number=number,
        title=title,
        body="issue body",
        labels=(_LABEL,) if labels is None else labels,
        created_at="2026-01-01T00:00:00Z",
        state=state,
    )


class _Stream(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"


class _Forge:
    def __init__(self, issues, comments, children=()):
        self.issues = issues
        self.comments = comments
        self.children = children
        self.child_error: BaseException | None = None
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

    def list_sub_issues(self, parent):
        self.calls.append(("list_sub_issues", int(parent)))
        if self.child_error is not None:
            raise self.child_error
        return list(self.children)

    def find_issues_by_parent_metadata(self, parent):
        self.calls.append(("find_issues_by_parent_metadata", int(parent)))
        return []


def _install(monkeypatch, forge: _Forge) -> None:
    monkeypatch.setattr(blocked_cli, "GitHubForge", lambda **kwargs: forge)


def _reads(forge: _Forge) -> list[tuple]:
    return [call for call in forge.calls if call[0].startswith("list_")]


def _output(monkeypatch, forge: _Forge, *, tty: bool, env: dict[str, str]):
    _install(monkeypatch, forge)
    stream = _Stream(tty=tty)
    return make_output(stream, env=env, width=120), stream


def _refuse_forge(monkeypatch) -> None:
    def explode(**kwargs):
        raise AssertionError(kwargs)

    monkeypatch.setattr(blocked_cli, "GitHubForge", explode)


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


def test_help_lists_triage_options_without_a_forge(monkeypatch, capsys):
    _refuse_forge(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--detail" in help_text
    assert "--parent-issue" in help_text


@pytest.mark.parametrize(
    "argv",
    [["--detail", "--json"], ["-d", "--json"], ["--json", "--detail"]],
)
def test_detail_and_json_exit_two_without_a_forge(argv, monkeypatch):
    _refuse_forge(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("value", ["0", "-1", "foo", "1.5"])
def test_parent_issue_must_be_a_positive_integer(value, monkeypatch):
    _refuse_forge(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["--parent-issue", value])
    assert exc.value.code == 2


def test_detail_on_plain_stream_is_a_table(monkeypatch, capsys):
    forge = _Forge(
        [_issue(2), _issue(4, title="Needs review")],
        {
            2: [],
            4: [
                {"body": "<!-- orchestune:semantic-review-state:v1 {} -->"},
                {"body": f"<!-- orchestune:outcome -->\n{_SECRET}"},
            ],
        },
    )
    _install(monkeypatch, forge)
    assert main(["--detail"]) == 0
    out = capsys.readouterr().out
    assert "Needs review" in out
    assert "semantic-review 1, outcome 1" in out
    assert "コメントなし" in out
    assert quote_command(("gh", "issue", "view", "4", "--comments")) in out
    assert quote_command(("gh", "issue", "view", "2", "--comments")) in out
    assert "Issue" in out and "Next Command" in out
    assert _SECRET not in out
    assert "停止理由" not in out


def test_detail_with_no_issues_prints_the_sentence(monkeypatch, capsys):
    _install(monkeypatch, _Forge([], {}))
    assert main(["--detail"]) == 0
    assert capsys.readouterr().out == "対象の Issue はありません\n"


def test_detail_quotes_recover_preview_for_token_limit(monkeypatch, capsys):
    forge = _Forge(
        [_issue(5, title="limited")],
        {5: [{"body": "<!-- orchestune:completion-token-limit completion-1 -->"}]},
    )
    _install(monkeypatch, forge)
    assert main(["-d"]) == 0
    out = capsys.readouterr().out
    command = quote_command(("orchestune", "recover", "--issue", "5"))
    assert command in out
    assert "--apply" not in out
    assert "token-limit 1" in out
    assert "limited" in out


def test_detail_sanitizes_control_characters_in_the_title(monkeypatch, capsys):
    forge = _Forge(
        [_issue(6, title="bad\x1b[31mtitle\nnext")],
        {6: [{"body": _SECRET}]},
    )
    _install(monkeypatch, forge)
    assert main(["--detail"]) == 0
    out = capsys.readouterr().out
    assert "badtitle next" in out
    assert "\x1b" not in out
    assert _SECRET not in out


def test_rich_tty_prints_triage_without_detail_flag(monkeypatch):
    forge = _Forge(
        [_issue(4, title="Needs review")],
        {4: [{"body": f"<!-- orchestune:outcome -->\n{_SECRET}"}]},
    )
    output, stream = _output(monkeypatch, forge, tty=True, env={})
    assert output.policy.rich
    assert main([], output=output) == 0
    text = stream.getvalue()
    assert "Needs review" in text
    assert "outcome 1" in text
    assert quote_command(("gh", "issue", "view", "4", "--comments")) in text
    assert _SECRET not in text
    assert "4 outcome 1\n" not in text


@pytest.mark.parametrize(
    "env",
    [{"CI": "1"}, {"TERM": "dumb"}, {"ORCHESTUNE_CLI_PLAIN": "1"}],
)
def test_suppressed_rich_keeps_legacy_text(env, monkeypatch):
    forge = _Forge([_issue(4)], {4: [{"body": "<!-- orchestune:outcome -->"}]})
    output, stream = _output(monkeypatch, forge, tty=True, env=env)
    assert not output.policy.rich
    assert main([], output=output) == 0
    assert stream.getvalue() == "4 outcome 1\n"


def test_parent_issue_limits_the_census(monkeypatch, capsys):
    forge = _Forge(
        [_issue(2), _issue(4), _issue(7)],
        {
            2: [{"body": f"<!-- orchestune:outcome -->\n{_SECRET}"}],
            4: [{"body": "<!-- orchestune:outcome -->"}],
            7: [{"body": "<!-- orchestune:outcome -->"}],
        },
        children=[_issue(4), _issue(9)],
    )
    _install(monkeypatch, forge)
    assert main(["-p", "100"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "4 outcome 1\n"
    assert _SECRET not in captured.out
    assert ("list_sub_issues", 100) in forge.calls
    assert ("find_issues_by_parent_metadata", 100) in forge.calls
    assert ("list_all_issue_comments", 4) in forge.calls
    assert ("list_all_issue_comments", 2) not in forge.calls
    assert ("list_all_issue_comments", 7) not in forge.calls


def test_parent_filter_applies_to_json(monkeypatch, capsys):
    forge = _Forge(
        [_issue(2), _issue(4)],
        {
            2: [{"body": "<!-- orchestune:outcome -->"}],
            4: [{"body": "<!-- orchestune:semantic-review-state:v1 {} -->"}],
        },
        children=[_issue(4)],
    )
    _install(monkeypatch, forge)
    assert main(["--parent-issue", "100", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert list(payload) == list(CATEGORIES)
    assert payload["semantic-review"] == 1
    assert payload["outcome"] == 0


def test_parent_lookup_failure_prints_nothing(monkeypatch, capsys):
    forge = _Forge([_issue(4)], {4: [{"body": f"visible {_SECRET}"}]})
    forge.child_error = RuntimeError("parent down")
    _install(monkeypatch, forge)
    assert main(["-p", "3"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "parent down" in captured.err
    assert _SECRET not in captured.err
    assert ("list_all_issue_comments", 4) not in forge.calls
