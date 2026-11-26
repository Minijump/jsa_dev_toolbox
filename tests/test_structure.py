import os
import subprocess
from datetime import datetime, timezone

import pytest
import typer

from jsa.core import addons, config, odoo
from jsa.structure import codebase, report, report_html
from jsa.structure.coverage import NO_REPORT, NOT_INSTALLABLE, Result


def make_addon(root, name, manifest="{'depends': ['base']}"):
    addon = root / name
    (addon / "models").mkdir(parents=True)
    (addon / "__manifest__.py").write_text(manifest)
    return addon


def test_manifest_and_loc(tmp_path):
    addon = make_addon(tmp_path, "my_addon", "# comment\n{'name': 'x', 'depends': ['sale'], 'installable': False}")
    (addon / "models" / "a.py").write_text("a = 1\nb = 2\n")  # 3 lines (counts the trailing one)
    (addon / "views.xml").write_text("<odoo/>")
    (addon / "tests").mkdir()
    (addon / "tests" / "test_a.py").write_text("ignored\n" * 50)
    (addon / "README.md").write_text("ignored\n" * 50)
    manifest = addons.read_manifest(addon)
    assert manifest["depends"] == ["sale"] and manifest["installable"] is False
    # a.py: 3, views.xml: 1, __manifest__.py: 2 (tests/ and non-code files are ignored)
    assert codebase.count_loc(addon) == 6


def test_find_addons(tmp_path):
    make_addon(tmp_path, "b")
    make_addon(tmp_path, "a")
    (tmp_path / "not_an_addon").mkdir()
    assert [a.name for a in addons.find_addons([tmp_path, tmp_path / "missing"])] == ["a", "b"]


def test_a_manifest_that_cannot_be_read_is_left_out(tmp_path, capsys):
    make_addon(tmp_path, "good")
    make_addon(tmp_path, "typo", "{'depends': ['base'],,}")
    assert addons.read_manifests(addons.find_addons([tmp_path])) == {"good": {"depends": ["base"]}}
    assert "typo: its __manifest__.py cannot be read" in capsys.readouterr().out
    addons.read_manifests(addons.find_addons([tmp_path]), warn=False)
    assert capsys.readouterr().out == ""


def test_fingerprints_follow_custom_dependencies(tmp_path):
    a, b, c = make_addon(tmp_path, "a"), make_addon(tmp_path, "b"), make_addon(tmp_path, "c")
    graph = addons.DependencyGraph({"a": ["base"], "b": ["a"], "c": ["base"]}, custom={"a", "b", "c"})
    paths = {"a": a, "b": b, "c": c}
    before = codebase.fingerprints(paths, graph)
    (a / "models" / "x.py").write_text("changed = True")
    after = codebase.fingerprints(paths, graph)
    assert before["a"] != after["a"] and before["b"] != after["b"]  # b depends on a
    assert before["c"] == after["c"]
    assert codebase.reusable({"coverage": 80, "fingerprint": "x", "coverage_mode": "whole"},
                             {"fingerprint": "x", "coverage_mode": "whole"})
    assert not codebase.reusable({"coverage": 80, "fingerprint": "x", "coverage_mode": "whole"},
                                 {"fingerprint": "x", "coverage_mode": "isolated"})
    assert not codebase.reusable({"coverage": None, "fingerprint": "x"}, {"fingerprint": "x"})


OLD = {"a": {"loc": 10, "coverage": 50, "depends": ["base"], "tests": 4, "failed": ["T.old"], "statements": 100, "covered": 50},
       "b": {"loc": 5, "coverage": 80, "depends": [], "tests": 2, "failed": [], "statements": 10, "covered": 8},
       "gone": {"loc": 1, "coverage": None, "depends": []}}
NEW = {"a": {"loc": 15, "coverage": 60, "depends": ["base", "sale"], "tests": 5, "failed": ["T.new"], "statements": 100,
             "covered": 60, "files": {"models/x.py": "3-5"}},
       "b": {"loc": 5, "coverage": NOT_INSTALLABLE, "depends": ["a"], "note": "install failed"},
       "c": {"loc": 3, "coverage": 100, "depends": [], "tests": 0, "failed": [], "statements": 300, "covered": 300},
       "new": {"loc": 3, "coverage": None, "depends": []}}


def test_status_and_summary():
    assert [report.status(NEW[n]) for n in ("a", "b", "c", "new")] == ["failing", "not_installable", "no_tests", "not_measured"]
    summary = report.summarize(NEW)
    assert summary == {"addons": 4, "loc": 26, "measured": 3, "coverage": 90.0,  # 360 / 400: weighted by size
                       "tests": 5, "failed": 1, "not_installable": 1, "no_tests": 1, "enterprise": None,
                       "security": None,  # a state made before the security review
                       "coverage_whole": None, "failed_whole": None, "not_installable_whole": None}  # nor whole
    assert report.summarize({})["coverage"] is None
    # whole: all the addons together, weighted by size too
    whole = {"a": {**NEW["a"], "whole": {"coverage": 75, "statements": 100, "covered": 75, "failed": ["T.w"]}},
             "c": {**NEW["c"], "whole": {"coverage": NOT_INSTALLABLE, "statements": 300, "covered": 0, "failed": []}}}
    assert {k: v for k, v in report.summarize(whole).items() if k.endswith("_whole")} == {
        "coverage_whole": 18.8, "failed_whole": 1, "not_installable_whole": 1}
    assert report.status(whole["c"]) == "failing"  # installs alone, not with all the addons


