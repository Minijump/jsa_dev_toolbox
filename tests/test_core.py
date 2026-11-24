import importlib.metadata
import sys
from pathlib import Path

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
    assert profile["enterprise"] is None and profile["db_prefix"] == "cov" and profile["addons_paths"] == [str(Path("/a"))]
    with pytest.raises(typer.Exit):
        config.load_profile("19", conf)
    assert "Available profiles: 17" in capsys.readouterr().out
    # odoo_bin and addons_paths can be left out: odoo-bin of odoo_root; addons_roots, enterprise, odoo_root/addons
    root = tmp_path / "odoo"
    root.mkdir()
    (root / "odoo-bin").touch()
    short = {"codebase": {"addons_roots": ["/mine", "/oca"]},
             "profiles": {"19": {"python": str(python), "odoo_root": str(root), "enterprise": "/ent"}}}
    profile = config.load_profile("19", short)
    assert profile["odoo_bin"] == str(root / "odoo-bin")
    assert profile["addons_paths"] == [str(Path(p)) for p in ("/mine", "/oca", "/ent", root / "addons")]
    with pytest.raises(typer.Exit):
        config.load_profile("19", {"profiles": {"19": {"python": str(python)}}})
    assert "missing required keys: odoo_root" in capsys.readouterr().out


class Screen:
    """What a terminal shows, for what the board writes: text, new lines, \\x1b[<n>F (up n lines), \\x1b[J (clear
    to the end of the screen)."""

    def __init__(self):
        self.rows, self.row, self.col = [""], 0, 0
        self.encoding, self.errors = "utf-8", "strict"

    def write(self, text):
        i = 0
        while i < len(text):
            if text.startswith("\x1b[", i):
                end = i + 2
                while not text[end].isalpha():
                    end += 1
                code, arg = text[end], text[i + 2:end]
                if code == "F":
                    self.row, self.col = max(0, self.row - int(arg or 1)), 0
                elif code == "J":
                    self.rows = self.rows[:self.row] + [self.rows[self.row][:self.col]]
                i = end + 1
                continue
            char = text[i]
            if char == "\n":
                self.row, self.col = self.row + 1, 0
                if self.row == len(self.rows):
                    self.rows.append("")
            else:
                line = self.rows[self.row]
                self.rows[self.row] = line[:self.col] + char + line[self.col + 1:]
                self.col += 1
            i += 1
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return True

    @property
    def shown(self):
        return [row for row in self.rows if row]


def test_the_board_stays_at_the_bottom_the_output_scrolls_above(monkeypatch):
    screen = Screen()
    monkeypatch.setattr(sys, "stdout", screen)
    clock = {'now': 10}
    with ui.board(tty=True) as board:
        board.set('backfill', "📍 Backfill 1/3")
        board.set('odoo', lambda: f"🧪 whole · {clock['now']}s\n   ▸ TestX.test_y")
        print("first message")
        sys.stdout.write("in two ")
        sys.stdout.write("parts\n")
        clock['now'] = 42
        board.redraw()  # what the ticking does every second
        assert screen.shown == ["first message", "in two parts", "📍 Backfill 1/3", "🧪 whole · 42s", "   ▸ TestX.test_y"]
        board.remove('odoo')
        board.set('backfill', "📍 Backfill 2/3")  # its place does not change
        typer.echo("second message")  # what jsa prints with
        assert screen.shown == ["first message", "in two parts", "second message", "📍 Backfill 2/3"]
    assert screen.shown == ["first message", "in two parts", "second message"]  # gone at the end
    assert sys.stdout is screen and ui.Board.shown is None
    with ui.board(tty=False) as nothing:  # not a terminal (a log file): no board, the output unchanged
        assert nothing is None and sys.stdout is screen
    assert ui.duration(42) == "42s" and ui.duration(725) == "12m05s" and ui.duration(8040) == "2h14m"


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
