"""Terminal output helpers, and opening a file for the user."""
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional, Union

import typer


def open_command(path: Path) -> Optional[list[str]]:
    """Command opening a file with the default application. In WSL: with the Windows one (the browser for .html)."""
    if shutil.which("wslpath") and shutil.which("explorer.exe"):
        windows_path = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True).stdout.strip()
        return ["explorer.exe", windows_path]
    opener = shutil.which("xdg-open") or shutil.which("open")
    return [opener, str(path)] if opener else None


def open_file(path: Path):
    """Open a file (a report...) in the browser."""
    command = open_command(path)
    if not command:
        typer.echo(f"⚠️ No way found to open a browser from here. The file is: {path}")
        return
    typer.echo(f"🌐 Opening {path}")
    subprocess.run(command, capture_output=True)  # explorer.exe returns 1 even when it works


def duration(seconds: float) -> str:
    """42s, 12m05s, 2h14m."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


class Board:
    """The few lines saying where a long command is (which commit, which addon, which test, for how long), kept
    together at the bottom of the terminal: redrawn in place every second, the rest of the output scrolling above
    them. Only in a terminal: elsewhere (a log file, a pipe) the output is unchanged. Use `board()`."""
    shown: Optional["Board"] = None  # the board on the screen (one at a time)

    def __init__(self, stream):
        self.stream = stream  # the real output
        self.levels: dict[str, Union[str, Callable[[], str]]] = {}  # in the order they were set, top to bottom
        self.drawn = 0  # its lines on the screen, just above the cursor
        self.partial = False  # the output above it ends in the middle of a line: drawn when the line ends
        self.lock = threading.RLock()

    def set(self, level: str, text: Union[str, Callable[[], str]]):
        """One part of the board (several lines if it holds \\n); a function is called at each redraw (times)."""
        with self.lock:
            self.levels[level] = text
            self.redraw()

    def remove(self, level: str):
        with self.lock:
            self.levels.pop(level, None)
            self.redraw()

    def lines(self) -> list[str]:
        width = shutil.get_terminal_size().columns - 1  # a longer line would wrap: the board would not fit any more
        texts = [text() if callable(text) else text for text in self.levels.values()]
        return [line[:width] for text in texts for line in str(text).split("\n")]

    def erase(self):
        if self.drawn:
            self.stream.write(f"\x1b[{self.drawn}F\x1b[J")  # up to its first line, then clear to the end of the screen
            self.drawn = 0

    def draw(self):
        if not self.partial:
            lines = self.lines()
            self.stream.write("".join(line + "\n" for line in lines))
            self.drawn = len(lines)
        self.stream.flush()

    def redraw(self):
        with self.lock:
            self.erase()
            self.draw()

    def write(self, text: str) -> int:
        """Output of the command: above the board."""
        with self.lock:
            self.erase()
            written = self.stream.write(text)
            if text:
                self.partial = not text.endswith("\n")
            self.draw()
            return written


class _AboveTheBoard:
    """sys.stdout while a board is shown: what is printed goes above it."""

    def __init__(self, board: Board):
        self._board = board

    def write(self, text):
        if not isinstance(text, str):  # like a real text stream: click (typer.echo) checks it this way
            raise TypeError(f"write() argument must be str, not {type(text).__name__}")
        return self._board.write(text)

    def flush(self):
        self._board.stream.flush()

    def isatty(self):
        return True

    def __getattr__(self, name):  # encoding, errors, fileno...
        return getattr(self._board.stream, name)


@contextmanager
def board(tty: bool = None):
    """A Board at the bottom of the terminal while the block runs (None when not in a terminal)."""
    if not (sys.stdout.isatty() if tty is None else tty) or Board.shown:
        yield None
        return
    shown = Board.shown = Board(sys.stdout)
    sys.stdout = _AboveTheBoard(shown)
    stop = threading.Event()

    def tick():  # the times move on even when nothing is printed (a test that takes long)
        while not stop.wait(1):
            shown.redraw()

    threading.Thread(target=tick, daemon=True).start()
    try:
        yield shown
    finally:
        stop.set()
        with shown.lock:
            shown.erase()
            shown.stream.flush()
        sys.stdout, Board.shown = shown.stream, None


class StatusLine:
    """A status that changes often. In a terminal: one line rewritten in place.
    Otherwise (log file, pipe): printed when its key changes, and at most every `interval` seconds in between."""

    def __init__(self, tty: bool = None, interval: float = 10):
        self.tty = sys.stdout.isatty() if tty is None else tty
        self.interval = interval
        self.last_time, self.last_key, self.shown = 0.0, None, False

    def update(self, text: str, key=None):
        now = time.monotonic()
        key_changed = key != self.last_key
        if now - self.last_time < (0.2 if self.tty else self.interval) and not (key_changed and not self.tty):
            return
        self.last_time, self.last_key = now, key
        if self.tty:
            width = shutil.get_terminal_size().columns - 1
            sys.stdout.write("\r   " + text[:width - 3].ljust(width - 3))
            sys.stdout.flush()
            self.shown = True
        else:
            typer.echo("   " + text)

    def done(self):
        """End the in-place line, so that the next output starts on a new line."""
        if self.tty and self.shown:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.shown = False
