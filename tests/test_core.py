import importlib.metadata

import pytest
import typer
from typer.testing import CliRunner

from jsa import self_update
from jsa.core import commands, config, plugins, ui

OLD_TYPER = tuple(int(n) for n in importlib.metadata.version("typer").split(".")[:2]) < (0, 16)


def test_load_config(tmp_path):
    missing = tmp_path / "config.toml"
    assert config.load_config(required=False, path=missing) == {}
    with pytest.raises(typer.Exit):
        config.load_config(path=missing)
    missing.write_text("[codebase]\naddons_roots = ['~/x']\n")
    assert config.load_config(path=missing) == {"codebase": {"addons_roots": ["~/x"]}}
    missing.write_text("not = valid = toml")
    with pytest.raises(typer.Exit):
        config.load_config(path=missing)


def test_load_profile(tmp_path, capsys):
    python, odoo_bin = tmp_path / "python", tmp_path / "odoo-bin"
    python.touch()
    odoo_bin.touch()
    conf = {"profiles": {"17": {"python": str(python), "odoo_root": "/odoo", "odoo_bin": str(odoo_bin),
                                "addons_paths": ["/a"]}}}
    profile = config.load_profile("17", conf)
    assert profile["enterprise"] is None and profile["db_prefix"] == "cov"
    with pytest.raises(typer.Exit):
        config.load_profile("19", conf)
    assert "Available profiles: 17" in capsys.readouterr().out


def test_plugins(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "PLUGINS_DIR", tmp_path)
    # "from jsa import pg, config" is what plugins write: it must keep working whatever the folders inside jsa become
    (tmp_path / "good.py").write_text("from jsa import config, pg\n\n\ndef register(app):\n    app.command()(lambda: None)\n")
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')\n")
    (tmp_path / "_private_helper.py").write_text("raise RuntimeError('not a plugin')\n")
    app = typer.Typer()
    files = plugins.find({})
    assert [f.name for f in files] == ["broken.py", "good.py"]
    for f in files:
        plugins.load(app, f)
    assert len(app.registered_commands) == 1  # good one registered, broken one only warned
    assert "broken.py failed to load" in capsys.readouterr().out


def test_a_plugin_can_be_a_folder_with_its_own_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PLUGINS_DIR", tmp_path)
    folder = tmp_path / "with_files"
    folder.mkdir()
    (folder / "settings.toml").write_text('greeting = "hello"\n')  # a file of its own, found next to its code
    (folder / "helpers.py").write_text("def shout(text):\n    return text.upper()\n")
    (folder / "__init__.py").write_text(
        "from pathlib import Path\n\nfrom jsa import config\n\nfrom .helpers import shout\n\n"
        "SETTINGS = config.load_config(path=Path(__file__).parent / 'settings.toml')\n\n\n"
        "def register(app):\n    app.command('greet')(lambda: print(shout(SETTINGS['greeting'])))\n")
    (tmp_path / "not_a_plugin").mkdir()  # a folder without __init__.py
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "single.py").write_text("def register(app):\n    pass\n")
    files = plugins.find({})
    assert files == [tmp_path / "single.py", folder / "__init__.py"]
    assert [plugins.name(f) for f in files] == ["single", "with_files"]
    app = typer.Typer()
    plugins.load(app, folder / "__init__.py")
    assert [c.name for c in app.registered_commands] == ["greet"]
    app.registered_commands[0].callback()  # its own module, its own settings


@pytest.mark.skipif(OLD_TYPER, reason="needs the typer version asked by pyproject.toml")
def test_a_command_can_call_another_one_by_its_name():
    app, seen, results = typer.Typer(), [], []

    @app.command()
    def hello(db: str, note: str = ""):
        seen.append((db, note))

    @app.command()
    def fails(db: str):
        raise typer.Exit(code=3)

    @app.command()
    def crashes(db: str):
        raise RuntimeError("boom")

    @app.command()
    def caller(ctx: typer.Context):
        for command, arguments in (("hello", ["db1"]), ("hello --note 'a b'", ["db2"]), ("fails", ["db"]), ("crashes", ["db"]),
                                   ("unknown", ["db"]), ("hello", [])):
            results.append(commands.call(ctx, command, *arguments))
        results.append(commands.names(ctx))

    assert CliRunner().invoke(app, ["caller"]).exit_code == 0  # what the called commands do never stops the caller
    assert seen == [("db1", ""), ("db2", "a b")]  # written as after 'jsa', the arguments added at the end
    assert results[:2] == [None, None] and results[2] == "exit code 3" and "boom" in results[3]
    assert results[4] == "unknown command 'unknown'" and "missing argument" in results[5].lower()  # no database given
    assert results[6] == ["caller", "crashes", "fails", "hello"]


def test_installed_matches_repo(tmp_path):
    installed, repo = tmp_path / "installed", tmp_path / "repo"
    for d in (installed, repo):
        (d / "db").mkdir(parents=True)
        for package in (d, d / "db"):
            (package / "__init__.py").write_text("")
        (d / "db" / "restore.py").write_text("v1")
    # what Robot Framework runs is not code jsa imports: a non-editable install does not have it
    (repo / "uitests" / "keywords").mkdir(parents=True)
    (repo / "uitests" / "keywords" / "OdooKeywords.py").write_text("keywords")
    assert self_update.installed_matches_repo(installed, repo)
    (repo / "db" / "restore.py").write_text("v2")  # git pull brought new code, the copy install still runs v1
    assert not self_update.installed_matches_repo(installed, repo)
    assert self_update.installed_matches_repo(repo, repo)  # editable install
    # what differs is said: a file deleted from the repo that an install keeps copying, a new file not installed yet
    (installed / "old_module.py").write_text("removed from the repo")
    (repo / "db" / "new_module.py").write_text("new")
    assert self_update.code_differences(installed, repo) == [
        "1 file(s) not in the folder anymore (old_module.py)", "1 file(s) missing (db/new_module.py)",
        "1 file(s) different (db/restore.py)"]


def test_open_command(monkeypatch, tmp_path):
    monkeypatch.setattr(ui.shutil, "which", lambda tool: "/usr/bin/xdg-open" if tool == "xdg-open" else None)
    assert ui.open_command(tmp_path / "report.html") == ["/usr/bin/xdg-open", str(tmp_path / "report.html")]
    monkeypatch.setattr(ui.shutil, "which", lambda tool: None)
    assert ui.open_command(tmp_path / "report.html") is None
