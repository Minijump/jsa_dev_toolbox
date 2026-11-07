"""Structure of the custom codebase: dependency graph, lines of code, test coverage, and their report; and the search
for the commit that broke a test."""
import typer

from . import bisect, codebase


def register(app: typer.Typer):
    for module in (codebase, bisect):
        module.register(app)
