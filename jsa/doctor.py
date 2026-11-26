"""jsa doctor: check that everything jsa relies on is installed and configured."""
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import typer

from . import self_update
from .core import config, pg, plugins
from .core.addons import find_addons
from .db import restore, restore_sql, restore_steps
from .structure import coverage

OK, WARN, FAIL = "✅", "⚠️", "❌"
Check = tuple[str, str, str]  # level, icon, message


def _run(*cmd: str) -> tuple[bool, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return res.returncode == 0, (res.stdout or res.stderr).strip()


def _sql(query: str) -> tuple[bool, str]:
    return _run("psql", "-X", "-q", "-tA", "-d", "postgres", "-c", query)


def check_postgres(data_dir: Path) -> Iterator[Check]:
    missing = [tool for tool in ("psql", "createdb", "dropdb") if not shutil.which(tool)]
    if missing:
        yield FAIL, "🐘", f"PostgreSQL tools not found: {', '.join(missing)} (sudo apt install postgresql-client)"
        return
    ok, out = _sql("SELECT current_user || '|' || rolsuper || '|' || current_setting('server_version') FROM pg_roles WHERE rolname = current_user;")
    if not ok:
        yield FAIL, "🐘", f"Cannot connect to PostgreSQL: {out.splitlines()[-1] if out else 'no answer'}"
        return
    user, superuser, version = out.split('|')
    yield OK, "🐘", f"PostgreSQL {version} reachable as '{user}'"
    if superuser == 't':
        yield OK, "👑", "Your role is superuser: restore never needs sudo"
    else:
        yield OK, "🙋", "Your role is not superuser: restore asks for sudo only when a dump uses pgvector"
    ok, out = _sql("SELECT default_version FROM pg_available_extensions WHERE name = 'vector';")
    if ok and out:
        yield OK, "🧬", f"pgvector {out} available"
    else:
        yield WARN, "🧬", "pgvector not installed: dumps of databases using it (Odoo AI) will not restore (sudo apt install postgresql-<version>-pgvector)"
    filestore = pg.filestores(data_dir)
    ok, out = _sql("SELECT datname FROM pg_database;")
    if ok and filestore.is_dir():
        orphans = sorted(d.name for d in filestore.iterdir() if d.is_dir() and d.name not in out.split())
        if orphans:
            shown = ", ".join(orphans[:5]) + (f" and {len(orphans) - 5} more" if len(orphans) > 5 else "")
            yield WARN, "👻", f"{len(orphans)} filestore(s) without database in {filestore}: {shown} (can be deleted)"
        else:
            yield OK, "🗂️", f"No filestore without database in {filestore}"


def check_tools() -> Iterator[Check]:
    for tool, icon, why in (("git", "🐙", "needed by jsa update and jsa version"),
                            ("pipx", "📦", "needed by jsa update"),
                            ("dot", "🕸️", "needed to draw the dependency graph (sudo apt install graphviz)")):
        if shutil.which(tool):
            yield OK, icon, f"{tool} found"
        else:
            yield WARN, icon, f"{tool} not found: {why}"
    try:
        matches = self_update.installed_matches_repo()
    except OSError:
        matches = True
    if matches:
        yield OK, "🏷️", f"jsa runs the code of {config.TOOLBOX_DIR}"
    else:
        yield WARN, "🏷️", "The installed jsa differs from the toolbox folder: run 'jsa update'"


def check_config() -> Iterator[Check]:
    if config.PROJECT_DIR:
        yield OK, "📁", f"Project {config.PROJECT_DIR}: {config.settings_file()} on top of {config.CONFIG_FILE}"
    if not config.CONFIG_FILE.exists():
        yield WARN, "📄", f"No {config.CONFIG_FILE}: needed for the sanitize steps of restore, structure, pending-upgrades (copy config.example.toml)"
        if not config.PROJECT_DIR:
            return
    try:
        conf = config.load_config()
    except typer.Exit:
        yield FAIL, "📄", "config.toml is not valid (see the message above)"
        return
    yield OK, "📄", "config.toml is valid" + (" (the toolbox's and the project's)" if config.PROJECT_DIR else "")
    try:
        steps, _ = restore_steps.steps_from_config(conf)
    except typer.Exit:
        yield FAIL, "🧼", "The restore steps of config.toml are not valid (see the message above)"
    else:
        if steps:
            yield OK, "🧼", f"{len(steps)} sanitize step(s) run by restore (jsa's standard ones and yours)"
        else:
            yield WARN, "🧼", "Every restore step is switched off: restore leaves passwords, 2FA and expiration date unchanged"
    roots = config.addons_roots(conf)
    missing = [str(r) for r in roots if not r.is_dir()]
    if missing:
        yield FAIL, "📂", f"addons_roots not found: {', '.join(missing)}"
    elif roots:
        yield OK, "📂", f"{len(find_addons(roots))} custom addons in {len(roots)} addons_roots folder(s)"
    else:
        yield WARN, "📂", "No [codebase] addons_roots: structure and pending-upgrades have nothing to look at"
    for name in conf.get('profiles', {}):
        try:
            profile = config.load_profile(name, conf)
        except typer.Exit:
            yield FAIL, "🦉", f"Profile '{name}' is not usable (see the message above)"
            continue
        missing = [p for p in profile['addons_paths'] if not Path(p).is_dir()]
        if missing:
            yield FAIL, "🦉", f"Profile '{name}': addons_paths not found: {', '.join(missing)}"
            continue
        ok, out = _run(profile['python'], profile['odoo_bin'], "--version")
        if not ok:
            yield FAIL, "🦉", f"Profile '{name}': odoo-bin does not start: {out.splitlines()[-1] if out else '?'}"
            continue
        yield OK, "🦉", f"Profile '{name}': {out.splitlines()[-1]}"
        ok, out = _run(profile['python'], "-c", "import coverage; print(coverage.__version__)")
        if ok and coverage.too_old(out):
            yield WARN, "🎯", (f"Profile '{name}': coverage {out} is too old for 'jsa structure -c' (needs "
                               f"{'.'.join(map(str, coverage.MIN_COVERAGE))}+): {profile['python']} -m pip install -U coverage")
        elif ok:
            yield OK, "🎯", f"Profile '{name}': coverage {out} installed"
        else:
            yield WARN, "🎯", f"Profile '{name}': 'coverage' is not installed in its python: needed by 'jsa structure -c'"
    found = plugins.find(conf)
    if found:
        yield OK, "🔌", f"{len(found)} plugin(s): {', '.join(plugins.name(p) for p in found)}"
    if not conf.get('profiles'):
        yield WARN, "🦉", "No [profiles]: needed by 'jsa structure -c' and 'jsa restore --neutralize'"


def check_dumps() -> Iterator[Check]:
    zips = sorted(config.DUMPS_DIR.glob("*.zip")) if config.DUMPS_DIR.is_dir() else []
    yield OK, "🗜️", f"{len(zips)} dump(s) in {config.DUMPS_DIR}" + (f": {', '.join(z.name for z in zips[:5])}" if zips else "")
    local = restore_sql.local_versions() if zips else None
    for zip_path in zips:  # only the first lines of each dump are read
        problem = restore.version_problem(zip_path, local)
        if problem:
            yield WARN, "🐘", f"{zip_path.name}: {problem[0]} {problem[1]} ('jsa restore' refuses it until then)"


def doctor(data_dir: Path = typer.Option(config.DEFAULT_DATA_DIR, "--data-dir", help="Odoo data_dir, where the filestores are")):
    """Check that everything jsa needs is installed and configured."""
    typer.echo("🩺 Checking your setup...")
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for check in (check_postgres(data_dir), check_tools(), check_config(), check_dumps()):
        for level, icon, message in check:
            counts[level] += 1
            typer.echo(f"{level} {icon} {message}")
    if counts[FAIL]:
        typer.echo(f"🚑 {counts[FAIL]} problem(s) to fix, {counts[WARN]} warning(s)")
        raise typer.Exit(code=1)
    typer.echo(f"🤒 All good, with {counts[WARN]} warning(s)" if counts[WARN] else "🎉 Everything looks good")


def register(app: typer.Typer):
    app.command()(doctor)
