import zipfile
from pathlib import Path

import pytest
import typer

from jsa.core import config
from jsa.db import restore, restore_sql, restore_steps
from jsa.db.restore_steps import Step


@pytest.fixture
def dumps(tmp_path):
    d = tmp_path / "dumps"
    d.mkdir()
    return d


def make_zip(path, dump=True, filestore=False):
    with zipfile.ZipFile(path, "w") as z:
        if dump:
            z.writestr("dump.sql", "SELECT 1;")
        z.writestr("manifest.json", "{}")
        if filestore:
            z.writestr("filestore/ab/abcdef", "attachment")
    return path


def test_zip_lookup(dumps, tmp_path, monkeypatch):
    prod = make_zip(dumps / "prod.zip")
    monkeypatch.chdir(tmp_path)
    assert restore.get_zip_path(None, dumps) == prod  # single zip picked automatically
    assert restore.get_zip_path("prod", dumps) == prod
    assert restore.get_zip_path("prod.zip", dumps) == prod
    assert restore.get_zip_path("dumps/prod.zip", dumps) == prod
    elsewhere = make_zip(tmp_path / "elsewhere.zip")
    assert restore.get_zip_path(str(elsewhere), dumps) == elsewhere


def test_zip_lookup_errors(dumps, capsys):
    with pytest.raises(typer.Exit):
        restore.get_zip_path(None, dumps)  # no zip
    make_zip(dumps / "a.zip")
    make_zip(dumps / "b.zip")
    with pytest.raises(typer.Exit):
        restore.get_zip_path(None, dumps)  # several zips
    with pytest.raises(typer.Exit):
        restore.get_zip_path("typo", dumps)
    assert "a.zip, b.zip" in capsys.readouterr().out  # available zips are listed


def test_extract_dump(tmp_path):
    out = tmp_path / "out"
    dump, filestore = restore.extract_dump(make_zip(tmp_path / "fs.zip", filestore=True), out)
    assert dump.read_text() == "SELECT 1;"
    assert (filestore / "ab" / "abcdef").read_text() == "attachment"
    assert not (out / "manifest.json").exists()  # only what is needed is extracted

    assert restore.extract_dump(make_zip(tmp_path / "nofs.zip"), tmp_path / "out2")[1] is None
    with pytest.raises(typer.Exit):
        restore.extract_dump(make_zip(tmp_path / "nodump.zip", dump=False), tmp_path / "out3")


def test_split_sql_errors():
    stderr = ("psql:dump.sql:20: ERROR:  must be owner of extension vector\n"
              "psql:dump.sql:99: ERROR:  relation \"foo\" does not exist\n"
              "DETAIL: something\n")
    real, ignored = restore.split_sql_errors(stderr)
    assert real == ['psql:dump.sql:99: ERROR:  relation "foo" does not exist']
    assert ignored == 1


def test_restore_filestore_replaces_existing(tmp_path):
    src = tmp_path / "filestore"
    (src / "ab").mkdir(parents=True)
    (src / "ab" / "new").write_text("new")
    stale = tmp_path / "data" / "filestore" / "db" / "stale"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    restore.restore_filestore("db", src, tmp_path / "data")
    assert (tmp_path / "data" / "filestore" / "db" / "ab" / "new").exists()
    assert not stale.exists()


# ---------- the snapshot at the end ----------

def _restore_without_postgres(monkeypatch, tmp_path, save_snapshot):
    """Run the restore command with everything that needs a database replaced. Gives the snapshots asked for."""
    saved, steps_done = [], []
    monkeypatch.setattr(restore.config, "load_config", lambda required: {})
    monkeypatch.setattr(restore.commands, "names", lambda ctx: [])
    monkeypatch.setattr(restore, "get_zip_path", lambda name, folder: tmp_path / "dump.zip")
    monkeypatch.setattr(restore, "extract_dump", lambda zip_path, tmp: (tmp / "dump.sql", None))
    monkeypatch.setattr(restore, "restore_db", lambda *args: None)
    monkeypatch.setattr(restore, "restore_filestore", lambda *args: None)
    monkeypatch.setattr(restore_steps, "run_steps", lambda *args, **kwargs: steps_done.append(1) or [])
    monkeypatch.setattr(restore.databases, "save_snapshot", lambda db, data_dir: saved.append((db, data_dir, len(steps_done))))
    restore.restore(None, "mydb", None, "postgres", False, None, tmp_path, False, save_snapshot)
    return saved


def test_restore_saves_a_snapshot_unless_told_not_to(monkeypatch, tmp_path, capsys):
    # after the steps: 'jsa reset' gets back to the database as restored and sanitized
    assert _restore_without_postgres(monkeypatch, tmp_path, True) == [("mydb", tmp_path, 1)]
    assert _restore_without_postgres(monkeypatch, tmp_path, False) == []
    assert "restored" in capsys.readouterr().out


