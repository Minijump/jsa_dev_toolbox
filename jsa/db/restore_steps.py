"""Make a production database safe and convenient to use locally.

Every step is defined in config.toml (config.example.toml has the usual ones: 2FA off, admin login, passwords,
expiration date), so each can be changed or switched off:

    [[restore.extra_steps]]
    label = "Outgoing mail servers disabled"
    sql = "UPDATE ir_mail_server SET active = false;"
    requires = ["ir_mail_server"]   # optional: skipped when this table (or "table.column") does not exist
    odoo_versions = [17]            # optional: skipped on the databases of the other versions of Odoo
    enabled = false                 # optional: keeps the step in the file without running it

A step can also be a jsa command instead of SQL, typically one added by a plugin (for what SQL cannot do: files,
calls to Odoo...). It is written as after 'jsa', and called with the name of the database at its end:

    [[restore.extra_steps]]
    label = "Cover and footer set"
    command = "set-cover-footer --company 1"    # runs: jsa set-cover-footer --company 1 <database>

The SQL steps run first, all in one transaction; then the commands, one by one, in the order of the file.
"""
from dataclasses import dataclass
from typing import Any, Callable, Optional

import typer

from ..core import commands, pg

EXPIRATION_DATE = "2135-12-31"
KEYS = ('label', 'sql', 'command', 'requires', 'odoo_versions', 'enabled')  # what a step can say


@dataclass
class Step:
    label: str
    sql: str = ""
    # "table" or "table.column" that must exist, else the step is skipped (module not installed)
    requires: tuple[str, ...] = ()
    command: str = ""  # a jsa command to run instead of SQL
    odoo_versions: tuple[int, ...] = ()  # the step only runs on databases of these versions of Odoo (all if empty)


# used by 'jsa exp-date'
EXPIRATION_STEP = Step(
    f"Expiration date set to {EXPIRATION_DATE}",
    "INSERT INTO ir_config_parameter (key, value) "
    f"VALUES ('database.expiration_date', '{EXPIRATION_DATE}') "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;",
)


def steps_from_config(config: dict[str, Any]) -> tuple[list[Step], list[str]]:
    """Return (steps to run, labels of the disabled steps)."""
    steps, disabled = [], []
    for i, raw in enumerate(config.get('restore', {}).get('extra_steps', [])):
        if not isinstance(raw, dict) or bool(raw.get('sql')) == bool(raw.get('command')):
            typer.echo(f"❌ config.toml: [[restore.extra_steps]] #{i + 1} needs a 'sql' key or a 'command' key (one of them)")
            raise typer.Exit(code=1)
        # a misspelled key would be ignored without a word: e.g. a step for one version of Odoo running on all of them
        unknown = sorted(set(raw) - set(KEYS))
        if unknown:
            typer.echo(f"❌ config.toml: [[restore.extra_steps]] #{i + 1} has the key(s) {', '.join(unknown)}, which do not "
                       f"exist (misspelled?). The keys of a step: {', '.join(KEYS)}")
            raise typer.Exit(code=1)
        sql, command = raw.get('sql', ""), raw.get('command', "")
        label = raw.get('label', sql or f"jsa {command}")
        step = Step(label, sql, tuple(raw.get('requires', ())), command, _versions(raw.get('odoo_versions', []), label))
        if raw.get('enabled', True):
            steps.append(step)
        else:
            disabled.append(step.label)
    return steps, disabled


def _versions(values, label: str) -> tuple[int, ...]:
    """odoo_versions = [17, "19.0"] -> (17, 19): the major versions."""
    values = values if isinstance(values, list) else [values]
    try:
        return tuple(int(str(v).split(".")[0]) for v in values)
    except ValueError:
        typer.echo(f"❌ config.toml: the step '{label}' has odoo_versions = {values}: give versions of Odoo, like [17, 19]")
        raise typer.Exit(code=1)


