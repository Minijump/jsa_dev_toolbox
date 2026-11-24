"""Data of the codebase report: state of each addon, totals, changes since the previous run, history of the runs
and layout of the dependency graph. The page itself is rendered by report_html.

Files written in the analysis folder:
- report.html   the report (the only file to open)
- state.json    last state of each addon; also the cache of the coverage measures
- history.jsonl one line per measured run, for the trend charts (delete a line to forget a run). Lines added by
                hand as {"date": "2026-11-01", "event": "Release 2.0"} are events: a vertical line on the charts.
"""
import json
import math
import shutil
import subprocess
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from . import report_html, security
from .coverage import NOT_INSTALLABLE, Result

REPORT_FILE = "report.html"
STATE_FILE = "state.json"
HISTORY_FILE = "history.jsonl"
SCHEMA = 2  # version of the measures stored in the state: older ones are measured again


# ---------- state ----------

def build_state(loc: dict, dependencies: dict, results: dict[str, Optional[Result]], extra: Optional[dict] = None) -> dict:
    """{addon: {loc, depends, coverage (None: not measured), tests, failed, ..., + extra (fingerprint, mode, ...)}}"""
    state = {}
    for name in sorted(loc):
        result = results.get(name)
        state[name] = {'loc': int(loc[name] or 0), 'depends': list(dependencies.get(name, [])),
                       **(asdict(result) if result else {'coverage': None}), **(extra or {}).get(name, {})}
    return state


def load_state(out_dir: Path) -> tuple[dict, Optional[str]]:
    """Return (state, date of the run) of the previous run, ({}, None) if there is none."""
    try:
        raw = json.loads((out_dir / STATE_FILE).read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}, None
    return raw.get('addons', {}), raw.get('generated_at')


def status(entry: dict) -> str:
    coverage = entry.get('coverage')
    if coverage is None:
        return 'not_measured'
    if coverage == NOT_INSTALLABLE:
        return 'not_installable'
    if entry.get('failed') or entry.get('together'):
        return 'failing'
    return 'ok' if entry.get('tests') else 'no_tests'


def security_ids(state: dict) -> Optional[set[str]]:
    """The ids of the security findings of a state; None when it was made before jsa had the security review."""
    if not any('security' in e for e in state.values()):
        return None
    return {f['id'] for e in state.values() for f in e.get('security') or []}


def summarize(state: dict) -> dict:
    """Totals of a state. The coverage is weighted by the size of the addons (covered / measured statements).
    'security': the ids of the findings of each rule (the chart counts those not reviewed: reviewing one lowers the
    whole curve), None when the state has none."""
    statuses = [status(e) for e in state.values()]
    scanned = [e for e in state.values() if 'security' in e]
    statements = sum(e.get('statements') or 0 for e in state.values())
    covered = sum(e.get('covered') or 0 for e in state.values())
    return {
        'addons': len(state),
        'loc': sum(e.get('loc') or 0 for e in state.values()),
        'measured': len(state) - statuses.count('not_measured'),
        'coverage': round(100 * covered / statements, 1) if statements else None,
        'tests': sum(e.get('tests') or 0 for e in state.values()),
        'failed': sum(len(e.get('failed') or []) for e in state.values()),
        'not_installable': statuses.count('not_installable'),
        'no_tests': statuses.count('no_tests'),
        # addons depending directly on Odoo enterprise modules (None: not checked)
        'enterprise': (sum(1 for e in state.values() if e.get('enterprise'))
                       if any(e.get('enterprise') is not None for e in state.values()) else None),
        'security': security.by_rule(f for e in scanned for f in e['security']) if scanned else None,
    }


def enterprise_dependencies(state: dict) -> dict[str, dict[str, list[str]]]:
    """{addon: {enterprise module: custom addons it comes through ([] when the addon depends on it directly)}}"""
    def reach(name, seen):
        found = {}
        for dep in state[name].get('depends', []):
            if dep in state and dep not in seen:
                seen.add(dep)
                for module in state[dep].get('enterprise') or []:
                    found.setdefault(module, []).append(dep)
                for module, via in reach(dep, seen).items():
                    found.setdefault(module, []).extend(via)
        return found

    out = {}
    for name, entry in state.items():
        modules = {module: [] for module in entry.get('enterprise') or []}
        for module, via in reach(name, {name}).items():
            modules.setdefault(module, via)
        if modules:
            out[name] = dict(sorted(modules.items()))
    return out


