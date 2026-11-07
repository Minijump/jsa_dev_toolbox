"""What is installed in a database, compared to the code:

- pending-upgrades: custom addons whose version in the code differs from the installed one. It only lists them
  (with the -u argument to paste): the update itself is up to you.
- enterprise: the Odoo enterprise modules installed, in case the database must run on the community edition. Odoo
  stores the license of every module: enterprise ones have 'OEEL-1'. Those that matter most are the ones needed by
  non-enterprise (custom, OCA) modules, and the applications, which users work with.
"""
from dataclasses import dataclass, field
from typing import Optional

import typer

from ..core import config, pg
from ..core.addons import find_addons, read_manifests

ENTERPRISE_LICENSE = "OEEL-1"
ENTERPRISE_QUERY = f"""
SELECT m.name, m.application, COALESCE(string_agg(d.name, ',' ORDER BY d.name), '')
  FROM ir_module_module m
  LEFT JOIN ir_module_module_dependency dep ON dep.name = m.name
  LEFT JOIN ir_module_module d ON d.id = dep.module_id AND d.state = 'installed'
                               AND COALESCE(d.license, '') != '{ENTERPRISE_LICENSE}'
 WHERE m.state = 'installed' AND m.license = '{ENTERPRISE_LICENSE}'
 GROUP BY m.name, m.application ORDER BY m.name;
"""


# ---------- pending upgrades ----------

def full_version(version: str, series: str) -> str:
    """Odoo stores '1.2' as '<series>.1.2' ('17.0.1.2'); a version already starting with the series is kept."""
    return version if version == series or version.startswith(series + ".") else f"{series}.{version}"


def pending_upgrades(installed: dict[str, str], code_versions: dict[str, str]) -> list[tuple[str, str, str]]:
    """[(addon, installed version, code version)] of the installed addons whose code version differs.
    `installed`: {module: latest_version} of the database, 'base' included (it gives the Odoo series)."""
    series = ".".join(installed.get('base', '').split(".")[:2])
    return [(name, installed[name], full_version(version, series)) for name, version in sorted(code_versions.items())
            if name in installed and version and full_version(version, series) != installed[name]]


def find_pending(db_name: str, conf: dict) -> Optional[list[tuple[str, str, str]]]:
    """None when no addons folder is configured."""
    addons = find_addons(config.addons_roots(conf))
    if not addons:
        return None
    rows = pg.psql(db_name, "SELECT name, latest_version FROM ir_module_module WHERE state IN ('installed', 'to upgrade');")
    installed = dict(line.split('|', 1) for line in rows.splitlines() if '|' in line)
    versions = {name: str(manifest.get('version', '')) for name, manifest in read_manifests(addons).items()}
    return pending_upgrades(installed, versions)


def show_pending(db_name: str, conf: dict):
    """Print the addons to update and the ready-to-paste -u argument."""
    pending = find_pending(db_name, conf)
    if pending is None:
        typer.echo("ℹ️ Pending upgrades not checked: no [codebase] addons_roots in config.toml")
    elif not pending:
        typer.echo(f"✅ No pending upgrade: the custom addons installed in '{db_name}' have the version of the code")
    else:
        typer.echo(f"⏫ {len(pending)} addon(s) of '{db_name}' have a different version in the code:")
        for name, old, new in pending:
            typer.echo(f"   {name}: {old} → {new}")
        typer.echo(f"   To update them:  -u {','.join(name for name, _, _ in pending)}")


def pending_upgrades_command(db_name: str = typer.Argument(..., help="Database to check")):
    """List the custom addons whose version in the code differs from the one installed in the database."""
    show_pending(db_name, config.load_config())


# ---------- enterprise modules ----------

@dataclass
class EnterpriseModule:
    name: str
    application: bool = False
    needed_by: list[str] = field(default_factory=list)  # installed non-enterprise modules depending on it


def parse_enterprise(rows: str) -> list[EnterpriseModule]:
    modules = []
    for line in rows.splitlines():
        name, application, needed_by = line.split('|')
        modules.append(EnterpriseModule(name, application == 't', [n for n in needed_by.split(',') if n]))
    return modules


def describe_enterprise(db_name: str, modules: list[EnterpriseModule]) -> list[str]:
    if not modules:
        return [f"✅ No Odoo enterprise module installed in '{db_name}'"]
    needed = [m for m in modules if m.needed_by]
    apps = [m for m in modules if m.application and not m.needed_by]
    others = [m for m in modules if not m.application and not m.needed_by]
    lines = [f"💎 {len(modules)} Odoo enterprise module(s) installed in '{db_name}'"]
    if needed:
        lines.append(f"⚠️ {len(needed)} needed by non-enterprise modules (your code depends on them):")
        width = max(len(m.name) for m in needed)
        lines.extend(f"   {m.name:<{width}}  ← {', '.join(m.needed_by)}" for m in needed)
    else:
        lines.append("✅ None of them is needed by a non-enterprise module")
    if apps:
        lines.append(f"📱 {len(apps)} other enterprise application(s), used by people: {', '.join(m.name for m in apps)}")
    if others:
        lines.append(f"🧩 {len(others)} other enterprise module(s): {', '.join(m.name for m in others)}")
    return lines


def enterprise(db_name: str = typer.Argument(..., help="Database to look at")):
    """List the Odoo enterprise modules installed in a database, and which non-enterprise modules need them."""
    for line in describe_enterprise(db_name, parse_enterprise(pg.psql(db_name, ENTERPRISE_QUERY))):
        typer.echo(line)


def register(app: typer.Typer):
    app.command("pending-upgrades")(pending_upgrades_command)
    app.command()(enterprise)
