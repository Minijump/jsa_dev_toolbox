"""Restore an Odoo backup zip (dump.sql + optional filestore) into a local database, then sanitize it."""
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import typer

from ..core import commands, config, odoo, pg
from . import databases, modules, restore_sql, restore_steps

DUMP_FILE = "dump.sql"
FILESTORE_DIR = "filestore"
# psql errors that are expected when restoring as a non-superuser and do not affect the data
IGNORED_SQL_ERRORS = (
    "must be owner of extension",
)


def get_zip_path(zip_name: Optional[str], dumps_dir: Path) -> Path:
    """If no zip_name provided, look in dumps_dir for a single zip file.
    If zip_name provided, use it if it is a path to an existing zip, else look for that zip in dumps_dir."""
    if not zip_name:
        typer.echo(f"No zip name provided, searching in {dumps_dir}")
        zip_files = sorted(dumps_dir.glob('*.zip'))
        if not zip_files:
            typer.echo(f"❌ No zip files found in {dumps_dir}")
            raise typer.Exit(code=1)
        if len(zip_files) > 1:
            typer.echo(f"❌ Multiple zip files found in {dumps_dir}. Please specify one.")
            for f in zip_files:
                typer.echo(f" - {f.name}")
            raise typer.Exit(code=1)
        typer.echo(f"📦 Using backup: {zip_files[0]}")
        return zip_files[0]

    if not zip_name.endswith('.zip'):
        zip_name = f"{zip_name}.zip"
    given = Path(zip_name).expanduser()
    zip_path = given.resolve() if given.is_file() else dumps_dir / given.name
    typer.echo(f"📦 Looking for backup: {zip_path}")
    if not zip_path.exists():
        typer.echo(f"❌ Zip file {zip_path} does not exist.")
        available = sorted(f.name for f in dumps_dir.glob('*.zip'))
        typer.echo(f"   Zips available in {dumps_dir}: {', '.join(available) if available else 'none'}")
        typer.echo("   Usage: jsa restore <db_name> [zip_name]")
        raise typer.Exit(code=1)
    return zip_path


def extract_dump(zip_path: Path, output_dir: Path) -> tuple[Path, Optional[Path]]:
    """Extract only the SQL dump and the filestore (if any) into output_dir.
    Return (dump_file, filestore_dir or None)."""
    typer.echo(f"📦 Unzipping {zip_path.name} to {output_dir}")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        names = zip_ref.namelist()
        if DUMP_FILE not in names:
            typer.echo(f"❌ {DUMP_FILE} not found in {zip_path.name}.")
            raise typer.Exit(code=1)
        for name in names:
            if name == DUMP_FILE or name.startswith(f"{FILESTORE_DIR}/"):
                zip_ref.extract(name, output_dir)

    filestore_dir = output_dir / FILESTORE_DIR
    return output_dir / DUMP_FILE, (filestore_dir if filestore_dir.is_dir() else None)


def split_sql_errors(stderr: str) -> tuple[list[str], int]:
    """Return (real error lines, number of ignored harmless errors) from psql's stderr."""
    errors = [line for line in stderr.splitlines() if "ERROR:" in line]
    real = [line for line in errors if not any(pattern in line for pattern in IGNORED_SQL_ERRORS)]
    return real, len(errors) - len(real)


def restore_db(db_name: str, dump_file: Path, postgres_user: str, ignore_sql_errors: bool = False):
    typer.echo("🧨 Dropping existing database (if exists)...")
    pg.dropdb(db_name)

    typer.echo("🧱 Creating database...")
    pg.createdb(db_name)

    # Created before the restore so that tables using e.g. the vector type can be restored
    restore_sql.create_privileged_extensions(db_name, restore_sql.dump_extensions(dump_file), postgres_user)

    typer.echo("📥 Restoring database...")
    returncode, stderr = restore_sql.stream_dump(db_name, dump_file)
    real_errors, ignored = split_sql_errors(stderr)
    if ignored:
        typer.echo(f"ℹ️ Ignored {ignored} harmless SQL error(s) (extension ownership, ...)")
    if returncode != 0 or real_errors:
        typer.echo(f"❌ Restore finished with {len(real_errors)} SQL error(s):")
        for line in real_errors[:20]:
            typer.echo(f"   {line}")
        if len(real_errors) > 20:
            typer.echo(f"   ... and {len(real_errors) - 20} more")
        if not ignore_sql_errors:
            typer.echo(f"❌ Database '{db_name}' is incomplete and was NOT sanitized. "
                       "Use --ignore-sql-errors to sanitize it anyway.")
            raise typer.Exit(code=1)
        typer.echo("⚠️ Continuing despite SQL errors (--ignore-sql-errors)")


