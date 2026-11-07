"""jsa version / jsa update: know which code runs, and get the latest one."""
import hashlib
import importlib.metadata
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import typer

from .core import config

PACKAGE_DIR = Path(__file__).resolve().parent


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang on a credentials prompt
    return subprocess.run(["git", "-C", str(config.TOOLBOX_DIR), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def _digest(folder: Path) -> dict[str, str]:
    """Content of the python packages of a folder (the code jsa imports; a non-editable install copies only those)."""
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*.py")) if (p.parent / "__init__.py").exists()}


def code_differences(package_dir: Path = PACKAGE_DIR, repo_package_dir: Path = None) -> list[str]:
    """How the running code differs from the repo's code. Nothing for an editable install, or for an unchanged copy."""
    repo_package_dir = repo_package_dir or config.TOOLBOX_DIR / "jsa"
    if package_dir == repo_package_dir.resolve():
        return []
    installed, repo = _digest(package_dir), _digest(repo_package_dir)
    groups = (("not in the folder anymore", set(installed) - set(repo)),
              ("missing", set(repo) - set(installed)),
              ("different", {f for f in set(installed) & set(repo) if installed[f] != repo[f]}))
    return [f"{len(files)} file(s) {label} ({', '.join(sorted(files)[:4])}{', ...' if len(files) > 4 else ''})"
            for label, files in groups if files]


def installed_matches_repo(package_dir: Path = PACKAGE_DIR, repo_package_dir: Path = None) -> bool:
    """True if the running code is the repo's code: always for an editable install, for a copy only if unchanged."""
    return not code_differences(package_dir, repo_package_dir)


def version(fetch: bool = typer.Option(True, "--fetch/--no-fetch", help="Check the remote for new commits (network)")):
    """Show the running version and whether it is the latest one."""
    typer.echo(f"jsa {importlib.metadata.version(config.DIST_NAME)}  ({config.TOOLBOX_DIR})")
    commit = _git("log", "-1", "--format=%h %cd %s", "--date=short")
    if commit.returncode != 0:
        typer.echo("⚠️ The toolbox folder is not a git repository")
        return
    typer.echo(f"   commit {commit.stdout.strip()}")
    differences = code_differences()
    if not differences:
        typer.echo("✅ Running the code of the toolbox folder")
    else:
        typer.echo("⚠️ The installed code differs from the toolbox folder: run 'jsa update'")
        typer.echo(f"   Installed in {PACKAGE_DIR}: {'; '.join(differences)}")
    if not fetch:
        return
    try:
        fetched = _git("fetch", "--quiet")
    except subprocess.TimeoutExpired:
        fetched = None
    if fetched is None or fetched.returncode != 0:
        typer.echo("⚠️ Could not reach the remote to check for new commits")
        return
    behind = _git("rev-list", "--count", "HEAD..@{upstream}")
    if behind.returncode != 0:
        typer.echo("⚠️ The current branch has no remote branch to compare with")
    elif int(behind.stdout.strip()):
        typer.echo(f"⚠️ {behind.stdout.strip()} new commit(s) on the remote: run 'jsa update'")
    else:
        typer.echo("✅ Up to date with the remote")


def update():
    """git pull the toolbox folder, then reinstall jsa (needed when dependencies changed or for a copy install)."""
    pipx = shutil.which("pipx")
    if not pipx:
        typer.echo("❌ pipx not found")
        raise typer.Exit(code=1)
    typer.echo(f"🔄 git pull in {config.TOOLBOX_DIR}...")
    pull = _git("pull", "--ff-only", timeout=120)
    if pull.returncode != 0:
        typer.echo(f"❌ git pull failed:\n{pull.stderr.strip()}")
        raise typer.Exit(code=1)
    typer.echo(f"   {pull.stdout.strip()}")

    typer.echo("🔄 Reinstalling jsa...")
    # a non-editable install is built in build/ and that folder is never cleaned: the files deleted from the repo
    # would be installed again, and the installed code would never match the toolbox folder
    shutil.rmtree(config.TOOLBOX_DIR / "build", ignore_errors=True)
    toolbox, dist = shlex.quote(str(config.TOOLBOX_DIR)), config.DIST_NAME
    # pipx output is only shown if the reinstall fails. 'uitests sync' updates the ui tests environment, if set up
    script = (f"{pipx} uninstall {dist} >/dev/null 2>&1; out=$({pipx} install -e {toolbox} 2>&1)"
              f" && echo '✅ jsa updated' && jsa version --no-fetch && jsa uitests sync"
              f" || {{ echo \"$out\"; echo '❌ Reinstall failed, run: cd {toolbox} && pipx install -e .'; exit 1; }}")
    # replace this process: the reinstall deletes the environment jsa is running from
    os.execvp("sh", ["sh", "-c", script])


def register(app: typer.Typer):
    app.command()(version)
    app.command()(update)
