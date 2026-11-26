"""Test coverage of the custom addons, measured two ways ('jsa structure -c' does both, see codebase.analyse):

- per module (run_isolated): each addon tested on a database holding exactly its dependencies, with its own tests
  (catches undeclared dependencies). Databases are built from "smart snapshots": a database with some dependencies
  installed is copied (seconds) and completed, instead of installing every dependency again for every addon.
- whole (run_whole): all custom addons installed together in one database, one test run, like production: the tests
  of an addon also run the code of those it depends on, and a test overridden in another addon counts.
  If an addon fails to install, it is excluded (with the addons depending on it) and the run is retried.

Standard dependencies are installed once in a snapshot, so a retry or a new addon starts from a copy of it. With
the tests of the standard modules (whole run only), everything is installed in one go instead, like odoo.sh: the
standard modules are then tested with the custom addons installed, and their tests count in the custom coverage.
"""
import json
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

import typer

from ..core import config, odoo, pg
from ..core.addons import DependencyGraph
from ..core.odoo import LogInfo
from ..core.pg import copy_database, drop_database

NOT_INSTALLABLE = -1
NO_REPORT = -2
# Not measured: code that runs when a module is installed, upgraded or uninstalled (hooks, migration scripts), never
# by its tests, so it would only lower the coverage. Tests themselves and manifests neither.
OMITTED = ("*/__manifest__.py", "*/__init__.py", "*/tests/*", "*/hooks.py", "*/hook.py", "*/hooks/*",
           "*/migrations/*", "*/upgrades/*")
RULES = 6  # version of what is measured: the measures of another version are not reused (3: demo data on Odoo 19+;
# 4: an addon that does not install counts as 0% covered, see never_run; 5: only the code inside functions, run by the
# tests, see module_coverage; 6: per module and whole, both measured). Measures of another version are not compared
# either (report.measured_the_same_way).
MIN_COVERAGE = (7, 6)  # coverage.py reports each function apart since 7.6 (July 2024)
TESTS_CONTEXT = "test"  # what coverage records while a test class runs (odoo_time_limit.py switches to it)


def too_old(version: str) -> bool:
    """Whether a version of coverage.py ("7.5.4") does not report each function apart (MIN_COVERAGE)."""
    try:
        return tuple(int(part) for part in version.split(".")[:2]) < MIN_COVERAGE
    except ValueError:
        return False  # a version not understood: let the run say


@dataclass
class Result:
    coverage: int  # percentage, NOT_INSTALLABLE or NO_REPORT
    note: str = ""  # why the addon could not be installed
    tests: int = 0  # tests run
    failed: list[str] = field(default_factory=list)  # names of the failed or errored tests
    statements: int = 0  # lines + branches measured
    covered: int = 0
    files: dict[str, str] = field(default_factory=dict)  # {file with uncovered lines: "12-15, 40"}

    @property
    def failures(self) -> int:
        return len(self.failed)

    @classmethod
    def from_state(cls, state: dict) -> "Result":
        return cls(**{f.name: state[f.name] for f in fields(cls) if f.name in state})


def display_percent(covered: int, total: int) -> int:
    """Like 'coverage report': never shows 100% (or 0%) unless everything (or nothing) is covered."""
    pct = 100 * covered / total
    shown = round(pct)
    if shown == 100 and covered < total:
        return 99
    if shown == 0 and covered > 0:
        return 1
    return shown


def line_ranges(lines: list[int]) -> str:
    """[1, 2, 3, 7] -> '1-3, 7'"""
    ranges, start, prev = [], None, None
    for n in sorted(lines) + [None]:
        if start is None:
            start = n
        elif n != prev + 1:
            ranges.append(str(start) if start == prev else f"{start}-{prev}")
            start = n
        prev = n
    return ", ".join(ranges)