def test_enterprise_dependencies(tmp_path):
    (tmp_path / "ent" / "web_studio").mkdir(parents=True)
    (tmp_path / "ent" / "web_studio" / "__manifest__.py").write_text("{'license': 'OEEL-1'}")
    conf = {"profiles": {"17": {"enterprise": str(tmp_path / "ent")}, "19": {"enterprise": str(tmp_path / "missing")}}}
    assert codebase.enterprise_modules(conf) == {"web_studio"}
    assert codebase.enterprise_modules({"profiles": {"17": {}}}) is None  # no enterprise folder: not checked

    old = {"a": {"depends": ["web_studio"], "enterprise": []}, "b": {"depends": ["a"], "enterprise": []}}
    new = {"a": {"depends": ["web_studio"], "enterprise": ["web_studio"]}, "b": {"depends": ["a"], "enterprise": []},
           "c": {"depends": [], "enterprise": []}}
    assert report.enterprise_dependencies(new) == {"a": {"web_studio": []}, "b": {"web_studio": ["a"]}}  # b: through a
    assert report.summarize(new)["enterprise"] == 1  # direct dependencies only
    assert report.compute_diff(old, new)["enterprise_added"] == [("a", ["web_studio"])]
    never_checked = {"a": {"depends": ["web_studio"]}}
    assert report.compute_diff(never_checked, new)["enterprise_added"] == []  # first check: not a change
    html = report_html.render(new, report.summarize(new), None, None, None, [], {}, {"generated_at": "2026-01-01T10:00:00"})
    for expected in ('<h2>Dependencies on Odoo enterprise</h2><span class="psum">1 modules, 2 addons</span>', "through a",
                     "Needs Odoo enterprise"):
        assert expected in html


def test_problem_with_all_the_addons_is_failing():
    entry = {"coverage": 70, "tests": 2, "failed": [], "whole": {"coverage": NOT_INSTALLABLE, "note": "install failed (x)"}}
    other = {"coverage": 80, "tests": 2, "failed": [], "whole": {"coverage": 90, "tests": 2, "failed": ["T.with_a"]}}
    assert report.status(entry) == report.status(other) == "failing"
    state = {"a": {**entry, "loc": 1, "depends": []}, "b": {**other, "loc": 1, "depends": ["a"]}}
    page = report_html.render(state, report.summarize({}), None, None, None, [], {},
                              {"generated_at": "2026-01-01T10:00:00",
                               "standard_tests": {"date": "2026-01-01T10:00:00",
                                                  "modules": {"sale": {"tests": 40, "failed": ["TestSale.test_a"]}}}})
    for expected in ("cannot be installed with all the addons (install failed (x))", "Tests of the standard modules",
                     "with all the addons (whole): <code>T.with_a</code>", "Failing only with all the addons (whole)",
                     "<b>✔</b>2 passed</span><span class=\"whole bad\"", ">whole: 1 failed</span>", ">whole: not installable</span>",
                     "80%</span><span class=\"whole\" title=\"With all the addons installed and tested together\">whole 90%</span>",
                     "40 tests in 1 standard modules", "TestSale.test_a", "(standard module, with your addons)"):
        assert expected in page


def test_compute_diff():
    diff = report.compute_diff(OLD, NEW)
    assert diff["added"] == ["c", "new"] and diff["removed"] == ["gone"]
    assert diff["could_to_cant"] == ["b"]
    assert diff["metrics_changed"] == [("a", 5, 10, 50, 60)]
    assert diff["newly_failing"] == [("a", ["T.new"])] and diff["fixed"] == [("a", ["T.old"])]
    assert diff["deps_changed"] == [("a", ["sale"], []), ("b", ["a"], [])]
    assert diff["not_comparable"] == []


def test_compute_diff_compares_only_measures_taken_the_same_way():
    old = {"a": {"loc": 10, "coverage": 80, "failed": [], "coverage_rules": 5, "odoo": 19},
           "b": {"loc": 10, "coverage": 50, "failed": ["T.x"], "coverage_rules": 6, "odoo": 19},
           "c": {"loc": 10, "coverage": 70, "failed": [], "coverage_rules": 6, "odoo": 17}}
    new = {"a": {"loc": 12, "coverage": 60, "failed": ["T.y"], "coverage_rules": 6, "odoo": 19},  # another version of jsa
           "b": {"loc": 10, "coverage": 55, "failed": [], "coverage_rules": 6, "odoo": 19},
           "c": {"loc": 10, "coverage": NOT_INSTALLABLE, "failed": [], "coverage_rules": 6, "odoo": 19}}  # another Odoo
    diff = report.compute_diff(old, new)
    assert diff["not_comparable"] == ["a"]  # another Odoo version compares (c), measures of other rules do not (a)
    assert diff["metrics_changed"] == [("a", 2, None, 80, 60), ("b", 0, 5, 50, 55)]  # a: its lines only, not its coverage
    assert diff["newly_failing"] == [] and diff["fixed"] == [("b", ["T.x"])] and diff["could_to_cant"] == ["c"]
    page = report_html._attention(new, report.summarize(new), report.summarize(old), diff, "2026-10-25T10:00:00")
    assert '<span class="tag">new</span><strong>c</strong>: could not be installed' in page  # not installable anymore
    # a: measured by another version of jsa, not compared: failing, but not said new; no coverage down
    assert "Coverage down" not in page and "<code>T.y</code>" in page and '<span class="tag">new</span><code>T.y</code>' not in page
    assert "1 test(s) fixed (b)" in page and "since the report of 2026-10-25 10:00" in page


