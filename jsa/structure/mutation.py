"""jsa mutation: what the tests of an addon run without checking it.

jsa changes the code of the addon one small change at a time (a mutant: `>` becomes `<=`, a call is removed, ...; see
mutants.py) and runs the tests that reach that code (mutation_driver.py): a change that no test notices (a mutant that
survives) is behaviour the tests run but do not check, what coverage cannot see.

How (the prototype measured it: 2 to 3 minutes for an addon of 800 lines, 4 workers):
1. the mutants are compiled into a copy of the addon (each one off until switched on), by the python of the profile;
2. a database with the addon's dependencies (with demo data), copied (seconds), the copy of the addon installed in it
   (recording which mutants installing it runs);
3. N workers, each with its own copy of that database (parallel tests in one database could wait on each other's
   locks), each loading Odoo once, then one copy of itself (fork) per mutant: Linux only (WSL);
4. a mutant that survived but whose code also runs while installing is checked again by a real install with that
   mutant on (the values Odoo computed while installing, e.g. on demo data, were computed by the original code: a test
   reading them might have noticed): slower, but few.
Results in codebase_analysis/mutation/<addon>.json, shown by the report of 'jsa structure' (made again at the end).
An addon whose code (and that of its custom dependencies), Odoo and rules did not change keeps its results (--all).
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from ..core import config, odoo, ui
from ..core.addons import DependencyGraph, files_digest, find_addons, read_manifests, standard_depends
from ..core.pg import copy_database, createdb, drop_database
from . import codebase, mutants, report_html

MUTANTS_SCRIPT = Path(__file__).with_name("mutants.py")
DRIVER = Path(__file__).with_name("mutation_driver.py")
# the scripts run by the python of the profile, through runpy: run directly, their folder would come first in
# sys.path, and jsa's bisect.py / coverage.py would hide Python's and coverage's modules inside Odoo
RUN = "import runpy, sys; sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
SWITCH = '''"""Which mutant is active (jsa mutation), which mutants the running test reached."""
import atexit, json, os

ACTIVE = int(os.environ.get("JSA_MUTANT", "0"))  # a real install with a mutant on (to check a survivor again)
REC = None
_INSTALL = os.environ.get("JSA_MUT_INSTALL_REC")  # the mutants that installing the addon runs
if _INSTALL:
    REC = set()
    atexit.register(lambda: json.dump(sorted(REC or ()), open(_INSTALL, "w")))


def m(k):
    if REC is not None:
        REC.add(k)
    return ACTIVE == k
'''
KEYS = ('addons', 'skip')


def results_dir() -> Path:
    return config.ANALYSIS_DIR / "mutation"


def settings(conf: dict) -> dict:
    """[mutation] of config.toml: addons (measured when none is given), skip (method name globs left out)."""
    def refuse(why: str):
        typer.echo(f"❌ config.toml: [mutation] {why}")
        raise typer.Exit(code=1)

    section = conf.get('mutation', {})
    if not isinstance(section, dict):
        refuse("must be a table")
    unknown = sorted(set(section) - set(KEYS))
    if unknown:
        refuse(f"has the key(s) {', '.join(unknown)}, which do not exist (misspelled?): use {', '.join(KEYS)}")
    for key in KEYS:
        value = section.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            refuse(f"{key} must be a list of names, e.g. {key} = [\"my_addon\"]")
    return {key: section.get(key, []) for key in KEYS}


def fingerprint(name: str, addons: dict[str, Path], graph: DependencyGraph, profile: dict, skip: list[str]) -> str:
    """Changes when the addon, a custom addon it depends on, Odoo, the rules of the mutants or `skip` change."""
    parts = [files_digest(addons[dep]) for dep in sorted(graph.closure([name]) & addons.keys())]
    parts += [odoo.source_version(profile), str(mutants.RULES_VERSION), json.dumps(sorted(skip))]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def load(name: str) -> dict:
    try:
        return json.loads((results_dir() / f"{name}.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def test_tags(module: str, tests: list[str]) -> str:
    """--test-tags of some tests: odoo.addons.m.tests.test_x.TestX.test_a -> /m:TestX.test_a."""
    return ",".join(f"/{module}:{'.'.join(name.split('.')[-2:])}" for name in tests)


def python_script(profile: dict, script: Path, *args: str, env: dict, cwd: Path, **kw) -> subprocess.Popen:
    return subprocess.Popen([profile['python'], "-c", RUN, str(script), *args], env=env, cwd=cwd, **kw)


# ---------- one addon ----------

def measure(name: str, addons: dict[str, Path], graph: DependencyGraph, profile: dict, skip: list[str], workers: int,
            time_limit: float) -> Optional[dict]:
    """The mutants of an addon, run. None when it could not be measured (said why)."""
    started = time.monotonic()
    log = results_dir() / f"{name}_odoo.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    (results_dir() / f"{name}_tests.log").write_text("", encoding="utf-8")  # Odoo's log of the workers (all of them)
    prefix = f"{profile['db_prefix']}_mut_{uuid.uuid4().hex[:6]}"
    databases = []
    with tempfile.TemporaryDirectory(prefix="jsa_mutation_") as tmp:
        tmp = Path(tmp)
        data_dir, copies, switch = tmp / "data", tmp / "addons", tmp / "switch"
        copies.mkdir()
        switch.mkdir()
        (switch / "jsa_mutant_switch.py").write_text(SWITCH, encoding="utf-8")
        env = config.odoo_env(profile)
        env['PYTHONPATH'] = f"{env['PYTHONPATH']}:{switch}"
        made = python_script(profile, MUTANTS_SCRIPT, str(addons[name]), str(copies), json.dumps(skip), env=env, cwd=tmp,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = made.communicate()
        if made.returncode != 0:
            typer.echo(f"❌ {name}: its mutants could not be made: {err.strip()[-500:]}")
            return None
        for line in err.splitlines():
            typer.echo(f"⚠️ {name}: {line}")
        found = json.loads((copies / "mutants.json").read_text(encoding="utf-8"))
        typer.echo(f"🧬 {name}: {len(found)} mutants")
        if not found:
            return {'mutants': [], 'tests': 0}
        # the copy of the addon first in the addons path: Odoo takes it instead of the original
        mutated = {**profile, 'addons_paths': [str(copies), *profile['addons_paths']]}
        demo = odoo.demo_arguments(profile, True)
        db = lambda label: databases.append(f"{prefix}_{label}"[:63]) or databases[-1]
        try:
            deps_db = db("deps")
            createdb(deps_db)
            info = odoo.install(profile, deps_db, sorted(graph.dependencies(name)), data_dir, log, f"{name}: dependencies",
                                odoo_args=demo, time_limit=time_limit)
            if info.failed:
                typer.echo(f"❌ {name}: its dependencies do not install ({info.culprit or 'see ' + str(log)})")
                return None
            addon_db = db("addon")
            copy_database(deps_db, addon_db, data_dir)
            installed = tmp / "install_reached.json"
            info = odoo.install(mutated, addon_db, [name], data_dir, log, f"{name}: with its mutants", odoo_args=demo,
                                time_limit=time_limit, extra_env={'PYTHONPATH': env['PYTHONPATH'],
                                                                  'JSA_MUT_INSTALL_REC': str(installed)})
            if info.failed:
                typer.echo(f"❌ {name} does not install ({info.culprit or 'see ' + str(log)})")
                return None
            at_install = set(json.loads(installed.read_text())) if installed.exists() else set()
            worker_dbs = []
            for i in range(workers):
                worker_dbs.append(db(f"w{i}"))
                copy_database(addon_db, worker_dbs[-1], data_dir)
            ran = run_workers(name, profile, mutated, worker_dbs, data_dir, tmp, copies / "mutants.json", env, len(found))
            if ran is None:
                return None
            outcome = {m['index']: m for part in ran for m in part['mutants']}
            for mutant in found:
                mutant.update({k: v for k, v in outcome.get(mutant['index'], {'status': 'not reached'}).items()
                               if k in ('status', 'killers', 'tests')})
                mutant['install'] = mutant['index'] in at_install
            left_out = set(ran[0]['left_out'])
            every_test = [t for t in ran[0]['tests'] if t not in left_out
                          and f"class:{t.rsplit('.', 1)[0]}" not in left_out]
            check_again(name, [m for m in found if m['status'] == 'survived' and m['install']], every_test, mutated,
                        deps_db, data_dir, log, demo, env, time_limit, db)
        finally:
            for name_db in databases:
                drop_database(name_db, data_dir, check=False)
    first = ran[0]
    return {'mutants': [{k: v for k, v in m.items() if k not in ('index', 'tests')} | {'killers': short_names(m.get('killers'))}
                        for m in found],
            'tests': len(first['tests']), 'left_out': short_names(first['left_out']), 'flaky': short_names(first['flaky']),
            'browser_tests': first['browser_tests'], 'seconds': round(time.monotonic() - started)}


def short_names(tests) -> list[str]:
    """odoo.addons.m.tests.test_x.TestX.test_a -> TestX.test_a; class:...TestX -> TestX"""
    return [".".join(name.removeprefix("class:").split(".")[-2:]) if not name.startswith("class:")
            else name.split(".")[-1] for name in tests or []]


def run_workers(name, profile, mutated, worker_dbs, data_dir, tmp, mutants_file, env, total) -> Optional[list[dict]]:
    """The drivers, one per worker database, each its share of the mutants; their progress on one line."""
    done = {'killed': 0, 'survived': 0, 'not reached': 0}
    lock, status, started = threading.Lock(), ui.StatusLine(interval=30), time.monotonic()
    procs, outs = [], []
    for i, worker_db in enumerate(worker_dbs):
        arguments, out = tmp / f"driver_{i}.json", tmp / f"results_{i}.json"
        arguments.write_text(json.dumps({
            'db': worker_db, 'addons_path': ",".join(mutated['addons_paths']), 'data_dir': str(data_dir), 'module': name,
            'mutants': str(mutants_file), 'slice': [i, len(worker_dbs)], 'log': str(results_dir() / f"{name}_tests.log"),
            'out': str(out)}), encoding="utf-8")
        procs.append(python_script(profile, DRIVER, str(arguments), env=env, cwd=tmp, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors="replace"))
        outs.append(out)
    tails = [[] for _ in procs]

    def follow(i, proc):
        for line in proc.stdout:
            if line.startswith("JSA "):
                with lock:
                    verdict = line.split(" ", 2)[2].strip()
                    done[verdict] = done.get(verdict, 0) + 1
                    count = sum(done.values())
                    left = (time.monotonic() - started) / count * (total - count)
                    status.update(f"{name}: {count}/{total} mutants, {done['killed']} killed, {done['survived']} survived, "
                                  f"≈ {ui.duration(left)} left", key=count * 10 // max(total, 1))
            else:
                tails[i] = (tails[i] + [line.rstrip()])[-15:]

    threads = [threading.Thread(target=follow, args=(i, p), daemon=True) for i, p in enumerate(procs)]
    for t in threads:
        t.start()
    try:
        for p in procs:
            p.wait()
        for t in threads:
            t.join(5)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        status.done()
    if any(p.returncode != 0 or not out.exists() for p, out in zip(procs, outs)):
        failing = next(i for i, (p, out) in enumerate(zip(procs, outs)) if p.returncode != 0 or not out.exists())
        typer.echo(f"❌ {name}: the tests could not run with the mutants:\n" + "\n".join(tails[failing][-10:]))
        return None
    return [json.loads(out.read_text(encoding="utf-8")) for out in outs]


def check_again(name, survivors, every_test, mutated, deps_db, data_dir, log, demo, env, time_limit, db):
    """A survivor whose code also runs while installing: installed for real with its mutant on, and every test of the
    addon run (not only those reaching its code: a test reading a value computed while installing notices it without
    running that code; the browser tests and those left out excepted). One fails: the tests notice it, killed."""
    for number, mutant in enumerate(survivors, 1):
        typer.echo(f"   🔁 {name}: survivor {number}/{len(survivors)} also runs while installing, checked by a real "
                   f"install ({mutant['file']}:{mutant['line']})")
        check_db = db(f"c{mutant['index']}")
        copy_database(deps_db, check_db, data_dir)
        info = odoo.install(mutated, check_db, [name], data_dir, log, f"{name}: mutant {mutant['index']} installed",
                            odoo_args=demo + ["--test-enable", "--test-tags", test_tags(name, every_test)],
                            time_limit=time_limit, extra_env={'PYTHONPATH': env['PYTHONPATH'], 'JSA_MUTANT': str(mutant['index'])})
        if info.failed or info.failed_tests.get(name):
            mutant.update(status='killed', killers=info.failed_tests.get(name) or ['(the install failed)'])
        mutant['checked'] = True
        drop_database(check_db, data_dir, check=False)


# ---------- command ----------

def mutation(
    names: Optional[list[str]] = typer.Argument(None, metavar="[ADDON]...", help="The addons to measure (default: "
                                                "\\[mutation] addons of config.toml)"),  # \\[: not markup
    profile: str = typer.Option(..., "--profile", "-p", help="Profile key from config.toml \\[profiles]"),
    workers: int = typer.Option(0, "--workers", "-w", help="Tests run in parallel (default: half the processors, 4 at "
                                "most); each one is an Odoo with its own database"),
    measure_all: bool = typer.Option(False, "--all", "-a", help="Measure again the addons that did not change"),
):
    """Find what the tests of critical addons run without checking it (mutation testing): each small change of their
    code that no test notices is listed in the report. Linux only (WSL)."""
    if not sys.platform.startswith("linux"):
        typer.echo("❌ jsa mutation runs on Linux only (it copies the running Odoo: fork): run it in WSL")
        raise typer.Exit(code=1)
    conf = config.load_config()
    chosen = settings(conf)
    names = names or chosen['addons']
    if not names:
        typer.echo("❌ Which addons? jsa mutation <addon>..., or [mutation] addons = [...] in config.toml")
        raise typer.Exit(code=1)
    roots = config.addons_roots(conf)
    found = find_addons(roots)
    manifests = read_manifests(found)
    addons = {a.name: a for a in found if a.name in manifests}
    unknown = [n for n in names if n not in addons]
    if unknown:
        typer.echo(f"❌ Not a custom addon of [codebase] addons_roots: {', '.join(unknown)}")
        raise typer.Exit(code=1)
    odoo_profile = config.load_profile(profile, conf)
    graph = DependencyGraph({**standard_depends(odoo_profile), **{n: m.get('depends', []) for n, m in manifests.items()}},
                            custom=addons)
    workers = workers or max(1, min(4, (os.cpu_count() or 2) // 2))
    time_limit = codebase.test_timeout(conf, warn=True)
    failed = []
    for name in names:
        key = fingerprint(name, addons, graph, odoo_profile, chosen['skip'])
        previous = load(name)
        if previous.get('fingerprint') == key and not measure_all:
            typer.echo(f"♻️ {name}: unchanged since its last measure ({previous.get('date', '?')[:16]}), kept (--all "
                       "measures it again)")
            continue
        typer.echo(f"🧬 {name}: mutation testing with profile '{profile}', {workers} worker(s)")
        result = measure(name, addons, graph, odoo_profile, chosen['skip'], workers, time_limit)
        if result is None:
            failed.append(name)
            continue
        now = datetime.now().isoformat(timespec='seconds')
        result = {'addon': name, 'date': now, 'profile': profile, 'odoo': odoo.major_version(odoo_profile),
                  'fingerprint': key, **result}
        counts = report_html.mutation_counts(result)
        result['runs'] = previous.get('runs', []) + [{'date': now, **counts}]
        results_dir().mkdir(parents=True, exist_ok=True)
        (results_dir() / f"{name}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        value = report_html.mutation_score(result)
        typer.echo(f"{'✅' if not counts['survived'] else '🔎'} {name}: {counts['killed']} change(s) noticed by the tests, "
                   f"{counts['survived']} not noticed, {counts['not reached']} not reached by any test"
                   + (f": score {value}%" if value is not None else "") + f" ({ui.duration(result['seconds'])})")
        if result.get('browser_tests'):
            typer.echo(f"   ℹ️ {result['browser_tests']} browser test(s) (tours) not run: what only they check counts as "
                       "not noticed")
        if result.get('left_out'):
            typer.echo(f"   ⚠️ left out (failing or flaky without any change): {', '.join(result['left_out'])}")
    typer.echo("📊 The report, with the changes not noticed")
    codebase.analyse(conf, roots, config.ANALYSIS_DIR)
    if failed:
        raise typer.Exit(code=1)


def register(app: typer.Typer):
    app.command()(mutation)
