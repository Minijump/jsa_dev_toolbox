"""Toolbox location and user configuration (config.toml)."""
import importlib.metadata
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

import typer

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

DIST_NAME = "jsa-dev-toolbox"


def _find_toolbox_dir():
    """The toolbox folder (the repo), where dumps/, config.toml and reports live.
    A non-editable install copies the package into site-packages; pip then records the folder it was installed from."""
    here = Path(__file__).resolve().parent.parent.parent
    if "site-packages" not in here.parts:
        return here
    try:
        info = json.loads(importlib.metadata.distribution(DIST_NAME).read_text("direct_url.json") or "{}")
    except importlib.metadata.PackageNotFoundError:
        info = {}
    url = urllib.parse.urlparse(info.get("url", ""))
    if url.scheme == "file":
        return Path(urllib.request.url2pathname(url.path))
    return here


TOOLBOX_DIR = _find_toolbox_dir()
CONFIG_FILE = TOOLBOX_DIR / "config.toml"
DUMPS_DIR = TOOLBOX_DIR / "dumps"
ANALYSIS_DIR = TOOLBOX_DIR / "codebase_analysis"
PLUGINS_DIR = TOOLBOX_DIR / "plugins"
# Odoo data_dir (holds the filestores) when not configured otherwise
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "Odoo"


def load_config(required: bool = True, path: Optional[Path] = None) -> dict[str, Any]:
    """Read config.toml. When not required, a missing file means an empty config."""
    path = path or CONFIG_FILE
    try:
        return tomllib.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        if not required:
            return {}
        typer.echo(f"❌ Config file not found: {path}")
        typer.echo("   Copy config.example.toml to config.toml and adapt it to your machine.")
        raise typer.Exit(code=1)
    except tomllib.TOMLDecodeError as e:
        typer.echo(f"❌ Invalid TOML in {path}: {e}")
        raise typer.Exit(code=1)


def load_profile(profile: str, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    profiles = (config if config is not None else load_config()).get('profiles', {})
    conf = profiles.get(profile)
    if not isinstance(conf, dict):
        available = ", ".join(sorted(profiles.keys())) if isinstance(profiles, dict) and profiles else "none"
        typer.echo(f"❌ Unknown profile '{profile}'. Available profiles: {available}")
        raise typer.Exit(code=1)

    required_keys = ('python', 'odoo_root', 'odoo_bin', 'addons_paths')
    missing = [k for k in required_keys if k not in conf]
    if missing:
        typer.echo(f"❌ Profile '{profile}' is missing required keys: {', '.join(missing)}")
        raise typer.Exit(code=1)

    python_exec = Path(conf['python']).expanduser()
    odoo_bin = Path(conf['odoo_bin']).expanduser()
    if not python_exec.exists():
        typer.echo(f"❌ Python executable not found for profile '{profile}': {python_exec}")
        raise typer.Exit(code=1)
    if not odoo_bin.exists():
        typer.echo(f"❌ odoo-bin not found for profile '{profile}': {odoo_bin}")
        raise typer.Exit(code=1)

    return {
        'python': str(python_exec),
        'odoo_root': str(Path(conf['odoo_root']).expanduser()),
        'odoo_bin': str(odoo_bin),
        'enterprise': str(Path(conf['enterprise']).expanduser()) if conf.get('enterprise') else None,
        'addons_paths': [str(Path(p).expanduser()) for p in conf['addons_paths']],
        'db_prefix': str(conf.get('db_prefix', 'cov')),
    }


def addons_roots(config: dict[str, Any]) -> list[Path]:
    """[codebase] addons_roots: the folders of your custom addons."""
    return [Path(p).expanduser() for p in config.get('codebase', {}).get('addons_roots', [])]


def odoo_env(profile: dict[str, Any]) -> dict[str, str]:
    """Environment to run odoo-bin of a profile."""
    env = os.environ.copy()
    env["PYTHONPATH"] = ":".join(p for p in (profile['odoo_root'], profile['enterprise']) if p)
    return env
