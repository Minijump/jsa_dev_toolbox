"""jsa backfill: record commits of your addons in the history of the codebase report (its trend).

The history is made of the commits listed in backfill.toml (at the root of the toolbox; in a project, in its .jsa/
folder), and only of them: 'jsa structure' never adds to it (the user's choice, 2026-11-27: adding to the history is
done on purpose). Each recorded commit is a small file, codebase_analysis/runs/<commit>.json: what the trend draws
(the totals of the report, the date of the commit, the Odoo version, the security findings, your measures).

'jsa backfill' checks out each commit of backfill.toml not recorded yet in a temporary git worktree (your checkout is
not touched), does what 'jsa structure -c' does (per module, then whole) with the profile running the Odoo version of
that code (read in its manifests: '17.0.1.0.0' is Odoo 17), and records it. In the whole run, unlike 'jsa structure',
an addon that does not install makes them all not installable together (whole coverage 0%: all or nothing, like in
production), with a single install per commit; the measures per module stay. Then it makes the report of today's
code again, with these commits in its trend. A commit already recorded is skipped: a backfill that stopped goes on
where it was; to measure one again, delete its file in runs/. Each commit is a full measure: count hours for many.
backfill.toml decides what the history holds: a commit removed from it is forgotten, its event follows the file. A
measure added to config.toml ([[report.measures]]) is counted on the commits already recorded, in seconds (no
install, no test); so is the security review on the commits recorded before jsa had it.
'jsa backfill --dry-run' shows the commits, their Odoo version and profile, without measuring anything.

Requires all the addons_roots of config.toml in one git repository.
"""
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import typer

from ..core import config, odoo, ui
from ..core.addons import find_addons, read_manifests
from . import codebase, report, security
from .bisect import in_worktree

PLAN_NAME = "backfill.toml"
KEYS = {'commits': ('commit', 'event'), 'events': ('date', 'event')}


def stop(message: str):
    typer.echo(f"❌ {message}")
    raise typer.Exit(code=1)


def git(folder: Path, *args: str) -> subprocess.CompletedProcess:
    # in English, as its answers are read; never waiting for a password nobody can type
    env = {**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, errors="replace", env=env)


# ---------- backfill.toml ----------

def plan_file() -> Path:
    """backfill.toml: in the .jsa/ folder of the project worked on, else at the root of the toolbox."""
    return config.backfill_file()


def read_plan(path: Optional[Path] = None) -> dict:
    """{commits: [{commit, event?}], events: [{date, event}], profiles: {version: profile}}. Stops on a mistake."""
    path = path or plan_file()
    if not path.exists():
        before = config.TOOLBOX_DIR / "plugins" / "backfill" / PLAN_NAME  # where the plugin of jsa 0.39 read it
        if before.exists() and path != before:
            stop(f"backfill is part of jsa since 0.40: move {before} to {path}")
        stop(f"No {path}: copy {config.TOOLBOX_DIR / (PLAN_NAME + '.sample')} there, and list the commits to record")
    try:
        plan = config.tomllib.loads(path.read_text(encoding="utf-8"))
    except config.tomllib.TOMLDecodeError as e:
        stop(f"{path.name}: {e}")
    if 'modes' in plan:
        stop(f"{path.name}: modes no longer exists (jsa 0.37): each commit is measured per module and whole, delete "
             "the line")
    unknown = sorted(set(plan) - {'commits', 'events', 'profiles'})
    if unknown:
        stop(f"{path.name}: {', '.join(unknown)} do(es) not exist (misspelled?): use [[commits]], [[events]], "
             "[profiles]")
    for table, allowed in KEYS.items():
        for number, item in enumerate(plan.get(table, []), 1):
            if not isinstance(item, dict):
                stop(f"{path.name}: [[{table}]] #{number} must be a table")
            wrong = sorted(set(item) - set(allowed))
            if wrong:
                stop(f"{path.name}: [[{table}]] #{number} has the key(s) {', '.join(wrong)}, which do not exist "
                     f"(misspelled?): use {', '.join(allowed)}")
            if 'event' in item and (not isinstance(item['event'], str) or not item['event'].strip()):
                stop(f"{path.name}: [[{table}]] #{number}: event must be a text")
    if not plan.get('commits'):
        stop(f"{path.name}: no [[commits]] to record")
    for number, item in enumerate(plan['commits'], 1):
        if not isinstance(item.get('commit'), str) or not item['commit'].strip():
            stop(f"{path.name}: [[commits]] #{number} needs commit = \"<a commit hash, a tag or a branch>\"")
    for number, item in enumerate(plan.get('events', []), 1):
        if not isinstance(item.get('date'), str) or not report.parse_date(item['date']) or 'event' not in item:
            stop(f"{path.name}: [[events]] #{number} needs date = \"2025-09-01\" and event = \"what happened\"")
    profiles = plan.get('profiles', {})
    if not isinstance(profiles, dict) or not all(str(v).isdigit() and isinstance(p, str) for v, p in profiles.items()):
        stop(f"{path.name}: [profiles] gives a profile for an Odoo version, e.g. 17 = \"17\"")
    return {'commits': plan['commits'], 'events': plan.get('events', []),
            'profiles': {str(version): name for version, name in profiles.items()}}


