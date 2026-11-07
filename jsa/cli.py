"""Entry point: builds the 'jsa' command from the features and the private plugins.

Features and plugins use the same mechanism: a `register(app)` function adding commands to the app (see
core/plugins.py for where the plugins are found).
"""
import sys

import typer

from . import db, doctor, self_update, structure, uitests
from .core import config, plugins

FEATURES = (db, structure, uitests, doctor, self_update)  # the three features, then jsa's own commands

app = typer.Typer(no_args_is_help=True)


@app.callback()
def _check_install():
    """Odoo developer toolbox."""
    if "site-packages" in config.TOOLBOX_DIR.parts:
        typer.echo(f"❌ Cannot find the toolbox folder (running from {config.TOOLBOX_DIR}).")
        raise typer.Exit(code=1)


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
    build_app()()