def module_coverage(coverage_json: dict, addons: dict[str, Path]) -> dict[str, dict]:
    """Per addon, from 'coverage json' output: coverage (lines + branches, like 'coverage report --branch'),
    statements, covered, and the uncovered lines of each file. Only the code inside functions counts (the user,
    2026-11-24): imports, class bodies (field definitions), def lines and decorators run when Odoo imports the
    module, tested or not; coverage.py reports them as the function "" (7.6+)."""
    out = {name: {'coverage': NO_REPORT, 'statements': 0, 'covered': 0, 'files': {}} for name in addons}
    roots = {name: addon.resolve() for name, addon in addons.items()}
    for path, data in coverage_json.get("files", {}).items():
        path = Path(path).resolve()
        for name, root in roots.items():
            if path.is_relative_to(root):
                module = out[name]
                inside = [region for function, region in data.get("functions", {}).items() if function]
                for region in inside:
                    s = region["summary"]
                    module['covered'] += s["covered_lines"] + s.get("covered_branches", 0)
                    module['statements'] += s["num_statements"] + s.get("num_branches", 0)
                missing = sorted(line for region in inside for line in region.get("missing_lines", []))
                if missing:
                    module['files'][path.relative_to(root).as_posix()] = line_ranges(missing)
                break
    for module in out.values():
        if module['statements']:
            module['coverage'] = display_percent(module['covered'], module['statements'])
    return out


def never_run(addons: dict[str, Path], profile: dict) -> dict[str, int]:
    """Lines + branches of addons whose tests never ran (not installable): they count as not covered (0%), else the
    coverage would go down when they become installable. coverage.py reports the files of --source that nothing ran
    with what it would have measured (same --branch and --omit as the test runs): it is given an empty script."""
    with tempfile.TemporaryDirectory(prefix="jsa_never_run_") as tmp:
        (Path(tmp) / "nothing.py").write_text("")
        coverage = lambda *args: subprocess.run([profile['python'], "-m", "coverage", *args], cwd=tmp,
                                                env=config.odoo_env(profile), capture_output=True)
        coverage("run", "--branch", "--source", ",".join(str(p) for p in addons.values()), "--omit", ",".join(OMITTED),
                 "nothing.py")
        coverage("json", "-q", "-o", "coverage.json")
        out = Path(tmp) / "coverage.json"
        data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    return {name: measured['statements'] for name, measured in module_coverage(data, addons).items()}


class OdooRunner:
    """Runs odoo-bin of a profile on temporary databases whose filestores live in a temporary data_dir."""

    def __init__(self, profile: dict, work_dir: Path, log_file: Path, append: bool = False,
                 time_limit: Optional[float] = None):
        self.profile, self.work_dir, self.log_file = profile, work_dir, log_file
        self.time_limit = time_limit  # minutes a test or a setUpClass may take (see odoo.install)
        self.data_dir = work_dir / "data"
        self.prefix = f"{profile['db_prefix']}_{uuid.uuid4().hex[:6]}"
        self.databases = []
        # the modules are installed with their demo data, on every version (Odoo 19 no longer loads it by default):
        # tests written for a database with demo data keep passing, as on odoo.sh's development builds
        self.demo_args = odoo.demo_arguments(profile, True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if not append:
            log_file.write_text("", encoding="utf-8")

    def new_db(self, name: str, template: Optional[str] = None) -> str:
        db = f"{self.prefix}_{name}"[:63]
        if template:
            copy_database(template, db, self.data_dir)
        else:
            pg.createdb(db)
        self.databases.append(db)
        return db

    def drop(self, db: str):
        drop_database(db, self.data_dir, check=False)
        self.databases.remove(db)

    def drop_all(self):
        for db in list(self.databases):
            self.drop(db)

    def installed(self, db: str, modules) -> set[str]:
        return odoo.installed_modules(db, modules)

    def run(self, db: str, install: list[str], label: str, test: list[str] = (), sources: list[Path] = (),
            measure: bool = True, skip: list[odoo.Stuck] = ()) -> LogInfo:
        """Install modules (+ run the tests of the `test` modules but those of `skip`, under coverage of `sources` if
        `measure`), following Odoo's log live."""
        python_args, odoo_args = [], []
        if test:
            if measure:
                python_args = ["-m", "coverage", "run", "--branch", "--source", ",".join(str(s) for s in sources),
                               "--omit", ",".join(OMITTED)]
            odoo_args = ["--test-enable", "--test-tags", ",".join([f"/{m}" for m in test] + [f"-{s.spec}" for s in skip])]
        return odoo.install(self.profile, db, install, self.data_dir, self.log_file, label, cwd=self.work_dir,
                            python_args=python_args, odoo_args=self.demo_args + odoo_args, time_limit=self.time_limit)

    def coverage_json(self) -> dict:
        """What the tests ran: the lines run while a test class ran (from its setUpClass to its tearDownClass), not
        those run to install the modules (their demo data runs computes and create overrides, tested or not). The
        launcher of the time limit marks the tests: without it (no time limit), everything counts."""
        out = self.work_dir / "coverage.json"
        tests_only = ["--contexts", f"^{TESTS_CONTEXT}$"] if self.time_limit else []
        subprocess.run([self.profile['python'], "-m", "coverage", "json", "-q", "-o", str(out), *tests_only],
                       cwd=self.work_dir, env=config.odoo_env(self.profile), capture_output=True)
        return json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}