def test_a_snapshot_that_fails_does_not_lose_the_restore(monkeypatch, tmp_path, capsys):
    def fails(db, data_dir):
        raise typer.Exit(code=1)

    _restore_without_postgres(monkeypatch, tmp_path, False)
    monkeypatch.setattr(restore.databases, "save_snapshot", fails)
    with pytest.raises(typer.Exit):
        restore.restore(None, "mydb", None, "postgres", False, None, tmp_path, False, True)
    assert "restored, but 1 step(s) failed: snapshot" in capsys.readouterr().out


# ---------- the steps of config.toml ----------

def test_split_steps_skips_missing_tables_and_columns():
    steps = [Step("always", "SELECT 1;"),
             Step("totp", "...", requires=("res_users.totp_secret",)),
             Step("sso", "...", requires=("some_sso_table",))]
    to_run, skipped = restore_steps.split_steps(steps, {"res_users", "res_users.totp_secret"})
    assert [s.label for s in to_run] == ["always", "totp"]
    assert [(s.label, why) for s, why in skipped] == [("sso", "some_sso_table not in database")]


def test_steps_limited_to_some_versions_of_odoo(monkeypatch, capsys):
    conf = {"restore": {"extra_steps": [{"label": "Quote PDFs", "command": "quote-pdfs -p 19", "odoo_versions": [19]},
                                        {"label": "Old column", "sql": "UPDATE a;", "odoo_versions": ["16.0", 17]},
                                        {"label": "Always", "sql": "UPDATE b;"}]}}
    steps, _ = restore_steps.steps_from_config(conf)
    assert [s.odoo_versions for s in steps] == [(19,), (16, 17), ()]
    with pytest.raises(typer.Exit):
        restore_steps.steps_from_config({"restore": {"extra_steps": [{"sql": "SELECT 1;", "odoo_versions": ["latest"]}]}})
    assert "give versions of Odoo, like [17, 19]" in capsys.readouterr().out

    to_run, skipped = restore_steps.split_steps(steps, set(), version=17)
    assert [s.label for s in to_run] == ["Old column", "Always"]
    assert [(s.label, why) for s, why in skipped] == [("Quote PDFs", "Odoo 17 database, step for 19")]
    to_run, skipped = restore_steps.split_steps(steps, set(), version=18)  # neither 17 nor 19
    assert [s.label for s in to_run] == ["Always"] and len(skipped) == 2
    _, skipped = restore_steps.split_steps(steps, set(), version=None)
    assert skipped[0][1] == "Odoo version of the database unknown, step for 19"

    # the version is read from the database only when a step asks for one
    queries, called = [], []
    monkeypatch.setattr(restore_steps.pg, "psql", lambda db, query: queries.append(query) or ("19.0.1.3" if "latest_version" in query else ""))
    monkeypatch.setattr(restore_steps, "_existing_objects", lambda db, steps: set())
    restore_steps.run_steps("db19", steps, call=lambda command, db: called.append(command))
    assert called == ["quote-pdfs -p 19"] and queries[-1] == "UPDATE b;"
    assert "⏭️ Old column: skipped (Odoo 19 database, step for 16, 17)" in capsys.readouterr().out
    queries.clear()
    restore_steps.run_steps("db", [steps[2]])
    assert queries == ["UPDATE b;"]


def test_steps_from_config():
    conf = {"restore": {"extra_steps": [{"label": "Mail off", "sql": "UPDATE x;", "requires": ["x"]},
                                        {"label": "Passwords", "sql": "UPDATE y;", "enabled": False}]}}
    assert restore_steps.steps_from_config(conf) == ([Step("Mail off", "UPDATE x;", ("x",))], ["Passwords"])
    assert restore_steps.steps_from_config({}) == ([], [])
    with pytest.raises(typer.Exit):
        restore_steps.steps_from_config({"restore": {"extra_steps": [{"label": "no sql"}]}})


def test_a_misspelled_key_is_refused(capsys):
    # ignored, it would make a step meant for Odoo 19 run on every version
    with pytest.raises(typer.Exit):
        restore_steps.steps_from_config({"restore": {"extra_steps": [{"sql": "SELECT 1;", "odoo_version": [19]}]}})
    assert "#1 has the key(s) odoo_version, which do not exist" in capsys.readouterr().out


def test_example_config_has_the_usual_steps():
    example = config.load_config(path=Path(__file__).parent.parent / "config.example.toml")
    steps, disabled = restore_steps.steps_from_config(example)
    assert [s.label for s in steps] == ["Mandatory 2FA policy removed", "2FA disabled for all users", "Admin login set to 'admin'",
                                        "All passwords set to 'admin'", "Expiration date set to 2135-12-31"]
    assert disabled == ["Outgoing mail servers disabled"]


