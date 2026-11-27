"""Data of the codebase report: state of each addon, totals, changes since the previous report, the recorded
commits (the trend) and layout of the dependency graph. The page itself is rendered by report_html.

Files written in the analysis folder:
- report.html   the report (the only file to open)
- state.json    last state of each addon; also the cache of the coverage measures
- runs/         one file per commit recorded by 'jsa backfill' (<commit>.json): the data of the trend. Only the
                backfill writes there (the user, 2026-11-27: recording is done on purpose); delete a file to forget
                a commit. The events of the trend are in backfill.toml.
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

from ..core import config
from . import report_html, security
from .coverage import NOT_INSTALLABLE, Result

REPORT_FILE = "report.html"
STATE_FILE = "state.json"
RUNS_FOLDER = "runs"  # one file per commit recorded by jsa backfill
SCHEMA = 2  # version of the measures stored in the state: older ones are measured again


# ---------- state ----------

def build_state(loc: dict, dependencies: dict, results: dict[str, Optional[Result]], extra: Optional[dict] = None) -> dict:
    """{addon: {loc, depends, coverage (None: not measured), tests, failed, ... (per module), + extra (fingerprint,
    whole: the same measures with all the addons together, ...)}}"""
    state = {}
    for name in sorted(loc):
        result = results.get(name)
        state[name] = {'loc': int(loc[name] or 0), 'depends': list(dependencies.get(name, [])),
                       **(asdict(result) if result else {'coverage': None}), **(extra or {}).get(name, {})}
    return state


def load_state(out_dir: Path, key: str = 'addons') -> tuple[dict, Optional[str]]:
    """Return (state, date of the run) of the previous run, ({}, None) if there is none. key='baseline': the state
    that report compared itself with (see write_reports)."""
    try:
        raw = json.loads((out_dir / STATE_FILE).read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}, None
    return raw.get(key) or {}, raw.get('generated_at' if key == 'addons' else f'{key}_date')


def status(entry: dict) -> str:
    """Of the measure per module; an addon whose tests fail with all the addons (whole), or that cannot be installed
    with them, is failing."""
    coverage, whole = entry.get('coverage'), entry.get('whole') or {}
    if coverage is None:
        return 'not_measured'
    if coverage == NOT_INSTALLABLE:
        return 'not_installable'
    if entry.get('failed') or whole.get('failed') or whole.get('coverage') == NOT_INSTALLABLE:
        return 'failing'
    return 'ok' if entry.get('tests') else 'no_tests'


def security_ids(state: dict) -> Optional[dict[str, str]]:
    """{id: rule} of the security findings of a state; None when it has no security review (made before jsa had it,
    or every rule switched off)."""
    if not any('security' in e for e in state.values()):
        return None
    return {f['id']: f['rule'] for e in state.values() for f in e.get('security') or []}


def summarize(state: dict) -> dict:
    """Totals of a state. The coverage is weighted by the size of the addons (covered / measured statements).
    Per module, and with all the addons together: coverage_whole, failed_whole, not_installable_whole (None when the
    state has no whole measure).
    'security': the ids of the findings of each rule (the chart counts those not reviewed: reviewing one lowers the
    whole curve), None when the state has none."""
    statuses = [status(e) for e in state.values()]
    scanned = [e for e in state.values() if 'security' in e]
    statements = sum(e.get('statements') or 0 for e in state.values())
    covered = sum(e.get('covered') or 0 for e in state.values())
    wholes = [e['whole'] for e in state.values() if e.get('whole')]
    whole_statements = sum(w.get('statements') or 0 for w in wholes)
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
        'coverage_whole': (round(100 * sum(w.get('covered') or 0 for w in wholes) / whole_statements, 1)
                           if whole_statements else None),
        'failed_whole': sum(len(w.get('failed') or []) for w in wholes) if wholes else None,
        'not_installable_whole': sum(1 for w in wholes if w.get('coverage') == NOT_INSTALLABLE) if wholes else None,
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
    """Whether two measures of an addon compare: taken with the same rules (coverage.RULES), whatever the Odoo
    version (the user's choice, 2026-11-21: the coverage on Odoo 17 and on 19 compare)."""
    return before.get('coverage_rules') == after.get('coverage_rules')


def compute_diff(old: dict, new: dict) -> dict:
    """What changed between two states. The measures of an addon (installable, coverage, tests) are compared only
    when both were taken the same way (measured_the_same_way); the lines of code and dependencies always are."""
    diff = {
        'added': sorted(new.keys() - old.keys()),
        'removed': sorted(old.keys() - new.keys()),
        'not_comparable': [],  # addons measured by another version of jsa: their measures are not compared
        'could_to_cant': [], 'cant_to_could': [],
        'newly_failing': [],  # (addon, tests failing now but not before)
        'newly_failing_whole': [],  # (addon, tests failing with all the addons now, not before, and passing alone)
        'whole_could_to_cant': [],  # addons installable alone that cannot be installed with all the addons anymore
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
            old_whole, new_whole = before.get('whole') or {}, after.get('whole') or {}
            if old_whole and new_whole and new_cov != NOT_INSTALLABLE:
                if old_whole.get('coverage') != NOT_INSTALLABLE and new_whole.get('coverage') == NOT_INSTALLABLE:
                    diff['whole_could_to_cant'].append(name)
                if newly := [t for t in new_whole.get('failed') or []
                             if t not in (old_whole.get('failed') or []) and t not in new_failed]:
                    diff['newly_failing_whole'].append((name, newly))
        old_deps, new_deps = set(before.get('depends') or []), set(after.get('depends') or [])
        if old_deps != new_deps:
            diff['deps_changed'].append((name, sorted(new_deps - old_deps), sorted(old_deps - new_deps)))
    return diff


# ---------- the recorded commits ----------

def parse_date(text) -> Optional[datetime]:
    """A date ('2026-11-01' or '2026-11-01T10:28:14'), without time zone; None if not a date."""
    try:
        return datetime.fromisoformat(str(text)).replace(tzinfo=None)
    except ValueError:
        return None


def runs_folder(out_dir: Path) -> Path:
    return out_dir / RUNS_FOLDER


def load_runs(out_dir: Path) -> list[dict]:
    """The commits recorded by jsa backfill, in the order of their code: {commit, code_date (its date), date (when it
    was measured), profile, odoo, the totals of summarize, measures, event?}. Only what the trend draws: a few KB."""
    runs = []
    for path in sorted(runs_folder(out_dir).glob("*.json")):
        try:
            run = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(run, dict) and run.get('commit'):
            runs.append(run)
    return sorted(runs, key=lambda r: parse_date(r.get('code_date')) or datetime.min)


def save_run(out_dir: Path, run: dict):
    runs_folder(out_dir).mkdir(parents=True, exist_ok=True)
    (runs_folder(out_dir) / f"{run['commit'][:12]}.json").write_text(json.dumps(run, indent=1), encoding='utf-8')


def forget_run(out_dir: Path, commit: str):
    (runs_folder(out_dir) / f"{commit[:12]}.json").unlink(missing_ok=True)


def plan_events(path: Path) -> list[dict]:
    """The [[events]] of backfill.toml with a valid date ({date, event}): drawn on the trend. A file that cannot be
    read gives none here (jsa backfill says why)."""
    try:
        plan = config.tomllib.loads(path.read_text(encoding='utf-8'))
    except (OSError, config.tomllib.TOMLDecodeError):
        return []
    return [{'date': str(e['date']), 'event': e['event']} for e in plan.get('events', [])
            if isinstance(e, dict) and isinstance(e.get('event'), str) and parse_date(e.get('date'))]


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


def load_mutation(out_dir: Path) -> dict[str, dict]:
    """{addon: the results of its last 'jsa mutation'} (out_dir/mutation/<addon>.json)."""
    found = {}
    for path in sorted((out_dir / "mutation").glob("*.json")):
        try:
            result = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict) and result.get('addon'):
            found[result['addon']] = result
    return found


# ---------- orchestration ----------

def load_standard_tests(out_dir: Path) -> Optional[dict]:
    """The results of the last run of the standard modules' tests: {date, modules: {module: {tests, failed}}}."""
    try:
        return json.loads((out_dir / STATE_FILE).read_text(encoding='utf-8')).get('standard_tests')
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_reports(loc: dict, dependencies: dict, results: dict[str, Optional[Result]], out_dir: Path,
                  extra: Optional[dict] = None, run: Optional[dict] = None, standard: Optional[dict] = None,
                  measures: Optional[dict] = None, trends: Optional[dict] = None, reviewed: Optional[dict] = None,
                  reviewed_changes: Optional[dict] = None, security_off: Optional[list] = None,
                  branch: Optional[dict] = None) -> Optional[dict]:
    """`run` describes a coverage run ({'profile', 'odoo', 'code_date'}) when addons were measured now: its record
    (what jsa backfill keeps of a commit: the totals, measures...) is returned, None when nothing was measured.
    `branch`: the git branch of the code (codebase.branch_of): the trend shows the recorded commits of that branch.
    `standard`: the results of the standard modules' tests run now; else those of the last time they were run.
    `measures`: the counts of [[report.measures]] of config.toml ({label: count}), recorded with the run.
    `trends`: [report] trends / trends_open of config.toml, the charts shown and unfolded (none: the default).
    `reviewed`: the security findings found fine ({id: reason}, reviewed.toml). `reviewed_changes`: the same for the
    changes of 'jsa mutation' that no test notices ([[mutation]]), whose results are read in out_dir/mutation/.
    `security_off`: the security rules switched off in config.toml ([security] skip)."""
    reviewed, security_off = reviewed or {}, security_off or []
    out_dir.mkdir(parents=True, exist_ok=True)
    old, old_date = load_state(out_dir)
    standard_tests = load_standard_tests(out_dir)
    new = build_state(loc, dependencies, results, extra)
    # what is new is said against the previous report; when nothing changed since, against the report it compared
    # itself with: a report made again without measuring (jsa mutation, without -c) keeps showing what changed
    if old and json.loads(json.dumps(new)) == old:
        base, base_date = load_state(out_dir, 'baseline')
        old, old_date = (base, base_date) if base else (old, old_date)
    before = security_ids(old)  # a rule switched off since: its findings are not "gone"
    before = None if before is None else {i: rule for i, rule in before.items() if rule not in security_off}
    now = datetime.now().isoformat(timespec='seconds')
    if standard is not None:
        standard_tests = {'date': now, 'modules': standard}
    summary = summarize(new)
    record = {'date': now, **run, **summary, **({'measures': measures} if measures else {})} if run else None
    # the trend: the commits recorded by jsa backfill in the history of the branch (those of other branches hidden)
    runs = load_runs(out_dir)
    shown = [r for r in runs if not branch or r['commit'] in branch['commits']]
    events = ([{'date': r['code_date'], 'event': r['event']} for r in shown if r.get('event') and r.get('code_date')]
              + plan_events(config.backfill_file()))

    typer.echo("🧭 Drawing the dependency graph...")
    # each picture has its own layout: without the duplicate links, it is drawn as if they did not exist
    duplicates = duplicate_links(dependencies)
    graphs = {f"{view}{'_dup' if shown else ''}": render_svg(build_dot(loc, dependencies, view == 'all', duplicates, shown))
              for view in ('custom', 'all') for shown in (False, True)}
    graphs['duplicates'] = sorted(f"{name}->{dep}" for name, dep in duplicates)
    if not graphs['custom']:
        typer.echo("⚠️ graphviz 'dot' not found: report generated without the dependency graph (sudo apt install graphviz)")

    html = report_html.render(new, summary, summarize(old) if old else None, compute_diff(old, new) if old else None,
                              old_date, shown, events, graphs,
                              {'generated_at': now, **(run or {}), 'measured': bool(run), 'standard_tests': standard_tests,
                               'branch': {k: v for k, v in (branch or {}).items() if k != 'commits'} or None,
                               'hidden_runs': len(runs) - len(shown),
                               'measures': list(measures or {}), 'trends': trends or {}, 'reviewed': reviewed,
                               'security_before': before, 'mutation': load_mutation(out_dir),
                               'reviewed_changes': reviewed_changes or {}, 'security_off': security_off})
    (out_dir / REPORT_FILE).write_text(html, encoding='utf-8')
    (out_dir / STATE_FILE).write_text(json.dumps({'schema': SCHEMA, 'generated_at': now, 'addons': new,
                                                  'standard_tests': standard_tests, 'baseline': old,
                                                  'baseline_date': old_date}, indent=1), encoding='utf-8')
    whole = lambda key, text: f" ({text.format(summary[key])} whole)" if summary.get(key) is not None else ""
    coverage = (f"{summary['coverage']:.0f}% coverage per module" + whole('coverage_whole', "{:.0f}%")
                if summary['coverage'] is not None else "coverage not measured")
    typer.echo(f"📦 {summary['addons']} addons, {summary['loc']:,} lines, {coverage}, "
               f"{summary['tests'] - summary['failed']}/{summary['tests']} tests passed" + whole('failed_whole', "{} failed")
               + f", {summary['not_installable']} not installable" + whole('not_installable_whole', "{}"))
    if measures:
        typer.echo("📏 " + ", ".join(f"{label}: {count:,}" for label, count in measures.items()))
    found = security_ids(new)
    if found is not None:
        to_review = found.keys() - reviewed.keys()
        fresh = len(to_review - before.keys()) if before is not None else 0
        gone = len(before.keys() - found.keys() - reviewed.keys()) if before is not None else 0
        change = ", ".join(part for part in (f"+{fresh} new" if fresh else "", f"-{gone} gone" if gone else "") if part)
        typer.echo(f"🔒 Security review: {len(to_review) or 'nothing'} to review" + (f" ({change})" if change else "")
                   + (f", {len(found) - len(to_review)} reviewed" if len(found) > len(to_review) else ""))
    typer.echo(f"✅ Report: {out_dir / REPORT_FILE}")
    return record