def _results(runner: OdooRunner, db: str, info: LogInfo, addons: dict[str, Path], measure: bool = True) -> dict[str, Result]:
    coverage = module_coverage(runner.coverage_json() if measure else {}, addons)
    installed = runner.installed(db, addons)
    return {name: Result(**coverage[name], tests=info.tests[name], failed=info.failed_tests.get(name, []))
            if name in installed else Result(NOT_INSTALLABLE, note="not installed (dependency missing from the addons path?)")
            for name in addons}


def run_whole(addons: dict[str, Path], graph: DependencyGraph, runner: OdooRunner, measure: bool = True,
              standard_tests: bool = False, all_or_nothing: bool = False) -> tuple[dict[str, Result], Optional[dict]]:
    """Results of the addons, and those of the tests of the standard modules ({module: {tests, failed}}, None when
    they were not run). `all_or_nothing`: an addon that cannot install makes them all not installable (like in
    production), instead of being left out and the others tried again."""
    # an addon needing a module found in no addons path (directly or through another addon) cannot install: left out
    # before, else the install of everything fails with no culprit in the log
    missing = {name: sorted(graph.closure([name]) - set(graph.depends)) for name in addons}
    results = {name: Result(NOT_INSTALLABLE, note=f"needs {', '.join(deps)}, in no addons path")
               for name, deps in missing.items() if deps}
    if results and all_or_nothing:
        first = min(results)
        typer.echo(f"⚠️ {first} needs {', '.join(missing[first])}, in no addons path: the {len(addons)} addons count "
                   "as not installable (all or nothing)")
        return {**{name: Result(NOT_INSTALLABLE, note=f"not tested: {first} needs a module in no addons path (all or nothing)")
                   for name in addons}, **results}, None
    addons = {name: path for name, path in addons.items() if name not in results}
    if not addons:
        return results, None
    standard = sorted(graph.closure(addons) - graph.custom)
    base = None
    if standard_tests:  # no snapshot: the standard modules have to be installed in the run that tests them
        typer.echo(f"🧱 The {len(standard)} standard modules are installed with the custom addons, and tested too (long)")
    else:
        typer.echo(f"🧱 Installing the {len(standard)} standard dependencies (once)...")
        base = runner.new_db("standard")
        info = runner.run(base, standard, "standard dependencies")
        if info.failed:
            return {**results, **{name: Result(NOT_INSTALLABLE, note=f"standard dependencies failed to install ({info.culprit})")
                                  for name in addons}}, None

    tried = {}  # the database of the last attempt

    def attempt(modules: list[str], skipped: list[odoo.Stuck]) -> LogInfo:
        typer.echo(f"🧪 Installing and testing {len(modules)} custom addons together...")
        db = tried['db'] = runner.new_db("whole", template=base)
        tested = modules + (sorted(graph.closure(modules) - graph.custom) if standard_tests else [])
        info = runner.run(db, modules, "whole", test=tested, sources=[addons[m] for m in modules], measure=measure,
                          skip=skipped)
        if info.failed:
            runner.drop(db)
        return info

    info, left_out = odoo.install_leaving_out(addons, graph, attempt, all_or_nothing)
    results.update({m: Result(NOT_INSTALLABLE, note=why) for m, why in left_out.items()})
    kept, standard_results = {name: path for name, path in addons.items() if name not in left_out}, None
    if info and info.failed:
        results.update({m: Result(NOT_INSTALLABLE, note=f"install failed ({info.culprit or 'see the log'})") for m in kept})
    elif info:
        results.update(_results(runner, tried['db'], info, kept, measure))
        if standard_tests:
            standard_results = {m: {'tests': info.tests[m], 'failed': info.failed_tests.get(m, [])}
                                for m in sorted(set(info.tests) | set(info.failed_tests)) if m not in graph.custom}
    return results, standard_results


