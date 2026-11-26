"""The backfill plugin (plugins/backfill): what can be checked without Odoo. The real run: ~/jsa_testenv/backfill_check.sh."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import typer

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "backfill" / "__init__.py"
spec = importlib.util.spec_from_file_location("jsa_plugin_backfill", PLUGIN)
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)


def test_the_sample_is_a_valid_plan():
    plan = backfill.read_plan(PLUGIN.parent / "backfill.toml.sample")
    assert [c["commit"] for c in plan["commits"]] == ["4f2a9c1", "v2.0"]
    assert plan["commits"][1]["event"] == "Release 2.0" and plan["events"][0]["date"] == "2025-09-01"
    assert plan["profiles"] == {} and "modes" not in plan


def test_mistakes_in_the_plan_are_refused(tmp_path, capsys, monkeypatch):
    plan = tmp_path / "backfill.toml"
    for text, why in (("", "no [[commits]] to measure"),
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
    # in a project, its backfill.toml is in its .jsa/ folder
    monkeypatch.setattr(backfill.config, "PROJECT_DIR", tmp_path)
    assert backfill.plan_file() == tmp_path / ".jsa" / "backfill.toml"
    monkeypatch.setattr(backfill.config, "PROJECT_DIR", None)
    assert backfill.plan_file() == PLUGIN.parent / "backfill.toml"
    plan.write_text("[[commits]]\ncommit = 'a'\n[profiles]\n17 = 'odoo17'\n")
    assert backfill.read_plan(plan)["profiles"] == {"17": "odoo17"}


def test_the_odoo_version_of_the_code_and_its_profile(monkeypatch):
    manifests = {"a": {"version": "17.0.1.0.0"}, "b": {"version": "17.0.2.1.0"}, "c": {"version": "19.0.1.0.0"},
                 "d": {"version": "1.0"}, "e": {}}
    assert backfill.odoo_version(manifests) == 17  # the most common
    assert backfill.odoo_version({"x": {"version": "1.2"}}) is None
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


def test_events_are_added_once_and_measured_commits_are_skipped(tmp_path):
    history = tmp_path / "history.jsonl"
    events = [{"date": "2025-09-01", "event": "Release 2.0"}, {"date": "2025-10-01T09:00:00", "event": "Hired"}]
    assert backfill.add_events(history, events) == 2
    assert backfill.add_events(history, events) == 0  # run again: not repeated
    lines = [{"commit": "aaa"}, {"commit": "bbb", "mode": "whole"}, {"mode": "isolated"},
             {"date": "2025-09-01", "event": "x", "commit": "ccc"}]
    assert backfill.measured_commits(lines) == {"aaa", "bbb"}  # bbb: measured before 0.37, in one mode: not again


def test_the_history_follows_backfill_toml(tmp_path):
    wanted, file_events = {"aaa", "bbb"}, {("2025-09-01", "Release 2.0")}
    lines = [
        {"commit": "aaa", "coverage": 50.0},                           # in backfill.toml: kept
        {"commit": "bbb", "mode": "isolated", "coverage": 40.0},       # in it, measured before 0.37: kept
        {"commit": "ccc", "mode": "whole", "coverage": 30.0},          # a commit removed from the file: removed
        {"coverage": 70.0, "date": "2026-11-22"},                      # a run of 'jsa structure -c': kept
        {"date": "2025-09-01", "event": "Release 2.0", "from": "backfill.toml"},   # still in the file: kept
        {"date": "2025-10-01", "event": "Old release", "from": "backfill.toml"},   # removed from the file: removed
        {"date": "2025-11-01", "event": "Added by hand"},                           # never in the file: kept
    ]
    history = tmp_path / "history.jsonl"
    history.write_text("".join(json.dumps(line) + "\n" for line in lines) + "not json, edited by hand\n")
    assert backfill.rewrite_history(history, lambda line: backfill.synced(line, wanted, file_events)) == 2
    kept = history.read_text().splitlines()
    assert [json.loads(text).get("coverage") or json.loads(text).get("event") for text in kept[:-1]] == \
        [50.0, 40.0, 70.0, "Release 2.0", "Added by hand"]
    assert kept[-1] == "not json, edited by hand"  # left as it was
    # an event written by a backfill of before (no "from"), still in the file: adopted, it will go with the file
    assert backfill.synced({"date": "2025-09-01", "event": "Release 2.0"}, wanted, file_events)["from"] == "backfill.toml"


def test_a_measure_added_later_is_counted_on_the_commits_already_measured(tmp_path):
    lines = [{"commit": "aaa", "mode": "whole", "measures": {"TODO": 3}}, {"commit": "aaa", "mode": "isolated"},
             {"commit": "bbb", "mode": "whole", "measures": {"TODO": 2, "sudo": 1}},
             {"commit": "zzz", "mode": "whole"}, {"date": "2025-09-01", "event": "x"}]
    missing = backfill.missing_measures(lines, {"aaa", "bbb"}, ["TODO", "sudo"])
    assert missing == {"aaa": ["TODO", "sudo"]}  # zzz: not in backfill.toml; bbb: complete
    history = tmp_path / "history.jsonl"
    history.write_text("".join(json.dumps(line) + "\n" for line in lines))
    backfill.rewrite_history(history, lambda line: backfill.with_counts(line, {"aaa": {"sudo": 5}}))
    after = [json.loads(text) for text in history.read_text().splitlines()]
    assert after[0]["measures"] == {"TODO": 3, "sudo": 5} and after[1]["measures"] == {"sudo": 5}
    assert after[2] == lines[2] and after[4] == lines[4]


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
    new = git("rev-parse", "HEAD")  # the plugin resolves the names in the repository: in the worktree, HEAD is its own
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
