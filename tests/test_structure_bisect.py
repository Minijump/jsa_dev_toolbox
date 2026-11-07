import shutil

import pytest
import typer

from jsa.core.addons import DependencyGraph
from jsa.core.odoo import LogInfo
from jsa.structure import bisect


def test_the_test_is_written_as_in_odoo():
    assert bisect.parse_test("/sale.test_x") == ("sale", "sale.test_x", None, "test_x")
    assert bisect.parse_test("sale.test_x") == ("sale", "sale.test_x", None, "test_x")
    assert bisect.parse_test("/sale:TestOrder.test_x") == ("sale", "sale:TestOrder.test_x", "TestOrder", "test_x")
    assert bisect.parse_test("/sale:TestOrder") == ("sale", "sale:TestOrder", "TestOrder", None)
    for wrong in ("", "/", "/sale", "/sale.test_x,/other", "/sale test", "post_install/sale"):
        with pytest.raises(typer.Exit):
            bisect.parse_test(wrong)


def test_the_file_of_the_test_is_found_in_the_tests_folder(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "__init__.py").write_text("from . import test_a, test_b\n")
    (tmp_path / "common.py").write_text("class Base:\n    def test_x(self):\n        pass\n")  # not a test file for Odoo
    (tmp_path / "test_a.py").write_text("class TestA(Base):\n    def test_x(self):\n        pass\n\n    def test_y(self):\n        pass\n")
    (tmp_path / "test_b.py").write_text("class TestB(Base):\n    def test_y(self):\n        pass\n")
    (tmp_path / "test_broken.py").write_text("def broken(:\n")
    (tmp_path / "sub" / "test_c.py").write_text("class TestC(Base):\n    def test_z(self):\n        pass\n")
    names = lambda *args: [f.name for f in bisect.files_of_test(tmp_path, *args)]
    assert names(None, "test_x") == ["test_a.py"]
    assert names(None, "test_y") == ["test_a.py", "test_b.py"]  # several files have it: Odoo runs both
    assert names("TestB", None) == ["test_b.py"]
    assert names("TestB", "test_x") == ["test_b.py"]  # inherited: its class is enough
    assert names(None, "test_nowhere") == [] and names("TestNowhere", None) == []
    assert bisect.files_of_test(tmp_path / "no_folder", None, "test_x") == []
    # only the file of the test is loaded: another one could need code that older commits do not have
    files = bisect.files_of_test(tmp_path, None, "test_y") + bisect.files_of_test(tmp_path, None, "test_z")
    assert bisect.tests_init(tmp_path, files) == "from . import test_a\nfrom . import test_b\nfrom .sub import test_c\n"


def test_each_commit_gets_the_test_of_the_checkout(tmp_path):
    tests = tmp_path / "copy"
    tests.mkdir()
    (tests / "test_new.py").write_text("new test")
    (tests / "common.py").write_text("helper")
    (tests / "__init__.py").write_text("from . import test_new, test_needing_newer_code\n")
    module = tmp_path / "tree" / "mine"
    (module / "tests").mkdir(parents=True)
    (module / "__manifest__.py").write_text("{}")
    (module / "tests" / "test_old.py").write_text("what this commit had")
    search = bisect.Search(tmp_path / "tree", "mine", module, "mine.test_x", tests, "from . import test_new\n")
    assert search.prepare() is None
    assert sorted(f.name for f in (module / "tests").iterdir()) == ["__init__.py", "common.py", "test_new.py"]
    assert (module / "tests" / "__init__.py").read_text() == "from . import test_new\n"
    # no tests/ folder at this commit: it is simply added
    shutil.rmtree(module / "tests")
    assert search.prepare() is None and (module / "tests" / "test_new.py").exists()
    # the module itself does not exist yet: nothing is created, the commit cannot tell
    (module / "__manifest__.py").unlink()
    shutil.rmtree(module / "tests")
    assert "does not exist" in search.prepare() and not (module / "tests").exists()