def measured_the_same_way(before: dict, after: dict) -> bool:
    """Whether two measures of an addon compare: same method, whatever the Odoo version (see series_of)."""
    return series_of({'mode': before.get('coverage_mode')}) == series_of({'mode': after.get('coverage_mode')})


def compute_diff(old: dict, new: dict) -> dict:
    """What changed between two states. The measures of an addon (installable, coverage, tests) are compared only
    when both were taken the same way (measured_the_same_way); the lines of code and dependencies always are."""
    diff = {
        'added': sorted(new.keys() - old.keys()),
        'removed': sorted(old.keys() - new.keys()),
        'not_comparable': [],  # addons measured another way than in the previous run: their measures are not compared
        'could_to_cant': [], 'cant_to_could': [],
        'newly_failing': [],  # (addon, tests failing now but not before)
        'fixed': [],  # (addon, tests that failed before and not anymore)
        'metrics_changed': [],  # (addon, loc change, coverage change or None, old coverage, new coverage)
        'deps_changed': [],  # (addon, added dependencies, removed dependencies)
        'enterprise_added': [],  # (addon, enterprise modules it now depends on directly)
    }
    for name in sorted(new):
        added = set(new[name].get('enterprise') or []) - set(old.get(name, {}).get('enterprise') or [])
        if added and (name not in old or old[name].get('enterprise') is not None):
            diff['enterprise_added'].append((name, sorted(added)))
    for name in sorted(old.keys() & new.keys()):
        before, after = old[name], new[name]
        old_cov, new_cov = before.get('coverage'), after.get('coverage')
        both_measured = old_cov is not None and new_cov is not None
        comparable = both_measured and measured_the_same_way(before, after)
        if both_measured and not comparable:
            diff['not_comparable'].append(name)
        if comparable and old_cov != NOT_INSTALLABLE and new_cov == NOT_INSTALLABLE:
            diff['could_to_cant'].append(name)
        elif comparable and old_cov == NOT_INSTALLABLE and new_cov != NOT_INSTALLABLE:
            diff['cant_to_could'].append(name)
        else:
            loc_change = after.get('loc', 0) - before.get('loc', 0)
            measured = comparable and all(isinstance(c, int) and c >= 0 for c in (old_cov, new_cov))
            cov_change = new_cov - old_cov if measured else None
            if loc_change or cov_change:
                diff['metrics_changed'].append((name, loc_change, cov_change, old_cov, new_cov))
        if comparable:
            old_failed, new_failed = before.get('failed') or [], after.get('failed') or []
            if newly := [t for t in new_failed if t not in old_failed]:
                diff['newly_failing'].append((name, newly))
            if fixed := [t for t in old_failed if t not in new_failed]:
                diff['fixed'].append((name, fixed))
        old_deps, new_deps = set(before.get('depends') or []), set(after.get('depends') or [])
        if old_deps != new_deps:
            diff['deps_changed'].append((name, sorted(new_deps - old_deps), sorted(old_deps - new_deps)))
    return diff


# ---------- history ----------

def load_history(history_file: Path) -> list[dict]:
    runs = []
    try:
        lines = history_file.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return runs
    for line in lines:
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # a line edited by hand: ignored
    return runs


def parse_date(text) -> Optional[datetime]:
    """A date of the history ('2026-11-01' or '2026-11-01T10:28:14'), without time zone; None if not a date."""
    try:
        return datetime.fromisoformat(str(text)).replace(tzinfo=None)
    except ValueError:
        return None


def runs_and_events(history: list[dict]) -> tuple[list[dict], list[dict]]:
    """The measured runs, and the events added by hand ({"date": ..., "event": "what happened"}, a valid date)."""
    runs = [line for line in history if not line.get('event')]
    events = [line for line in history if line.get('event') and parse_date(line.get('date'))]
    return runs, events


