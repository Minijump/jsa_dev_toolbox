"""jsa backfill (jsa/structure/backfill.py): what can be checked without Odoo. The real run: ~/jsa_testenv/backfill_check.sh."""
import subprocess
from pathlib import Path

import pytest
import typer

from jsa.core import config
from jsa.structure import backfill, codebase, report

ROOT = Path(__file__).resolve().parent.parent


def test_the_sample_is_a_valid_plan():
    plan = backfill.read_plan(ROOT / "backfill.toml.sample")
    assert [c["commit"] for c in plan["commits"]] == ["4f2a9c1", "v2.0"]
    assert plan["commits"][1]["event"] == "Release 2.0" and plan["events"][0]["date"] == "2025-09-01"
    assert plan["profiles"] == {} and "modes" not in plan


def test_mistakes_in_the_plan_are_refused(tmp_path, capsys, monkeypatch):
    plan = tmp_path / "backfill.toml"
    for text, why in (("", "no [[commits]] to record"),
                      ("[[commits]]\ncomit = 'abc'\n", "has the key(s) comit, which do not exist"),
                      ("[[commits]]\nevent = 'x'\n", "needs commit = "),
                      ("[[commits]]\ncommit = 'a'\n[[events]]\ndate = 'soon'\nevent = 'x'\n", "[[events]] #1 needs date"),
                      ("[[commits]]\ncommit = 'a'\n[profiles]\nseventeen = '17'\n", "[profiles] gives a profile"),
                      ("[[commit]]\ncommit = 'a'\n", "commit do(es) not exist"),
                      ("[[commits]]\ncommit = 'a'\nevent = ''\n", "event must be a text"),
                      # before 0.37: each commit is now measured per module and whole
                      ("modes = ['whole']\n[[commits]]\ncommit = 'a'\n", "modes no longer exists (jsa 0.37)")):
        plan.write_text(text)
        with pytest.raises(typer.Exit):
            backfill.read_plan(plan)
        assert why in capsys.readouterr().out
    with pytest.raises(typer.Exit):
        backfill.read_plan(tmp_path / "missing.toml")
    out = capsys.readouterr().out
    assert "missing.toml: copy " in out and "backfill.toml.sample there" in out
    # at the root of the toolbox; in a project, in its .jsa/ folder
    monkeypatch.setattr(config, "TOOLBOX_DIR", tmp_path / "toolbox")
    monkeypatch.setattr(config, "PROJECT_DIR", None)
    assert backfill.plan_file() == tmp_path / "toolbox" / "backfill.toml"
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    assert backfill.plan_file() == tmp_path / ".jsa" / "backfill.toml"
    # where the plugin of jsa 0.39 read it: said where it goes now
    monkeypatch.setattr(config, "PROJECT_DIR", None)
    (tmp_path / "toolbox" / "plugins" / "backfill").mkdir(parents=True)
    (tmp_path / "toolbox" / "plugins" / "backfill" / "backfill.toml").write_text("[[commits]]\ncommit = 'a'\n")
    with pytest.raises(typer.Exit):
        backfill.read_plan()
    assert "backfill is part of jsa since 0.40: move" in capsys.readouterr().out
    plan.write_text("[[commits]]\ncommit = 'a'\n[profiles]\n17 = 'odoo17'\n")
    assert backfill.read_plan(plan)["profiles"] == {"17": "odoo17"}


def test_the_odoo_version_of_the_code_and_its_profile(monkeypatch):
    manifests = {"a": {"version": "17.0.1.0.0"}, "b": {"version": "17.0.2.1.0"}, "c": {"version": "19.0.1.0.0"},
                 "d": {"version": "1.0"}, "e": {}}
    assert backfill.odoo.written_for(manifests) == 17  # the most common
    assert backfill.odoo.written_for({"x": {"version": "1.2"}}) is None
    conf = {"profiles": {"old": {}, "new": {}, "new_too": {}}}
    versions = {"old": 17, "new": 19, "new_too": 19}
    monkeypatch.setattr(backfill.config, "load_profile", lambda name, conf: {"name": name})
    monkeypatch.setattr(backfill.odoo, "major_version", lambda profile: versions[profile["name"]])
    assert backfill.profile_for(17, {}, conf) == "old"  # the only profile running Odoo 17
    with pytest.raises(ValueError, match=r"several profiles \(new, new_too\) of config.toml run Odoo 19"):
        backfill.profile_for(19, {}, conf)
    assert backfill.profile_for(19, {"19": "new_too"}, conf) == "new_too"  # said in backfill.toml
    with pytest.raises(ValueError, match="no profile of config.toml run Odoo 16"):
        backfill.profile_for(16, {}, conf)
    with pytest.raises(ValueError, match="no profile 'typo' in config.toml"):
        backfill.profile_for(19, {"19": "typo"}, conf)
    with pytest.raises(ValueError, match="no manifest says"):
        backfill.profile_for(None, {}, conf)


