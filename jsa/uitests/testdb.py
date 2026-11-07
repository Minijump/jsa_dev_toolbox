"""The database made for the ui tests ('jsa uitests run --new-db'): modules installed with demo data.

Which modules: those of [uitests] modules in config.toml, else every custom addon ([codebase] addons_roots).

Installing them takes minutes, so it is done once, in a template database. Every run of the tests then starts from
a copy of it (seconds): always from the same state, whatever the previous tests did. The template is built again
when its modules, the code of a custom one, or the version of Odoo changed.
"""
import hashlib
import http.cookiejar
import json
import re
import time
import urllib.request
from pathlib import Path

import typer

from ..core import config, odoo, pg
from ..core.addons import DependencyGraph, files_digest, find_addons, profile_addons, read_manifests, standard_depends

DATA_DIR = config.DEFAULT_DATA_DIR  # where Odoo puts the filestores: these are databases like your other ones
ASSET_RE = re.compile(r'(?:src|href)="(/web/assets/[^"]+)"')


def names(profile_key: str) -> tuple[str, str]:
    """(template, database of a run) for a profile."""
    base = "uitests_" + re.sub(r"\W", "_", profile_key)
    return base + "__template", base


def requested_modules(conf: dict) -> list[str]:
    """[uitests] modules of config.toml, else the installable custom addons of [codebase] addons_roots."""
    modules = conf.get('uitests', {}).get('modules')
    if modules:
        return sorted(modules)
    return sorted(name for name, manifest in read_manifests(find_addons(config.addons_roots(conf))).items()
                  if manifest.get('installable', True))


def custom_addons(profile: dict, modules) -> dict[str, Path]:
    """Among the modules, those that are not Odoo's own (not in the Odoo and enterprise folders): {name: folder}."""
    standard = [Path(folder).resolve() for folder in (profile['odoo_root'], profile['enterprise']) if folder]
    available = profile_addons(profile)
    return {name: available[name] for name in modules if name in available
            and not any(available[name].resolve().is_relative_to(folder) for folder in standard)}


def fingerprint(modules: list[str], custom: dict[str, Path], demo: bool, version: str) -> str:
    """Changes when the template has to be built again."""
    content = {'modules': sorted(modules), 'demo': demo, 'odoo': version,
               'code': {name: files_digest(path) for name, path in sorted(custom.items())}}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def built_from(template: str) -> str:
    """The fingerprint the template was built from (kept as the comment of the database), '' if it does not exist."""
    return pg.psql("postgres", f"SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = '{template}';").strip()


def install_modules(template: str, modules: list[str], custom: dict[str, Path], graph: DependencyGraph, install, copy, drop) -> list[str]:
    """Install the modules in the template. A custom addon that fails to install is left out, with those depending
    on it, and the others are installed again from a copy of the standard modules. Gives the addons left out.
    `install(db, modules, label)` gives what Odoo's log said, `copy(source, target)` and `drop(db)` handle databases."""
    standard = sorted(graph.closure(modules) - custom.keys())
    base = template + "_std" if custom else template
    drop(base)
    copy(None, base)
    info = install(base, standard, "standard modules")
    if info.failed:
        drop(base)
        typer.echo(f"❌ The standard modules could not be installed ({info.culprit or 'see the log'})")
        raise typer.Exit(code=1)
    if not custom:
        return []

    def attempt(names_: list[str]):
        copy(base, template)
        result = install(template, names_, "your addons")
        if result.failed:
            drop(template)
        return result

    info, left_out = odoo.install_leaving_out(custom, graph, attempt)
    if info and info.failed:  # not because of one of your addons
        drop(base)
        typer.echo(f"❌ Your addons could not be installed ({info.culprit or 'see the log'})")
        raise typer.Exit(code=1)
    if info is None:  # none of them could be installed: the standard modules alone
        copy(base, template)
    drop(base)
    return sorted(left_out)


