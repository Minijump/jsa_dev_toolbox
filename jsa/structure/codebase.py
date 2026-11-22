"""structure: dependency graph, lines of code and optional test coverage of the custom addons."""
import fnmatch
import hashlib
import re
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional

import typer

from ..core import config, odoo, ui
from ..core.addons import DependencyGraph, files_digest, find_addons, read_manifests, standard_depends
from . import coverage, report, report_html

LOC_EXTENSIONS = {'.py', '.xml', '.js', '.scss', '.css', '.csv'}
LOC_EXCLUDED_DIRS = {'migrations', 'tests', 'i18n'}
MEASURE_KEYS = ('label', 'pattern', 'files')
TEST_TIMEOUT = 10  # minutes a test (or a setUpClass) may take in a coverage run, unless [codebase] test_timeout


class Mode(str, Enum):
    whole = "whole"
    isolated = "isolated"
    both = "both"


def code_files(addon: Path) -> Iterator[Path]:
    """The files counted as code: no tests, translations, migrations."""
    for f in addon.rglob('*'):
        if f.is_file() and f.suffix.lower() in LOC_EXTENSIONS \
                and not any(part in LOC_EXCLUDED_DIRS for part in f.relative_to(addon).parts):
            yield f


def count_loc(addon: Path) -> int:
    """Lines of code of an addon, without tests, translations and migrations."""
    loc = 0
    for f in code_files(addon):
        text = f.read_text(encoding='utf-8', errors='ignore')
        if text:
            loc += text.count('\n') + 1
    return loc


# ---------- measures of config.toml: a pattern counted in the code, drawn in the trends ----------

@dataclass
class Measure:
    label: str
    regex: re.Pattern
    files: tuple[str, ...] = ()  # file name patterns ('*.py'); empty: every code file


def measures_from_config(conf: dict) -> list[Measure]:
    """[[report.measures]] of config.toml. A mistake stops the command before anything is measured."""
    def refuse(number: int, why: str):
        typer.echo(f"❌ config.toml: [[report.measures]] #{number} {why}")
        raise typer.Exit(code=1)

    measures = []
    for number, item in enumerate(conf.get('report', {}).get('measures', []), 1):
        if not isinstance(item, dict):
            refuse(number, "must be a table: label = \"...\", pattern = \"...\"")
        unknown = sorted(set(item) - set(MEASURE_KEYS))
        if unknown:
            refuse(number, f"has the key(s) {', '.join(unknown)}, which do not exist (misspelled?): use {', '.join(MEASURE_KEYS)}")
        label, pattern, files = item.get('label'), item.get('pattern'), item.get('files', [])
        if not isinstance(label, str) or not label.strip() or not isinstance(pattern, str) or not pattern:
            refuse(number, "needs a label and a pattern (a regular expression)")
        if label in (m.label for m in measures):
            refuse(number, f"has the label '{label}' of another measure")
        if label in (key for key, *_ in report_html.TREND_CHARTS):
            refuse(number, f"has the label '{label}', the name of a chart of jsa: choose another one")
        files = [files] if isinstance(files, str) else files
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            refuse(number, "files must be a list of file name patterns, e.g. [\"*.py\", \"*.xml\"]")
        try:
            regex = re.compile(pattern, re.MULTILINE)
        except re.error as e:
            refuse(number, f"('{label}'): the pattern is not a valid regular expression: {e}")
        measures.append(Measure(label.strip(), regex, tuple(files)))
    return measures


REPORT_KEYS = ('trends', 'trends_open', 'measures')


def trends_from_config(conf: dict, measures: list[Measure]) -> dict:
    """[report] trends (the charts shown, in this order) and trends_open (unfolded at first) of config.toml; the keys
    left out keep their default (every chart; coverage and lines of code unfolded). A mistake stops the command."""
    def refuse(why: str):
        typer.echo(f"❌ config.toml: [report] {why}")
        raise typer.Exit(code=1)

    section = conf.get('report', {})
    if not isinstance(section, dict):
        refuse("must be a table")
    unknown = sorted(set(section) - set(REPORT_KEYS))
    if unknown:
        refuse(f"has the key(s) {', '.join(unknown)}, which do not exist (misspelled?): use {', '.join(REPORT_KEYS)}")
    names = [key for key, *_ in report_html.TREND_CHARTS] + [m.label for m in measures]
    choice = {}
    for key in ('trends', 'trends_open'):
        if key not in section:
            continue
        value = section[key]
        if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
            refuse(f"{key} must be a list of chart names, e.g. [\"coverage\", \"loc\"]")
        wrong = [name for name in value if name not in names]
        if wrong:
            refuse(f"{key}: no chart named {', '.join(repr(n) for n in wrong)}. The charts: {', '.join(names)}")
        choice[key] = value
    hidden = [name for name in choice.get('trends_open', []) if 'trends' in choice and name not in choice['trends']]
    if hidden:
        refuse(f"trends_open lists {', '.join(repr(n) for n in hidden)}, which trends does not show")
    return choice


