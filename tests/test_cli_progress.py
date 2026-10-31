"""Tests for orchestune.infra.cli_progress — Live lifecycle (#1408)."""

from __future__ import annotations

import inspect
import io
import re

import pytest
from rich.text import Text

import orchestune.infra.cli_progress as cli_progress
from orchestune.infra.cli_output import CliOutput, make_output
from orchestune.infra.cli_progress import ProgressHandle, progress

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SHOW_CURSOR = "\x1b[?25h"
HIDE_CURSOR = "\x1b[?25l"


class FakeStream(io.StringIO):
    def __init__(self, *, tty: bool = True, encoding: str = "utf-8") -> None:
        super().__init__()
        self._tty = tty
        self._encoding = encoding

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding


def rich_output(**kw) -> tuple[CliOutput, FakeStream]:
    stream = FakeStream()
    return make_output(stream, env={}, width=60, **kw), stream


def plain_output(**kw) -> tuple[CliOutput, FakeStream]:
    stream = FakeStream(tty=False)
    return make_output(stream, env={}, width=60, **kw), stream


class TestPlainFallback:
    def test_start_and_updates_are_appended_as_static_lines(self) -> None:
        output, stream = plain_output()
        with progress(output, "fetching") as handle:
            handle.update("parsing")
            handle.update("parsing")
        assert stream.getvalue() == "fetching\nparsing\nparsing\n"

    def test_multiline_update_is_appended_whole(self) -> None:
        output, stream = plain_output()
        with progress(output, "") as handle:
            handle.update("a\nb")
            handle.update("a\nb")
        assert stream.getvalue() == "a\nb\na\nb\n"

    def test_plain_never_emits_ansi_cr_or_clear(self) -> None:
        output, stream = plain_output()
        with progress(output, "x\x1b[2J\r") as handle:
            handle.update("y\x1b]0;t\x07")
        text = stream.getvalue()
        assert "\x1b" not in text and "\r" not in text and "\x07" not in text

    def test_plain_does_not_start_a_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args, **kwargs):
            raise AssertionError("Live must not be constructed in plain mode")

        monkeypatch.setattr(cli_progress, "Live", boom)
        output, _ = plain_output()
        with progress(output, "x") as handle:
            handle.update("y")
            with handle.pause():
                pass

    def test_exception_still_propagates_without_control_output(self) -> None:
        output, stream = plain_output()
        with pytest.raises(RuntimeError):
            with progress(output, "x"):
                raise RuntimeError("boom")
        assert "\x1b" not in stream.getvalue()


class TestMachineMode:
    def test_progress_is_silent(self) -> None:
        stream = FakeStream()
        output = make_output(stream, env={}, machine=True)
        with progress(output, "x") as handle:
            handle.update("y")
            with handle.pause():
                pass
        assert stream.getvalue() == ""