def test_dot_with_and_without_standard_modules():
    deps = {"a": ["base", "sale"], "b": ["a"]}
    custom, everything = report.build_dot({"a": 10, "b": 5}, deps), report.build_dot({"a": 10, "b": 5}, deps, with_standard=True)
    assert '"b" -> "a";' in custom and '"sale"' not in custom
    assert '"a" -> "sale";' in everything and '"sale" [shape=box' in everything


def test_duplicate_links():
    # a -> b -> c: the link a -> c adds nothing to the picture, and so does a -> sale (a -> b -> sale)
    deps = {"a": ["b", "c", "sale"], "b": ["c", "sale"], "c": ["base"], "d": ["c", "account"]}
    assert report.duplicate_links(deps) == {("a", "c"), ("a", "sale")}
    # longer paths count too: x -> y -> z -> w makes x -> w a duplicate
    assert report.duplicate_links({"x": ["y", "w"], "y": ["z"], "z": ["w"]}) == {("x", "w")}
    # the dependencies of the standard modules are not drawn: sale -> account does not make a -> account a duplicate
    assert report.duplicate_links({"a": ["sale", "account"]}) == set()
    # a loop between addons (not installable, but drawn): what is reachable stays reachable
    looped = {"p": ["q", "r"], "q": ["p", "r"]}
    duplicates = report.duplicate_links(looped)
    assert len(duplicates) == 1 and duplicates <= {("p", "r"), ("q", "r")}


def test_dot_hides_or_dots_the_duplicate_links():
    deps, loc = {"a": ["b", "c"], "b": ["c"], "c": []}, {"a": 1, "b": 1, "c": 1}
    duplicates = report.duplicate_links(deps)
    hidden = report.build_dot(loc, deps, duplicates=duplicates)
    shown = report.build_dot(loc, deps, duplicates=duplicates, show_duplicates=True)
    assert '"a" -> "c"' not in hidden and '"a" -> "b";' in hidden and '"b" -> "c";' in hidden
    assert '"a" -> "c" [style=dotted];' in shown and '"a" -> "b";' in shown
    page = report_html.render({}, report.summarize({}), None, None, None, [],
                              {"custom": "<svg>1</svg>", "all": "<svg>2</svg>", "custom_dup": "<svg>3</svg>",
                               "all_dup": "<svg>4</svg>", "duplicates": ["a->c"]}, {"generated_at": "2026-01-01T10:00:00"})
    for expected in ('id="graph-custom"><svg>1', 'id="graph-all-dup"><svg>4', 'id="dup"', '"duplicates": ["a->c"]'):
        assert expected in page


def test_one_trend_two_lines_and_the_old_lines_of_the_history(tmp_path):
    run = lambda day, version, coverage, whole, **more: {"date": f"2026-10-{day:02d}T10:00", "odoo": version,
                                                         "coverage": coverage, "coverage_whole": whole, "failed": 1,
                                                         "failed_whole": 2, "not_installable": 0, "loc": 100, **more}
    # before 0.37, a run had a mode: kept as it is in the history, read as the values of that way of measuring; the
    # two lines of one commit (jsa backfill measured it in modes whole and isolated) are one run
    old = lambda day, mode, coverage, **more: {"date": f"2026-10-{day:02d}T10:00", "mode": mode, "odoo": 17,
                                              "coverage": coverage, "failed": 0, "not_installable": 0, "loc": 90, **more}
    history = [old(1, "whole", 50.0), old(2, "isolated", 40.0, commit="abc"), old(2, "whole", 55.0, commit="abc"),
               old(3, "both", 42.0), run(4, 19, 60.0, 70.0), run(5, 19, 61.0, 72.0)]
    runs = report.runs_and_events(history)[0]
    assert [(r.get("coverage"), r.get("coverage_whole")) for r in runs] == [
        (None, 50.0), (40.0, 55.0), (42.0, None), (60.0, 70.0), (61.0, 72.0)]
    assert history[0]["coverage"] == 50.0  # the history itself is not changed
    page = report_html._trend(history)
    assert "<select" not in page and "series" not in page  # one trend
    coverage = page[page.index('data-chart="coverage"'):page.index('data-chart="loc"')]
    zoomed = coverage[:coverage.index('class="chart full"')]  # each chart drawn twice: zoomed (shown) and from 0
    assert zoomed.count('class="dot"') == 4 and zoomed.count('class="dot l1"') == 4  # per module, whole (dashed)
    assert zoomed.count("<polyline") == 2 and '<polyline class="l1"' in zoomed
    assert '<span class="tnow">61%</span><span class="tnote">whole 72%</span>' in coverage  # the last values
    assert '<span class="lines"><span><i></i>per module</span><span><i class="l1"></i>whole</span></span>' in coverage
    assert "<title>2026-10-04 10:00 · Odoo 19: per module 60%, whole 70%</title>" in zoomed  # both in its tooltip
    assert "<title>2026-10-01 10:00 · Odoo 17: whole 50%</title>" in zoomed
    assert "<title>Odoo 17 → 19</title>" in coverage  # every Odoo version on the same curve, a line where it changes
    loc = page[page.index('data-chart="loc"'):page.index('data-chart="failed"')]
    assert '<span class="lines">' not in loc and loc.count("<polyline") == 2  # one line (drawn twice: zoomed, from 0)
    assert report_html._trend([run(1, 19, 70.0, 75.0)]) == ""  # one run: no trend yet
    release = tmp_path / "odoo" / "release.py"
    release.parent.mkdir()
    release.write_text("version_info = (19, 0, 0, FINAL, 0, '')\n")
    assert odoo.major_version({"odoo_root": str(tmp_path)}) == 19
    release.write_text("version_info = ('saas~18', 3, 0, 'final', 0, '')\n")
    assert odoo.major_version({"odoo_root": str(tmp_path)}) == 18
    assert odoo.major_version({"odoo_root": str(tmp_path / "nowhere")}) is None


