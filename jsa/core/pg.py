"""Local databases: thin wrappers around the Postgres command line tools (psql, createdb, dropdb), and copies of
Odoo databases with their filestore."""
import shutil
import subprocess
from pathlib import Path

import typer


def psql(db_name: str, sql: str, *, user: str = None) -> str:
    """Run SQL in one psql call, in a single transaction, stopping at the first error.
    Return the unaligned output (rows separated by newlines, columns by '|'). Exit with a message on error."""
    cmd = ["psql", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", "-d", db_name, "-c", sql]
    if user:
        cmd = ["sudo", "-u", user] + cmd
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        typer.echo(f"❌ SQL failed on '{db_name}': {res.stderr.strip()}")
        raise typer.Exit(code=1)
    return res.stdout


def is_superuser() -> bool:
    return psql("postgres", "SELECT rolsuper FROM pg_roles WHERE rolname = current_user;").strip() == "t"


def createdb(db_name: str, template: str = None):
    """Create a database, as a copy of `template` if given (fails if the template is in use, e.g. by a running Odoo)."""
    res = subprocess.run(["createdb", db_name] + (["-T", template] if template else []), capture_output=True, text=True)
    if res.returncode != 0:
        typer.echo(f"❌ Could not create database '{db_name}': {res.stderr.strip()}")
        raise typer.Exit(code=1)


def exists(db_name: str) -> bool:
    return psql("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{db_name}';").strip() == "1"


def dropdb(db_name: str, check: bool = True):
    """Drop the database if it exists. Fails (when check) if it is in use, e.g. by a running Odoo."""
    res = subprocess.run(["dropdb", "--if-exists", db_name], capture_output=True, text=True)
    if check and res.returncode != 0:
        typer.echo(f"❌ Could not drop database '{db_name}': {res.stderr.strip()}")
        raise typer.Exit(code=1)


# ---------- Odoo databases: the Postgres database and its filestore ----------

def filestores(data_dir: Path) -> Path:
    """The folder holding the filestore of every database, in an Odoo data_dir."""
    return Path(data_dir).expanduser() / "filestore"


def filestore(db_name: str, data_dir: Path) -> Path:
    return filestores(data_dir) / db_name


def copy_database(source: str, target: str, data_dir: Path):
    """Copy a database (Postgres template copy: seconds) and its filestore. The source must not be in use."""
    createdb(target, template=source)
    source_files, target_files = filestore(source, data_dir), filestore(target, data_dir)
    if target_files.exists():
        shutil.rmtree(target_files)
    if source_files.exists():
        shutil.copytree(source_files, target_files)


def drop_database(db_name: str, data_dir: Path, check: bool = True):
    dropdb(db_name, check=check)
    shutil.rmtree(filestore(db_name, data_dir), ignore_errors=True)
