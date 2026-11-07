"""Load a plain SQL dump (pg_dump format) into a database: extensions first, then the dump streamed into psql
with a progress display."""
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import typer

from ..core import pg
from ..core.ui import StatusLine

EXTENSION_RE = re.compile(rb'^CREATE EXTENSION (?:IF NOT EXISTS )?"?([\w-]+)"?')
COPY_RE = re.compile(rb'^COPY (?:public\.)?"?([\w.]+)"? ')
INDEX_PREFIXES = (b"CREATE INDEX ", b"CREATE UNIQUE INDEX ")


def dump_extensions(dump_file: Path) -> list[str]:
    """Extensions created by the dump. pg_dump writes them before the first table, so only the header is read."""
    extensions = []
    with open(dump_file, 'rb') as f:
        for line in f:
            if line.startswith((b"CREATE TABLE ", b"COPY ")):
                break
            m = EXTENSION_RE.match(line)
            if m:
                extensions.append(m.group(1).decode())
    return extensions


def create_privileged_extensions(db_name: str, extensions: list[str], postgres_user: str):
    """Create beforehand the extensions only a superuser can create (e.g. vector); the dump's own
    CREATE EXTENSION IF NOT EXISTS then does nothing. sudo is used only if needed and if our role is not superuser."""
    if not extensions:
        return
    names = ", ".join(f"'{e}'" for e in extensions)
    privileged = pg.psql(db_name, "SELECT DISTINCT name FROM pg_available_extension_versions "
                                  f"WHERE name IN ({names}) AND superuser AND NOT trusted ORDER BY name;").split()
    if not privileged:
        return
    user = None if pg.is_superuser() else postgres_user
    typer.echo(f"🔌 Creating extension(s) {', '.join(privileged)}" + (f" as '{user}' (sudo)..." if user else "..."))
    pg.psql(db_name, "".join(f'CREATE EXTENSION IF NOT EXISTS "{e}";' for e in privileged), user=user)


@dataclass
class DumpStats:
    """What has been sent to psql so far, from the dump's lines."""
    phase: str = "schema"
    tables: int = 0
    data_blocks: int = 0
    indexes: int = 0  # indexes and constraints
    current_table: str = ""
    in_copy: bool = False

    def feed(self, line: bytes):
        if self.in_copy:  # data rows: only look for the end of the COPY block
            if line.startswith(b"\\."):
                self.in_copy = False
            return
        if line.startswith(b"CREATE TABLE "):
            self.tables += 1
        elif line.startswith(b"COPY "):
            m = COPY_RE.match(line)
            self.in_copy, self.phase = True, "data"
            self.data_blocks += 1
            self.current_table = m.group(1).decode() if m else ""
        elif line.startswith(INDEX_PREFIXES) or line.lstrip().startswith(b"ADD CONSTRAINT "):
            self.indexes += 1
            self.phase = "indexes & constraints"

    def label(self) -> str:
        if self.phase == "data":
            return f"data ({self.current_table})"
        if self.phase == "schema":
            return f"schema ({self.tables} tables)"
        return f"{self.phase} ({self.indexes})"

    def summary(self) -> str:
        return f"{self.tables} tables, {self.data_blocks} data blocks, {self.indexes} indexes/constraints"


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def duration(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


class ProgressDisplay:
    """In a terminal: one line updated in place. Otherwise (log file, pipe): a line on each phase change and every 10s."""

    def __init__(self, total: int, tty: bool = None):
        self.total = max(total, 1)
        self.start = time.monotonic()
        self.status = StatusLine(tty)

    def line(self, done: int, stats: DumpStats) -> str:
        pct = done * 100 // self.total
        bar = "#" * (pct // 10) + "-" * (10 - pct // 10)
        return (f"[{bar}] {pct:3d}%  {human_size(done)}/{human_size(self.total)}  "
                f"{duration(time.monotonic() - self.start)}  {stats.label()}")

    def update(self, done: int, stats: DumpStats):
        self.status.update(self.line(done, stats), key=stats.phase)

    def finish(self, stats: DumpStats):
        self.status.done()
        typer.echo(f"   SQL executed in {duration(time.monotonic() - self.start)}: {stats.summary()}")


def stream_dump(db_name: str, dump_file: Path) -> tuple[int, str]:
    """Feed the dump to psql line by line (to follow the progress). Return (psql exit code, psql stderr)."""
    proc = subprocess.Popen(["psql", "-X", "-q", "-d", db_name],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    stderr = []  # read in parallel, else psql blocks when its error output fills the pipe
    reader = threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True)
    reader.start()

    stats, done = DumpStats(), 0
    display = ProgressDisplay(dump_file.stat().st_size)
    try:
        with open(dump_file, 'rb') as f:
            for line in f:
                stats.feed(line)
                proc.stdin.write(line)
                done += len(line)
                display.update(done, stats)
        proc.stdin.close()
    except BrokenPipeError:  # psql stopped: its stderr says why
        pass
    returncode = proc.wait()
    reader.join()
    display.finish(stats)
    return returncode, b"".join(stderr).decode(errors="replace")
