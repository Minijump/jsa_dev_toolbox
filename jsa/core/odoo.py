"""Running the Odoo of a profile: installing modules while following its log, serving a database for a while."""
import re
import socket
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

import typer

from . import config, pg
from .ui import StatusLine

LOADING_RE = re.compile(r"odoo\.modules\.loading: Loading module (\w+) \((\d+)/(\d+)\)")
TEST_START_RE = re.compile(r"odoo\.addons\.(\w+)\.tests\S*: Starting (\S+)")
TEST_FAIL_RE = re.compile(r"odoo\.addons\.(\w+)\.tests\S*: (?:FAIL|ERROR): (\S+)")
REGISTRY_FAILED = ("Failed to load registry", "Failed to initialize database")


@dataclass
class LogInfo:
    """What Odoo's log says about an install (and the tests run with it)."""
    loaded: list[str] = field(default_factory=list)  # modules in loading order
    tests: Counter = field(default_factory=Counter)  # tests run per module
    failed_tests: dict[str, list[str]] = field(default_factory=dict)  # failed test names per module
    registry_failed: bool = False  # Odoo reported that a module failed to install
    crashed: bool = False  # odoo-bin failed without that message

    def feed(self, line: str):
        m = LOADING_RE.search(line)
        if m:
            self.loaded.append(m.group(1))
        m = TEST_START_RE.search(line)
        if m:
            self.tests[m.group(1)] += 1
        m = TEST_FAIL_RE.search(line)
        if m and m.group(2) not in self.failed_tests.setdefault(m.group(1), []):
            self.failed_tests[m.group(1)].append(m.group(2))
        if any(text in line for text in REGISTRY_FAILED):
            self.registry_failed = True

    @property
    def failed(self) -> bool:
        return self.registry_failed or self.crashed

    @property
    def culprit(self) -> Optional[str]:
        """The module whose loading made the install fail."""
        return self.loaded[-1] if self.registry_failed and self.loaded else None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def install(profile: dict, db: str, modules: list[str], data_dir: Path, log_file: Path, label: str, *,
            cwd: Optional[Path] = None, python_args: list[str] = (), odoo_args: list[str] = ()) -> LogInfo:
    """Install modules in a database with the odoo-bin of a profile, following Odoo's log live (it is appended to
    `log_file`). `python_args` come before odoo-bin (e.g. to run it under coverage), `odoo_args` after its options."""
    cmd = [profile['python'], *python_args, profile['odoo_bin'], "-d", db, "--addons-path", ",".join(profile['addons_paths']),
           "--data-dir", str(data_dir), "--http-port", str(free_port()), "--stop-after-init", "--max-cron-threads=0",
           "--log-level=info", "-i", ",".join(modules), *odoo_args]
    info, status, start = LogInfo(), StatusLine(interval=30), time.monotonic()
    with open(log_file, "a", encoding="utf-8") as log:
        log.write(f"\n===== {label}: {' '.join(cmd)}\n")
        proc = subprocess.Popen(cmd, cwd=cwd, env=config.odoo_env(profile), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace")
        for line in proc.stdout:
            log.write(line)
            info.feed(line)
            elapsed = time.monotonic() - start
            if m := LOADING_RE.search(line):
                status.update(f"{label}  {int(elapsed)}s  installing {m.group(1)} ({m.group(2)}/{m.group(3)})", key=m.group(1))
            elif m := TEST_START_RE.search(line):
                status.update(f"{label}  {int(elapsed)}s  testing {m.group(1)}: {m.group(2)}", key=m.group(1))
        proc.wait()
    status.done()
    if proc.returncode != 0 and not info.registry_failed and not info.failed_tests:
        info.crashed = True
    return info


def install_leaving_out(modules, graph, attempt: Callable[[list[str]], LogInfo]) -> tuple[Optional[LogInfo], dict[str, str]]:
    """Install modules together: `attempt(modules)` installs them on a new database and gives what Odoo's log said.
    When one of them fails to install, it is left out with the modules depending on it (`graph`), and the others are
    tried again. Gives the log of the last attempt (None when no module was left to try) and the modules left out:
    {module: the one that failed}."""
    remaining, left_out = sorted(modules), {}
    while remaining:
        info = attempt(remaining)
        culprit = info.culprit
        if culprit not in remaining:  # installed, or failed for a reason that is none of them
            return info, left_out
        excluded = [m for m in remaining if culprit in graph.closure([m])]
        typer.echo(f"⚠️ {culprit} failed to install: left out" +
                   (f", with {len(excluded) - 1} addon(s) depending on it" if len(excluded) > 1 else ""))
        left_out.update(dict.fromkeys(excluded, culprit))
        remaining = [m for m in remaining if m not in excluded]
    return None, left_out


def installed_modules(db: str, modules) -> set[str]:
    """Those of the modules that are installed in a database."""
    if not modules:
        return set()
    names = ", ".join(f"'{m}'" for m in modules)
    return set(pg.psql(db, f"SELECT name FROM ir_module_module WHERE state = 'installed' AND name IN ({names});").split())


@contextmanager
def serving(profile: dict, db: str, log_file: Path, data_dir: Optional[Path] = None):
    """Run the Odoo of a profile on a database for the time of the block. Gives its url, None if it did not start."""
    url = f"http://127.0.0.1:{free_port()}"
    cmd = [profile['python'], profile['odoo_bin'], "-d", db, f"--db-filter=^{db}$", "--addons-path",
           ",".join(profile['addons_paths']), f"--http-port={url.rsplit(':', 1)[1]}", "--http-interface=127.0.0.1",
           "--max-cron-threads=0", "--log-level=warn"] + (["--data-dir", str(data_dir)] if data_dir else [])
    with open(log_file, "w") as out:
        process = subprocess.Popen(cmd, env=config.odoo_env(profile), stdout=out, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 120
            while True:
                if process.poll() is not None or time.monotonic() > deadline:
                    url = None
                    break
                try:
                    urllib.request.urlopen(f"{url}/web/login", timeout=5).close()
                    break
                except OSError:
                    time.sleep(1)
            yield url
        finally:
            process.terminate()
            process.wait(timeout=30)


def supports(profile: dict, option: str) -> bool:
    """Whether the Odoo of the profile knows a command line option (they change with the versions)."""
    res = subprocess.run([profile['python'], profile['odoo_bin'], "server", "--help"], env=config.odoo_env(profile),
                         capture_output=True, text=True, errors="replace")
    return option in res.stdout + res.stderr


def demo_arguments(profile: dict, demo: bool) -> list[str]:
    """The options of odoo-bin to install modules with (or without) their demo data. Up to Odoo 18 they are loaded
    unless told not to (--without-demo); from 19 on only when asked (--with-demo): the option is looked for, not the
    version, so that a later version works the same."""
    on_request = supports(profile, "--with-demo")
    if demo:
        return ["--with-demo"] if on_request else []
    return [] if on_request else ["--without-demo=all"]


def major_version(profile: dict) -> Optional[int]:
    """Major version of the Odoo of a profile, from its odoo/release.py (version_info = (19, 0, ...)), None if unknown."""
    try:
        text = (Path(profile['odoo_root']) / "odoo" / "release.py").read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(r"^version_info\s*=\s*\(\s*['\"]?(?:saas~)?(\d+)", text, re.M)
    return int(found.group(1)) if found else None


def source_version(profile: dict) -> str:
    """Commits of Odoo and enterprise: what was built or measured with another one is outdated."""
    versions = []
    for folder in (profile['odoo_root'], profile['enterprise']):
        if folder:
            res = subprocess.run(["git", "-C", folder, "rev-parse", "HEAD"], capture_output=True, text=True)
            versions.append(res.stdout.strip() if res.returncode == 0 else "?")
    return "+".join(versions)