# ---------- which Odoo, which profile ----------

def profile_for(version: Optional[int], chosen: dict[str, str], conf: dict) -> str:
    """The profile of config.toml that runs this Odoo version: the one given in [profiles] of backfill.toml, else
    the only profile whose Odoo has this version. ValueError: why there is none."""
    if version is None:
        raise ValueError("no manifest says for which Odoo version it is written (version = \"17.0.1.0.0\")")
    if str(version) in chosen:
        if chosen[str(version)] not in conf.get('profiles', {}):
            raise ValueError(f"[profiles] of backfill.toml: no profile '{chosen[str(version)]}' in config.toml")
        return chosen[str(version)]
    matching = odoo.profiles_running(version, conf)
    if len(matching) != 1:
        which = f"several profiles ({', '.join(matching)})" if matching else "no profile"
        raise ValueError(f"{which} of config.toml run Odoo {version}: say which one in backfill.toml, "
                         f"[profiles] {version} = \"<profile>\"")
    return matching[0]


# ---------- the repository and its worktree ----------

def repository(roots: list[Path]) -> Path:
    """The git repository holding all the addons folders."""
    tops = set()
    for root in roots:
        res = git(root, "rev-parse", "--show-toplevel")
        if res.returncode != 0:
            stop(f"{root} is not in a git repository")
        tops.add(Path(res.stdout.strip()).resolve())
    if len(tops) > 1:
        stop(f"The addons_roots of config.toml are in several git repositories ({', '.join(map(str, sorted(tops)))}): "
             "jsa backfill moves one repository through its commits")
    return tops.pop()


@contextmanager
def worktree(repo: Path) -> Iterator[Path]:
    """A temporary checkout of the repository: the user's own checkout never moves. Removed at the end, Ctrl-C too."""
    with tempfile.TemporaryDirectory(prefix="jsa_backfill_") as tmp:
        tree = Path(tmp) / "tree"
        res = git(repo, "worktree", "add", "--quiet", "--detach", str(tree), "HEAD")
        if res.returncode != 0:
            stop(f"git worktree: {res.stderr.strip()}")
        try:
            yield tree
        finally:
            git(repo, "worktree", "remove", "--force", str(tree))
            git(repo, "worktree", "prune")


def checkout(tree: Path, commit: str):
    """The commit in the worktree, without anything left by the previous one (files of Python, of Odoo)."""
    for args in (("checkout", "--quiet", "--force", "--detach", commit), ("clean", "-ffdxq"),
                 ("submodule", "update", "--init", "--recursive", "--quiet")):
        res = git(tree, *args)
        if res.returncode != 0:
            stop(f"git {args[0]} {commit}: {res.stderr.strip()}")