def test_a_chart_of_two_lines_keeps_their_last_labels_apart():
    chart = report_html.line_chart("Coverage", [("a", (60.0, 60.5)), ("b", (61.0, 61.0))], "%", 100, True,
                                   names=report_html.LINES)
    ys = [float(part.split('y="')[1].split('"')[0]) for part in chart.split('<text class="last')[1:]]
    assert len(ys) == 2 and abs(ys[0] - ys[1]) >= 13


def test_tiles_compare_the_measures_with_the_previous_run():
    run = lambda coverage, failed, loc, **more: {"date": "2026-10-25T10:00", "odoo": 19, "coverage": coverage,
                                                 "failed": failed, "not_installable": 0, "no_tests": 0, "addons": 2,
                                                 "loc": loc, **more}
    # a run of jsa 0.36 in mode whole, then one in mode isolated: each value compares with the last run that has it
    history = [run(39.0, 4, 100, coverage_whole=50.0, failed_whole=3, not_installable_whole=0),
               {**run(None, None, 100, mode="whole"), "coverage": 50.0, "failed": 3, "not_installable": 0},
               run(40.0, 2, 100, mode="isolated"),
               run(43.0, 1, 110, coverage_whole=52.5, failed_whole=1, not_installable_whole=1)]  # the last: this run
    summary = {**history[-1], "tests": 9, "measured": 2, "enterprise": None}
    previous_report = run(40.0, 2, 100)
    tiles_of = lambda page: page[page.index('<div class="tiles">'):page.index('</div></div>', page.index('Enterprise dependencies'))]
    tiles = tiles_of(report_html.render({}, summary, previous_report, None, None, history, {},
                                        {"generated_at": "2026-10-25T10:00:00", "measured": True, "profile": "19", "odoo": 19}))
    # per module in the value, whole under it; the change in a few words, what it is compared with in its tooltip
    previous = '<span title="Compared with the previous run">'
    assert f'{previous}+3.0 pts since last run</span>' in tiles
    assert f'whole: 52% ({previous}+2.5 pts</span>)' in tiles
    assert f'1 failed ({previous}-1</span>)' in tiles and f'whole: 1 failed ({previous}-2</span>)' in tiles
    assert f'whole: 1 ({previous}+1</span>)' in tiles  # not installable with all the addons
    assert '<span title="Compared with the previous report">+10 since last run</span>' in tiles  # lines of code
    assert tiles.count('class="tile"') == 6  # one row (no tile for the addons without tests: a trend chart has them)
    assert "without tests" not in tiles and 'title="Branch coverage of the code inside functions, per module' in tiles
    # at most two lines under each value
    assert all(tile.count('class="sub"') <= 2 for tile in tiles.split('class="tile"'))
    # a report that measured nothing compares no measure
    tiles = tiles_of(report_html.render({}, summary, previous_report, None, None, history, {}, {"generated_at": "2026-10-25T10:00:00"}))
    assert "pts since" not in tiles and "+10 since last run" in tiles


def _run(day, coverage, loc, todo=None, **more):
    line = {"date": f"2026-10-{day:02d}T10:00:00", "odoo": 19, "coverage": coverage, "loc": loc,
            "tests": 100 + day, "failed": 1, "not_installable": 0, "no_tests": 2, "addons": 10, "enterprise": None, **more}
    return {**line, "measures": {"TODO": todo}} if todo is not None else line


