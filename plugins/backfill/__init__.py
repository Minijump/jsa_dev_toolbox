"""Plugin: measure old commits of your addons, to fill the past of the trends of the codebase report.

A plugin made of a folder: its code (this file), and its own file next to it, so nothing goes in config.toml:

    plugins/backfill/
        __init__.py       this code (part of the toolbox)
        backfill.toml     the commits to measure and your events (yours: copy backfill.toml.sample)

'jsa backfill' checks out each commit of backfill.toml in a temporary git worktree (your checkout is not touched),
does what 'jsa structure -c -m whole' then '-m isolated' do (`modes` of backfill.toml) with the profile running the
Odoo version of that code (read in its manifests: '17.0.1.0.0' is Odoo 17), and adds the runs to the history of the
report at the date of the commit. Then it makes the report of today's code again, with these runs in its trends. The
reports of each commit stay in codebase_analysis/backfill/<commit>/<mode>/. A commit already measured (in a mode) is
skipped: a backfill that stopped goes on where it was. Each commit is a full measure: count hours for many commits.
'jsa backfill --dry-run' shows the commits, their Odoo version and profile, without measuring anything.

Requires all the addons_roots of config.toml in one git repository. Uses jsa's own code (jsa.structure): it moves
with the toolbox.
"""
import os
import re
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import typer

from jsa import config
from jsa.core import odoo
from jsa.core.addons import find_addons, read_manifests
from jsa.structure import codebase, report
from jsa.structure.bisect import in_worktree

FOLDER = Path(__file__).resolve().parent
PLAN = FOLDER / "backfill.toml"
MODES = ("whole", "isolated")  # each commit measured both ways, unless `modes` of backfill.toml says otherwise
KEYS = {'commits': ('commit', 'event'), 'events': ('date', 'event')}
VERSION_RE = re.compile(r'^(\d+)\.0\.')


def stop(message: str):
    typer.echo(f"❌ {message}")
    raise typer.Exit(code=1)


def git(folder: Path, *args: str) -> subprocess.CompletedProcess:
    # in English, as its answers are read; never waiting for a password nobody can type
    env = {**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, errors="replace", env=env)


# ---------- backfill.toml ----------

def read_plan(path: Path = PLAN) -> dict:
    """{commits: [{commit, event?}], events: [{date, event}], profiles: {version: profile}, modes: [...]}. Stops on
    a mistake."""
    if not path.exists():
        stop(f"No {path}: copy {path.name}.sample next to it, and list the commits to measure")
    try:
        plan = config.tomllib.loads(path.read_text(encoding="utf-8"))
    except config.tomllib.TOMLDecodeError as e:
        stop(f"{path.name}: {e}")
    unknown = sorted(set(plan) - {'commits', 'events', 'profiles', 'modes'})
    if unknown:
        stop(f"{path.name}: {', '.join(unknown)} do(es) not exist (misspelled?): use [[commits]], [[events]], "
             "[profiles], modes")
    modes = plan.get('modes', list(MODES))
    if not isinstance(modes, list) or not modes or any(mode not in [m.value for m in codebase.Mode] for mode in modes):
        stop(f"{path.name}: modes is a list of {', '.join(m.value for m in codebase.Mode)}, e.g. [\"whole\", \"isolated\"]")
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
        stop(f"{path.name}: no [[commits]] to measure")
    for number, item in enumerate(plan['commits'], 1):
        if not isinstance(item.get('commit'), str) or not item['commit'].strip():
            stop(f"{path.name}: [[commits]] #{number} needs commit = \"<a commit hash, a tag or a branch>\"")
    for number, item in enumerate(plan.get('events', []), 1):
        if not isinstance(item.get('date'), str) or not report.parse_date(item['date']) or 'event' not in item:
            stop(f"{path.name}: [[events]] #{number} needs date = \"2025-09-01\" and event = \"what happened\"")
    profiles = plan.get('profiles', {})
    if not isinstance(profiles, dict) or not all(str(v).isdigit() and isinstance(p, str) for v, p in profiles.items()):
        stop(f"{path.name}: [profiles] gives a profile for an Odoo version, e.g. 17 = \"17\"")
    return {'commits': plan['commits'], 'events': plan.get('events', []), 'modes': list(dict.fromkeys(modes)),
            'profiles': {str(version): name for version, name in profiles.items()}}


# ---------- which Odoo, which profile ----------

def odoo_version(manifests: dict[str, dict]) -> Optional[int]:
    """The Odoo version the addons are written for: the major of their versions ('17.0.1.0.0'), the most common."""
    found = Counter(int(m.group(1)) for m in (VERSION_RE.match(str(manifest.get('version', '')))
                                              for manifest in manifests.values()) if m)
    return found.most_common(1)[0][0] if found else None


def profile_for(version: Optional[int], chosen: dict[str, str], conf: dict) -> str:
    """The profile of config.toml that runs this Odoo version: the one given in [profiles] of backfill.toml, else
    the only profile whose Odoo has this version. ValueError: why there is none."""
    if version is None:
        raise ValueError("no manifest says for which Odoo version it is written (version = \"17.0.1.0.0\")")
    if str(version) in chosen:
        if chosen[str(version)] not in conf.get('profiles', {}):
            raise ValueError(f"[profiles] of backfill.toml: no profile '{chosen[str(version)]}' in config.toml")
        return chosen[str(version)]
    matching = [name for name in conf.get('profiles', {})
                if odoo.major_version(config.load_profile(name, conf)) == version]
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


# ---------- the history of the report ----------

