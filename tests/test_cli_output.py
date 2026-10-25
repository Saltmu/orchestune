"""Tests for orchestune.infra.cli_output — the shared human-output layer (#1408).

Streams, environment and width are injected; nothing here depends on the real
terminal of the test runner.
"""

from __future__ import annotations

import ast
import io
import re
import sys
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.console import Console

import orchestune.infra.cli_output as cli_output
from orchestune.infra.cli_output import (
    CliOutput,
    Level,
    OutputPolicy,
    label_token,
    make_output,
    sanitize_text,
)

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class FakeStream(io.StringIO):
    """A text stream whose TTY-ness and encoding the test controls."""

    def __init__(self, *, tty: bool = True, encoding: str | None = "utf-8") -> None:
        super().__init__()
        self._tty = tty
        self._encoding = encoding

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str | None:  # type: ignore[override]
        return self._encoding


class BareStream:
    """Neither ``isatty`` nor ``encoding``: an injected minimal writer."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def write(self, text: str) -> int:
        self.parts.append(text)
        return len(text)

    def flush(self) -> None:
        pass


class BrokenIsatty(FakeStream):
    def isatty(self) -> bool:
        raise ValueError("I/O operation on closed file")


def policy_of(stream: object, env: dict[str, str] | None = None, **kw) -> OutputPolicy:
    return make_output(stream, env=env or {}, **kw).policy  # type: ignore[arg-type]


class TestPolicyPrecedence:
    def test_tty_with_clean_env_uses_rich(self) -> None:
        policy = policy_of(FakeStream())
        assert policy.kind == "human"
        assert not policy.plain
        assert policy.color
        assert policy.dynamic

    def test_non_tty_is_plain(self) -> None:
        policy = policy_of(FakeStream(tty=False))
        assert policy.plain
        assert not policy.color
        assert not policy.dynamic

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " On "])
    def test_ci_truthy_values_force_plain(self, value: str) -> None:
        assert policy_of(FakeStream(), {"CI": value}).plain

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_ci_falsy_values_do_not_force_plain(self, value: str) -> None:
        assert not policy_of(FakeStream(), {"CI": value}).plain

    @pytest.mark.parametrize("term", ["dumb", "DUMB"])
    def test_term_dumb_is_plain(self, term: str) -> None:
        assert policy_of(FakeStream(), {"TERM": term}).plain

    def test_orchestune_cli_plain_forces_plain_on_a_tty(self) -> None:
        policy = policy_of(FakeStream(), {"ORCHESTUNE_CLI_PLAIN": "1"})
        assert policy.plain
        assert not policy.dynamic
        assert not policy.color

    def test_orchestune_cli_plain_zero_does_not_force_plain(self) -> None:
        assert not policy_of(FakeStream(), {"ORCHESTUNE_CLI_PLAIN": "0"}).plain

    def test_machine_beats_everything_even_on_a_tty(self) -> None:
        policy = policy_of(FakeStream(), {"FORCE_COLOR": "1"}, machine=True)
        assert policy.kind == "machine"
        assert policy.plain
        assert not policy.color
        assert not policy.dynamic

    def test_no_color_disables_color_only(self) -> None:
        policy = policy_of(FakeStream(), {"NO_COLOR": "1"})
        assert not policy.color
        assert not policy.plain
        assert policy.dynamic

    def test_empty_no_color_is_ignored(self) -> None:
        assert policy_of(FakeStream(), {"NO_COLOR": ""}).color

    @pytest.mark.parametrize(
        "env",
        [
            {"CI": "1"},
            {"TERM": "dumb"},
            {"ORCHESTUNE_CLI_PLAIN": "1"},
        ],
    )
    def test_force_color_never_overrides_plain(self, env: dict[str, str]) -> None:
        policy = policy_of(FakeStream(), {**env, "FORCE_COLOR": "1"})
        assert policy.plain
        assert not policy.color
        assert not policy.dynamic

    def test_force_color_never_turns_a_non_tty_into_rich(self) -> None:
        policy = policy_of(FakeStream(tty=False), {"FORCE_COLOR": "1"})
        assert policy.plain
        assert not policy.color

    def test_stdout_and_stderr_are_decided_independently(self) -> None:
        tty_out = policy_of(FakeStream(tty=True))
        piped_err = policy_of(FakeStream(tty=False))
        assert not tty_out.plain
        assert piped_err.plain

    def test_stream_without_isatty_or_encoding_is_plain(self) -> None:
        assert policy_of(BareStream()).plain

    def test_isatty_failure_is_plain(self) -> None:
        assert policy_of(BrokenIsatty()).plain

    def test_unknown_encoding_value_is_safe(self) -> None:
        policy = policy_of(FakeStream(encoding=None))
        assert not policy.unicode

    def test_non_utf8_encoding_disables_unicode_icons(self) -> None:
        assert policy_of(FakeStream(encoding="utf-8")).unicode
        assert not policy_of(FakeStream(encoding="cp932")).unicode
        assert not policy_of(FakeStream(encoding="ascii")).unicode

    def test_environment_is_read_at_call_time_not_import_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = FakeStream()
        monkeypatch.setenv("ORCHESTUNE_CLI_PLAIN", "1")
        monkeypatch.delenv("CI", raising=False)
        assert make_output(stream).policy.plain
        monkeypatch.delenv("ORCHESTUNE_CLI_PLAIN")
        monkeypatch.setenv("TERM", "xterm")
        assert not make_output(stream).policy.plain

    def test_env_snapshot_is_taken_at_creation(self) -> None:
        env = {"TERM": "xterm"}
        output = make_output(FakeStream(), env=env)
        env["ORCHESTUNE_CLI_PLAIN"] = "1"
        assert not output.policy.plain


class TestModuleContract:
    def test_no_console_is_created_at_import_time(self) -> None:
        consoles = [v for v in vars(cli_output).values() if isinstance(v, Console)]
        assert consoles == []

    def test_module_imports_only_stdlib_and_rich(self) -> None:
        tree = ast.parse(Path(cli_output.__file__).read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        assert roots <= set(sys.stdlib_module_names) | {"rich"}

    def test_module_does_not_install_global_hooks(self) -> None:
        tree = ast.parse(Path(cli_output.__file__).read_text(encoding="utf-8"))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not names & {"install", "basicConfig", "addHandler", "excepthook"}


class TestSanitize:
    def test_strips_csi_and_osc_sequences(self) -> None:
        raw = "a\x1b[31mred\x1b[0m\x1b]0;pwned\x07b\x1b]8;;http://x\x1b\\c"
        assert sanitize_text(raw) == "aredbc"

    def test_strips_c0_c1_and_bidi_controls(self) -> None:
        raw = "x\x00\x07\x08\x7f\x85y‮z⁦"
        assert sanitize_text(raw) == "xyz"

    def test_strips_eight_bit_csi_and_osc(self) -> None:
        assert sanitize_text("a\x9b31mb\x9d0;t\x9cc") == "abc"

    def test_unterminated_osc_is_dropped_to_the_end(self) -> None:
        assert sanitize_text("keep\x1b]0;never ends") == "keep"

    def test_single_line_cells_flatten_newlines_and_tabs(self) -> None:
        assert sanitize_text("a\nb\r\nc\td\re") == "a b c d e"

    def test_multiline_detail_keeps_newlines_and_tabs(self) -> None:
        assert sanitize_text("a\r\nb\rc\td", multiline=True) == "a\nb\nc\td"

    def test_unencodable_characters_are_replaced_visibly(self) -> None:
        out = sanitize_text("ok \U0001f600 日本", encoding="ascii")
        assert out.encode("ascii") and "ok" in out and "U0001f600" in out

    def test_cp932_keeps_japanese_and_replaces_emoji(self) -> None:
        out = sanitize_text("日本語\U0001f600", encoding="cp932")
        assert out.startswith("日本語") and "\U0001f600" not in out

    def test_unknown_encoding_falls_back_to_ascii(self) -> None:
        assert sanitize_text("é", encoding="no-such-codec") == "\\xe9"

    def test_lone_surrogate_does_not_break_utf8_streams(self) -> None:
        assert "\\udcff" in sanitize_text("a\udcffb")

    def test_input_is_returned_not_mutated(self) -> None:
        raw = "x\x1b[31my"
        sanitize_text(raw)
        assert raw == "x\x1b[31my"


class TestLabels:
    def test_every_level_has_a_distinct_text_token(self) -> None:
        tokens = {label_token(level) for level in Level}
        assert len(tokens) == len(list(Level))
        assert label_token(Level.SUCCESS) == "[OK]"
        assert label_token(Level.UNVERIFIED) == "[UNVERIFIED]"
        assert label_token(Level.SKIPPED) == "[SKIPPED]"
        assert label_token(Level.DRY_RUN) == "[DRY-RUN]"

    def test_unverified_and_skipped_are_not_success(self) -> None:
        assert label_token(Level.UNVERIFIED) != label_token(Level.SUCCESS)
        assert label_token(Level.SKIPPED) != label_token(Level.SUCCESS)
        assert label_token(Level.DRY_RUN) != label_token(Level.SUCCESS)


class TestPlainRendering:
    def render(self, **kw) -> tuple[CliOutput, io.StringIO]:
        stream = FakeStream(tty=False)
        return make_output(stream, env={}, **kw), stream

    def test_message_is_label_and_text_only(self) -> None:
        output, stream = self.render()
        output.message(Level.WARNING, "disk almost full")
        assert stream.getvalue() == "[WARN] disk almost full\n"

    def test_plain_output_has_no_control_characters(self) -> None:
        output, stream = self.render()
        output.message(
            Level.FAILURE, "bad \x1b[31mtitle\x1b[0m\x1b]0;x\x07", detail="l1\nl2"
        )
        output.table(["id", "name"], [["1", "n\x1b[1m"]], title="T")
        output.summary(Level.SUCCESS, "done", [("k", "v\r")])
        text = stream.getvalue()
        assert "\x1b" not in text and "\r" not in text and "\x07" not in text

    def test_literal_markup_is_not_interpreted(self) -> None:
        output, stream = self.render()
        output.message(Level.INFO, "[red]x[/red] [bold]y")
        assert stream.getvalue() == "[INFO] [red]x[/red] [bold]y\n"

    def test_detail_is_indented_and_multiline(self) -> None:
        output, stream = self.render()
        output.message(Level.FAILURE, "boom", detail="line1\nline2")
        assert stream.getvalue() == "[FAIL] boom\n    line1\n    line2\n"

    def test_table_columns_align_by_display_width(self) -> None:
        output, stream = self.render()
        output.table(["id", "name"], [["1", "日本語"], ["22", "ab"]])
        lines = stream.getvalue().splitlines()
        assert len(lines) == 3
        starts = {
            cell_len(line[: line.index(tail)])
            for line, tail in zip(lines, ["name", "日本語", "ab"], strict=True)
        }
        assert len(starts) == 1

    def test_table_never_truncates_or_wraps_long_values(self) -> None:
        output, stream = self.render(width=20)
        token = "t" * 80
        output.table(["id", "token"], [["1", token]])
        assert token in stream.getvalue().splitlines()[1]

    def test_table_title_comes_first(self) -> None:
        output, stream = self.render()
        output.table(["a"], [["1"]], title="Issues")
        assert stream.getvalue().splitlines()[0] == "Issues"

    def test_table_rejects_ragged_rows(self) -> None:
        output, _ = self.render()
        with pytest.raises(ValueError, match="expected 2"):
            output.table(["a", "b"], [["only-one"]])

    def test_empty_table_prints_header_only(self) -> None:
        output, stream = self.render()
        output.table(["a", "b"], [])
        assert stream.getvalue().splitlines() == ["a  b"]

    def test_summary_lists_items(self) -> None:
        output, stream = self.render()
        output.summary(Level.DRY_RUN, "plan", [("created", "0"), ("reused", "2")])
        assert stream.getvalue() == "[DRY-RUN] plan\n  created: 0\n  reused: 2\n"

    def test_source_data_is_not_modified(self) -> None:
        output, _ = self.render()
        rows = [["1", "a\x1b[31mb"]]
        output.table(["id", "x"], rows)
        assert rows == [["1", "a\x1b[31mb"]]


class TestMachineMode:
    def test_human_helpers_write_nothing(self) -> None:
        stream = FakeStream()
        output = make_output(stream, env={}, machine=True)
        output.message(Level.SUCCESS, "hi")
        output.table(["a"], [["1"]])
        output.summary(Level.SUCCESS, "done")
        assert stream.getvalue() == ""

    def test_payload_is_written_verbatim_without_sanitizing_or_styling(self) -> None:
        stream = FakeStream()
        output = make_output(stream, env={}, machine=True)
        payload = '{"a": "[red]x[/red]\\u001b"}'
        output.payload(payload)
        assert stream.getvalue() == payload + "\n"

    def test_payload_is_verbatim_on_a_rich_stream_too(self) -> None:
        stream = FakeStream()
        output = make_output(stream, env={})
        output.payload("/tmp/path with [markup]")
        assert stream.getvalue() == "/tmp/path with [markup]\n"


class TestRichRendering:
    def render(
        self, env: dict[str, str] | None = None, **kw
    ) -> tuple[CliOutput, FakeStream]:
        stream = FakeStream(**{k: kw.pop(k) for k in ("encoding",) if k in kw})
        return make_output(stream, env=env or {}, width=kw.pop("width", 60)), stream

    def test_rich_message_has_color_and_the_text_label(self) -> None:
        output, stream = self.render()
        output.message(Level.SUCCESS, "saved")
        raw = stream.getvalue()
        assert "\x1b[" in raw
        text = ANSI.sub("", raw)
        assert "[OK]" in text and "saved" in text
        assert "✓" in text

    def test_no_color_has_no_color_codes_but_keeps_labels(self) -> None:
        output, stream = self.render({"NO_COLOR": "1"})
        output.message(Level.FAILURE, "nope")
        raw = stream.getvalue()
        assert not re.search(r"\x1b\[[0-9;]*3[0-7]m|\x1b\[[0-9;]*9[0-7]m|38;", raw)
        assert "[FAIL]" in ANSI.sub("", raw)

    def test_markup_in_external_strings_is_literal(self) -> None:
        output, stream = self.render()
        output.message(Level.INFO, "[red]boom[/red] :smile: [link=http://x]l[/link]")
        text = ANSI.sub("", stream.getvalue())
        assert "[red]boom[/red] :smile: [link=http://x]l[/link]" in text

    def test_terminal_controls_are_neutralised(self) -> None:
        output, stream = self.render()
        output.message(Level.INFO, "a\x1b]0;owned\x07b\x1b[2Jc", detail="d\x1b[31me")
        raw = stream.getvalue()
        assert "\x1b]" not in raw and "\x07" not in raw and "\x1b[2J" not in raw
        assert "abc" in ANSI.sub("", raw)

    def test_cp932_stream_uses_ascii_labels_and_is_encodable(self) -> None:
        output, stream = self.render(encoding="cp932")
        output.message(Level.WARNING, "警告 \U0001f600")
        output.table(["名前"], [["値"]])
        output.summary(Level.SKIPPED, "skip", [("k", "v")])
        raw = stream.getvalue()
        raw.encode("cp932")
        text = ANSI.sub("", raw)
        assert "[WARN]" in text and "▲" not in text and "[SKIPPED]" in text
        assert "警告" in text

    def test_table_renders_headers_rows_and_japanese(self) -> None:
        output, stream = self.render({"NO_COLOR": "1"})
        output.table(
            ["Issue", "状態"], [["#1", "実行中"], ["#22", "待機"]], title="一覧"
        )
        text = ANSI.sub("", stream.getvalue())
        for needle in ("Issue", "状態", "#22", "実行中", "待機", "一覧"):
            assert needle in text

    def test_narrow_width_keeps_long_ids_whole_via_stacked_layout(self) -> None:
        output, stream = self.render({"NO_COLOR": "1"}, width=30)
        long_id = "claim-" + "a" * 50
        output.table(["id", "state"], [[long_id, "done"]])
        text = ANSI.sub("", stream.getvalue())
        assert long_id in text
        assert "state: done" in text

    def test_wide_width_uses_a_real_table(self) -> None:
        output, stream = self.render({"NO_COLOR": "1"}, width=80)
        output.table(["id", "state"], [["7", "done"]])
        text = ANSI.sub("", stream.getvalue())
        assert "state: done" not in text
        assert len(text.splitlines()) >= 4  # border, header, row, border

    def test_summary_renders_items(self) -> None:
        output, stream = self.render({"NO_COLOR": "1"})
        output.summary(Level.UNVERIFIED, "ownership", [("status", "unverified")])
        text = ANSI.sub("", stream.getvalue())
        assert "[UNVERIFIED] ownership" in text and "status: unverified" in text

    def test_console_is_created_lazily_and_only_for_rich(self) -> None:
        plain = make_output(FakeStream(tty=False), env={})
        assert plain.console is None
        rich = make_output(FakeStream(), env={})
        assert isinstance(rich.console, Console)
        assert rich.console is rich.console
        machine = make_output(FakeStream(), env={}, machine=True)
        assert machine.console is None
