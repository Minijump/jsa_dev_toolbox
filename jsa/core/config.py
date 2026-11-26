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
DUMPS_DIR = TOOLBOX_DIR / "dumps"  # shared by every project
PLUGINS_DIR = TOOLBOX_DIR / "plugins"
# Odoo data_dir (holds the filestores) when not configured otherwise
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "Odoo"

# A project (jsa init): a folder holding .jsa/, with its own config.toml (only what differs from the toolbox's),
# reviewed.toml, backfill.toml and reports. Set by use_project; the toolbox's own files when there is none.
PROJECT_FOLDER = ".jsa"
PROJECT_DIR: Optional[Path] = None
ANALYSIS_DIR = TOOLBOX_DIR / "codebase_analysis"
REVIEWED_FILE = TOOLBOX_DIR / "reviewed.toml"  # findings of the security review looked at and found fine


def find_project(start: Path) -> Optional[Path]:
    """The folder holding a .jsa/ folder: `start` or one of its parents, like git finds .git. None: there is none."""
    for folder in (start, *start.parents):
        if (folder / PROJECT_FOLDER).is_dir():
            return folder
    return None


def use_project(root: Optional[Path]):
    """Work on a project (the folder holding .jsa/), or on the toolbox's own files (None)."""
    global PROJECT_DIR, ANALYSIS_DIR, REVIEWED_FILE
    PROJECT_DIR = root
    base = root / PROJECT_FOLDER if root else TOOLBOX_DIR
    ANALYSIS_DIR, REVIEWED_FILE = base / "codebase_analysis", base / "reviewed.toml"


def settings_file() -> Path:
    """Where the settings of what jsa works on are written: the project's .jsa/config.toml, else config.toml."""
    return PROJECT_DIR / PROJECT_FOLDER / "config.toml" if PROJECT_DIR else CONFIG_FILE


def merged(base: dict, over: dict) -> dict:
    """`over` on top of `base`: a key replaces the same key, a table is merged key by key."""
    out = dict(base)
    for key, value in over.items():
        out[key] = merged(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return out


def _read(path: Path, required: bool) -> dict[str, Any]:
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


def load_config(required: bool = True, path: Optional[Path] = None) -> dict[str, Any]:
    """Read config.toml (or `path`). When not required, a missing file means an empty config.
    In a project: its .jsa/config.toml on top of the toolbox's (merged), which holds what every project shares
    (profiles, plugins). Its addons are its own: [codebase] addons_roots of the project (relative to its folder),
    by default the project folder itself; never those of the toolbox's config.toml."""
    if path or not PROJECT_DIR:
        return _read(path or CONFIG_FILE, required)
    own = _read(settings_file(), False)
    roots = (own.get('codebase') or {}).get('addons_roots', ["."])
    roots = [roots] if isinstance(roots, str) else roots
    conf = merged(_read(CONFIG_FILE, False), own)
    conf['codebase'] = {**(conf.get('codebase') or {}),
                        'addons_roots': [str(PROJECT_DIR / Path(p).expanduser()) for p in roots]}
    return conf


def load_profile(profile: str, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """A profile of config.toml: python and odoo_root are needed; odoo_bin is <odoo_root>/odoo-bin unless given;
    addons_paths is [codebase] addons_roots, then the enterprise folder, then <odoo_root>/addons, unless given (a
    profile using other checkouts, during a migration)."""
    config = config if config is not None else load_config()
    profiles = config.get('profiles', {})
    conf = profiles.get(profile)
    if not isinstance(conf, dict):
        available = ", ".join(sorted(profiles.keys())) if isinstance(profiles, dict) and profiles else "none"
        typer.echo(f"❌ Unknown profile '{profile}'. Available profiles: {available}")
        raise typer.Exit(code=1)

    required_keys = ('python', 'odoo_root')
    missing = [k for k in required_keys if k not in conf]
    if missing:
        typer.echo(f"❌ Profile '{profile}' is missing required keys: {', '.join(missing)}")
        raise typer.Exit(code=1)

    odoo_root = Path(conf['odoo_root']).expanduser()
    enterprise = Path(conf['enterprise']).expanduser() if conf.get('enterprise') else None
    addons_paths = conf.get('addons_paths') or [*addons_roots(config), *([enterprise] if enterprise else []),
                                                odoo_root / "addons"]
    python_exec = Path(conf['python']).expanduser()
    odoo_bin = Path(conf.get('odoo_bin') or odoo_root / "odoo-bin").expanduser()
    if not python_exec.exists():
        typer.echo(f"❌ Python executable not found for profile '{profile}': {python_exec}")
        raise typer.Exit(code=1)
    if not odoo_bin.exists():
        typer.echo(f"❌ odoo-bin not found for profile '{profile}': {odoo_bin}")
        raise typer.Exit(code=1)

    return {
        'python': str(python_exec),
        'odoo_root': str(odoo_root),
        'odoo_bin': str(odoo_bin),
        'enterprise': str(enterprise) if enterprise else None,
        'addons_paths': [str(Path(p).expanduser()) for p in addons_paths],
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