def test_trend_charts_one_under_the_other_coverage_and_lines_of_code_open():
    history = [_run(1, 60.0, 12000, todo=40), {"date": "2026-10-02", "event": "Release 2.0"},
               {"date": "2026-09-01", "event": "Before the first run"}, {"date": "not a date", "event": "Broken"},
               _run(3, 62.0, 12480, todo=31)]
    page = report_html._trend(history, ["TODO", "Not recorded yet"])
    order = [page.index(f'data-chart="{key}"') for key in ("coverage", "loc", "tests", "failed", "not_installable",
                                                           "no_tests", "addons", "m0", "m1")]
    assert order == sorted(order)
    assert 'data-chart="coverage" open>' in page and 'data-chart="loc" open>' in page
    assert 'data-chart="tests">' in page and 'data-chart="failed">' in page and 'data-chart="m0">' in page  # folded
    assert 'data-chart="enterprise"' not in page  # never measured: no chart
    assert '<span class="tname">Lines of code</span><span class="tnow">12,480</span></summary>' in page  # the last value only
    assert "since" not in page
    assert "recorded from the next run measuring the coverage (-c)" in page[page.index('data-chart="m1"'):]
    # the event between the two runs is drawn on every chart, the one before the first run is not
    # (each chart is drawn twice: zoomed on its values, and from 0)
    assert page.count("<title>Release 2.0</title>") == 16 and "Before the first run" not in page and "Broken" not in page
    runs, events = report.runs_and_events(history)
    assert len(runs) == 2 and [e["event"] for e in events] == ["Release 2.0", "Before the first run"]
    assert report_html.event_positions([report.parse_date("2026-10-01"), report.parse_date("2026-10-03")],
                                       [{"date": "2026-10-02", "event": "x"}, {"date": "2026-10-03", "event": "y"}]) == [(0.5, "x"), (1.0, "y")]


def test_charts_shown_and_unfolded_chosen_in_config(capsys):
    measures = codebase.measures_from_config({"report": {"measures": [{"label": "TODO", "pattern": "TODO"}]}})
    choose = lambda report: codebase.trends_from_config({"report": report}, measures)
    assert choose({}) == {}  # nothing set: the default
    runs = [_run(1, 60.0, 12000, todo=40), _run(3, 62.0, 12480, todo=31)]
    order = lambda page: sorted((key for key in ("m0", "coverage", "failed", "tests", "loc") if f'data-chart="{key}"' in page),
                                key=lambda key: page.index(f'data-chart="{key}"'))
    choice = choose({"trends": ["tests", "TODO", "coverage", "failed"], "trends_open": ["failed", "TODO"]})
    page = report_html._trend(runs, ["TODO"], choice)
    # the unfolded first, in the order of trends_open; then the folded, in the order of trends
    assert order(page) == ["failed", "m0", "tests", "coverage"]
    assert 'data-chart="loc"' not in page  # not listed: not shown
    assert 'data-chart="m0" open>' in page and 'data-chart="failed" open>' in page and 'data-chart="coverage">' in page
    page = report_html._trend(runs, ["TODO"], choose({"trends": ["failed", "TODO", "loc", "coverage"]}))
    assert order(page) == ["coverage", "loc", "failed", "m0"]  # the default unfolded ones (coverage, loc) first
    # only trends_open set: every chart shown, these unfolded
    page = report_html._trend([_run(1, 60.0, 12000), _run(3, 62.0, 12480)], [], choose({"trends_open": ["tests"]}))
    assert 'data-chart="tests" open>' in page and 'data-chart="coverage">' in page and 'data-chart="loc">' in page
    assert report_html._trend([_run(1, 60.0, 12000), _run(3, 62.0, 12480)], [], {"trends": []}) == ""
    for wrong, why in (({"trends": ["coverag"]}, "no chart named 'coverag'. The charts: coverage, loc,"),
                       ({"trends_open": "coverage"}, "trends_open must be a list of chart names"),
                       ({"trend": ["coverage"]}, "has the key(s) trend, which do not exist"),
                       ({"trends": ["coverage"], "trends_open": ["loc"]}, "trends_open lists 'loc', which trends does not show")):
        with pytest.raises(typer.Exit):
            choose(wrong)
        assert why in capsys.readouterr().out
    with pytest.raises(typer.Exit):  # a measure named like a chart of jsa
        codebase.measures_from_config({"report": {"measures": [{"label": "loc", "pattern": "x"}]}})


def test_trend_in_the_order_of_the_code_measured():
    # old commits measured today, newest first: each run is placed at the date of its code
    history = [_run(20, 70.0, 13000), _run(21, 50.0, 10000, code_date="2025-11-15T09:00:00"),
               _run(22, 60.0, 11500, code_date="2026-05-10T17:30:00"), {"date": "2026-01-01", "event": "Release 2.0"}]
    page = report_html._trend(history)
    loc = page[page.index('data-chart="loc"'):page.index('data-chart="tests"')]
    assert '<polyline points="48.0,' in loc
    ys = [float(p.split(",")[1]) for p in loc.split('<polyline points="')[1].split('"')[0].split()]
    assert ys[0] > ys[1] > ys[2]  # 10,000 then 11,500 then 13,000 lines: going up
    assert ">2025-11-15 09:00<" in loc and ">2026-10-20 10:00<" in loc  # the x axis: from the oldest code to the newest
    assert "<title>2025-11-15 09:00 · Odoo 19 (measured 2026-10-21): 10,000</title>" in loc
    assert "<title>Release 2.0</title>" in loc  # between the code of November and the code of May