def count_measures(addons: dict[str, Path], measures: list[Measure]) -> dict[str, int]:
    """{label: matches of its pattern in the code files of all the addons}."""
    counts = dict.fromkeys((m.label for m in measures), 0)
    for addon in addons.values() if measures else ():
        for f in code_files(addon):
            wanted = [m for m in measures if not m.files or any(fnmatch.fnmatch(f.name, p) for p in m.files)]
            if wanted:
                text = f.read_text(encoding='utf-8', errors='ignore')
                for m in wanted:
                    counts[m.label] += len(m.regex.findall(text))
    return counts


def code_date(roots: list[Path]) -> Optional[str]:
    """When the measured code was written: the last commit changing the addons folders, so that measuring an old
    commit places it in the past of the trends. None (the run is placed at its own date) when a folder is not in git
    or has uncommitted changes (of its tracked files)."""
    dates = []
    for root in roots:
        try:
            # changes of the tracked files only: Python's __pycache__ is not a change of the code
            status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no", "--", "."],
                                    capture_output=True, text=True, timeout=60)
            log = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%ct", "--", "."],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if status.returncode or status.stdout.strip() or log.returncode or not log.stdout.strip().isdigit():
            return None
        dates.append(datetime.fromtimestamp(int(log.stdout.strip())))  # local time, like the date of the runs
    return max(dates).isoformat(timespec='seconds') if dates else None


def enterprise_modules(conf: dict) -> Optional[set[str]]:
    """Names of the Odoo enterprise modules: the addons of the `enterprise` folder of the profiles.
    None when no profile has an (existing) enterprise folder: enterprise dependencies cannot be flagged."""
    folders = [Path(p['enterprise']).expanduser() for p in conf.get('profiles', {}).values()
               if isinstance(p, dict) and p.get('enterprise')]
    folders = [f for f in folders if f.is_dir()]
    return {addon.name for addon in find_addons(folders)} if folders else None


# ---------- only re-run what changed ----------


def fingerprints(addons: dict[str, Path], graph: DependencyGraph) -> dict[str, str]:
    """Changes when the addon or one of its custom dependencies changes."""
    own = {name: files_digest(path) for name, path in addons.items()}
    return {name: hashlib.sha256("".join(own[d] for d in sorted(graph.closure([name]) & own.keys())).encode()).hexdigest()
            for name in addons}


def reusable(previous: dict, key: dict) -> bool:
    """A previous measure can be reused if it was made on the same code, with the same profile and mode."""
    return previous.get('coverage') is not None and all(previous.get(k) == v for k, v in key.items())


# ---------- command ----------


