"""Commands on a local database: list, remove, exp-date, clone, and snapshot / reset (a copy to get back to in seconds)."""
import subprocess
from pathlib import Path

import typer

from ..core import config, pg
from . import modules, restore_steps

SNAPSHOT_SUFFIX = "__snapshot"
DATA_DIR_OPTION = typer.Option(config.DEFAULT_DATA_DIR, "--data-dir", help="Odoo data_dir, where the filestores are")


def list_db():
    """List all existing databases not owned by postgres with additional info."""
    typer.echo("📋 Listing databases not owned by postgres...")
    result = subprocess.run([
        "psql", "-X", "-d", "postgres", "-c",
        "SELECT d.datname, u.usename as owner, d.encoding, d.datallowconn, d.datconnlimit, d.datistemplate "
        "FROM pg_database d "
        "JOIN pg_user u ON d.datdba = u.usesysid "
        "WHERE d.datdba != (SELECT usesysid FROM pg_user WHERE usename = 'postgres') "
        "ORDER BY d.datname;"
    ], capture_output=True, text=True)
    if result.returncode == 0:
        typer.echo(result.stdout)
    else:
        typer.echo("❌ Failed to list databases.")
        typer.echo(result.stderr)


def remove(db_name: str = typer.Argument(..., help="Name of the database to delete"), data_dir: Path = DATA_DIR_OPTION):
    """Delete a database, its filestore and its snapshot (to delete only the snapshot: jsa remove <db>__snapshot)."""
    snap = db_name + SNAPSHOT_SUFFIX
    has_snapshot = pg.exists(snap)
    if not typer.confirm(f"Are you sure you want to delete the database '{db_name}'" + (" and its snapshot?" if has_snapshot else "?")):
        typer.echo("❌ Deletion cancelled.")
        return
    typer.echo(f"🗑️ Deleting database '{db_name}'...")
    pg.drop_database(db_name, data_dir)
    if has_snapshot:
        pg.drop_database(snap, data_dir)
    typer.echo(f"✅ Database '{db_name}'{' and its snapshot' if has_snapshot else ''} deleted successfully.")


def exp_date(db_name: str = typer.Argument(..., help="Name of the database to update expiration date")):
    """Update 'db' expiration date to 2135-12-31 (already done by 'restore')."""
    restore_steps.run_steps(db_name, [restore_steps.EXPIRATION_STEP])
    value = pg.psql(db_name, "SELECT value FROM ir_config_parameter WHERE key = 'database.expiration_date';")
    typer.echo(f"✅ Current expiration date: {value.strip()}")


def save_snapshot(db_name: str, data_dir: Path):
    """Copy a database and its filestore to <db>__snapshot, replacing the previous snapshot."""
    snap = db_name + SNAPSHOT_SUFFIX
    typer.echo(f"📸 Saving '{db_name}' to '{snap}'...")
    pg.drop_database(snap, data_dir)
    pg.copy_database(db_name, snap, data_dir)
    typer.echo(f"✅ Snapshot saved. Get back to it with: jsa reset {db_name}")


def snapshot(db_name: str = typer.Argument(..., help="Database to save"), data_dir: Path = DATA_DIR_OPTION):
    """Save a copy of a database (and its filestore), to get back to it in seconds with 'jsa reset'."""
    if not pg.exists(db_name):
        typer.echo(f"❌ Database '{db_name}' does not exist")
        raise typer.Exit(code=1)
    save_snapshot(db_name, data_dir)


def reset(db_name: str = typer.Argument(..., help="Database to reset to its snapshot"), data_dir: Path = DATA_DIR_OPTION):
    """Replace a database (and its filestore) by its last 'jsa snapshot', then list the addons to update."""
    snap = db_name + SNAPSHOT_SUFFIX
    if not pg.exists(snap):
        typer.echo(f"❌ No snapshot of '{db_name}': run 'jsa snapshot {db_name}' first")
        raise typer.Exit(code=1)
    typer.echo(f"⏪ Resetting '{db_name}' to its snapshot...")
    pg.drop_database(db_name, data_dir)
    pg.copy_database(snap, db_name, data_dir)
    typer.echo(f"✅ '{db_name}' reset")
    # the code may have changed since the snapshot: what to update, like at the end of 'jsa restore'
    conf = config.load_config(required=False)
    if config.addons_roots(conf):
        modules.show_pending(db_name, conf)


def clone(
    source: str = typer.Argument(..., help="Database to copy"),
    target: str = typer.Argument(..., help="Name of the new database"),
    save_a_snapshot: bool = typer.Option(True, "--snapshot/--no-snapshot", help="Save the new database as its snapshot (see 'jsa reset')"),
    data_dir: Path = DATA_DIR_OPTION,
):
    """Copy a database and its filestore into a new database, and save the copy as its snapshot."""
    if not pg.exists(source):
        typer.echo(f"❌ Database '{source}' does not exist")
        raise typer.Exit(code=1)
    if pg.exists(target):
        typer.echo(f"❌ Database '{target}' already exists. To replace it, delete it first: jsa remove {target}")
        raise typer.Exit(code=1)
    typer.echo(f"🐑 Cloning '{source}' into '{target}'...")
    pg.copy_database(source, target, data_dir)
    typer.echo(f"✅ '{source}' cloned into '{target}'")
    if save_a_snapshot:
        save_snapshot(target, data_dir)


def register(app: typer.Typer):
    # 'list' would shadow the Python builtin, hence the list_db function name
    app.command("list")(list_db)
    app.command()(remove)
    app.command("exp-date")(exp_date)
    app.command()(snapshot)
    app.command()(reset)
    app.command()(clone)