def test_date_of_the_code_measured(tmp_path):
    repo = tmp_path / "addons"
    repo.mkdir()
    git = lambda *args, **env: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                                             capture_output=True, text=True, env={**os.environ, **env}, check=True)
    assert codebase.code_date([repo]) is None  # not in git
    git("init", "-q")
    (repo / "a.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "old", GIT_COMMITTER_DATE="2025-11-15T09:00:00+00:00", GIT_AUTHOR_DATE="2025-11-15T09:00:00+00:00")
    expected = datetime(2025, 11, 15, 9, tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat()
    assert codebase.code_date([repo]) == expected
    (repo / "a.py").write_text("x = 2\n")
    assert codebase.code_date([repo]) is None  # uncommitted changes: the date of the run


def test_zoomed_axis_for_lines_of_code():
    assert report_html.zoomed_axis([12000, 12480]) == (12000, 12500, 250)
    low, high, _ = report_html.zoomed_axis([500, 500])  # a flat line: a narrow band around it
    assert low <= 500 < high
    assert report_html.axis_number(12250) == "12.25k" and report_html.axis_number(9950) == "9,950"
    chart = report_html.line_chart("Lines of code", [("a", 12000), ("b", 12480)], zoom=True)
    assert ">12k<" in chart and ">12.5k<" in chart and ">0<" not in chart and ">12,480<" in chart
    assert ">0<" in report_html.line_chart("Failed tests", [("a", 3), ("b", 1)])  # counts start at 0


def test_every_chart_zoomed_at_first_its_axis_from_0_one_click_away():
    page = report_html._trend([_run(1, 80.0, 12000), _run(3, 82.0, 12480)])
    coverage = page[page.index('data-chart="coverage"'):page.index('data-chart="loc"')]
    zoomed = coverage[coverage.index('class="chart zoomed"'):coverage.index('class="chart full"')]
    full = coverage[coverage.index('class="chart full"'):]
    assert ">80%<" in zoomed and ">82%<" in zoomed and ">0%<" not in zoomed  # 80% -> 82% shows
    assert ">0%<" in full and ">100%<" in full
    assert '<button type="button" class="axis"' in coverage and ">Axis from 0</button>" in coverage
    assert "details.trend svg.full,details.trend.from0 svg.zoomed{display:none}" in report_html.CSS
    low, high, _ = report_html.zoomed_axis([99.2, 99.9])
    assert 99 <= low and high <= 100  # a coverage near 100% never gets an axis above 100%


def test_the_time_a_test_may_take(capsys):
    assert codebase.test_timeout({}) == 10  # minutes, by default
    assert codebase.test_timeout({"codebase": {"test_timeout": 2.5}}) == 2.5
    for wrong in (0, -1, "10", True):
        with pytest.raises(typer.Exit):
            codebase.test_timeout({"codebase": {"test_timeout": wrong}})
        assert "test_timeout is the minutes a test may take" in capsys.readouterr().out
    # written at the end of the file, after another [table]: it belongs to that table, and is not read
    misplaced = {"codebase": {"addons_roots": []}, "profiles": {"19": {"python": "x", "test_timeout": 1}}}
    assert codebase.test_timeout(misplaced, warn=True) == 10
    assert "test_timeout is in [profiles.19], where it is not read: put it under [codebase]" in capsys.readouterr().out
    assert codebase.test_timeout({"codebase": {"test_timeout": 1}}, warn=True) == 1 and capsys.readouterr().out == ""


def test_measures_of_config_counted_in_the_code(tmp_path, capsys):
    addon = make_addon(tmp_path, "my_addon")
    (addon / "models" / "a.py").write_text("# TODO one\nx = 1  # TODO two\n")
    (addon / "views.xml").write_text("<!-- TODO three -->")
    (addon / "tests").mkdir()
    (addon / "tests" / "test_a.py").write_text("# TODO not counted: tests are not code\n")
    measures = codebase.measures_from_config({"report": {"measures": [
        {"label": "TODO", "pattern": r"\bTODO\b"}, {"label": "TODO in python", "pattern": "^# TODO", "files": ["*.py"]}]}})
    assert codebase.count_measures({"my_addon": addon}, measures) == {"TODO": 3, "TODO in python": 1}
    assert codebase.count_measures({"my_addon": addon}, []) == {}
    for wrong, why in (({"label": "x", "pattern": "("}, "not a valid regular expression"),
                       ({"label": "x", "regex": "a"}, "regex, which do not exist"),
                       ({"label": "x"}, "needs a label and a pattern"),
                       ({"label": "x", "pattern": "a", "files": "*.py"}, None)):
        try:
            codebase.measures_from_config({"report": {"measures": [wrong]}})
            assert why is None
        except typer.Exit:
            assert why in capsys.readouterr().out
    with pytest.raises(typer.Exit):
        codebase.measures_from_config({"report": {"measures": [{"label": "x", "pattern": "a"}, {"label": "x", "pattern": "b"}]}})


def test_state_history_and_report_files(tmp_path):
    assert report.load_state(tmp_path) == ({}, None)  # no previous run
    results = {"a": Result(60, tests=5, failed=["T.new"], statements=100, covered=60, files={"models/x.py": "3-5"}), "new": None}
    for _ in range(2):  # two measured runs: the trend appears
        report.write_reports({"a": 15, "new": 3}, {"a": ["base"], "new": []}, results, tmp_path,
                             run={"mode": "whole", "profile": "17"}, measures={"TODO comments": 4})
    assert all(line["measures"] == {"TODO comments": 4} for line in report.load_history(tmp_path / "history.jsonl"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["history.jsonl", "report.html", "state.json"]
    state, _ = report.load_state(tmp_path)
    assert state["a"]["failed"] == ["T.new"] and state["new"]["coverage"] is None
    assert Result.from_state(state["a"]) == results["a"]
    assert len(report.load_history(tmp_path / "history.jsonl")) == 2
    (tmp_path / "history.jsonl").write_text("not json\n" + (tmp_path / "history.jsonl").read_text())
    assert len(report.load_history(tmp_path / "history.jsonl")) == 2  # a line broken by hand is ignored
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    for expected in ("Needs attention", "T.new", "models/x.py", "3-5", "Trend of the measured runs", "60%", "Not measured",
                     '<span class="tname">TODO comments</span>'):
        assert expected in html


def test_coverage_color_is_continuous():
    colors = [report_html.coverage_color(c) for c in (0, 50, 80, 100)]
    assert colors == ["hsl(0,55%,58%)", "hsl(60,55%,58%)", "hsl(96,55%,58%)", "hsl(120,55%,58%)"]  # 80% is not 100%


def test_structure_measures_per_module_then_whole(tmp_path, monkeypatch):
    roots, out = tmp_path / "addons", tmp_path / "out"
    make_addon(roots, "a")
    make_addon(roots, "b", "{'depends': ['a']}")
    calls = []

    def measure(addons, graph, profile, mode, log_file, **options):
        calls.append((mode, sorted(addons), options.get("all_or_nothing", False)))
        if mode == "isolated":
            return {name: Result(60, tests=2, statements=10, covered=6, files={"m.py": "3"}) for name in addons}, None
        if options.get("all_or_nothing"):  # one addon does not install: none of them together
            return {name: Result(NOT_INSTALLABLE, note="not tested: a failed to install (all or nothing)") for name in addons}, None
        return {"a": Result(80, tests=2, statements=10, covered=8),
                "b": Result(70, tests=2, failed=["T.with_a"], statements=10, covered=7)}, None

    monkeypatch.setattr(codebase.coverage, "measure", measure)
    monkeypatch.setattr(codebase.coverage, "never_run", lambda addons, profile: {name: 10 for name in addons})
    monkeypatch.setattr(codebase.odoo, "source_version", lambda profile: "odoo-commit")
    monkeypatch.setattr(codebase.odoo, "major_version", lambda profile: 19)
    monkeypatch.setattr(codebase, "standard_depends", lambda profile: {"base": []})
    monkeypatch.setattr(config, "REVIEWED_FILE", tmp_path / "reviewed.toml")
    conf, profile = {"codebase": {"addons_roots": [str(roots)]}}, {"python": "python"}
    analyse = lambda **options: codebase.analyse(conf, [roots], out, "19", profile, **options)

    run = analyse()
    assert calls == [("isolated", ["a", "b"], False), ("whole", ["a", "b"], False)] and "mode" not in run
    state, _ = report.load_state(out)
    assert state["b"]["coverage"] == 60 and state["b"]["whole"]["failed"] == ["T.with_a"]
    assert "files" not in state["b"]["whole"] and state["b"]["files"] == {"m.py": "3"}  # uncovered lines: per module
    line = report.load_history(out / report.HISTORY_FILE)[-1]
    assert (line["coverage"], line["coverage_whole"], line["failed"], line["failed_whole"]) == (60.0, 75.0, 0, 1)
    assert "mode" not in line
    # nothing changed: nothing runs, the whole measure is kept too
    calls.clear()
    assert analyse() is None and calls == [] and report.load_state(out)[0]["b"]["whole"]["coverage"] == 70
    # b changed: measured again alone, the whole run again (the addons interact)
    (roots / "b" / "models" / "x.py").write_text("x = 1\n")
    analyse()
    assert calls == [("isolated", ["b"], False), ("whole", ["a", "b"], False)]
    # a report without -c keeps both measures of the unchanged addons
    codebase.analyse(conf, [roots], out)
    assert report.load_state(out)[0]["a"]["whole"]["coverage"] == 80
    # all or nothing (jsa backfill): nothing installs together, whole counts as 0%, the measures per module stay
    calls.clear()
    analyse(measure_all=True, all_or_nothing=True)
    assert calls == [("isolated", ["a", "b"], False), ("whole", ["a", "b"], True)]
    line = report.load_history(out / report.HISTORY_FILE)[-1]
    assert (line["coverage"], line["coverage_whole"], line["not_installable_whole"]) == (60.0, 0.0, 2)


def test_needs_attention_says_what_got_worse_first(tmp_path):
    entry = lambda coverage, failed=(), **more: {"loc": 10, "depends": [], "coverage": coverage, "tests": 3,
                                                  "failed": list(failed), "coverage_rules": 6, **more}
    still = {"coverage": NOT_INSTALLABLE, "note": "x", "loc": 1, "depends": [], "coverage_rules": 6}
    old = {"a": entry(80, ["T.old"]), "b": entry(60), "gone": entry(50), "x": still,
           "w": entry(70, whole={"coverage": 75, "failed": [], "tests": 3})}
    new = {"a": entry(78, ["T.old", "T.new"]), "b": entry(64), "c": {**entry(0), "tests": 0}, "x": still,
           "w": entry(70, whole={"coverage": NOT_INSTALLABLE, "note": "conflicts with b", "failed": []})}
    diff = report.compute_diff(old, new)
    assert diff["whole_could_to_cant"] == ["w"] and diff["newly_failing"] == [("a", ["T.new"])]
    page = report_html._attention(new, report.summarize(new), report.summarize(old), diff, "2026-11-25T10:00:00",
                                  findings=({"eval() or exec()": 2}, 1), survivors={"a": 1})
    items = page[page.index("<ul>"):page.index("</ul>")]
    assert items.index("Coverage down") < items.index("<strong>x</strong>: x")  # what is new first, then what still is
    assert '<span class="tag">new</span><code>T.new</code>' in items and "<code>T.old</code>" in items
    assert '<strong>a</strong>: 80% → 78% (<span class="neg">-2 pts</span>)' in items  # coverage down, per addon
    assert '<span class="tag">new</span>cannot be installed with all the addons (conflicts with b)' in items
    assert '<a href="#security">2 new security finding(s) to review</a>: eval() or exec() (2)' in items
    assert '<a href="#mutation">1 new change(s) no test notices</a>: a (1)' in items
    assert "<strong>c</strong>: a new addon without tests" in items
    assert "coverage up: b +4 pts" in page and "1 security finding(s) gone" in page  # what got better, one line
    assert '<details class="part" id="attention" open><summary><h2>Needs attention</h2><span class="psum">' \
           '<span class="neg">6 new</span> · 1 still to fix (since the report of 2026-11-25 10:00)</span>' in page
    calm = report_html._attention({"a": entry(80)}, {}, None, None, None)
    assert '<span class="psum">nothing</span>' in calm and "Nothing needs attention." in calm


def test_a_report_made_again_keeps_saying_what_changed(tmp_path):
    loc, deps = {"a": 10}, {"a": []}
    report.write_reports(loc, deps, {"a": Result(80, tests=2, statements=10, covered=8)}, tmp_path)
    report.write_reports(loc, deps, {"a": Result(70, tests=2, failed=["T.x"], statements=10, covered=7)}, tmp_path)
    tagged = lambda: '<span class="tag">new</span><code>T.x</code>' in (tmp_path / "report.html").read_text(encoding="utf-8")
    assert tagged()
    # made again with nothing changed (jsa mutation, a report without -c): the same comparison, still new
    report.write_reports(loc, deps, {"a": Result(70, tests=2, failed=["T.x"], statements=10, covered=7)}, tmp_path)
    assert tagged() and report.load_state(tmp_path, "baseline")[0]["a"]["coverage"] == 80
    # something changed: compared with the previous report again
    report.write_reports(loc, deps, {"a": Result(72, tests=2, failed=["T.x"], statements=10, covered=7)}, tmp_path)
    assert not tagged()


def test_the_table_says_the_tests_in_one_column():
    state = {"ok": {"coverage": 90, "tests": 4, "failed": [], "loc": 1, "depends": []},
             "bad": {"coverage": 50, "tests": 5, "failed": ["T.a", "T.b"], "loc": 1, "depends": ["ok"], "enterprise": ["helpdesk"]},
             "none": {"coverage": NO_REPORT, "tests": 0, "failed": [], "loc": 1, "depends": []},
             "broken": {"coverage": NOT_INSTALLABLE, "note": "x", "loc": 1, "depends": []}}
    diff = {**report.compute_diff({}, {}), "added": ["none"], "newly_failing": [("bad", ["T.b"])],
            "metrics_changed": [("ok", 0, 3, 87, 90)]}
    table = report_html._table(state, report.enterprise_dependencies(state), diff=diff)
    head = table[table.index("<thead>"):table.index("</thead>")]
    assert [part.split(">", 1)[1].split("<")[0] for part in head.split("<th data-key")[1:]] == [
        "Addon", "Coverage", "Tests", "Lines of code", "Enterprise", "Security"]
    assert "Used by" not in head and "Status" not in head and "Failed" not in head
    assert '<b>⚠</b>2 of 5 failed</span> <span class="neg" title="Failing since the report compared with">+1</span>' in table
    assert '<b>✔</b>4 passed</span>' in table and '<b>○</b>no tests</span>' in table and '<b>✖</b>not installable</span>' in table
    assert '90% <span class="pos" title="Since the report compared with">+3</span>' in table
    assert '<span class="tag">new</span>none' in table and "◆" not in table
    ranks = [int(part.split('"')[1]) for part in table.split('data-tests=')[1:]]
    assert ranks == sorted(ranks)  # not installable, failing (most failures first), no tests, passing
