"""Make a production database safe and convenient to use locally.

jsa's standard steps (restore_steps.toml, next to this file: 2FA off, admin login, passwords, expiration date, mail
servers off but switched off itself) run first, then those of config.toml. A step of config.toml with the label of a
standard one replaces its keys (enabled = false switches it off, enabled = true on, sql = "..." changes it), so a
config.toml that copied the standard steps (before jsa 0.34) keeps doing the same, nothing runs twice:

    [[restore.extra_steps]]
    label = "Outgoing mail servers disabled"   # a standard step, switched on
    enabled = true

Your own steps, after the standard ones:

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
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from ..core import commands, config, pg

STANDARD = Path(__file__).with_name("restore_steps.toml")

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


def standard_steps() -> list[dict]:
    """jsa's standard steps (restore_steps.toml)."""
    return config.tomllib.loads(STANDARD.read_text(encoding="utf-8"))["steps"]


def steps_from_config(conf: dict[str, Any]) -> tuple[list[Step], list[str]]:
    """Return (steps to run, labels of the disabled steps): the standard steps, each one completed by the step of
    config.toml with its label, then the other steps of config.toml."""
    own = conf.get('restore', {}).get('extra_steps', [])
    for i, raw in enumerate(own):
        if not isinstance(raw, dict):
            typer.echo(f"❌ config.toml: [[restore.extra_steps]] #{i + 1} must be a table: label = \"...\", sql = \"...\"")
            raise typer.Exit(code=1)
        # a misspelled key would be ignored without a word: e.g. a step for one version of Odoo running on all of them
        unknown = sorted(set(raw) - set(KEYS))
        if unknown:
            typer.echo(f"❌ config.toml: [[restore.extra_steps]] #{i + 1} has the key(s) {', '.join(unknown)}, which do not "
                       f"exist (misspelled?). The keys of a step: {', '.join(KEYS)}")
            raise typer.Exit(code=1)
    standard = standard_steps()
    labels = {step['label'] for step in standard}
    mine = {raw['label']: raw for raw in own if raw.get('label') in labels}
    merged = [{**step, **mine.get(step['label'], {})} for step in standard] + [
        raw for raw in own if raw.get('label') not in labels]
    steps, disabled = [], []
    for raw in merged:
        sql, command = raw.get('sql', ""), raw.get('command', "")
        label = raw.get('label', sql or f"jsa {command}")
        if bool(sql) == bool(command):
            typer.echo(f"❌ config.toml: the restore step '{label}' needs a 'sql' key or a 'command' key (one of them)")
            raise typer.Exit(code=1)
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
        typer.echo("⚠️ No sanitize step: every one is switched off (enabled = false), the database is a raw copy "
                   "(passwords, 2FA, expiration date unchanged)")
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
        typer.echo(f"💤 {label}: disabled (enabled = false)")
    return failed