class TestRichLive:
    def test_live_is_configured_for_manual_refresh_without_redirects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}
        real = cli_progress.Live

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(cli_progress, "Live", spy)
        output, _ = rich_output()
        with progress(output, "work"):
            pass
        assert seen["auto_refresh"] is False
        assert seen["screen"] is False
        assert seen["redirect_stdout"] is False
        assert seen["redirect_stderr"] is False
        assert seen["console"] is output.console

    def test_updates_are_refreshed_manually_and_cursor_is_restored(self) -> None:
        output, stream = rich_output()
        with progress(output, "first") as handle:
            assert handle.active
            handle.update("second")
            assert "second" in ANSI.sub("", stream.getvalue())
        assert not handle.active
        raw = stream.getvalue()
        assert HIDE_CURSOR in raw
        assert raw.rindex(SHOW_CURSOR) > raw.rindex(HIDE_CURSOR)

    @pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyboardInterrupt()])
    def test_live_stops_and_cursor_is_restored_on_exception(
        self, exc: BaseException
    ) -> None:
        output, stream = rich_output()
        holder = {}
        with pytest.raises(type(exc)):
            with progress(output, "work") as handle:
                holder["h"] = handle
                raise exc
        assert not holder["h"].active
        raw = stream.getvalue()
        assert raw.rindex(SHOW_CURSOR) > raw.rindex(HIDE_CURSOR)

    def test_does_not_use_alternate_screen_or_clear_screen(self) -> None:
        output, stream = rich_output()
        with progress(output, "x") as handle:
            handle.update("y")
        raw = stream.getvalue()
        assert "\x1b[?1049h" not in raw and "\x1b[2J" not in raw

    def test_external_strings_are_literal_and_sanitized(self) -> None:
        output, stream = rich_output()
        with progress(output, "[red]x[/red]\x1b]0;t\x07") as handle:
            handle.update("[bold]y[/bold]\x1b[2J")
        raw = stream.getvalue()
        assert "\x1b]" not in raw and "\x07" not in raw and "\x1b[2J" not in raw
        text = ANSI.sub("", raw)
        assert "[red]x[/red]" in text and "[bold]y[/bold]" in text

    def test_shows_the_status_text_without_spinner_glyphs(self) -> None:
        output, stream = rich_output()
        with progress(output, "") as handle:
            handle.update("snapshot-body")
        text = ANSI.sub("", stream.getvalue())
        assert "snapshot-body" in text
        assert not re.search("[\u2800-\u28ff]", text)

    def test_non_unicode_stream_output_is_encodable(self) -> None:
        stream = FakeStream(encoding="cp932")
        output = make_output(stream, env={}, width=60)
        with progress(output, "working") as handle:
            handle.update("日本語")
        stream.getvalue().encode("cp932")

    def test_pause_stops_live_and_resumes_it(self) -> None:
        output, stream = rich_output()
        with progress(output, "work") as handle:
            with handle.pause():
                assert not handle.active
                stream.write("interactive prompt\n")
            assert handle.active
        assert not handle.active
        assert "interactive prompt" in stream.getvalue()

    def test_pause_restarts_even_when_the_body_raises(self) -> None:
        output, _ = rich_output()
        with progress(output, "work") as handle:
            with pytest.raises(RuntimeError):
                with handle.pause():
                    raise RuntimeError("x")
            assert handle.active

    def test_update_after_exit_is_a_noop(self) -> None:
        output, stream = rich_output()
        with progress(output, "work") as handle:
            pass
        before = stream.getvalue()
        handle.update("late")
        assert stream.getvalue() == before

    def test_update_during_pause_is_shown_when_the_display_resumes(self) -> None:
        output, stream = rich_output()
        with progress(output, "old status") as handle:
            with handle.pause():
                mark = len(stream.getvalue())
                handle.update("new status")
            assert "new status" in ANSI.sub("", stream.getvalue()[mark:])

    def test_live_renders_the_status_block_not_a_spinner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[object] = []
        real_init = cli_progress.Live.__init__
        real_update = cli_progress.Live.update

        def spy_init(self, renderable=None, **kwargs):
            seen.append(renderable)
            real_init(self, renderable, **kwargs)

        def spy_update(self, renderable, **kwargs):
            seen.append(renderable)
            real_update(self, renderable, **kwargs)

        monkeypatch.setattr(cli_progress.Live, "__init__", spy_init)
        monkeypatch.setattr(cli_progress.Live, "update", spy_update)
        output, _ = rich_output()
        with progress(output, "one") as handle:
            handle.update("two")
        assert len(seen) == 2
        renderables = [r for r in seen if isinstance(r, Text)]
        assert len(renderables) == len(seen)
        assert [r.plain for r in renderables] == ["one", "two"]


class TestNoSpinner:
    def test_progress_and_handle_take_no_spinner_argument(self) -> None:
        assert "spinner" not in inspect.signature(progress).parameters
        assert "spinner" not in inspect.signature(ProgressHandle).parameters

    def test_module_does_not_use_rich_spinner(self) -> None:
        assert not hasattr(cli_progress, "Spinner")
