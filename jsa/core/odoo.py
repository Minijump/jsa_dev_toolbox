"""Running the Odoo of a profile: installing modules while following its log, serving a database for a while."""
import os
import queue
import re
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from collections import Counter
from pathlib import Path
from typing import Callable, Iterator, Optional

import typer

from . import config, pg
from .ui import Board, StatusLine, duration

LOADING_RE = re.compile(r"odoo\.modules\.loading: Loading module (\w+) \((\d+)/(\d+)\)")
TEST_START_RE = re.compile(r"odoo\.addons\.(\w+)\.tests\S*: Starting (\S+)")
TEST_FAIL_RE = re.compile(r"odoo\.addons\.(\w+)\.tests\S*: (?:FAIL|ERROR): (\S+)")
# a test class whose setUpClass (or tearDownClass) failed: none of its tests ran. Logged by Odoo's test suite
CLASS_FAIL_RE = re.compile(r"odoo\.tests\.suite: (?:FAIL|ERROR): (setUpClass|tearDownClass) \(odoo\.addons\.(\w+)\.\S*?(\w+)\)")
# written by odoo_time_limit.py when a test class starts or ends: what runs when no "Starting" line says it
CLASS_STEP_RE = re.compile(r"odoo\.addons\.(\w+)\.tests\S*: jsa: (setUpClass|tearDownClass) (\w+)(?:: (.*))?")
TESTS_END_RE = re.compile(r"odoo\.service\.server: \d+ post-tests in|when loading database|Initiating shutdown")
REGISTRY_FAILED = ("Failed to load registry", "Failed to initialize database")
TIME_LIMIT = Path(__file__).with_name("odoo_time_limit.py")  # run by the python of a profile, see install
# minutes without a test starting (or a module loading), after the time limit, before Odoo is killed: during the
# tests, a stopped test cleans up in seconds; otherwise a big module may load, the coverage be saved, for a while
TEST_MARGIN = 1
SILENCE_MARGIN = 5
CANCEL_GRACE = 30  # seconds for a test whose query was cancelled to end, before Odoo is killed


@dataclass
class Stuck:
    """What made Odoo stop answering during the tests: tested again without it, counted as failed."""
    module: str
    spec: str  # for --test-tags: "/module:TestClass.test_method", or "/module:TestClass" for its setUpClass
    failed: list[str]  # the test, or the setUpClass and every test of its class

    @property
    def what(self) -> str:
        return self.failed[0]