def run_isolated(addons: dict[str, Path], graph: DependencyGraph, runner: OdooRunner) -> dict[str, Result]:
    results, snapshots, failed = {}, {}, {}  # snapshots: {installed modules: db}, failed: {modules: reason}
    start = {m: graph.dependencies(m) for m in addons}
    order = sorted(addons, key=lambda m: (len(start[m]), m))  # small dependency sets first: they seed the bigger ones
    for i, module in enumerate(order):
        deps = start[module]
        label = f"[{i + 1}/{len(order)}] {module}"
        if deps not in snapshots and deps not in failed:
            base = max((s for s in snapshots if s <= deps), key=len, default=None)
            db = runner.new_db(f"snap{i}", template=snapshots.get(base))
            info = runner.run(db, sorted(deps - (base or frozenset())), f"{label}: dependencies")
            if info.failed:
                failed[deps] = f"dependency {info.culprit or '?'} failed to install"
                runner.drop(db)
            else:
                snapshots[deps] = db
        if deps in failed:
            results[module] = Result(NOT_INSTALLABLE, note=failed[deps])
        else:
            skipped = []  # tests that made Odoo stop answering: run again without them, counted as failed
            while True:
                db = runner.new_db(f"test{i}", template=snapshots[deps])
                info = runner.run(db, [module], f"{label}: tests", test=[module], sources=[addons[module]], skip=skipped)
                stuck = info.stuck
                if not stuck or stuck.spec in [s.spec for s in skipped]:
                    break
                skipped.append(stuck)
                typer.echo(f"⚠️ {stuck.module}: {stuck.what} made Odoo stop answering: counted as failed, the tests "
                           "run again without it")
                runner.drop(db)
            if not info.failed:
                odoo.record_skipped(info, skipped)
            results[module] = (Result(NOT_INSTALLABLE, note=odoo.why_it_failed(info)) if info.failed
                               else _results(runner, db, info, {module: addons[module]})[module])
            runner.drop(db)
        typer.echo(f"   {label}: {describe(results[module])}")
        # a snapshot is kept only while a remaining addon can start from it
        pending = [start[m] for m in order[i + 1:]]
        for deps_set in [s for s in snapshots if not any(s <= p for p in pending)]:
            runner.drop(snapshots.pop(deps_set))
    return results


def describe(result: Result) -> str:
    if result.coverage == NOT_INSTALLABLE:
        return f"could not install ({result.note})" if result.note else "could not install"
    text = "no python to measure" if result.coverage == NO_REPORT else f"{result.coverage}% coverage"
    return f"{text}, {result.tests} test(s)" + (f", {result.failures} failed" if result.failures else "")


def measure(addons: dict[str, Path], graph: DependencyGraph, profile: dict, mode: str, log_file: Path, *,
            measure_coverage: bool = True, standard_tests: bool = False, append: bool = False,
            time_limit: Optional[float] = None, all_or_nothing: bool = False) -> tuple[dict[str, Result], Optional[dict]]:
    """Results of the given (installable) addons in mode 'whole' (all together) or 'isolated' (per module), and those
    of the tests of the standard modules (whole only; None when not run).
    Temporary databases and filestores are always cleaned up. `append`: add to the log of a previous measure.
    `time_limit`: minutes a test, or the setUpClass of a test class, may take (see odoo.install).
    `all_or_nothing` (whole): one addon that cannot install makes them all not installable (see run_whole)."""
    if not addons:
        return {}, None
    with tempfile.TemporaryDirectory(prefix="jsa_coverage_") as tmp:
        runner = OdooRunner(profile, Path(tmp), log_file, append, time_limit)
        try:
            if mode == "whole":
                return run_whole(addons, graph, runner, measure_coverage, standard_tests, all_or_nothing)
            return run_isolated(addons, graph, runner), None
        finally:
            runner.drop_all()