def test_the_recorded_commits(tmp_path):
    report.save_run(tmp_path, {"commit": "b" * 40, "code_date": "2025-09-01T10:00:00", "coverage": 50.0})
    report.save_run(tmp_path, {"commit": "a" * 40, "code_date": "2025-06-01T10:00:00", "coverage": 40.0,
                               "measures": {"TODO": 3}, "security": {"eval": ["x"]}})
    (report.runs_folder(tmp_path) / "broken.json").write_text("not json")
    runs = report.load_runs(tmp_path)
    assert [r["commit"][0] for r in runs] == ["a", "b"]  # in the order of their code, whatever the file names
    assert (report.runs_folder(tmp_path) / f"{'a' * 12}.json").exists()
    # measured again: replaced, never twice
    report.save_run(tmp_path, {**runs[1], "coverage": 52.0})
    assert [r["coverage"] for r in report.load_runs(tmp_path)] == [40.0, 52.0]
    assert backfill.missing_measures(runs, ["TODO", "sudo"]) == {"a" * 40: ["sudo"], "b" * 40: ["TODO", "sudo"]}
    assert backfill.missing_security(runs) == {"b" * 40}  # recorded before jsa had the review
    assert backfill.with_event(runs[0], "Release 2.0")["event"] == "Release 2.0"
    assert "event" not in backfill.with_event({**runs[0], "event": "old"}, None)  # the event follows backfill.toml
    report.forget_run(tmp_path, "a" * 40)
    assert [r["commit"][0] for r in report.load_runs(tmp_path)] == ["b"]
    plan = tmp_path / "backfill.toml"
    plan.write_text('[[commits]]\ncommit = "x"\n[[events]]\ndate = "2025-07-01"\nevent = "Hired"\n'
                    '[[events]]\ndate = "soon"\nevent = "broken"\n')
    assert report.plan_events(plan) == [{"date": "2025-07-01", "event": "Hired"}]
    assert report.plan_events(tmp_path / "missing.toml") == []


def test_the_branch_of_the_code(tmp_path):
    repo = tmp_path / "repo"
    (repo / "addons" / "a").mkdir(parents=True)
    git = lambda *args: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                                       capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (repo / "addons" / "a" / "__manifest__.py").write_text("{}")
    git("add", ".")
    git("commit", "-q", "-m", "one")
    first = git("rev-parse", "HEAD")
    git("checkout", "-q", "-b", "feature")
    (repo / "addons" / "a" / "x.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "two")
    branch = codebase.branch_of([repo / "addons"])
    assert branch["branch"] == "feature" and branch["commit"] == git("rev-parse", "HEAD") and not branch["dirty"]
    assert branch["commits"] == {first, branch["commit"]}
    git("checkout", "-q", "main")
    assert codebase.branch_of([repo / "addons"])["commits"] == {first}  # the commit of the feature is not in main
    (repo / "addons" / "a" / "__manifest__.py").write_text("{'x': 1}")
    assert codebase.branch_of([repo / "addons"])["dirty"]
    assert codebase.branch_of([tmp_path / "elsewhere"]) is None  # not in git


def test_the_worktree_moves_through_the_commits_and_leaves_the_checkout_alone(tmp_path):
    repo = tmp_path / "repo"
    (repo / "addons" / "a").mkdir(parents=True)
    git = lambda *args: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                                       capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q")
    (repo / "addons" / "a" / "__manifest__.py").write_text("{'version': '17.0.1.0.0'}")
    git("add", ".")
    git("commit", "-q", "-m", "17")
    old = git("rev-parse", "HEAD")
    (repo / "addons" / "a" / "__manifest__.py").write_text("{'version': '19.0.1.0.0'}")
    git("commit", "-q", "-am", "19")
    new = git("rev-parse", "HEAD")  # names are resolved in the repository: in the worktree, HEAD is its own
    (repo / "mine.txt").write_text("work in progress")  # the user's checkout: never touched
    assert backfill.repository([repo / "addons"]) == repo.resolve()
    assert backfill.resolve(repo, "HEAD~1") == old
    with pytest.raises(typer.Exit):
        backfill.resolve(repo, "no_such_commit")
    with backfill.worktree(repo) as tree:
        backfill.checkout(tree, old)
        assert "17.0" in (tree / "addons" / "a" / "__manifest__.py").read_text()
        (tree / "addons" / "a" / "__pycache__").mkdir()  # left by Odoo: removed before the next commit
        backfill.checkout(tree, new)
        assert "19.0" in (tree / "addons" / "a" / "__manifest__.py").read_text()
        assert not (tree / "addons" / "a" / "__pycache__").exists()
    assert not tree.exists() and len(git("worktree", "list").splitlines()) == 1
    assert (repo / "mine.txt").read_text() == "work in progress" and "19.0" in (repo / "addons" / "a" / "__manifest__.py").read_text()
    assert backfill.commit_date(repo, old)[:4].isdigit()
