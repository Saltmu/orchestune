"""Rich TTY adapter for the dispatch progress sink (#1410, parent #1368).

``select_progress_sink`` keeps the existing ``StdoutProgress`` line format for every
stdout that is not a decorated TTY (pipe, file, ``CI``, ``TERM=dumb``,
``ORCHESTUNE_CLI_PLAIN``), so that output is byte-for-byte unchanged. Only a human
TTY gets ``RichDispatchProgress``, which renders the same ``emit`` phase/event
notifications through ``infra.cli_output``/``infra.cli_progress``:

* A ``started`` event opens ``(phase, task_issue)``; any other event for it closes
  it together with everything opened after it. While something is open a manually refreshed spinner line shows the run ID,
  parent, mode and the open phases. When nothing is open the live display is
  stopped, so the dispatcher's own stderr messages, which follow a terminal event,
  never share a line with it.
* Every other event is printed once as a labelled line (``[OK]``, ``[FAIL]``,
  ``[SKIPPED]``, ``[WARN]``, ``[UNVERIFIED]``, ``[INFO]``) with its task and
  reason. Phases have no known total, so there is no percentage and no ETA.
* Output is best-effort as in ``StdoutProgress``: the first failure (closed or
  broken stream, Rich rendering error) disables the sink, stops the live display,
  closes only the duplicated descriptor the sink owns, and leaves one warning on
  stderr. Dispatch and report saving continue.
* ``close`` stops the live display and restores the cursor; ``main`` calls it on
  normal exit, on an exception and on ``KeyboardInterrupt``.

stdout/stderr are not redirected. A module or subprocess that writes straight to
the terminal while a phase is open appends to the spinner line; its text stays
visible because the live display never erases lines it did not draw.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from typing import TextIO

from orchestune.dispatch.progress import (
    DispatchProgressSink,
    StdoutProgress,
    _stdout_stream,
    safe_stderr,
)
from orchestune.infra.cli_output import CliOutput, Level, make_output
from orchestune.infra.cli_progress import ProgressHandle, progress

_EVENT_LEVELS: dict[str, Level] = {
    "completed": Level.SUCCESS,
    "launched": Level.SUCCESS,
    "failed": Level.FAILURE,
    "skipped": Level.SKIPPED,
    "warning": Level.WARNING,
    "held": Level.WARNING,
    "unknown": Level.UNVERIFIED,
}

_OpenKey = tuple[str, int | None]


def _mode(apply: bool | None) -> str:
    return "configuration" if apply is None else "apply" if apply else "dry-run"


def _label(phase: str, task_issue: int | None) -> str:
    return phase if task_issue is None else f"{phase} #{task_issue}"


class RichDispatchProgress:
    """``DispatchProgressSink`` for a decorated TTY; see the module docstring."""

    def __init__(
        self,
        run_id: str,
        parent_issue: int | None,
        apply: bool | None,
        *,
        stream: TextIO | None = None,
        env: Mapping[str, str] | None = None,
        width: int | None = None,
    ) -> None:
        self.run_id = run_id
        self.parent_issue = parent_issue
        self.apply = apply
        self.stream, self.owns_stream = (
            (stream, False) if stream is not None else _stdout_stream()
        )
        self.disabled = False
        self._output: CliOutput = make_output(self.stream, env=env, width=width)
        self._open: list[_OpenKey] = []
        self._live = ExitStack()
        self._handle: ProgressHandle | None = None
        self._announced = False

    @property
    def live_active(self) -> bool:
        """Whether the spinner line is currently on screen."""
        return self._handle is not None and self._handle.active

    def emit(
        self,
        phase: str,
        event: str,
        *,
        task_issue: int | None = None,
        reason: str | None = None,
    ) -> None:
        if self.disabled:
            return
        try:
            self._render(phase, event, task_issue, reason)
        except Exception:  # noqa: BLE001 - progress output is best-effort
            self.close()
            safe_stderr(
                "Warning: progress unavailable; continuing dispatch and report saving"
            )

    def _render(
        self, phase: str, event: str, task_issue: int | None, reason: str | None
    ) -> None:
        if not self._announced:
            self._announced = True
            self._output.message(Level.INFO, f"dispatch run {self.run_id}")
        key = (phase, task_issue)
        if event == "started":
            self._open.append(key)
        else:
            if key in self._open:
                # A phase ends together with everything opened inside it, so an
                # exception that skipped an inner terminal event cannot keep the
                # live display running.
                del self._open[self._open.index(key) :]
            self._print_event(phase, event, task_issue, reason)
        self._refresh()

    def _print_event(
        self, phase: str, event: str, task_issue: int | None, reason: str | None
    ) -> None:
        if (phase, event) == ("configuration", "completed"):
            level = Level.INFO if self.apply else Level.DRY_RUN
            self._output.summary(
                level,
                f"configuration completed: run {self.run_id}",
                [("parent", f"#{self.parent_issue}"), ("mode", _mode(self.apply))],
            )
            return
        text = f"{_label(phase, task_issue)} {event}"
        if reason:
            text += f": {reason}"
        self._output.message(_EVENT_LEVELS.get(event, Level.INFO), text)

    def _status(self) -> str:
        parent = "-" if self.parent_issue is None else f"#{self.parent_issue}"
        phases = " > ".join(_label(phase, task) for phase, task in self._open)
        return " / ".join((self.run_id, f"parent {parent}", _mode(self.apply), phases))

    def _refresh(self) -> None:
        if not self._open:
            self._stop_live()
            return
        if self._handle is None:
            self._handle = self._live.enter_context(progress(self._output))
        self._handle.update(self._status())

    def _stop_live(self) -> None:
        self._handle = None
        self._live.close()

    def close(self) -> None:
        self.disabled = True
        try:
            self._stop_live()
        except Exception:  # noqa: BLE001 - restoring the terminal is best-effort
            pass
        if self.owns_stream:
            try:
                self.stream.close()
            except (OSError, ValueError):
                pass


def select_progress_sink(
    run_id: str,
    *,
    stream: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> DispatchProgressSink:
    """Return the Rich adapter for a decorated TTY, else the plain ``StdoutProgress``.

    ``stream`` defaults to stdout: the decision reads ``sys.stdout`` and the
    environment at call time, and the chosen sink then writes to its own duplicate
    of the stdout descriptor exactly as ``StdoutProgress`` always has.
    """
    if make_output(stream, env=env).policy.rich:
        return RichDispatchProgress(run_id, None, None, stream=stream, env=env)
    return StdoutProgress(run_id, None, None, stream=stream)