def test_what_is_in_the_repository_goes_to_the_worktree(tmp_path):
    repo, tree, other = tmp_path / "repo", tmp_path / "tree", tmp_path / "other"
    profile = {"python": "/venv/python", "odoo_root": str(other), "odoo_bin": str(other / "odoo-bin"), "enterprise": None,
               "addons_paths": [str(repo), str(repo / "oca" / "web"), str(other / "addons")]}
    moved = bisect.in_worktree(profile, repo.resolve(), tree)
    assert moved["addons_paths"] == [str(tree), str(tree / "oca" / "web"), str(other / "addons")]
    assert moved["odoo_root"] == str(other) and moved["python"] == "/venv/python" and moved["enterprise"] is None
    # searching in Odoo itself: the Odoo that runs is the one of each commit
    odoo = bisect.in_worktree(profile, other.resolve(), tree)
    assert odoo["odoo_bin"] == str(tree / "odoo-bin") and odoo["odoo_root"] == str(tree)
    assert odoo["addons_paths"][2] == str(tree / "addons")


def test_only_what_no_commit_changes_is_installed_once():
    graph = DependencyGraph({"mine": ["sale", "mine_base"], "mine_base": ["mail"], "sale": ["mail"], "mail": ["base"],
                             "oca_on_mine": ["mine_base"], "mine2": ["oca_on_mine"]})
    assert bisect.installed_once("mine", graph, {"mine", "mine_base"}) == ["base", "mail", "sale"]
    # a module of another repository depending on the bisected one cannot be installed once
    assert bisect.installed_once("mine2", graph, {"mine2", "mine_base"}) == ["base", "mail"]
    # searching in Odoo itself: nothing is installed once
    assert bisect.installed_once("mine", graph, {"base", "mail", "sale", "mine", "mine_base"}) == []


def test_verdict_of_a_commit():
    info = LogInfo()
    assert bisect.verdict(info, "mine")[0] == bisect.UNTESTABLE  # no test ran: never taken for a success
    info.tests["mine"] = 2
    assert bisect.verdict(info, "mine") == (bisect.PASSES, "passes (2 test(s))")
    info.failed_tests["mine"] = ["T.test_x"]
    assert bisect.verdict(info, "mine") == (bisect.FAILS, "fails: T.test_x")
    assert bisect.verdict(LogInfo(loaded=["mine"], registry_failed=True), "mine")[0] == bisect.UNTESTABLE
    assert bisect.verdict(LogInfo(crashed=True), "mine")[0] == bisect.UNTESTABLE
    assert (bisect.PASSES, bisect.FAILS, bisect.UNTESTABLE) == ("good", "bad", "skip")  # what git bisect is told


A, B = "a" * 40, "b" * 40


def test_what_git_bisect_answers():
    assert bisect.parse_step("Bisecting: 3 revisions left to test after this (roughly 2 steps)\n[abc] subject\n") == ("next", [])
    assert bisect.steps_left("Bisecting: 3 revisions left to test after this (roughly 2 steps)\n") == ", about 2 left"
    assert bisect.steps_left("Bisecting: a merge base must be tested\n") == ""
    assert bisect.parse_step(f"{A} is the first bad commit\ncommit {A}\nAuthor: x\n") == ("found", [A])
    skipped = f"There are only 'skip'ped commits left to test.\nThe first bad commit could be any of:\n{A}\n{B}\nWe cannot bisect more!\n"
    assert bisect.parse_step(skipped) == ("skipped", [A, B])
    assert bisect.parse_step("fatal: something else") == ("error", [])


def test_nothing_starts_without_the_module_or_the_test(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(bisect.config, "load_config", lambda: {})
    monkeypatch.setattr(bisect.config, "load_profile", lambda profile, conf: {})
    monkeypatch.setattr(bisect, "profile_addons", lambda profile: {})
    with pytest.raises(typer.Exit):
        bisect.bisect("/mine.test_x", "abc", None, "19", False)
    assert "No module mine in the addons paths" in capsys.readouterr().out
    monkeypatch.setattr(bisect, "profile_addons", lambda profile: {"mine": tmp_path})
    with pytest.raises(typer.Exit):
        bisect.bisect("/mine.test_x", "abc", None, "19", False)
    assert "No test /mine.test_x" in capsys.readouterr().out
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("class T:\n    def test_x(self):\n        pass\n")
    with pytest.raises(typer.Exit):
        bisect.bisect("/mine.test_x", "abc", None, "19", False)
    assert "is not in a git repository" in capsys.readouterr().out