@dataclass
class LogInfo:
    """What Odoo's log says about an install (and the tests run with it)."""
    loaded: list[str] = field(default_factory=list)  # modules in loading order
    tests: Counter = field(default_factory=Counter)  # tests run per module
    failed_tests: dict[str, list[str]] = field(default_factory=dict)  # failed test names per module
    registry_failed: bool = False  # Odoo reported that a module failed to install
    crashed: bool = False  # odoo-bin failed without that message
    testing: Optional[str] = None  # the module whose tests run
    timed_out: bool = False  # Odoo was killed: its log stayed silent too long
    running: Optional[tuple] = None  # (module, "TestClass.test_method") or (module, "TestClass", "setUpClass")
    class_tests: dict = field(default_factory=dict)  # {(module, class): its test names}, from odoo_time_limit.py
    in_tests: bool = False  # between a test starting and the end of the tests (see install)

    def feed(self, line: str):
        m = LOADING_RE.search(line)
        if m:
            self.loaded.append(m.group(1))
            self.in_tests = False
        m = TEST_START_RE.search(line)
        if m:
            self.tests[m.group(1)] += 1
            self.testing, self.running, self.in_tests = m.group(1), (m.group(1), m.group(2)), True
        m = CLASS_STEP_RE.search(line)
        if m:
            module, step, test_class, names = m.groups()
            if names is not None:
                self.class_tests[(module, test_class)] = names.split()
            self.testing, self.running, self.in_tests = module, (module, test_class, step), True
        if TESTS_END_RE.search(line):
            self.in_tests = False
        m = TEST_FAIL_RE.search(line)
        if m:
            self.fail(m.group(1), m.group(2))
        m = CLASS_FAIL_RE.search(line)
        if m:  # counted as a test too: the tests passed are the tests minus the failed ones
            self.tests[m.group(2)] += 1
            self.fail(m.group(2), f"{m.group(3)}.{m.group(1)}")
        if any(text in line for text in REGISTRY_FAILED):
            self.registry_failed = True

    def fail(self, module: str, test: str):
        if test not in self.failed_tests.setdefault(module, []):
            self.failed_tests[module].append(test)

    @property
    def failed(self) -> bool:
        return self.registry_failed or self.crashed or self.timed_out

    @property
    def culprit(self) -> Optional[str]:
        """The module whose loading made the install fail; when Odoo stopped answering, the one being tested (else
        being loaded)."""
        if self.timed_out:
            return (self.testing if self.in_tests else None) or (self.loaded[-1] if self.loaded else None)
        return self.loaded[-1] if self.registry_failed and self.loaded else None

    @property
    def stuck(self) -> Optional[Stuck]:
        """When Odoo was killed during the tests: the test, or the test class, that was running."""
        if not self.timed_out or not self.in_tests or not self.running:
            return None
        module, name, *step = self.running
        if not step:  # a test
            return Stuck(module, f"/{module}:{name}", [name])
        return Stuck(module, f"/{module}:{name}",
                     [f"{name}.{step[0]}"] + [f"{name}.{test}" for test in self.class_tests.get((module, name), [])])


