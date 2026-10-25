"""Live/spinner lifecycle on top of ``cli_output`` (#1408, parent #1368).

``progress`` wraps a Rich ``Live`` for decorated TTY output and degrades to static
appended lines otherwise. The lifecycle contract:

* ``Live`` is created with ``auto_refresh=False``, ``screen=False`` and without
  stdout/stderr redirection, so nothing is captured or cleared; the display is
  refreshed only when the caller calls ``update``. No polling thread is started.
* The live display is stopped (cursor restored) on normal exit, on an exception and
  on ``KeyboardInterrupt``. The exception is never swallowed.
* ``pause`` stops the display while an interactive prompt or an external command
  owns the terminal, then resumes it.
* Plain mode never emits clear/ANSI/CR sequences; machine mode writes nothing.

An unknown-length task gets a spinner and text only: no percentage and no ETA.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from orchestune.infra.cli_output import Block, CliOutput

_UNICODE_SPINNER = "dots"
_ASCII_SPINNER = "line"


class ProgressHandle:
    """Updates the display started by ``progress``."""

    def __init__(self, output: CliOutput, *, spinner: bool) -> None:
        self._output = output
        self._spinner = spinner
        self._live: Live | None = None
        self._latest: Block | None = None
        self._spinner_frame: Spinner | None = None
        self._active = False
        self._closed = False

    @property
    def active(self) -> bool:
        """Whether the live display is currently running (always false when plain)."""
        return self._active

    def _renderable(self) -> RenderableType:
        body: RenderableType = self._latest.rich if self._latest else Text()
        if not self._spinner:
            return body
        # One Spinner for the handle's lifetime: a new instance would restart its
        # animation clock, so manual refreshes would always draw the first frame.
        if self._spinner_frame is None:
            name = _UNICODE_SPINNER if self._output.policy.unicode else _ASCII_SPINNER
            self._spinner_frame = Spinner(name, text=body)
        else:
            self._spinner_frame.update(text=body)
        return self._spinner_frame

    def _start(self) -> None:
        console = self._output.console
        if console is None:
            return
        self._live = Live(
            self._renderable(),
            console=console,
            auto_refresh=False,
            screen=False,
            redirect_stdout=False,
            redirect_stderr=False,
            transient=True,
        )
        self._live.start(refresh=True)
        self._active = True

    def _stop(self) -> None:
        live, self._live = self._live, None
        self._active = False
        self._closed = True
        if live is not None:
            live.stop()

    def update(self, content: str | Block) -> None:
        """Show ``content``: replaces the live display, or appends a static block."""
        policy = self._output.policy
        if policy.kind == "machine" or self._closed:
            return
        block = (
            self._output.text_block(content) if isinstance(content, str) else content
        )
        self._latest = block
        if policy.plain:
            self._output.emit(block)
        elif self._live is not None and self._active:
            self._live.update(self._renderable(), refresh=True)

    @contextmanager
    def pause(self) -> Iterator[None]:
        """Stop the live display for the body, then resume it (even on error)."""
        live = self._live
        if live is None:
            yield
            return
        live.stop()
        self._active = False
        try:
            yield
        finally:
            # Updates made while paused only recorded the content: show it now.
            live.update(self._renderable())
            live.start(refresh=True)
            self._active = True


@contextmanager
def progress(
    output: CliOutput, description: str = "", *, spinner: bool = True
) -> Iterator[ProgressHandle]:
    """Run a block with progress display; see the module docstring for the contract.

    ``description`` is shown immediately when non-empty. Use ``spinner=False`` for a
    snapshot view (e.g. a watch loop) that only needs a refreshable region.
    """
    handle = ProgressHandle(output, spinner=spinner)
    try:
        if description:
            handle.update(description)
        if output.policy.rich:
            handle._start()  # noqa: SLF001 - lifecycle owned by this module
        yield handle
    finally:
        handle._stop()  # noqa: SLF001 - lifecycle owned by this module