def measured_commits(history: list[dict]) -> set[tuple[str, str]]:
    """(commit, mode) already measured by a backfill."""
    return {(line['commit'], line.get('mode')) for line in history if line.get('commit')}


def add_events(history_file: Path, events: list[dict]) -> int:
    """The events not in the history yet (same date and text): a backfill run again does not repeat them."""
    known = {(str(line.get('date')), line.get('event')) for line in report.load_history(history_file) if line.get('event')}
    new = [{'date': event['date'], 'event': event['event']} for event in events
           if (str(event['date']), event['event']) not in known]
    for event in new:
        report.append_history(history_file, event)
    return len(new)


@dataclass
class Step:
    name: str  # as written in backfill.toml
    sha: str
    date: str
    version: int
    profile: str
    addons: int


def backfill(dry_run: bool = typer.Option(False, "--dry-run", help="Show the commits, their Odoo version and profile; measure nothing")):
    """Measure the commits of plugins/backfill/backfill.toml (coverage, mode whole then isolated) for the trends of the report."""
    plan = read_plan()
    conf = config.load_config()
    roots = config.addons_roots(conf)
    if not roots or any(not root.is_dir() for root in roots):
        stop(f"Set [codebase] addons_roots in {config.CONFIG_FILE} to existing folders")
    measures = codebase.measures_from_config(conf)
    trends = codebase.trends_from_config(conf, measures)
    limit = codebase.test_timeout(conf, warn=True)  # checked before hours of measures
    repo = repository(roots)
    history_file = config.ANALYSIS_DIR / report.HISTORY_FILE
    done = measured_commits(report.load_history(history_file))
    commits = [(item, resolve(repo, item['commit'])) for item in plan['commits']]
    events = plan['events'] + [{'date': commit_date(repo, sha), 'event': item['event']} for item, sha in commits
                               if item.get('event')]

    with worktree(repo) as tree:
        in_tree = [tree / root.resolve().relative_to(repo) for root in roots]
        steps = []
        for item, sha in commits:  # all of them first: a missing profile stops before hours of measures
            checkout(tree, sha)
            found = find_addons(in_tree)
            version = odoo_version(read_manifests(found, warn=False))
            try:
                profile = profile_for(version, plan['profiles'], conf)
            except ValueError as e:
                stop(f"{item['commit']}: {e}")
            steps.append(Step(item['commit'], sha, commit_date(repo, sha), version, profile, len(found)))
        for name in sorted({step.profile for step in steps}):
            moved = in_worktree(codebase.coverage_profile_of(name, conf), repo, tree)
            paths = {Path(p).resolve() for p in moved['addons_paths']}
            missing = [str(root.resolve().relative_to(tree)) for root in in_tree if root.resolve() not in paths]
            if missing:
                stop(f"Profile '{name}' does not load the addons of {repo / missing[0]}: its addons_paths must include it")

        modes = plan['modes']
        todo = [(step, mode) for step in steps for mode in modes if (step.sha, mode) not in done]
        typer.echo(f"📋 {len(steps)} commit(s) of {repo}, coverage measured in mode {' then '.join(modes)}, each test "
                   f"and setUpClass {limit:g} min at most ({len(steps) * len(modes) - len(todo)} of "
                   f"{len(steps) * len(modes)} measures already done):")
        for number, step in enumerate(steps, 1):
            label = step.name if step.sha.startswith(step.name) else f"{step.name} ({step.sha[:9]})"
            already = [mode for mode in modes if (step.sha, mode) in done]
            typer.echo(f"   [{number}] {label}  {step.date[:10]}  Odoo {step.version}, profile '{step.profile}', "
                       f"{step.addons} addons" + (f"  ✔ already measured: {', '.join(already)}" if already else ""))
        if dry_run:
            typer.echo(f"ℹ️ Dry run: nothing measured. {len(events)} event(s) in backfill.toml")
            return
        history_file.parent.mkdir(parents=True, exist_ok=True)
        added = add_events(history_file, events)
        if added:
            typer.echo(f"📌 {added} event(s) added to the history")

        failed = []
        for number, (step, mode) in enumerate(todo, 1):
            typer.echo(f"\n📍 [{number}/{len(todo)}] {step.name}, mode {mode}: {step.date[:10]}, Odoo {step.version}, "
                       f"profile '{step.profile}'")
            checkout(tree, step.sha)
            profile = in_worktree(codebase.coverage_profile_of(step.profile, conf), repo, tree)
            out = config.ANALYSIS_DIR / "backfill" / step.sha[:12] / mode
            try:
                run = codebase.analyse(conf, in_tree, out, step.profile, profile, codebase.Mode(mode), measure_all=True,
                                       measures=measures, trends=trends)
            except typer.Exit:  # said why: the next measures are taken
                run = None
            lines = report.load_history(out / report.HISTORY_FILE)
            if run and lines:
                report.append_history(history_file, {**lines[-1], 'code_date': lines[-1].get('code_date') or step.date,
                                                     'commit': step.sha})
            else:
                failed.append(f"{step.name} ({mode})")
                typer.echo(f"⚠️ {step.name}, mode {mode}: nothing measured (see above)")

    typer.echo("\n📊 The report of today's code, with the measured commits in its trends")
    codebase.analyse(conf, roots, config.ANALYSIS_DIR, measures=measures, trends=trends)
    typer.echo(f"✅ {len(todo) - len(failed)} measure(s) taken" + (f", ⚠️ {len(failed)} not: {', '.join(failed)}" if failed else ""))
    if failed:
        raise typer.Exit(code=1)


def register(app: typer.Typer):
    app.command("backfill")(backfill)
