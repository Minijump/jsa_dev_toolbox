"""Odoo addons on disk: finding them, reading their manifest, their dependencies and whether their code changed."""
import ast
import hashlib
from pathlib import Path

import typer

IGNORED_FILES = {'__pycache__', '.pyc'}


def read_manifest(addon: Path) -> dict:
    parsed = ast.literal_eval((addon / "__manifest__.py").read_text(encoding='utf-8'))
    return parsed if isinstance(parsed, dict) else {}


def read_manifests(addons, warn: bool = True) -> dict[str, dict]:
    """{name: manifest} of the addons. One whose manifest cannot be read (a typo in it) is left out, with a warning
    when `warn`: Odoo cannot load it either."""
    manifests = {}
    for addon in addons:
        try:
            manifests[addon.name] = read_manifest(addon)
        except (ValueError, SyntaxError) as e:
            if warn:
                typer.echo(f"⚠️ {addon.name}: its __manifest__.py cannot be read ({e}): left out")
    return manifests


def find_addons(roots: list[Path]) -> list[Path]:
    """The addons (folders with a manifest) directly inside the given folders (those that exist)."""
    return [addon for root in roots if root.is_dir() for addon in sorted(root.iterdir(), key=lambda p: p.name)
            if addon.is_dir() and (addon / "__manifest__.py").exists()]


def files_digest(addon: Path) -> str:
    """Changes when a file of the addon changes."""
    h = hashlib.sha256()
    for f in sorted(addon.rglob('*')):
        if f.is_file() and not IGNORED_FILES & {f.suffix, *f.parts}:
            h.update(f.relative_to(addon).as_posix().encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


def profile_addons(profile: dict) -> dict[str, Path]:
    """Every module of the profile's addons paths (+ odoo/addons, where 'base' lives). Like Odoo, the first folder
    having a module wins."""
    paths = [Path(p) for p in profile['addons_paths']] + [Path(profile['odoo_root']) / "odoo" / "addons"]
    found = {}
    for addon in find_addons(paths):
        found.setdefault(addon.name, addon)
    return found


def standard_depends(profile: dict) -> dict[str, list[str]]:
    """Direct dependencies of every module of the profile (see profile_addons)."""
    return {name: manifest.get('depends', [])
            for name, manifest in read_manifests(profile_addons(profile).values(), warn=False).items()}


class DependencyGraph:
    def __init__(self, depends: dict[str, list[str]], custom=()):
        self.depends = depends  # {module: direct dependencies}, standard and custom modules
        self.custom = set(custom)

    def closure(self, modules) -> set[str]:
        """The modules and all their dependencies, recursively ('base' included: every module needs it)."""
        todo, seen = list(modules) + ["base"], set()
        while todo:
            module = todo.pop()
            if module not in seen:
                seen.add(module)
                todo.extend(self.depends.get(module, []))
        return seen

    def dependencies(self, module: str) -> frozenset[str]:
        """All the modules to install before `module`."""
        return frozenset(self.closure(self.depends.get(module, [])))