def series_of(run: dict) -> str:
    """The method of a measured run: it is compared with the runs measured the same way only, whatever their Odoo
    version (the user's choice, 2026-11-21: the coverage on Odoo 17 and on 19 compare). 'isolated' and 'both' share
    their method: both take the coverage of each addon alone."""
    return 'whole' if run.get('mode') == 'whole' else 'one by one'


def series_label(method: str, version: Optional[int] = None) -> str:
    """'whole', 'one by one (isolated, both)'; with a version: the runs on that Odoo version only."""
    return ("whole" if method == 'whole' else "one by one (isolated, both)") + (f" · Odoo {version} only" if version else "")


def append_history(history_file: Path, run: dict):
    with open(history_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(run) + "\n")


# ---------- dependency graph ----------

def duplicate_links(dependencies: dict) -> set[tuple[str, str]]:
    """The links A -> C that the graph already shows through another path, e.g. A -> B -> C: drawing them adds
    nothing. Only the links drawn count (between custom addons, and from them to standard modules): a path
    through the dependencies of the standard modules, which are not drawn, would not show A -> C."""
    links = {name: set(deps) for name, deps in dependencies.items()}

    def reaches(start: str, target: str) -> bool:
        todo, seen = [start], set()
        while todo:
            module = todo.pop()
            for dep in links.get(module, ()):
                if dep == target:
                    return True
                if dep not in seen:
                    seen.add(dep)
                    todo.append(dep)
        return False

    duplicates = set()
    for name in sorted(links):
        for dep in sorted(links[name]):
            links[name].discard(dep)
            if reaches(name, dep):  # still reached without this link: it is a duplicate, it stays out
                duplicates.add((name, dep))
            else:
                links[name].add(dep)
    return duplicates


def build_dot(loc: dict, dependencies: dict, with_standard: bool = False, duplicates: set = frozenset(),
              show_duplicates: bool = False) -> str:
    """Graphviz description of the dependencies between the custom addons (node size: lines of code),
    plus, with_standard, the standard modules they directly depend on. Colors are set by the report page.
    The `duplicates` links (see duplicate_links) are left out, or drawn dotted with show_duplicates."""
    lines = ["digraph dependencies {", "  rankdir=LR;", '  bgcolor="transparent";',
             '  node [shape=ellipse, fontname="Helvetica", fontsize=11];', "  edge [arrowsize=0.7];"]
    max_loc = max(loc.values(), default=0)
    for name in sorted(dependencies):
        addon_loc = loc.get(name, 0)
        width = 0.6 + math.sqrt(addon_loc) / math.sqrt(max_loc) * (3.0 - 0.6) if max_loc > 0 and addon_loc > 0 else 0.8
        lines.append(f'  "{name}" [width={width:.2f}, height={max(0.35, width * 0.45):.2f}, fixedsize=true];')
    standard = sorted({dep for deps in dependencies.values() for dep in deps if dep not in dependencies})
    if with_standard:
        lines.extend(f'  "{name}" [shape=box, height=0.3];' for name in standard)
    for name, deps in dependencies.items():
        for dep in deps:
            if (dep in dependencies or with_standard) and (show_duplicates or (name, dep) not in duplicates):
                lines.append(f'  "{name}" -> "{dep}"' + (' [style=dotted];' if (name, dep) in duplicates else ';'))
    lines.append('}')
    return "\n".join(lines)


def render_svg(dot_text: str) -> Optional[str]:
    """Layout computed by graphviz, as an <svg> element. None if graphviz is not installed."""
    dot = shutil.which('dot')
    if not dot:
        return None
    res = subprocess.run([dot, '-Tsvg'], input=dot_text, capture_output=True, text=True, encoding='utf-8')
    if res.returncode != 0 or '<svg' not in res.stdout:
        typer.echo(f"⚠️ graphviz failed to draw the dependency graph: {res.stderr.strip()[:200]}")
        return None
    return res.stdout[res.stdout.index('<svg'):]


