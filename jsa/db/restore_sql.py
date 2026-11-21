"""Load a plain SQL dump (pg_dump format) into a database: the PostgreSQL versions checked, extensions first, then
the dump streamed into psql with a progress display."""
import itertools
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

import typer

from ..core import pg
from ..core.ui import StatusLine

EXTENSION_RE = re.compile(rb'^CREATE EXTENSION (?:IF NOT EXISTS )?"?([\w-]+)"?')
COPY_RE = re.compile(rb'^COPY (?:public\.)?"?([\w.]+)"? ')
INDEX_PREFIXES = (b"CREATE INDEX ", b"CREATE UNIQUE INDEX ")
DUMPED_FROM_RE = re.compile(rb'^-- Dumped from database version (.+)')
DUMPED_BY_RE = re.compile(rb'^-- Dumped by pg_dump version (.+)')
NUMBERS_RE = re.compile(r'\d+(?:\.\d+)*')

Version = tuple[int, ...]


def header_lines(lines: Iterable[bytes]) -> Iterator[bytes]:
    """The lines pg_dump writes before the first table: versions, settings, extensions, types, functions."""
    for line in lines:
        if line.startswith((b"CREATE TABLE ", b"COPY ")):
            return
        yield line


def extensions_in(lines: Iterable[bytes]) -> list[str]:
    return [m.group(1).decode() for m in map(EXTENSION_RE.match, header_lines(lines)) if m]


def dump_extensions(dump_file: Path) -> list[str]:
    """Extensions created by the dump. pg_dump writes them before the first table, so only the header is read."""
    with open(dump_file, 'rb') as f:
        return extensions_in(f)


# ---------- PostgreSQL versions: an older PostgreSQL can refuse what a newer one wrote ----------

def parse_version(text: str) -> Optional[Version]:
    """(18, 7) from '18.7 (Ubuntu 18.7-1.pgdg24.04+1)' or 'psql (PostgreSQL) 18.7'."""
    m = NUMBERS_RE.search(text)
    return tuple(int(n) for n in m.group().split('.')) if m else None


def show(version: Version) -> str:
    return '.'.join(map(str, version))


def dump_versions(lines: Iterable[bytes]) -> tuple[Optional[Version], Optional[Version]]:
    """(version of the PostgreSQL that was dumped, version of the pg_dump that wrote the dump), from its first lines."""
    source = writer = None
    for line in itertools.islice(lines, 50):
        if m := DUMPED_FROM_RE.match(line):
            source = parse_version(m.group(1).decode(errors="replace"))
        elif m := DUMPED_BY_RE.match(line):
            writer = parse_version(m.group(1).decode(errors="replace"))
    return source, writer


def local_versions() -> tuple[Optional[Version], Optional[Version]]:
    """(version of the PostgreSQL server, version of psql); None when unknown (the restore itself says why)."""
    def output(*cmd: str) -> str:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return res.stdout if res.returncode == 0 else ""
    return (parse_version(output("psql", "-X", "-tA", "-d", "postgres", "-c", "SHOW server_version;")),
            parse_version(output("psql", "--version")))


def version_problem(dump: tuple, local: tuple, extensions: Iterable[str] = ()) -> Optional[list[str]]:
    """Why this PostgreSQL may refuse the dump (older than the server it comes from, or psql older than the pg_dump
    that wrote it), then how to fix it. None when fine or unknown.
    E.g. PostgreSQL 18.7 writes a column option (pg_attver) that older 18.x servers refuse."""
    (source, writer), (server, client) = dump, local
    facts, upgrade, install = [], [], []
    if source and server and server < source:
        facts.append(f"the dump comes from PostgreSQL {show(source)} and your server is {show(server)}")
        if server[0] == source[0]:
            upgrade += [f"postgresql-{server[0]}"] + ([f"postgresql-{server[0]}-pgvector"] if "vector" in extensions else [])
        else:
            install.append(f"PostgreSQL {source[0]}")
    if writer and client and client < writer:
        facts.append(f"{'it' if facts else 'the dump'} was written by pg_dump {show(writer)} and your psql is {show(client)}")
        if client[0] == writer[0]:
            upgrade.append(f"postgresql-client-{client[0]}")
        else:
            install.append(f"postgresql-client-{writer[0]}")
    if not facts:
        return None
    fixes = ([f"sudo apt update && sudo apt install --only-upgrade {' '.join(upgrade)}"] if upgrade else []) + \
            ([f"install {' and '.join(install)} (a newer major version)"] if install else [])
    sentence = "; ".join(facts)
    return [f"{sentence[0].upper()}{sentence[1:]}: an older PostgreSQL can refuse what a newer one wrote.",
            f"To update it: {', then '.join(fixes)}"]


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
