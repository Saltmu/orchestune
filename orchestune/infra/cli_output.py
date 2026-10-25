"""Shared human-readable CLI output (#1408, parent #1368).

Presenters in each CLI module turn their domain objects into plain strings and
rows; this module decides *how* those strings reach a stream:

* ``make_output`` evaluates one stream and the environment at call time and returns
  a ``CliOutput``. Nothing is created at import time, and the module installs no
  global hook (no ``print`` replacement, no ``rich.traceback.install``, no logging
  handler).
* Display mode precedence: machine payload (never through Rich) >
  ``ORCHESTUNE_CLI_PLAIN`` > non-TTY / ``TERM=dumb`` / ``CI`` (plain) > Rich on a TTY.
  ``NO_COLOR`` only switches colour off. ``FORCE_COLOR`` is ignored: it can never
  turn machine, non-TTY or plain output into decorated output.
* Every external string (Issue titles, branches, paths, logs, diagnostics) is
  rendered literally: ANSI/OSC/control characters are stripped and Rich markup is
  never interpreted. The caller's data is never modified.
* Outcomes carry a text label (``[OK]``, ``[WARN]``, ``[FAIL]``, ``[UNVERIFIED]``,
  ``[SKIPPED]``, ``[DRY-RUN]``) so colour and Unicode icons are decoration only.

This module is dependency free inside Orchestune (L0): standard library and Rich
only, and it knows no domain model.
"""

from __future__ import annotations

import codecs
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, TextIO

from rich import box
from rich.cells import cell_len
from rich.console import Console, RenderableType
from rich.table import Table
from rich.text import Text

OutputKind = Literal["human", "machine"]

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_DEFAULT_ENCODING = "utf-8"
_MISSING = object()


