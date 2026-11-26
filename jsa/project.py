"""jsa init: make a folder a jsa project. Its .jsa/ holds its own settings (only what differs from the toolbox's
config.toml), reviewed.toml, backfill.toml and reports; jsa uses it when run from that folder or below, or with
-P <folder> (see core/config.py: find_project, use_project, load_config)."""
from pathlib import Path

import typer

from .core import config
from .core.addons import find_addons

TEMPLATE = """\
# The settings of this project for jsa: only what differs from the config.toml of the toolbox
# ({toolbox}), which every project shares (profiles, plugins...). A key here replaces the same key
# there, a table ([profiles.19]) is merged key by key.

# The folders of the custom addons of this project, relative to the project folder (default: the folder itself).
# Those of the toolbox's config.toml never count here.
# [codebase]
# addons_roots = ["addons"]

# What 'jsa restore' does to the databases of this project, after jsa's standard steps (these replace the
# [[restore.extra_steps]] of the toolbox's config.toml).
# [[restore.extra_steps]]
# label = "SSO of this company"
# sql = "UPDATE auth_saml_provider SET ..."

# The addons measured by 'jsa mutation' when none is given
# [mutation]
# addons = ["my_critical_addon"]
"""


def init(folder: Path = typer.Argument(Path("."), help="The folder of the project (default: the current one)")):
    """Make a folder a project of its own: its settings, reviewed findings and reports in its .jsa/ folder."""
    root = folder.expanduser().resolve()
    target = root / config.PROJECT_FOLDER
    if not root.is_dir():
        typer.echo(f"❌ No folder {root}")
        raise typer.Exit(code=1)
    if target.exists():
        typer.echo(f"❌ {target} already exists: {root} is a project already")
        raise typer.Exit(code=1)
    target.mkdir()
    (target / "config.toml").write_text(TEMPLATE.format(toolbox=config.CONFIG_FILE), encoding="utf-8")
    found = find_addons([root])
    typer.echo(f"✅ {root} is a project: its settings in {target / 'config.toml'} (only what differs from the "
               f"toolbox's), its reports in {target / 'codebase_analysis'}")
    typer.echo(f"   {len(found)} addon(s) directly in the folder" + ("" if found else
               ": if they are in a sub-folder, set [codebase] addons_roots there"))
    typer.echo(f"   jsa works on it when run from {root} or below, or with: jsa -P {root} <command>")
    typer.echo(f"   Keep {config.PROJECT_FOLDER}/ out of git (its reports are written there): add it to your .gitignore")


def register(app: typer.Typer):
    app.command()(init)