def structure(
    coverage_enabled: bool = typer.Option(False, "--coverage", "-c", help="Measure the test coverage of each addon"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Profile key from config.toml [profiles] (required with --coverage)"),
    mode: Mode = typer.Option(Mode.whole, "--mode", "-m", help="whole: all addons installed and tested together (fast). "
                              "isolated: each addon on a database with only its dependencies (catches missing dependencies). "
                              "both: the coverage of isolated, then all addons tested together (catches what breaks only with the others)"),
    standard_tests: bool = typer.Option(False, "--standard-tests", help="With -c in mode whole or both: also run the tests of the "
                                        "standard modules your addons need, with your addons installed, like odoo.sh (long)"),
    measure_all: bool = typer.Option(False, "--all", "-a", help="Measure every addon, even those unchanged since the last measure"),
    open_it: bool = typer.Option(False, "--open", "-o", help="Open the report in the browser when it is ready"),
):
    """Generate the structure of the custom codebase (addons folders from config.toml).

    With --coverage / -c, also run the tests of the addons under coverage (creates and drops temporary databases).
    Addons unchanged since the last measure (same code, same Odoo commit, same mode) keep their previous result; in
    mode whole, any change runs everything again.
    """
    conf = config.load_config()
    measures = measures_from_config(conf)
    trends = trends_from_config(conf, measures)
    test_timeout(conf, warn=True)  # checked before anything runs
    roots = config.addons_roots(conf)
    missing = [str(r) for r in roots if not r.is_dir()]
    if not roots or missing:
        typer.echo(f"❌ Set [codebase] addons_roots in {config.CONFIG_FILE} to existing folders"
                   + (f" (not found: {', '.join(missing)})" if missing else ""))
        raise typer.Exit(code=1)
    coverage_profile = None
    if coverage_enabled:
        if not profile:
            typer.echo("❌ --profile/-p is required when --coverage/-c is enabled.")
            raise typer.Exit(code=1)
        coverage_profile = coverage_profile_of(profile, conf)
    if standard_tests and (not coverage_enabled or mode == Mode.isolated):
        typer.echo("❌ --standard-tests needs --coverage/-c, in mode whole or both.")
        raise typer.Exit(code=1)
    analyse(conf, roots, config.ANALYSIS_DIR, profile, coverage_profile, mode,
            measure_all or standard_tests, standard_tests, measures, trends)  # --standard-tests: a full run is asked for
    if open_it:
        open_report()
    else:
        typer.echo("   Open it with: jsa report")


def test_timeout(conf: dict, warn: bool = False) -> float:
    """[codebase] test_timeout of config.toml: the minutes a test, or the setUpClass of a test class, may take in a
    coverage run before it is stopped and fails (default TEST_TIMEOUT; see odoo.install). `warn`: say when the key
    is in another table, where it is not read (in TOML, a key written after a [table] belongs to that table)."""
    if warn:
        elsewhere = [path for path in _tables_with(conf, 'test_timeout') if path != 'codebase']
        if elsewhere:
            typer.echo(f"⚠️ config.toml: test_timeout is in {', '.join(f'[{p}]' if p else 'no table' for p in elsewhere)}, "
                       "where it is not read: put it under [codebase] (a key written after a [table] belongs to it)")
    value = conf.get('codebase', {}).get('test_timeout', TEST_TIMEOUT)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        typer.echo("❌ config.toml: [codebase] test_timeout is the minutes a test may take, e.g. test_timeout = 10")
        raise typer.Exit(code=1)
    return float(value)


def _tables_with(table: dict, key: str, path: str = "") -> list[str]:
    """The tables of config.toml holding `key` ('' for the top of the file, 'profiles.19' for a profile)."""
    found = [path] if key in table else []
    for name, value in table.items():
        if isinstance(value, dict):
            found += _tables_with(value, key, f"{path}.{name}" if path else name)
    return found


def coverage_profile_of(name: str, conf: dict) -> dict:
    """The profile that runs the tests under coverage. Stops when 'coverage' is not installed in its python."""
    profile = config.load_profile(name, conf)
    found = subprocess.run([profile['python'], "-c", "import coverage"], capture_output=True)
    if found.returncode != 0:
        typer.echo(f"❌ 'coverage' is not installed in the python of profile '{name}' ({profile['python']}): "
                   f"install it there with: {profile['python']} -m pip install coverage")
        raise typer.Exit(code=1)
    return profile


def analyse(conf: dict, roots: list[Path], out_dir: Path, profile: Optional[str] = None,
            coverage_profile: Optional[dict] = None, mode: Mode = Mode.whole, measure_all: bool = False,
            standard_tests: bool = False, measures: list[Measure] = (), trends: Optional[dict] = None,
            all_or_nothing: bool = False) -> Optional[dict]:
    """What 'jsa structure' does: the report of the addons of `roots`, written in `out_dir` (state.json, history.jsonl,
    report.html), with their tests run under coverage when `coverage_profile` (named `profile`) is given.
    `all_or_nothing` (mode whole): an addon that cannot install makes them all not installable, as in production,
    instead of being left out and the others tried again (jsa backfill).
    Returns the run added to the history, None when nothing was measured."""
    written = code_date(roots)  # before Odoo runs
    found = find_addons(roots)
    manifests = read_manifests(found)
    addons = {a.name: a for a in found if a.name in manifests}  # a manifest that cannot be read: left out, said
    dependencies = {name: m.get('depends', []) for name, m in manifests.items()}
    loc = {name: count_loc(path) for name, path in addons.items()}
    std_depends = standard_depends(coverage_profile) if coverage_profile else {}
    graph = DependencyGraph({**std_depends, **dependencies}, custom=addons)
    prints = fingerprints(addons, graph)

    previous, _ = report.load_state(out_dir)
    state_extra = {name: {'fingerprint': prints[name], 'schema': report.SCHEMA} for name in addons}
    results: dict[str, Optional[coverage.Result]] = {}
    run = None  # set when addons are measured now
    standard = None  # results of the tests of the standard modules, when run now
    if not coverage_profile:
        # no measure: keep the previous one for the unchanged addons
        for name in addons:
            prev = previous.get(name, {})
            if reusable(prev, state_extra[name]):
                results[name] = coverage.Result.from_state(prev)
                state_extra[name].update({k: prev.get(k) for k in ('coverage_mode', 'profile', 'standard_version',
                                                                     'coverage_rules', 'odoo')})
            else:
                results[name] = None
    else:
        key = {'coverage_mode': mode.value, 'profile': profile, 'standard_version': odoo.source_version(coverage_profile),
               'coverage_rules': coverage.RULES}
        odoo_version = odoo.major_version(coverage_profile)
        for name in addons:
            # 'odoo' is kept out of the key: a reused measure was taken on the same Odoo (same standard_version)
            state_extra[name].update(key, odoo=odoo_version)
        installable = {name: path for name, path in addons.items() if manifests[name].get('installable', True)}
        for name in addons.keys() - installable.keys():
            results[name] = coverage.Result(coverage.NOT_INSTALLABLE, note="marked as not installable")
        unchanged = [name for name in installable if reusable(previous.get(name, {}), state_extra[name])]
        # isolated (and the measures alone of both): each unchanged addon keeps its result. whole: addons interact,
        # any change means a full run
        keep = [] if measure_all else (unchanged if mode != Mode.whole or len(unchanged) == len(installable) else [])
        for name in keep:
            results[name] = coverage.Result.from_state(previous[name])
        if keep:
            typer.echo(f"♻️ {len(keep)} unchanged addon(s) keep their previous measure (use --all to measure everything)")
        to_measure = {name: path for name, path in installable.items() if name not in keep}
        if to_measure:
            log_file = out_dir / "coverage_odoo.log"
            typer.echo(f"📊 Measuring the coverage of {len(to_measure)} addon(s), mode '{mode.value}', each test and "
                       f"setUpClass {test_timeout(conf):g} min at most (Odoo log: {log_file})")
            measured, standard = coverage.measure(to_measure, graph, coverage_profile, Mode.isolated.value if mode == Mode.both
                                                  else mode.value, log_file, standard_tests=standard_tests,
                                                  time_limit=test_timeout(conf),
                                                  all_or_nothing=all_or_nothing and mode == Mode.whole)
            results.update(measured)
            if mode == Mode.both:
                alone = {name: coverage.alone_part(results[name]) for name in installable}
                together = {name: path for name, path in installable.items() if alone[name].coverage != coverage.NOT_INSTALLABLE}
                typer.echo(f"🧩 Then the {len(together)} addons installed and tested together: what breaks only with the others")
                found, standard = coverage.measure(together, graph, coverage_profile, Mode.whole.value, log_file,
                                                   measure_coverage=False, standard_tests=standard_tests, append=True,
                                                   time_limit=test_timeout(conf))
                results.update(coverage.merge_together(alone, found))
            run = {'mode': mode.value, 'profile': profile, 'odoo': odoo_version}
            if written:
                run['code_date'] = written
        problems = {name: r for name, r in results.items() if r.coverage == coverage.NOT_INSTALLABLE or r.failures}
        for name, r in sorted(problems.items()):
            typer.echo(f"⚠️ {name}: {coverage.describe(r)}")
        # their tests never ran: 0% covered, weighed with what coverage would have measured in them
        unweighed = {name: addons[name] for name, r in results.items() if r.coverage == coverage.NOT_INSTALLABLE and not r.statements}
        if unweighed:
            for name, statements in coverage.never_run(unweighed, coverage_profile).items():
                results[name] = replace(results[name], statements=statements, covered=0)

    # direct dependencies on Odoo enterprise (None: not checked). Added last: not part of the cache key above
    enterprise = enterprise_modules(conf)
    for name in addons:
        state_extra[name]['enterprise'] = None if enterprise is None else sorted(set(dependencies[name]) & enterprise)
        if state_extra[name]['enterprise']:
            typer.echo(f"⚠️ {name} depends on Odoo enterprise: {', '.join(state_extra[name]['enterprise'])}")

    if standard:
        failed = sum(len(r['failed']) for r in standard.values())
        typer.echo(f"{'⚠️' if failed else '✅'} Standard modules: {sum(r['tests'] for r in standard.values())} test(s) "
                   f"in {len(standard)} module(s), {failed} failed")
    report.write_reports(loc, dependencies, results, out_dir, state_extra, run, standard,
                         count_measures(addons, measures), trends)
    return run


def open_report():
    """Open the last report of 'jsa structure' in the browser."""
    path = config.ANALYSIS_DIR / report.REPORT_FILE
    if not path.exists():
        typer.echo("❌ No report yet: run 'jsa structure' first")
        raise typer.Exit(code=1)
    ui.open_file(path)


def register(app: typer.Typer):
    app.command()(structure)
    app.command("report")(open_report)
