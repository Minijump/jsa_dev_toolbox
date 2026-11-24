"""Structure of the custom codebase: dependency graph, lines of code, test coverage, and their report; the search
for the commit that broke a test; mutation testing (what the tests run without checking it)."""
import typer

from . import bisect, codebase, mutation


def register(app: typer.Typer):
    for module in (codebase, bisect, mutation):
        module.register(app)
