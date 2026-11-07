"""jsa bisect: find the commit that broke a test, with git bisect, a new database being made for each commit tried.

The test comes from your checkout, committed or not: it usually did not exist in the commits searched, being written
to reproduce a bug found today. The tests/ folder of the module is copied into each commit tried (so its helpers come
along), and only the file holding the test is loaded: another test file needing newer code cannot get in the way.

The commits are checked out in a temporary git worktree: your checkout, its branch and your uncommitted changes are
not touched, and you can keep working meanwhile. The addons paths of the profile that are inside the repository are
pointed to that worktree for the time of the search.

A commit where the test cannot tell (the module does not install, the test cannot be loaded) is skipped, as
'git bisect skip' does: it is neither good nor bad.
"""
import ast
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import typer

from ..core import config
from ..core.addons import DependencyGraph, profile_addons, standard_depends
from ..core.odoo import LogInfo
from .coverage import OdooRunner

PASSES, FAILS, UNTESTABLE = "good", "bad", "skip"  # also the words of 'git bisect'


def parse_test(test: str) -> tuple[str, str, Optional[str], Optional[str]]:
    """'/module:TestClass.test_x' -> (module, 'module:TestClass.test_x', class, method): the tag is the test without
    its first slash. As in Odoo's --test-tags: /module.test_method, /module:TestClass, /module:TestClass.test_method"""
    found = re.fullmatch(r"/?(\w+)(?::(\w+))?(?:\.(\w+))?", test)
    if not found or not (found.group(2) or found.group(3)):
        typer.echo(f"❌ '{test}' is not a test: write it /module.test_name or /module:TestClass.test_name")
        raise typer.Exit(code=1)
    return found.group(1), test.lstrip("/"), found.group(2), found.group(3)


def files_of_test(tests: Path, class_name: Optional[str], method: Optional[str]) -> list[Path]:
    """The test files (test_*.py, the only ones Odoo looks at) of a tests/ folder holding a test."""
    found = []
    for path in sorted(tests.rglob("test_*.py")):
        try:
            classes = [node for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(node, ast.ClassDef)]
        except (SyntaxError, ValueError):
            continue
        # with its class given, the method can come from a parent class
        if any(node.name == class_name if class_name else
               any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method for item in node.body)
               for node in classes):
            found.append(path)
    return found


def tests_init(tests: Path, files: list[Path]) -> str:
    """The tests/__init__.py loading these files only."""
    lines = []
    for file in files:
        relative = file.relative_to(tests)
        lines.append(f"from .{'.'.join(relative.parts[:-1])} import {relative.stem}\n")
    return "".join(lines)


def in_worktree(profile: dict, repo: Path, tree: Path) -> dict:
    """The profile with what is inside the repository pointed to its worktree."""
    def moved(path: Optional[str]) -> Optional[str]:
        if not path:
            return path
        resolved = Path(path).resolve()
        return str(tree / resolved.relative_to(repo)) if resolved.is_relative_to(repo) else path

    return {**profile, 'odoo_root': moved(profile['odoo_root']), 'odoo_bin': moved(profile['odoo_bin']),
            'enterprise': moved(profile['enterprise']), 'addons_paths': [moved(p) for p in profile['addons_paths']]}


def installed_once(module: str, graph: DependencyGraph, from_repo: set[str]) -> list[str]:
    """What a module needs and that no commit of the repository changes: not from it, and not depending on it."""
    return sorted(m for m in graph.closure([module]) if m not in from_repo and not graph.closure([m]) & from_repo)


def verdict(info: LogInfo, module: str) -> tuple[str, str]:
    """(PASSES, FAILS or UNTESTABLE, why) from what Odoo's log said. No test run is not a success."""
    if info.failed:
        return UNTESTABLE, f"it does not install, or the test cannot be loaded ({info.culprit or 'see the log'})"
    if info.failed_tests.get(module):
        return FAILS, f"fails: {', '.join(info.failed_tests[module])}"
    if not info.tests[module]:
        return UNTESTABLE, "the test did not run (see the log)"
    return PASSES, f"passes ({info.tests[module]} test(s))"


def parse_step(output: str) -> tuple[str, list[str]]:
    """What 'git bisect' answered: ('next', []), ('found', [commit]), ('skipped', [the commits it could be])."""
    found = re.search(r"^([0-9a-f]{40,64}) is the first bad commit", output, re.M)
    if found:
        return 'found', [found.group(1)]
    if "only 'skip'ped commits left" in output:
        return 'skipped', re.findall(r"^[0-9a-f]{40,64}$", output, re.M)
    return ('next', []) if "Bisecting:" in output else ('error', [])