def test_no_step_configured_warns(capsys):
    restore_steps.run_steps("db", [])  # no database call: nothing to run
    assert "No sanitize step in config.toml" in capsys.readouterr().out


def test_a_step_can_be_a_command(capsys):
    conf = {"restore": {"extra_steps": [{"label": "Cover set", "command": "set-cover --company 1"},
                                        {"command": "hello", "requires": ["some_table"]},
                                        {"label": "Off", "command": "hello", "enabled": False}]}}
    steps, disabled = restore_steps.steps_from_config(conf)
    assert steps == [Step("Cover set", command="set-cover --company 1"), Step("jsa hello", requires=("some_table",), command="hello")]
    assert disabled == ["Off"]
    with pytest.raises(typer.Exit):  # a step is SQL or a command, not both
        restore_steps.steps_from_config({"restore": {"extra_steps": [{"sql": "SELECT 1;", "command": "hello"}]}})

    # a command that does not exist is refused before the restore starts, and so is the restore itself
    known = ["hello", "restore", "set-cover"]
    restore_steps.check_commands(steps + [Step("sql", "SELECT 1;")], known)
    for command in ("set-covre", "restore"):
        with pytest.raises(typer.Exit):
            restore_steps.check_commands([Step("typo", command=command)], known)
    assert "names the command 'set-covre', which does not exist. Commands: hello, restore, set-cover" in capsys.readouterr().out


def test_sql_steps_in_one_transaction_then_the_commands(monkeypatch, capsys):
    sql, called = [], []
    monkeypatch.setattr(restore_steps.pg, "psql", lambda db, query: sql.append(query) or "")
    monkeypatch.setattr(restore_steps, "_existing_objects", lambda db, steps: set())

    def call(command, db_name):
        called.append((command, db_name))
        return "exit code 1" if command == "fails" else None

    steps = [Step("first", "UPDATE a;"), Step("Cover set", command="set-cover --company 1"), Step("second", "UPDATE b;"),
             Step("Broken", command="fails"), Step("After", command="hello"), Step("Needs sso", command="hello", requires=("sso",))]
    assert restore_steps.run_steps("mydb", steps, call=call) == ["Broken"]
    assert sql == ["UPDATE a;\nUPDATE b;"]  # the SQL steps together, first
    # then the commands, with the database: a failing one does not stop the next, a step whose table is missing is skipped
    assert called == [("set-cover --company 1", "mydb"), ("fails", "mydb"), ("hello", "mydb")]
    out = capsys.readouterr().out
    assert "✅ Cover set" in out and "❌ Broken failed: exit code 1" in out and "⏭️ Needs sso: skipped" in out


# ---------- loading the dump ----------

DUMP = b"""SET client_encoding = 'UTF8';
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
COMMENT ON EXTENSION vector IS 'vector data type';
CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;
CREATE TABLE public.res_partner (
    id integer NOT NULL
);
CREATE TABLE public.res_users (
    id integer NOT NULL
);
COPY public.res_partner (id) FROM stdin;
1
2
\\.
COPY public.res_users (id) FROM stdin;
CREATE TABLE this is data, not a statement
\\.
ALTER TABLE ONLY public.res_partner
    ADD CONSTRAINT res_partner_pkey PRIMARY KEY (id);
CREATE INDEX res_partner_idx ON public.res_partner USING btree (id);
CREATE EXTENSION IF NOT EXISTS ignored_after_tables;
"""


def test_dump_extensions(tmp_path):
    dump = tmp_path / "dump.sql"
    dump.write_bytes(DUMP)
    assert restore_sql.dump_extensions(dump) == ["vector", "uuid-ossp"]


def test_dump_stats_phases():
    stats = restore_sql.DumpStats()
    phases = []
    for line in DUMP.splitlines(keepends=True):
        stats.feed(line)
        phases.append(stats.label())
    assert "schema (2 tables)" in phases
    assert "data (res_partner)" in phases and "data (res_users)" in phases
    assert phases[-1] == "indexes & constraints (2)"
    assert (stats.tables, stats.data_blocks, stats.indexes) == (2, 2, 2)  # data rows are not parsed as statements


def test_progress_display_without_terminal(capsys):
    display = restore_sql.ProgressDisplay(total=2048, tty=False)
    stats = restore_sql.DumpStats()
    display.update(1024, stats)
    display.update(1100, stats)  # same phase, within 10s: not printed
    stats.phase = "data"
    display.update(2048, stats)  # phase changed: printed
    display.finish(stats)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert "50%" in lines[0] and "1.0 KB/2.0 KB" in lines[0]
    assert "100%" in lines[1] and "data" in lines[1]
    assert lines[2].startswith("   SQL executed in")