def database_version(db_name: str) -> Optional[int]:
    """Major version of Odoo of a database (that of its module 'base': '19.0.1.3' -> 19), None if unknown."""
    out = pg.psql(db_name, "SELECT latest_version FROM ir_module_module WHERE name = 'base';").strip()
    return int(out.split(".")[0]) if out.split(".")[0].isdigit() else None


def split_steps(steps: list[Step], existing: set[str], version: Optional[int] = None) -> tuple[list[Step], list[tuple[Step, str]]]:
    """Return (steps to run, [(skipped step, why)]). `version`: Odoo version of the database."""
    to_run, skipped = [], []
    for step in steps:
        missing = [r for r in step.requires if r not in existing]
        wanted = ", ".join(str(v) for v in step.odoo_versions)
        if step.odoo_versions and version not in step.odoo_versions:
            skipped.append((step, f"Odoo {version} database, step for {wanted}" if version
                           else f"Odoo version of the database unknown, step for {wanted}"))
        elif missing:
            skipped.append((step, f"{', '.join(missing)} not in database"))
        else:
            to_run.append(step)
    return to_run, skipped


def _existing_objects(db_name: str, steps: list[Step]) -> set[str]:
    """Among the tables required by the steps, return the existing "table" and "table.column" names."""
    tables = sorted({r.split('.')[0] for s in steps for r in s.requires})
    if not tables:
        return set()
    names = ", ".join(f"'{t}'" for t in tables)
    out = pg.psql(db_name, "SELECT table_name, column_name FROM information_schema.columns "
                           f"WHERE table_schema = 'public' AND table_name IN ({names});")
    existing = set()
    for line in out.splitlines():
        table, column = line.split('|')
        existing |= {table, f"{table}.{column}"}
    return existing


def check_commands(steps: list[Step], known: list[str]):
    """Stop now if a step names a command that is not one of the `known` ones, rather than after a long restore."""
    for step in steps:
        name = commands.name_of(step.command)
        if step.command and (name not in known or name == "restore"):
            typer.echo(f"❌ config.toml: the step '{step.label}' names the command '{name}', which "
                       + ("is the restore itself" if name == "restore" else f"does not exist. Commands: {', '.join(known)}"))
            raise typer.Exit(code=1)


def run_steps(db_name: str, steps: list[Step], disabled: list[str] = (),
              call: Optional[Callable[[str, str], Optional[str]]] = None) -> list[str]:
    """Run the SQL steps in a single transaction (either all the applicable ones are applied, or none), then the
    command steps one by one: `call(command, database)` runs one and gives why it failed, if it did.
    Gives the labels of the command steps that failed: one does not stop the others."""
    typer.echo("🔄 Sanitizing database...")
    if not steps and not disabled:
        typer.echo("⚠️ No sanitize step in config.toml: the database is a raw copy (passwords, 2FA, expiration date "
                   "unchanged). Copy the [[restore.extra_steps]] of config.example.toml to your config.toml")
        return []
    version = database_version(db_name) if any(step.odoo_versions for step in steps) else None
    to_run, skipped = split_steps(steps, _existing_objects(db_name, steps), version)
    sql_steps = [step for step in to_run if step.sql]
    if sql_steps:
        pg.psql(db_name, "\n".join(step.sql for step in sql_steps))
    for step in sql_steps:
        typer.echo(f"✅ {step.label}")
    failed = []
    for step in to_run:
        if step.command:
            typer.echo(f"▶️ {step.label}: jsa {step.command} {db_name}")
            error = call(step.command, db_name) if call else "commands cannot be run from here"
            if error:
                failed.append(step.label)
            typer.echo(f"❌ {step.label} failed: {error}" if error else f"✅ {step.label}")
    for step, why in skipped:
        typer.echo(f"⏭️ {step.label}: skipped ({why})")
    for label in disabled:
        typer.echo(f"💤 {label}: disabled in config.toml")
    return failed