def warm_up(profile: dict, template: str, log_file: Path, password: str):
    """Open Odoo once on the template, so that it prepares its pages (scripts, styles) there and not at the first
    test of every run, where the wait could make it fail. If it does not work, the tests are only slower."""
    with odoo.serving(profile, template, log_file, DATA_DIR) as url:
        if not url:
            return
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        login = json.dumps({"jsonrpc": "2.0", "params": {"db": template, "login": "admin", "password": password}}).encode()
        try:
            pages = [opener.open(f"{url}/web/login", timeout=120).read().decode(errors="replace")]
            opener.open(urllib.request.Request(f"{url}/web/session/authenticate", data=login,
                                               headers={"Content-Type": "application/json"}), timeout=120).read()
            pages.append(opener.open(f"{url}/web", timeout=300).read().decode(errors="replace"))
            for asset in sorted({a for page in pages for a in ASSET_RE.findall(page)}):
                opener.open(url + asset, timeout=300).read()
        except (OSError, ValueError):
            pass


def build(profile: dict, template: str, modules: list[str], custom: dict[str, Path], graph: DependencyGraph, demo: bool,
          password: str, log_file: Path) -> list[str]:
    """Build the template from nothing. Gives the modules that are missing in it without having failed (those are
    already reported): typically a module that is not in the addons_paths of the profile."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")
    extra = odoo.demo_arguments(profile, demo)

    def copy(source, target):
        if source:
            pg.copy_database(source, target, DATA_DIR)
        else:
            pg.createdb(target)

    pg.drop_database(template, DATA_DIR)
    left_out = install_modules(template, modules, custom, graph,
                               install=lambda db, names_, label: odoo.install(profile, db, names_, DATA_DIR, log_file, label, odoo_args=extra),
                               copy=copy, drop=lambda db: pg.drop_database(db, DATA_DIR))
    installed = odoo.installed_modules(template, modules)
    warm_up(profile, template, log_file.with_name("odoo_warm_up.log"), password)
    return sorted(set(modules) - installed - set(left_out))


def prepare(profile_key: str, profile: dict, conf: dict, log_file: Path, rebuild: bool = False) -> str:
    """Give a fresh database for a run of the tests, building its template first if needed. Gives its name."""
    settings = conf.get('uitests', {})
    modules, demo = requested_modules(conf), bool(settings.get('demo', True))
    if not modules:
        typer.echo("❌ Nothing to install in the test database: set [uitests] modules or [codebase] addons_roots in config.toml")
        raise typer.Exit(code=1)
    template, database = names(profile_key)
    graph = DependencyGraph(standard_depends(profile))
    custom = custom_addons(profile, graph.closure(modules))
    wanted = fingerprint(modules, custom, demo, odoo.source_version(profile))
    pg.drop_database(database, DATA_DIR)  # the one of the previous run, if it was kept
    if rebuild or built_from(template) != wanted:
        typer.echo(f"🧱 Building the test database: {len(modules)} module(s) and what they need, "
                   f"{'with' if demo else 'without'} demo data. Done once, it takes minutes (Odoo log: {log_file})")
        start = time.monotonic()
        missing = build(profile, template, modules, custom, graph, demo, str(settings.get('password', 'admin')), log_file)
        if missing:
            typer.echo(f"⚠️ Not installed: {', '.join(missing)} (not in the addons_paths of the profile? See the log)")
        pg.psql("postgres", f"COMMENT ON DATABASE \"{template}\" IS '{wanted}';")
        typer.echo(f"✅ Test database built in {int(time.monotonic() - start)}s: kept as '{template}' for the next runs")
    else:
        typer.echo("♻️ Test database already built, nothing changed since (--rebuild builds it again)")
    pg.copy_database(template, database, DATA_DIR)
    return database


def drop(profile_key: str):
    pg.drop_database(names(profile_key)[1], DATA_DIR, check=False)
