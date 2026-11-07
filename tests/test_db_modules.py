import pytest
import typer

from jsa.db import databases, modules


def test_full_version():
    assert modules.full_version("1.2", "17.0") == "17.0.1.2"
    assert modules.full_version("17.0.1.2", "17.0") == "17.0.1.2"


def test_pending_upgrades():
    installed = {"base": "17.0.1.3", "a": "17.0.1.0.0", "b": "17.0.2.0", "c": "17.0.1.0"}
    code = {"a": "17.0.1.1.0", "b": "2.0", "c": "17.0.1.0", "not_installed": "1.0", "no_version": ""}
    assert modules.pending_upgrades(installed, code) == [("a", "17.0.1.0.0", "17.0.1.1.0")]


def test_enterprise_modules():
    found = modules.parse_enterprise("account_accountant|t|my_addon,other_addon\ndocuments|t|\nweb_enterprise|f|\n")
    assert [(m.name, m.application, m.needed_by) for m in found] == [
        ("account_accountant", True, ["my_addon", "other_addon"]), ("documents", True, []), ("web_enterprise", False, [])]
    lines = modules.describe_enterprise("db", found)
    assert lines[0] == "💎 3 Odoo enterprise module(s) installed in 'db'"
    assert "1 needed by non-enterprise modules" in lines[1]
    assert lines[2].strip() == "account_accountant  ← my_addon, other_addon"
    assert "documents" in lines[3] and "web_enterprise" in lines[4]
    assert modules.describe_enterprise("db", []) == ["✅ No Odoo enterprise module installed in 'db'"]


def test_remove_deletes_the_snapshot_too(monkeypatch, capsys):
    asked, dropped = [], []
    answer = {"yes": True}
    monkeypatch.setattr(databases.typer, "confirm", lambda text: asked.append(text) or answer["yes"])
    monkeypatch.setattr(databases.pg, "drop_database", lambda db, data_dir: dropped.append(db))
    monkeypatch.setattr(databases.pg, "exists", lambda db: db == "mydb__snapshot")
    databases.remove("mydb", data_dir="/data")
    assert dropped == ["mydb", "mydb__snapshot"] and "and its snapshot?" in asked[-1]
    assert "and its snapshot deleted" in capsys.readouterr().out
    del dropped[:]
    databases.remove("other", data_dir="/data")  # no snapshot
    assert dropped == ["other"] and "snapshot" not in asked[-1] and "snapshot" not in capsys.readouterr().out
    del dropped[:]
    databases.remove("mydb__snapshot", data_dir="/data")  # only the snapshot: the database stays
    assert dropped == ["mydb__snapshot"]
    del dropped[:]
    answer["yes"] = False
    databases.remove("mydb", data_dir="/data")  # cancelled: nothing deleted
    assert dropped == []


def test_clone(monkeypatch, capsys):
    calls, existing = [], {"db1"}
    monkeypatch.setattr(databases.pg, "exists", lambda db: db in existing)
    monkeypatch.setattr(databases.pg, "copy_database", lambda source, target, data_dir: calls.append(("copy", source, target)))
    monkeypatch.setattr(databases.pg, "drop_database", lambda db, data_dir: calls.append(("drop", db)))
    databases.clone("db1", "db2", True, "/data")
    assert calls == [("copy", "db1", "db2"), ("drop", "db2__snapshot"), ("copy", "db2", "db2__snapshot")]
    del calls[:]
    databases.clone("db1", "db2", False, "/data")  # --no-snapshot
    assert calls == [("copy", "db1", "db2")]
    del calls[:]
    capsys.readouterr()
    with pytest.raises(typer.Exit):  # nothing to copy
        databases.clone("nowhere", "db2", True, "/data")
    assert "'nowhere' does not exist" in capsys.readouterr().out
    existing.add("db2")
    with pytest.raises(typer.Exit):  # a database is never replaced by a clone
        databases.clone("db1", "db2", True, "/data")
    assert "'db2' already exists" in capsys.readouterr().out and calls == []


def test_reset_ends_with_the_addons_to_update(monkeypatch):
    shown, calls = [], []
    monkeypatch.setattr(databases.pg, "exists", lambda db: True)
    monkeypatch.setattr(databases.pg, "drop_database", lambda db, data_dir: calls.append(("drop", db)))
    monkeypatch.setattr(databases.pg, "copy_database", lambda source, target, data_dir: calls.append(("copy", source, target)))
    monkeypatch.setattr(databases.modules, "show_pending", lambda db, conf: shown.append(db))
    monkeypatch.setattr(databases.config, "load_config", lambda required: {"codebase": {"addons_roots": ["~/addons"]}})
    databases.reset("mydb", data_dir="/data")
    assert calls == [("drop", "mydb"), ("copy", "mydb__snapshot", "mydb")] and shown == ["mydb"]
    # no addons folder configured: nothing to compare with
    monkeypatch.setattr(databases.config, "load_config", lambda required: {})
    databases.reset("mydb", data_dir="/data")
    assert shown == ["mydb"]
