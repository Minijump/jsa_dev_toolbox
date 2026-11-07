"""Database tools: restore and sanitize a backup, manage the local databases, see what is installed in them."""
import typer

from . import databases, modules, restore


def register(app: typer.Typer):
    for module in (restore, databases, modules):
        module.register(app)