def record_skipped(info: LogInfo, skipped: list[Stuck]):
    """The tests left out of the last try because Odoo stopped answering with them: failed, and counted."""
    for stuck in skipped:
        for name in stuck.failed:
            if name not in info.failed_tests.get(stuck.module, []):
                info.tests[stuck.module] += 1
                info.fail(stuck.module, name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def install(profile: dict, db: str, modules: list[str], data_dir: Path, log_file: Path, label: str, *,
            cwd: Optional[Path] = None, python_args: list[str] = (), odoo_args: list[str] = (),
            time_limit: Optional[float] = None) -> LogInfo:
    """Install modules in a database with the odoo-bin of a profile, following Odoo's log live (it is appended to
    `log_file`). `python_args` come before odoo-bin (e.g. to run it under coverage), `odoo_args` after its options.
    `time_limit`: the minutes a test, or the setUpClass of a test class, may take: it is stopped and fails beyond
    (odoo_time_limit.py runs odoo-bin). The database errs on a lock waited for longer (lock_timeout, PGOPTIONS): the
    test fails, the run goes on. For what Python cannot stop: when no test starts and no module loads for `time_limit`
    + TEST_MARGIN minutes during the tests, the queries running on the database are cancelled (a long query: the test
    fails, the run goes on); if that is not enough after CANCEL_GRACE seconds (or outside the tests, after
    + SILENCE_MARGIN minutes), Odoo is killed (`info.timed_out`, `info.stuck`). Progress, not any line: a stuck test
    may keep logging."""
    odoo_bin = [str(TIME_LIMIT), f"{time_limit:g}", profile['odoo_bin']] if time_limit else [profile['odoo_bin']]
    cmd = [profile['python'], *python_args, *odoo_bin, "-d", db, "--addons-path", ",".join(profile['addons_paths']),
           "--data-dir", str(data_dir), "--http-port", str(free_port()), "--stop-after-init", "--max-cron-threads=0",
           "--log-level=info", "-i", ",".join(modules), *odoo_args]
    env = with_lock_timeout(config.odoo_env(profile), time_limit) if time_limit else config.odoo_env(profile)
    info, status, start = LogInfo(), StatusLine(interval=30), time.monotonic()
    with open(log_file, "a", encoding="utf-8") as log:
        log.write(f"\n===== {label}: {' '.join(cmd)}\n")
        # its own process group: what it starts (a browser for the tours) is stopped with it
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
        last_progress, waited = [time.monotonic()], [0.0]
        if time_limit:
            def watch():
                cancelled_at = None  # the progress after which the queries were cancelled: done once per stall
                while proc.poll() is None:
                    waited[0] = time_limit + (TEST_MARGIN if info.in_tests else SILENCE_MARGIN)
                    stalled = time.monotonic() - last_progress[0]
                    if info.in_tests and cancelled_at is None and stalled > waited[0] * 60:
                        cancelled_at = last_progress[0]
                        cancelled = cancel_queries(db)
                        log.write(f"\n===== jsa: no test started for {waited[0]:g} min: {cancelled} query(ies) cancelled\n")
                        if cancelled:
                            typer.echo(f"⚠️ {info.running[1] if info.running else 'a test'}: no answer for {waited[0]:g} "
                                       f"min, its query cancelled")
                            time.sleep(CANCEL_GRACE)
                            continue
                    if cancelled_at is not None and last_progress[0] != cancelled_at:
                        cancelled_at = None  # it moved on: a next stall is a new one
                    elif stalled > waited[0] * 60:
                        info.timed_out = True
                        log.write(f"\n===== jsa: no test started and no module loaded for {waited[0]:g} min, Odoo killed\n")
                        _kill(proc)
                        return
                    time.sleep(1)

            threading.Thread(target=watch, daemon=True).start()
        board, now_doing = Board.shown, {'what': "starting Odoo", 'since': start}  # the board: see ui.board
        if board:
            board.set('odoo', lambda: _on_the_board(label, start, now_doing, info))
        try:
            for line in lines_of(proc):
                if LOADING_RE.search(line) or TEST_START_RE.search(line) or CLASS_STEP_RE.search(line):
                    last_progress[0] = time.monotonic()
                log.write(line)
                info.feed(line)
                elapsed = time.monotonic() - start
                if m := LOADING_RE.search(line):
                    now_doing.update(what=f"installing {m.group(1)} ({m.group(2)}/{m.group(3)})", since=time.monotonic())
                    if not board:
                        status.update(f"{label}  {int(elapsed)}s  {now_doing['what']}", key=m.group(1))
                elif m := TEST_START_RE.search(line):
                    now_doing.update(what=f"{m.group(1)}: {m.group(2)}", since=time.monotonic())
                    if not board:
                        status.update(f"{label}  {int(elapsed)}s  testing {m.group(1)}: {m.group(2)}", key=m.group(1))
                elif m := CLASS_STEP_RE.search(line):
                    now_doing.update(what=f"{m.group(1)}: {m.group(3)}.{m.group(2)}", since=time.monotonic())
            proc.wait()
        finally:  # Ctrl-C: Odoo, in its own process group, does not get it from the terminal. And what Odoo leaves
            _kill(proc)  # behind (a browser of a tour whose test was stopped) goes with its group
            if board:
                board.remove('odoo')
    status.done()
    if info.timed_out:
        stuck = info.stuck
        typer.echo(f"⚠️ Odoo stopped answering ({f'{stuck.module}: {stuck.what}' if stuck else info.culprit or 'see the log'}): "
                   f"killed after {waited[0]:g} min without a test starting")
    elif proc.returncode != 0 and not info.registry_failed and not info.failed_tests:
        info.crashed = True
    return info


def with_lock_timeout(env: dict, minutes: float) -> dict:
    """Odoo's environment where PostgreSQL gives up waiting for a lock after `minutes`: a test stuck on a lock fails
    with an error instead of waiting forever where Python cannot stop it (libpq reads PGOPTIONS)."""
    option = f"-c lock_timeout={max(1000, round(minutes * 60000))}"
    return {**env, "PGOPTIONS": f"{env['PGOPTIONS']} {option}" if env.get("PGOPTIONS") else option}


def cancel_queries(db: str) -> int:
    """Cancel the queries running on a database (a test waiting on one): the test gets an error. How many."""
    query = (f"SELECT count(pg_cancel_backend(pid)) FROM pg_stat_activity WHERE datname = '{db}' "
             "AND state = 'active' AND pid <> pg_backend_pid()")
    try:
        res = subprocess.run(["psql", "-X", "-q", "-tA", "-d", "postgres", "-c", query],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return int(res.stdout.strip()) if res.returncode == 0 and res.stdout.strip().isdigit() else 0


def _on_the_board(label: str, start: float, now_doing: dict, info: LogInfo) -> str:
    """Two lines of the board: this run of Odoo, and what it is doing (the test that runs), with their times."""
    now = time.monotonic()
    tests, failed = sum(info.tests.values()), sum(len(names) for names in info.failed_tests.values())
    return (f"🧪 {label} · {duration(now - start)}" + (f" · {tests} tests, {failed} failed" if tests else "")
            + f"\n   ▸ {now_doing['what']} · {duration(now - now_doing['since'])}")


def lines_of(proc: subprocess.Popen, grace: float = 2) -> Iterator[str]:
    """The lines Odoo writes, until it has exited. Not until its output closes: a process it started (the browser
    of a tour, anything a test launches) keeps that output open after Odoo has gone, and jsa would wait forever."""
    lines = queue.Queue()

    def read():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)  # closed

    threading.Thread(target=read, daemon=True).start()
    ended = None  # when Odoo was seen gone
    while True:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            if proc.poll() is None:
                continue
            ended = ended or time.monotonic()
            if time.monotonic() - ended > grace:  # what it wrote before exiting has been read
                return
            continue
        if line is None:
            return
        yield line


def _kill(proc: subprocess.Popen):
    """Odoo and what it started (its process group), even after Odoo itself has exited."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def install_leaving_out(modules, graph, attempt: Callable[[list[str], list[Stuck]], LogInfo],
                        all_or_nothing: bool = False) -> tuple[Optional[LogInfo], dict[str, str]]:
    """Install modules together: `attempt(modules, skipped)` installs them on a new database, without running the
    tests `skipped`, and gives what Odoo's log said. When Odoo stopped answering during a test (or a setUpClass), the
    same modules are tried again without it, and it counts as failed. When one of them fails to install (or Odoo
    stopped answering outside the tests), it is left out with the modules depending on it (`graph`), and the others
    are tried again; `all_or_nothing`: none is tried again, they all count as not installable (like production).
    Gives the log of the last attempt (None when no module was left to try) and the modules left out: {module: why}."""
    remaining, left_out, skipped = sorted(modules), {}, []
    while remaining:
        info = attempt(remaining, skipped)
        stuck = info.stuck
        if stuck and stuck.spec not in [s.spec for s in skipped]:
            skipped.append(stuck)
            typer.echo(f"⚠️ {stuck.module}: {stuck.what} made Odoo stop answering: counted as failed, the tests run "
                       "again without it")
            continue
        culprit = info.culprit
        if culprit not in remaining:  # installed, or failed for a reason that is none of them
            record_skipped(info, skipped)
            return info, left_out
        why = "Odoo stopped answering during its tests" if info.timed_out else "install failed"
        what = "made Odoo stop answering during its tests" if info.timed_out else "failed to install"
        if all_or_nothing:  # like production: one addon that does not install stops them all
            typer.echo(f"⚠️ {culprit} {what}: the {len(remaining)} addons count as not installable (all or nothing)")
            left_out.update({m: why if m == culprit else f"not tested: {culprit} {what} (all or nothing)"
                             for m in remaining})
            return None, left_out
        excluded = [m for m in remaining if culprit in graph.closure([m])]
        typer.echo(f"⚠️ {culprit} {what}: left out"
                   + (f", with {len(excluded) - 1} addon(s) depending on it" if len(excluded) > 1 else ""))
        left_out.update({m: why if m == culprit else f"depends on {culprit}" for m in excluded})
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