def resolve(repo: Path, name: str) -> str:
    res = git(repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
    if res.returncode != 0:
        stop(f"backfill.toml: no commit '{name}' in {repo}")
    return res.stdout.strip()


def commit_date(repo: Path, commit: str) -> str:
    return datetime.fromtimestamp(int(git(repo, "log", "-1", "--format=%ct", commit).stdout.strip())).isoformat()


# ---------- the recorded commits (codebase_analysis/runs/) ----------

def missing_measures(runs: list[dict], labels: list[str]) -> dict[str, list[str]]:
    """{commit: the measures of config.toml its record lacks}: added to config.toml after it was recorded."""
    missing = {}
    for run in runs:
        lacking = [label for label in labels if label not in (run.get('measures') or {})]
        if lacking:
            missing[run['commit']] = lacking
    return missing


def missing_security(runs: list[dict]) -> set[str]:
    """The recorded commits without a security review (recorded before jsa had it)."""
    return {run['commit'] for run in runs if run.get('security') is None}


def with_event(run: dict, event: Optional[str]) -> dict:
    """A record with the event backfill.toml gives to its commit (none: no event)."""
    run = {key: value for key, value in run.items() if key != 'event'}
    return {**run, 'event': event} if event else run


def where(number: int, total: int, step: "Step", start: float, took: list[float]) -> str:
    """The top line of the board: which measure, of which commit, since when, and the time left (the average of
    the measures taken so far)."""
    label = step.name if step.sha.startswith(step.name) else f"{step.name} ({step.sha[:9]})"
    left = f" · ≈ {ui.duration(sum(took) / len(took) * (total - len(took)))} left" if took else ""
    return (f"📍 Backfill {number}/{total} · {label} ({step.date[:10]}, Odoo {step.version}) · "
            f"{ui.duration(time.monotonic() - start)} elapsed{left}")


@dataclass
class Step:
    name: str  # as written in backfill.toml
    sha: str
    date: str
    version: int
    profile: str
    addons: int
    event: Optional[str] = None


def backfill(dry_run: bool = typer.Option(False, "--dry-run", help="Show the commits, their Odoo version and profile; measure nothing")):
    """Record the commits of backfill.toml in the history of the report (coverage per module and whole): its trend."""
    plan = read_plan()
    conf = config.load_config()
    roots = config.addons_roots(conf)
    if not roots or any(not root.is_dir() for root in roots):
        stop(f"Set [codebase] addons_roots in {config.settings_file()} to existing folders")
    measures = codebase.measures_from_config(conf)
    trends = codebase.trends_from_config(conf, measures)
    limit = codebase.test_timeout(conf, warn=True)  # checked before hours of measures
    repo = repository(roots)
    out = config.ANALYSIS_DIR
    runs = report.load_runs(out)
    done = {run['commit'] for run in runs}
    commits = [(item, resolve(repo, item['commit'])) for item in plan['commits']]

    with worktree(repo) as tree:
        in_tree = [tree / root.resolve().relative_to(repo) for root in roots]
        steps = []
        for item, sha in commits:  # all of them first: a missing profile stops before hours of measures
            checkout(tree, sha)
            found = find_addons(in_tree)
            version = odoo.written_for(read_manifests(found, warn=False))
            try:
                profile = profile_for(version, plan['profiles'], conf)
            except ValueError as e:
                stop(f"{item['commit']}: {e}")
            steps.append(Step(item['commit'], sha, commit_date(repo, sha), version, profile, len(found), item.get('event')))
        for name in sorted({step.profile for step in steps}):
            moved = in_worktree(codebase.coverage_profile_of(name, conf), repo, tree)
            paths = {Path(p).resolve() for p in moved['addons_paths']}
            missing = [str(root.resolve().relative_to(tree)) for root in in_tree if root.resolve() not in paths]
            if missing:
                stop(f"Profile '{name}' does not load the addons of {repo / missing[0]}: its addons_paths must include it")

        todo = [step for step in steps if step.sha not in done]
        typer.echo(f"📋 {len(steps)} commit(s) of {repo}, coverage measured per module and whole, each test and "
                   f"setUpClass {limit:g} min at most ({len(steps) - len(todo)} already recorded):")
        for number, step in enumerate(steps, 1):
            label = step.name if step.sha.startswith(step.name) else f"{step.name} ({step.sha[:9]})"
            typer.echo(f"   [{number}] {label}  {step.date[:10]}  Odoo {step.version}, profile '{step.profile}', "
                       f"{step.addons} addons" + ("  ✔ already recorded" if step.sha in done else ""))
        wanted = {step.sha: step for step in steps}
        leaving = [run for run in runs if run['commit'] not in wanted]
        kept = [run for run in runs if run['commit'] in wanted]
        to_count = missing_measures(kept, [m.label for m in measures])
        off = security.switched_off(conf)  # [security] skip of config.toml: every rule off = no review at all
        to_review = missing_security(kept) if len(off) < len(security.RULES) else set()
        if leaving:
            typer.echo(f"🗑️ {len(leaving)} recorded commit(s) no longer in backfill.toml: "
                       + ("would be forgotten" if dry_run else "forgotten"))
        if to_count:
            labels = sorted({label for missing in to_count.values() for label in missing})
            typer.echo(f"🔢 {', '.join(labels)}: to count on {len(to_count)} commit(s) already recorded (no test run)")
        if to_review:
            typer.echo(f"🔒 Security review: to make on {len(to_review)} commit(s) already recorded (no test run)")
        if dry_run:
            typer.echo(f"ℹ️ Dry run: nothing measured. {len(plan['events'])} event(s) in backfill.toml")
            return
        for run in leaving:
            report.forget_run(out, run['commit'])
        for run in kept:  # the event of a commit follows backfill.toml
            if run.get('event') != wanted[run['commit']].event:
                report.save_run(out, with_event(run, wanted[run['commit']].event))
        if to_count:  # in seconds: the code of each commit, no install
            for sha, labels in to_count.items():
                checkout(tree, sha)
                found = find_addons(in_tree)
                manifests = read_manifests(found, warn=False)
                counts = codebase.count_measures({a.name: a for a in found if a.name in manifests},
                                                 [m for m in measures if m.label in labels])
                run = next(run for run in report.load_runs(out) if run['commit'] == sha)
                report.save_run(out, {**run, 'measures': {**(run.get('measures') or {}), **counts}})
                typer.echo(f"   🔢 {sha[:9]}: " + ", ".join(f"{label} {count:,}" for label, count in counts.items()))
        if to_review:  # in seconds too: the code of each commit is read
            for sha in sorted(to_review):
                checkout(tree, sha)
                addons_found = find_addons(in_tree)
                manifests = read_manifests(addons_found, warn=False)
                findings, _ = security.scan({a.name: a for a in addons_found if a.name in manifests}, codebase.code_files,
                                            off)
                run = next(run for run in report.load_runs(out) if run['commit'] == sha)
                report.save_run(out, {**run, 'security': security.by_rule(vars(f) for f in findings)})
                typer.echo(f"   🔒 {sha[:9]}: {len(findings)} finding(s)")

        failed, start, took = [], time.monotonic(), []  # took: how long each measure lasted, for the time left
        with ui.board() as board:  # in a terminal: where it is, kept at the bottom (see ui.board)
            for number, step in enumerate(todo, 1):
                typer.echo(f"\n📍 [{number}/{len(todo)}] {step.name}: {step.date[:10]}, Odoo {step.version}, "
                           f"profile '{step.profile}'")
                began = time.monotonic()
                if board:
                    board.set('backfill', lambda n=number, s=step: where(n, len(todo), s, start, took))
                checkout(tree, step.sha)
                profile = in_worktree(codebase.coverage_profile_of(step.profile, conf), repo, tree)
                with tempfile.TemporaryDirectory(prefix="jsa_backfill_report_") as measured:
                    try:
                        # whole: one addon that cannot install fails them all, like in production (no retry without it)
                        run = codebase.analyse(conf, in_tree, Path(measured), step.profile, profile, measure_all=True,
                                               measures=measures, trends=trends, all_or_nothing=True)
                    except typer.Exit:  # said why: the next measures are taken
                        run = None
                    if run:
                        report.save_run(out, with_event({**run, 'commit': step.sha,
                                                         'code_date': run.get('code_date') or step.date}, step.event))
                    else:  # Odoo's log, to see why
                        log = Path(measured) / "coverage_odoo.log"
                        kept_log = report.runs_folder(out) / f"{step.sha[:12]}.log"
                        if log.exists():
                            kept_log.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy(log, kept_log)
                        failed.append(step.name)
                        typer.echo(f"⚠️ {step.name}: nothing measured (see above" + (f", Odoo's log: {kept_log})" if log.exists() else ")"))
                took.append(time.monotonic() - began)

    typer.echo("\n📊 The report of today's code, with the recorded commits in its trend")
    codebase.analyse(conf, roots, config.ANALYSIS_DIR, measures=measures, trends=trends)
    typer.echo(f"✅ {len(todo) - len(failed)} commit(s) recorded" + (f", ⚠️ {len(failed)} not: {', '.join(failed)}" if failed else ""))
    if failed:
        raise typer.Exit(code=1)


def register(app: typer.Typer):
    app.command("backfill")(backfill)