class Level(StrEnum):
    """Outcome vocabulary. The display layer never invents new states."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    FAILURE = "failure"
    UNVERIFIED = "unverified"
    SKIPPED = "skipped"
    DRY_RUN = "dry-run"


_LABELS: dict[Level, str] = {
    Level.INFO: "INFO",
    Level.SUCCESS: "OK",
    Level.WARNING: "WARN",
    Level.FAILURE: "FAIL",
    Level.UNVERIFIED: "UNVERIFIED",
    Level.SKIPPED: "SKIPPED",
    Level.DRY_RUN: "DRY-RUN",
}
_ICONS: dict[Level, str] = {
    Level.INFO: "•",
    Level.SUCCESS: "✓",
    Level.WARNING: "▲",
    Level.FAILURE: "✗",
    Level.UNVERIFIED: "?",
    Level.SKIPPED: "–",
    Level.DRY_RUN: "~",
}
_STYLES: dict[Level, str] = {
    Level.INFO: "blue",
    Level.SUCCESS: "green",
    Level.WARNING: "yellow",
    Level.FAILURE: "bold red",
    Level.UNVERIFIED: "magenta",
    Level.SKIPPED: "dim",
    Level.DRY_RUN: "cyan",
}


def label_token(level: Level) -> str:
    """The text label of ``level``, readable without colour or Unicode."""
    return f"[{_LABELS[level]}]"


# --- sanitizing -------------------------------------------------------------

# ST-terminated strings (OSC/DCS/SOS/PM/APC), 7-bit and 8-bit; an unterminated one
# swallows the rest of the value rather than leaking into the terminal.
_STRING_SEQUENCE = re.compile(
    r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f])[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
)
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?")
_ESCAPE = re.compile(r"\x1b[ -/]*[0-~]?")
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f‪-‮⁦-⁩]")
_LINE_BREAKS = re.compile(r"\r\n|[\r  \x0b\x0c]")


def _encodable(value: str, encoding: str) -> str:
    try:
        codecs.lookup(encoding)
    except LookupError:
        encoding = "ascii"
    return value.encode(encoding, "backslashreplace").decode(encoding)


def sanitize_text(
    value: str, *, multiline: bool = False, encoding: str = _DEFAULT_ENCODING
) -> str:
    """Return ``value`` with terminal control sequences removed.

    A one-line cell flattens newlines and tabs into spaces; a detail area
    (``multiline=True``) keeps ``\\n`` and ``\\t``. Characters ``encoding`` cannot
    represent are replaced by their ``\\x``/``\\u`` escape so the write cannot fail.
    The input string is not modified.
    """
    stripped = _STRING_SEQUENCE.sub("", value)
    stripped = _CSI.sub("", stripped)
    stripped = _ESCAPE.sub("", stripped)
    stripped = _LINE_BREAKS.sub("\n", stripped)
    stripped = _CONTROLS.sub("", stripped)
    if not multiline:
        stripped = stripped.replace("\n", " ").replace("\t", " ")
    return _encodable(stripped, encoding)


# --- policy -----------------------------------------------------------------


@dataclass(frozen=True)
class OutputPolicy:
    """How one stream is rendered. ``rich`` is true only for decorated TTY output."""

    kind: OutputKind
    plain: bool
    color: bool
    dynamic: bool
    unicode: bool
    encoding: str
    stream: TextIO = field(compare=False, repr=False)

    @property
    def rich(self) -> bool:
        return self.kind == "human" and not self.plain


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY


def _is_tty(stream: object) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (ValueError, OSError):
        return False


def _supports_icons(encoding: object) -> bool:
    if not isinstance(encoding, str) or not encoding:
        return False
    try:
        "".join(_ICONS.values()).encode(encoding)
    except (LookupError, UnicodeError):
        return False
    return True


def resolve_policy(
    stream: TextIO, *, env: Mapping[str, str], machine: bool = False
) -> OutputPolicy:
    """Decide the display mode of ``stream``; see the module docstring."""
    encoding = getattr(stream, "encoding", _MISSING)
    name = encoding if isinstance(encoding, str) and encoding else _DEFAULT_ENCODING
    unicode_ok = _supports_icons(encoding)
    if machine:
        return OutputPolicy("machine", True, False, False, unicode_ok, name, stream)
    plain = (
        _truthy(env.get("ORCHESTUNE_CLI_PLAIN"))
        or encoding is _MISSING
        or not _is_tty(stream)
        or env.get("TERM", "").strip().lower() == "dumb"
        or _truthy(env.get("CI"))
    )
    color = not plain and env.get("NO_COLOR", "") == ""
    return OutputPolicy("human", plain, color, not plain, unicode_ok, name, stream)


# --- rendering --------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """One renderable unit with a plain and a Rich form (see ``CliOutput.emit``)."""

    plain: str
    rich: RenderableType
    soft_wrap: bool = True


def _plain_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [
        max(cell_len(cell) for cell in column)
        for column in zip(headers, *rows, strict=True)
    ]
    lines = []
    for record in (headers, *rows):
        padded = [
            cell + " " * (width - cell_len(cell))
            for cell, width in zip(record, widths, strict=True)
        ]
        lines.append("  ".join(padded).rstrip())
    return "\n".join(lines)


def _stacked(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    records = [
        "\n".join(f"{h}: {c}" for h, c in zip(headers, row, strict=True))
        for row in rows
    ]
    return "\n\n".join(records) if records else "  ".join(headers)


class CliOutput:
    """Human output helpers bound to one stream. Machine mode renders nothing."""

    def __init__(
        self,
        policy: OutputPolicy,
        *,
        env: Mapping[str, str],
        width: int | None = None,
    ) -> None:
        self.policy = policy
        self._env = env
        self._width = width
        self._console: Console | None = None

    @property
    def console(self) -> Console | None:
        """The Rich console, created on first use; ``None`` unless the mode is Rich."""
        if not self.policy.rich:
            return None
        if self._console is None:
            self._console = Console(
                file=self.policy.stream,
                force_terminal=True,
                force_interactive=True,
                force_jupyter=False,
                no_color=not self.policy.color,
                width=self._width,
                markup=False,
                emoji=False,
                highlight=False,
                _environ=self._env,
            )
        return self._console

    def _clean(self, value: str, *, multiline: bool = False) -> str:
        return sanitize_text(value, multiline=multiline, encoding=self.policy.encoding)

    def _labeled(self, level: Level, body: str) -> Text:
        text = Text()
        style = _STYLES[level]
        if self.policy.rich and self.policy.unicode:
            text.append(f"{_ICONS[level]} ", style=style)
        text.append(label_token(level), style=style)
        text.append(f" {body}")
        return text

    def text_block(self, text: str, *, multiline: bool = True) -> Block:
        """A literal (sanitized, markup-free) block of text."""
        rendered = Text(self._clean(text, multiline=multiline))
        return Block(rendered.plain, rendered)

    def emit(self, block: Block) -> None:
        """Write ``block`` in the current mode; machine mode writes nothing."""
        if self.policy.kind == "machine":
            return
        console = self.console
        if console is None:
            self.policy.stream.write(block.plain + "\n")
        else:
            console.print(block.rich, soft_wrap=block.soft_wrap)

    def message(self, level: Level, text: str, *, detail: str | None = None) -> None:
        """One labelled line, optionally followed by an indented detail area."""
        body = self._clean(text)
        if detail:
            body += "".join(
                f"\n    {line}"
                for line in self._clean(detail, multiline=True).splitlines()
            )
        labeled = self._labeled(level, body)
        self.emit(Block(labeled.plain, labeled))

    def summary(
        self,
        level: Level,
        title: str,
        items: Sequence[tuple[str, str]] = (),
    ) -> None:
        """A labelled title followed by ``key: value`` lines."""
        lines = "".join(
            f"\n  {self._clean(key)}: {self._clean(value)}" for key, value in items
        )
        labeled = self._labeled(level, self._clean(title) + lines)
        self.emit(Block(labeled.plain, labeled))

    def table_block(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[str]],
        *,
        title: str | None = None,
    ) -> Block:
        """Build a table; see ``table`` for the layout rules."""
        for row in rows:
            if len(row) != len(headers):
                raise ValueError(
                    f"table row has {len(row)} cells, expected {len(headers)}"
                )
        heads = [self._clean(h) for h in headers]
        cells = [[self._clean(c) for c in row] for row in rows]
        heading = self._clean(title) if title else None
        plain = _plain_table(heads, cells)
        console = self.console
        if console is None:
            return Block(f"{heading}\n{plain}" if heading else plain, Text(plain))
        widths = [
            max(cell_len(c) for c in col) for col in zip(heads, *cells, strict=True)
        ]
        if sum(widths) + 3 * len(widths) + 1 > console.width:
            stacked = _stacked(heads, cells)
            text = Text(f"{heading}\n{stacked}" if heading else stacked)
            return Block(text.plain, text)
        grid = Table(
            title=Text(heading) if heading else None,
            title_justify="left",
            box=box.ROUNDED,
            header_style="bold",
        )
        for head in heads:
            grid.add_column(Text(head), no_wrap=True, overflow="ignore")
        for row in cells:
            grid.add_row(*(Text(c) for c in row))
        return Block(plain, grid, soft_wrap=False)

    def table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[str]],
        *,
        title: str | None = None,
    ) -> None:
        """Print a table of one-line cells.

        Plain output is aligned columns (display width aware) that never truncate or
        wrap a value. Rich output is a boxed table; when it would not fit the
        console width the rows are stacked as ``header: value`` lines instead, so an
        Issue number, ID or token is never abbreviated or split.
        """
        self.emit(self.table_block(headers, rows, title=title))

    def payload(self, text: str) -> None:
        """Write a machine payload (JSON, a path, ...) verbatim plus a newline."""
        self.policy.stream.write(text + "\n")


def make_output(
    stream: TextIO | None = None,
    *,
    env: Mapping[str, str] | None = None,
    machine: bool = False,
    width: int | None = None,
) -> CliOutput:
    """Evaluate ``stream`` and the environment now and return its ``CliOutput``.

    ``stream`` defaults to ``sys.stdout`` and ``env`` to ``os.environ``, both read at
    call time; ``env`` is snapshotted. ``width`` pins the Rich console width for tests.
    """
    target = sys.stdout if stream is None else stream
    snapshot = dict(os.environ if env is None else env)
    return CliOutput(
        resolve_policy(target, env=snapshot, machine=machine),
        env=snapshot,
        width=width,
    )
