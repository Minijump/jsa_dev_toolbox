"""structure: dependency graph, lines of code and optional test coverage of the custom addons."""
import hashlib
import subprocess
from enum import Enum
from pathlib import Path
from typing import Optional

import typer

from ..core import config, odoo, ui
from ..core.addons import DependencyGraph, files_digest, find_addons, read_manifests, standard_depends
from . import coverage, report

LOC_EXTENSIONS = {'.py', '.xml', '.js', '.scss', '.css', '.csv'}
LOC_EXCLUDED_DIRS = {'migrations', 'tests', 'i18n'}


class Mode(str, Enum):
    whole = "whole"
    isolated = "isolated"
    both = "both"


def count_loc(addon: Path) -> int:
    """Lines of code of an addon, without tests, translations and migrations."""
    loc = 0
    for f in addon.rglob('*'):
        if not f.is_file() or f.suffix.lower() not in LOC_EXTENSIONS:
            continue
        if any(part in LOC_EXCLUDED_DIRS for part in f.relative_to(addon).parts):
            continue
        text = f.read_text(encoding='utf-8', errors='ignore')
        if text:
            loc += text.count('\n') + 1
    return loc


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
        coverage_profile = config.load_profile(profile, conf)
        found = subprocess.run([coverage_profile['python'], "-c", "import coverage"], capture_output=True)
        if found.returncode != 0:
            typer.echo(f"❌ 'coverage' is not installed in the python of profile '{profile}' ({coverage_profile['python']}): "
                       f"install it there with: {coverage_profile['python']} -m pip install coverage")
            raise typer.Exit(code=1)
    if standard_tests and (not coverage_enabled or mode == Mode.isolated):
        typer.echo("❌ --standard-tests needs --coverage/-c, in mode whole or both.")
        raise typer.Exit(code=1)
    measure_all = measure_all or standard_tests  # a full run is what is asked for

    found = find_addons(roots)
    manifests = read_manifests(found)
    addons = {a.name: a for a in found if a.name in manifests}  # a manifest that cannot be read: left out, said
    dependencies = {name: m.get('depends', []) for name, m in manifests.items()}
    loc = {name: count_loc(path) for name, path in addons.items()}
    std_depends = standard_depends(coverage_profile) if coverage_profile else {}
    graph = DependencyGraph({**std_depends, **dependencies}, custom=addons)
    prints = fingerprints(addons, graph)

    previous, _ = report.load_state(config.ANALYSIS_DIR)
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
            log_file = config.ANALYSIS_DIR / "coverage_odoo.log"
            typer.echo(f"📊 Measuring the coverage of {len(to_measure)} addon(s), mode '{mode.value}' (Odoo log: {log_file})")
            measured, standard = coverage.measure(to_measure, graph, coverage_profile, Mode.isolated.value if mode == Mode.both
                                                  else mode.value, log_file, standard_tests=standard_tests)
            results.update(measured)
            if mode == Mode.both:
                alone = {name: coverage.alone_part(results[name]) for name in installable}
                together = {name: path for name, path in installable.items() if alone[name].coverage != coverage.NOT_INSTALLABLE}
                typer.echo(f"🧩 Then the {len(together)} addons installed and tested together: what breaks only with the others")
                found, standard = coverage.measure(together, graph, coverage_profile, Mode.whole.value, log_file,
                                                   measure_coverage=False, standard_tests=standard_tests, append=True)
                results.update(coverage.merge_together(alone, found))
            run = {'mode': mode.value, 'profile': profile, 'odoo': odoo_version}
        problems = {name: r for name, r in results.items() if r.coverage == coverage.NOT_INSTALLABLE or r.failures}
        for name, r in sorted(problems.items()):
            typer.echo(f"⚠️ {name}: {coverage.describe(r)}")

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
    report.write_reports(loc, dependencies, results, config.ANALYSIS_DIR, state_extra, run, standard)
    if open_it:
        open_report()
    else:
        typer.echo("   Open it with: jsa report")


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