def restore_filestore(db_name: str, filestore_dir: Optional[Path], data_dir: Path):
    if filestore_dir is None:
        typer.echo("ℹ️ No filestore in the backup, skipping filestore restore")
        return
    target = pg.filestore(db_name, data_dir)
    typer.echo(f"🗂️ Restoring filestore to {target}...")
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(filestore_dir), str(target))
    typer.echo("✅ Filestore restored")


def neutralize(db_name: str, profile: dict):
    typer.echo("🧯 Neutralizing database (odoo-bin neutralize)...")
    subprocess.run([
        profile['python'], profile['odoo_bin'], "neutralize",
        "-d", db_name, "--addons-path", ",".join(profile['addons_paths']),
    ], env=config.odoo_env(profile), check=True)
    typer.echo("✅ Database neutralized")


def restore(
    ctx: typer.Context,
    db_name: str = typer.Argument(..., help="Name of the database to restore to"),
    zip_name: Optional[str] = typer.Argument(None, help="Backup zip name (looked up in ./dumps/) or path; optional if ./dumps/ holds a single zip"),
    postgres_user: str = typer.Option("postgres", "--postgres-user", help="System user for PostgreSQL superuser commands"),
    neutralize_db: bool = typer.Option(False, "--neutralize", help="Run 'odoo-bin neutralize' (not needed for odoo.sh dumps, already neutralized)"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Profile key from config.toml [profiles] (required with --neutralize)"),
    data_dir: Path = typer.Option(config.DEFAULT_DATA_DIR, "--data-dir", help="Odoo data_dir; the filestore (if any) is restored in <data_dir>/filestore/<db_name>"),
    ignore_sql_errors: bool = typer.Option(False, "--ignore-sql-errors", help="Sanitize the database even if the SQL restore reported errors"),
    save_snapshot: bool = typer.Option(True, "--snapshot/--no-snapshot", help="Save the restored database as its snapshot, replacing the previous one (see 'jsa reset')"),
):
    """Restore and sanitize Odoo DB from a backup zip."""
    conf = config.load_config(required=neutralize_db)
    sanitize_steps, disabled_steps = restore_steps.steps_from_config(conf)  # validate config before the long restore
    restore_steps.check_commands(sanitize_steps, commands.names(ctx))
    odoo_profile = None
    if neutralize_db:
        if not profile:
            typer.echo("❌ --profile/-p is required with --neutralize.")
            raise typer.Exit(code=1)
        odoo_profile = config.load_profile(profile, conf)
        version = odoo.major_version(odoo_profile)
        if version is not None and version < 16:
            typer.echo(f"❌ The Odoo of profile '{profile}' is version {version}: 'odoo-bin neutralize' exists from Odoo 16 on. "
                       "Neutralize the database with steps of [[restore.extra_steps]] instead.")
            raise typer.Exit(code=1)

    zip_path = get_zip_path(zip_name, config.DUMPS_DIR)
    with tempfile.TemporaryDirectory(prefix="odoo_restore_") as tmp_dir:
        dump_file, filestore_dir = extract_dump(zip_path, Path(tmp_dir))
        restore_db(db_name, dump_file, postgres_user, ignore_sql_errors)
        restore_filestore(db_name, filestore_dir, data_dir)

    if neutralize_db:
        neutralize(db_name, odoo_profile)
    failed = restore_steps.run_steps(db_name, sanitize_steps, disabled_steps,
                                     call=lambda command, database: commands.call(ctx, command, database))
    if save_snapshot:
        try:
            databases.save_snapshot(db_name, data_dir)
        except typer.Exit:  # the reason is already printed, and the restored database is fine
            failed.append("snapshot")
    if config.addons_roots(conf):
        modules.show_pending(db_name, conf)
    if failed:
        typer.echo(f"⚠️ Database '{db_name}' restored, but {len(failed)} step(s) failed: {', '.join(failed)}")
        raise typer.Exit(code=1)
    typer.echo(f"🎉 Database '{db_name}' restored")


def register(app: typer.Typer):
    app.command()(restore)