def steps_left(output: str) -> str:
    found = re.search(r"roughly (\d+) step", output)
    return f", about {found.group(1)} left" if found else ""


def _git(folder: Path, *args: str) -> subprocess.CompletedProcess:
    # in English, as its answers are read; never waiting for a password nobody can type
    env = {**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, errors="replace", env=env)


def _stop(message: str):
    typer.echo(f"❌ {message}")
    raise typer.Exit(code=1)


def _commit(repo: Path, name: str) -> str:
    res = _git(repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
    if res.returncode != 0:
        _stop(f"No commit '{name}' in {repo}")
    return res.stdout.strip()


def _describe(repo: Path, commit: str) -> str:
    return _git(repo, "log", "-1", "--date=short", "--format=%h %ad %an: %s", commit).stdout.strip()


class Search:
    """What every commit tried needs: where to put the test, how to run it."""

    def __init__(self, tree: Path, module: str, module_folder: Path, tag: str, tests: Path, init: str):
        self.tree, self.module, self.module_folder, self.tag, self.tests, self.init = tree, module, module_folder, tag, tests, init
        self.runner, self.template = None, None

    def prepare(self) -> Optional[str]:
        """Make the commit checked out ready for the test. Gives why it cannot be, if so."""
        if (self.tree / ".gitmodules").exists():
            res = _git(self.tree, "submodule", "update", "--init", "--recursive")
            if res.returncode != 0:
                return f"its submodules cannot be fetched ({res.stderr.strip()[-200:]})"
        if not (self.module_folder / "__manifest__.py").exists():
            return "the module does not exist at this commit"
        shutil.rmtree(self.module_folder / "tests", ignore_errors=True)
        shutil.copytree(self.tests, self.module_folder / "tests")
        (self.module_folder / "tests" / "__init__.py").write_text(self.init, encoding="utf-8")
        return None

    def attempt(self, label: str) -> tuple[str, str]:
        """Try the commit checked out, on a new database, then leave the worktree as git expects it."""
        try:
            problem = self.prepare()
            if problem:
                return UNTESTABLE, problem
            db = self.runner.new_db(label, template=self.template)
            try:
                return verdict(self.runner.run(db, [self.module], label, test=[self.tag], measure=False), self.module)
            finally:
                self.runner.drop(db)
        finally:
            _git(self.tree, "reset", "--hard", "--quiet")
            _git(self.tree, "clean", "-fdxq")


def _search(repo: Path, search: Search, profile: dict, good: str, bad: str, fast: bool, log_file: Path, work_dir: Path):
    tree = search.tree
    profile = in_worktree(profile, repo, tree)
    search.runner = runner = OdooRunner(profile, work_dir, log_file)
    try:
        if fast:
            problem = search.prepare()
            if problem:
                _stop(f"At {_describe(repo, bad)}: {problem}")
            from_repo = {name for name, path in profile_addons(profile).items() if path.resolve().is_relative_to(tree.resolve())}
            once = installed_once(search.module, DependencyGraph(standard_depends(profile)), from_repo)
            _git(tree, "reset", "--hard", "--quiet")
            _git(tree, "clean", "-fdxq")
            if once:
                typer.echo(f"🧱 --fast: installing once the {len(once)} modules that do not come from this repository...")
                search.template = runner.new_db("modules")
                info = runner.run(search.template, once, "modules")
                if info.failed:
                    _stop(f"They could not be installed ({info.culprit or 'see the log'}). Odoo log: {log_file}")

        state, why = search.attempt("bad")
        typer.echo(f"   {_describe(repo, bad)}: {why}")
        if state == PASSES:
            _stop("The test passes at the commit said to fail: nothing to search. Uncommitted code does not count, only the test.")
        if state == UNTESTABLE:
            _stop(f"Nothing can be told at the commit said to fail. Odoo log: {log_file}")
        _git(tree, "checkout", "--quiet", "--detach", good)
        state, why = search.attempt("good")
        typer.echo(f"   {_describe(repo, good)}: {why}")
        if state == FAILS:
            _stop("The test already fails at the commit said to work: give an older one.")
        if state == UNTESTABLE:
            _stop(f"Nothing can be told at the commit said to work: give another one. Odoo log: {log_file}")

        res = _git(tree, "bisect", "start", bad, good)
        tried, skipped = 0, 0
        while True:
            output = res.stdout + res.stderr
            outcome, commits = parse_step(output)
            if outcome == 'found':
                typer.echo(f"🎯 First bad commit: {_describe(repo, commits[0])}")
                typer.echo(f"   {commits[0]}")
                typer.echo(f"   {tried} commit(s) tried" + (f", {skipped} of them skipped" if skipped else "") + f". Odoo log: {log_file}")
                return
            if outcome == 'skipped':
                typer.echo("⚠️ The first bad commit is one of these (those in between could not be tested):")
                for commit in commits:
                    typer.echo(f"   {_describe(repo, commit)}")
                raise typer.Exit(code=1)
            if outcome == 'error':
                _stop(f"git bisect: {output.strip()[-500:]}")
            tried += 1
            commit = _git(tree, "rev-parse", "HEAD").stdout.strip()
            start = time.monotonic()
            state, why = search.attempt(f"step{tried}")
            skipped += state == UNTESTABLE
            typer.echo(f"   [{tried}{steps_left(output)}] {_describe(repo, commit)}: {why} ({int(time.monotonic() - start)}s)")
            res = _git(tree, "bisect", state)
    finally:
        runner.drop_all()


def bisect(
    test: str = typer.Argument(..., help="The test, taken from your checkout (committed or not): /module.test_name or /module:TestClass.test_name"),
    good: str = typer.Argument(..., help="A commit where the test passes"),
    bad: Optional[str] = typer.Argument(None, help="A commit where the test fails (default: the current commit)"),
    profile: str = typer.Option(..., "--profile", "-p", help="Profile of config.toml: the Odoo to run the test with"),
    fast: bool = typer.Option(False, "--fast", help="Install the modules from outside the repository once and copy that database for each commit: "
                              "faster, but every commit then gets the modules needed by the bad commit"),
):
    """Find the commit that broke a test: git bisect, with a new database for each commit tried.

    The test is copied into each commit: it does not have to exist in them. Your checkout is not touched (the commits
    are checked out in a temporary worktree) and the databases are deleted.
    """
    module, tag, class_name, method = parse_test(test)
    odoo_profile = config.load_profile(profile, config.load_config())
    module_folder = profile_addons(odoo_profile).get(module)
    if module_folder is None:
        _stop(f"No module {module} in the addons paths of profile '{profile}'")
    files = files_of_test(module_folder / "tests", class_name, method)
    if not files:
        _stop(f"No test {test} in {module_folder / 'tests'}: the test is taken from your checkout")
    res = _git(module_folder, "rev-parse", "--show-toplevel")
    if res.returncode != 0:
        _stop(f"{module_folder} is not in a git repository")
    root = Path(res.stdout.strip()).resolve()
    good_commit, bad_commit = _commit(root, good), _commit(root, bad or "HEAD")
    if good_commit == bad_commit or _git(root, "merge-base", "--is-ancestor", good_commit, bad_commit).returncode != 0:
        _stop(f"'{good}' must be older than the commit that fails ('{bad or 'the current commit'}'), on the same history")
    count = _git(root, "rev-list", "--count", f"{good_commit}..{bad_commit}").stdout.strip()
    log_file = config.ANALYSIS_DIR / "bisect_odoo.log"
    typer.echo(f"🔎 Searching {count} commit(s) of {root} for what broke {test} (Odoo log: {log_file})")
    typer.echo(f"🧪 The test comes from your checkout: {', '.join(str(f.relative_to(root)) for f in files)}")

    with tempfile.TemporaryDirectory(prefix="jsa_bisect_") as tmp:
        tree = Path(tmp) / "tree"
        # a copy of the test as it is now: you can keep working on your checkout during the search
        tests = Path(tmp) / "tests"
        shutil.copytree(module_folder / "tests", tests, ignore=shutil.ignore_patterns("__pycache__"))
        init = tests_init(module_folder / "tests", files)
        res = _git(root, "worktree", "add", "--quiet", "--detach", str(tree), bad_commit)
        if res.returncode != 0:
            _stop(f"git worktree: {res.stderr.strip()}")
        try:
            search = Search(tree, module, tree / module_folder.resolve().relative_to(root), tag, tests, init)
            _search(root, search, odoo_profile, good_commit, bad_commit, fast, log_file, Path(tmp))
        finally:
            _git(root, "worktree", "remove", "--force", str(tree))
            _git(root, "worktree", "prune")


def register(app: typer.Typer):
    app.command()(bisect)