# ---------- orchestration ----------

def load_standard_tests(out_dir: Path) -> Optional[dict]:
    """The results of the last run of the standard modules' tests: {date, modules: {module: {tests, failed}}}."""
    try:
        return json.loads((out_dir / STATE_FILE).read_text(encoding='utf-8')).get('standard_tests')
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_reports(loc: dict, dependencies: dict, results: dict[str, Optional[Result]], out_dir: Path,
                  extra: Optional[dict] = None, run: Optional[dict] = None, standard: Optional[dict] = None,
                  measures: Optional[dict] = None, trends: Optional[dict] = None, reviewed: Optional[dict] = None):
    """`run` describes a coverage run ({'mode', 'profile'}) when addons were measured now: it is added to the history.
    `standard`: the results of the standard modules' tests run now; else those of the last time they were run.
    `measures`: the counts of [[report.measures]] of config.toml ({label: count}), recorded with the run.
    `trends`: [report] trends / trends_open of config.toml, the charts shown and unfolded (none: the default).
    `reviewed`: the security findings found fine ({id: reason}, reviewed.toml)."""
    reviewed = reviewed or {}
    out_dir.mkdir(parents=True, exist_ok=True)
    old, old_date = load_state(out_dir)
    standard_tests = load_standard_tests(out_dir)
    new = build_state(loc, dependencies, results, extra)
    now = datetime.now().isoformat(timespec='seconds')
    if standard is not None:
        standard_tests = {'date': now, 'modules': standard}
    summary = summarize(new)
    if run:
        append_history(out_dir / HISTORY_FILE, {'date': now, **run, **summary, **({'measures': measures} if measures else {})})

    typer.echo("🧭 Drawing the dependency graph...")
    # each picture has its own layout: without the duplicate links, it is drawn as if they did not exist
    duplicates = duplicate_links(dependencies)
    graphs = {f"{view}{'_dup' if shown else ''}": render_svg(build_dot(loc, dependencies, view == 'all', duplicates, shown))
              for view in ('custom', 'all') for shown in (False, True)}
    graphs['duplicates'] = sorted(f"{name}->{dep}" for name, dep in duplicates)
    if not graphs['custom']:
        typer.echo("⚠️ graphviz 'dot' not found: report generated without the dependency graph (sudo apt install graphviz)")

    html = report_html.render(new, summary, summarize(old) if old else None, compute_diff(old, new) if old else None,
                              old_date, load_history(out_dir / HISTORY_FILE), graphs,
                              {'generated_at': now, **(run or {}), 'standard_tests': standard_tests,
                               'measures': list(measures or {}), 'trends': trends or {}, 'reviewed': reviewed,
                               'security_before': security_ids(old)})
    (out_dir / REPORT_FILE).write_text(html, encoding='utf-8')
    (out_dir / STATE_FILE).write_text(json.dumps({'schema': SCHEMA, 'generated_at': now, 'addons': new,
                                                  'standard_tests': standard_tests}, indent=1), encoding='utf-8')
    coverage = f"{summary['coverage']:.0f}% coverage" if summary['coverage'] is not None else "coverage not measured"
    typer.echo(f"📦 {summary['addons']} addons, {summary['loc']:,} lines, {coverage}, "
               f"{summary['tests'] - summary['failed']}/{summary['tests']} tests passed, {summary['not_installable']} not installable")
    if measures:
        typer.echo("📏 " + ", ".join(f"{label}: {count:,}" for label, count in measures.items())
                   + ("" if run else " (the trends record them with the runs that measure the coverage, -c)"))
    found = security_ids(new)
    if found is not None:
        to_review, before = found - reviewed.keys(), security_ids(old)
        fresh = len(to_review - before) if before is not None else 0
        typer.echo(f"🔒 Security review: {len(to_review) or 'nothing'} to review" + (f", {fresh} new" if fresh else "")
                   + (f" ({len(found) - len(to_review)} reviewed)" if len(found) > len(to_review) else ""))
    typer.echo(f"✅ Report: {out_dir / REPORT_FILE}")
