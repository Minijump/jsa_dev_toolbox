"""The plugins: commands of your own, added to jsa without changing it.

A plugin is a *.py file of ./plugins/ (gitignored) or of the folders listed in config.toml `plugins_dirs`, or a folder
there holding an __init__.py (a plugin with its own files next to its code: settings, data). Like jsa's features, it
has a `register(app)` function adding its commands.
"""
import importlib.util
import sys
from pathlib import Path

import typer

from . import config


def find(conf: dict) -> list[Path]:
    """The plugins: a *.py file, or the __init__.py of a folder. Names starting with "_" or "." are left out (helpers,
    caches)."""
    dirs = [config.PLUGINS_DIR] + [Path(d).expanduser() for d in conf.get('plugins_dirs', [])]
    found = []
    for folder in (d for d in dirs if d.is_dir()):
        for entry in sorted(folder.iterdir()):
            if entry.name.startswith(('_', '.')):
                continue
            if entry.suffix == '.py' and entry.is_file():
                found.append(entry)
            elif entry.is_dir() and (entry / '__init__.py').is_file():
                found.append(entry / '__init__.py')
    return found


def name(path: Path) -> str:
    """'plugins/hello.py' -> 'hello', 'plugins/quote_pdfs/__init__.py' -> 'quote_pdfs'"""
    return path.parent.name if path.name == '__init__.py' else path.stem


def load(app: typer.Typer, path: Path):
    """Import a plugin and let it register its commands. A broken plugin never breaks the core commands."""
    try:
        package = path.name == '__init__.py'  # a folder: the files of its folder can import each other (from . import x)
        module_name = f"jsa_plugin_{name(path)}"
        spec = importlib.util.spec_from_file_location(module_name, path,
                                                      submodule_search_locations=[str(path.parent)] if package else None)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        if not hasattr(module, 'register'):
            typer.echo(f"⚠️ Plugin {path} has no register(app) function, ignored")
            return
        module.register(app)
    except Exception as e:
        typer.echo(f"⚠️ Plugin {path} failed to load: {e!r}")
