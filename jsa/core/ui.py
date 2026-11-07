"""Terminal output helpers, and opening a file for the user."""
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

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
