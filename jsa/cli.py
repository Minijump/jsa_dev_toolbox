"""Entry point: builds the 'jsa' command from the features and the private plugins.

Features and plugins use the same mechanism: a `register(app)` function adding commands to the app (see
core/plugins.py for where the plugins are found).
"""
import sys
from pathlib import Path
from typing import Optional

import typer

from . import db, doctor, project, self_update, structure
from .core import config, plugins

FEATURES = (db, structure, project, doctor, self_update)  # the two features, then jsa's own commands

app = typer.Typer(no_args_is_help=True)


@app.callback()
def _check_install(_project: Optional[Path] = typer.Option(
        None, "--project", "-P", help="The project to work on: a folder holding .jsa/ (jsa init). Default: the one of "
                                      "the current folder, or of a folder above it")):
    """Odoo developer toolbox."""
    if "site-packages" in config.TOOLBOX_DIR.parts:
        typer.echo(f"❌ Cannot find the toolbox folder (running from {config.TOOLBOX_DIR}).")
        raise typer.Exit(code=1)
    if config.PROJECT_DIR:  # chosen by main(), before the plugins (a project's config.toml may add some)
        typer.echo(f"📁 Project: {config.PROJECT_DIR}")


def project_of(args: list[str]) -> Optional[Path]:
    """The project to work on: -P/--project <folder> given before the command, else the folder holding .jsa/ found
    from the current folder up (like git finds .git); None: the toolbox's own settings."""
    given = (args[1] if args[:1] in (["-P"], ["--project"]) and len(args) > 1 else
             args[0].split("=", 1)[1] if args and args[0].startswith("--project=") else None)
    if given is None:
        try:
            return config.find_project(Path.cwd())
        except OSError:  # the current folder was deleted
            return None
    root = Path(given).expanduser().resolve()
    if not (root / config.PROJECT_FOLDER).is_dir():
        typer.echo(f"❌ No {config.PROJECT_FOLDER} folder in {root}: 'jsa init {given}' makes one")
        sys.exit(1)
    return root


def build_app() -> typer.Typer:
    for module in FEATURES:
        module.register(app)
    try:
        conf = config.load_config(required=False)
    except typer.Exit as e:  # invalid config.toml: message already printed
        sys.exit(e.exit_code)
    for path in plugins.find(conf):
        plugins.load(app, path)
    return app


def main():
    config.use_project(project_of(sys.argv[1:]))
    build_app()()
