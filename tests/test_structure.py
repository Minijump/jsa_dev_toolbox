import os
import subprocess
from datetime import datetime, timezone

import pytest
import typer

from jsa.core import addons, odoo
from jsa.structure import codebase, report, report_html
from jsa.structure.coverage import NOT_INSTALLABLE, Result


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
                       "security": None}  # a state made before the security review
    assert report.summarize({})["coverage"] is None


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
    for expected in ("Dependencies on Odoo enterprise (1 modules, 2 addons)", "through a", "Needs Odoo enterprise"):
        assert expected in html


def test_problem_with_all_the_addons_is_failing():
    entry = {"coverage": 70, "tests": 2, "failed": [], "together": "cannot be installed with all the other addons (x)"}
    assert report.status(entry) == "failing"
    page = report_html.render({"a": {**entry, "loc": 1, "depends": []}}, report.summarize({}), None, None, None, [], {},
                              {"generated_at": "2026-01-01T10:00:00",
                               "standard_tests": {"date": "2026-01-01T10:00:00",
                                                  "modules": {"sale": {"tests": 40, "failed": ["TestSale.test_a"]}}}})
    for expected in ("cannot be installed with all the other addons (x)", "Tests of the standard modules",
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
    old = {"a": {"loc": 10, "coverage": 80, "failed": [], "coverage_mode": "whole", "odoo": 19},
           "b": {"loc": 10, "coverage": 50, "failed": ["T.x"], "coverage_mode": "isolated", "odoo": 19},
           "c": {"loc": 10, "coverage": 70, "failed": [], "coverage_mode": "whole", "odoo": 17}}
    new = {"a": {"loc": 12, "coverage": 60, "failed": ["T.y"], "coverage_mode": "isolated", "odoo": 19},  # another method
           "b": {"loc": 10, "coverage": 55, "failed": [], "coverage_mode": "both", "odoo": 19},  # one by one, like isolated
           "c": {"loc": 10, "coverage": NOT_INSTALLABLE, "failed": [], "coverage_mode": "whole", "odoo": 19}}  # another Odoo
    diff = report.compute_diff(old, new)
    assert diff["not_comparable"] == ["a"]  # another Odoo version compares (c), another method does not (a)
    assert diff["metrics_changed"] == [("a", 2, None, 80, 60), ("b", 0, 5, 50, 55)]  # a: its lines only, not its coverage
    assert diff["newly_failing"] == [] and diff["fixed"] == [("b", ["T.x"])] and diff["could_to_cant"] == ["c"]
    page = report_html._changes(diff, "2026-10-25T10:00:00")
    assert "Coverage and tests of 1 addon not compared: measured another way" in page and "+2 lines" in page
    only = report_html._changes({**report.compute_diff({}, {}), "not_comparable": ["a"]}, None)
    assert "Nothing changed" not in only and "Coverage and tests of 1 addon not compared" in only


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


def test_one_trend_per_way_of_measuring_every_odoo_version_together(tmp_path):
    assert report.series_of({"mode": "isolated", "odoo": 19}) == report.series_of({"mode": "both", "odoo": 17}) == "one by one"
    assert report.series_of({"mode": "whole", "odoo": 19}) == report.series_of({"mode": "whole"}) == "whole"
    assert report.series_label("whole") == "whole" and report.series_label("one by one", 17) == "one by one (isolated, both) · Odoo 17 only"
    run = lambda mode, version, coverage, day: {"date": f"2026-10-{day:02d}T10:00", "mode": mode, "odoo": version,
                                                "coverage": coverage, "failed": 0, "not_installable": 0}
    history = [run("whole", 17, 50, 1), run("whole", 17, 52, 2), run("whole", 19, 70, 3), run("isolated", 19, 40, 4),
               run("whole", 19, 72, 5), run("both", 19, 41, 6)]
    page = report_html._trend(history)
    options = page[page.index("<select"):page.index("</select>")]
    # the method of the last run first (shown), every Odoo version together; each version alone one choice away
    assert options.index("one by one (isolated, both) (2 runs)") < options.index(">whole (4 runs)")
    assert "whole · Odoo 17 only (2 runs)" in options and "whole · Odoo 19 only (2 runs)" in options
    assert "one by one (isolated, both) · Odoo" not in options  # one version only: nothing to split
    shown = page[page.index('data-key="0">'):page.index('data-key="1"')]
    assert "41%" in shown and "72%" not in shown and "hidden" not in shown[:20]
    whole = page[page.index('data-key="1"'):page.index('data-key="2"')]
    coverage = whole[:whole.index('data-chart="failed"')]
    zoomed = coverage[:coverage.index('class="chart full"')]  # each chart drawn twice: zoomed (shown) and from 0
    assert zoomed.count('class="dot"') == 4  # Odoo 17 and 19 on the same curve
    assert "<title>Odoo 17 → 19</title>" in coverage and "· Odoo 17: 50%" in coverage  # where the version changes
    assert page.count(" hidden>") == 3
    assert ".series[hidden]{display:none}" in report_html.CSS  # the charts are display:flex, which beats "hidden"
    assert report_html._trend([run("whole", 19, 70, 1)]) == ""  # one run: no trend yet
    single = report_html._trend([run("whole", 19, 70, 1), run("whole", 19, 71, 2)])
    assert "<select" not in single and '<p class="meta">whole</p>' in single and "→" not in single
    release = tmp_path / "odoo" / "release.py"
    release.parent.mkdir()
    release.write_text("version_info = (19, 0, 0, FINAL, 0, '')\n")
    assert odoo.major_version({"odoo_root": str(tmp_path)}) == 19
    release.write_text("version_info = ('saas~18', 3, 0, 'final', 0, '')\n")
    assert odoo.major_version({"odoo_root": str(tmp_path)}) == 18
    assert odoo.major_version({"odoo_root": str(tmp_path / "nowhere")}) is None


def test_tiles_compare_the_measures_with_the_run_measured_the_same_way():
    run = lambda mode, coverage, failed, loc: {"date": "2026-10-25T10:00", "mode": mode, "odoo": 19, "coverage": coverage,
                                               "failed": failed, "not_installable": 0, "no_tests": 0, "addons": 2, "loc": loc}
    history = [run("isolated", 40.0, 2, 100), run("whole", 70.0, 0, 100), run("both", 43.0, 1, 110)]  # the last: this run
    history[0]["odoo"] = 17  # another Odoo version: compared all the same
    summary = {**history[-1], "tests": 9, "measured": 2, "enterprise": None}
    previous_report = run("whole", 70.0, 0, 100)
    tiles_of = lambda page: page[page.index('<div class="tiles">'):page.index('</div></div>', page.index('Enterprise dependencies'))]
    tiles = tiles_of(report_html.render({}, summary, previous_report, None, None, history, {},
                                        {"generated_at": "2026-10-25T10:00:00", "mode": "both", "profile": "19", "odoo": 19}))
    # coverage and failures: against the previous isolated run (same method), not against the whole run just before;
    # the change in a few words, which run it is compared with in its tooltip
    same_way = '<span title="Compared with the previous run measured the same way: one by one (isolated, both)">'
    assert f'{same_way}+3.0 pts since last run</span>' in tiles
    assert f'1 failed ({same_way}-1</span>)' in tiles
    assert "-27" not in tiles
    assert '<span title="Compared with the previous report">+10 since last run</span>' in tiles  # lines of code
    assert tiles.count('class="tile"') == 6  # one row (no tile for the addons without tests: a trend chart has them)
    assert "without tests" not in tiles and "branch coverage" in tiles
    # at most two lines under each value
    assert all(tile.count('class="sub"') <= 2 for tile in tiles.split('class="tile"'))
    # a report that measured nothing compares no measure
    tiles = tiles_of(report_html.render({}, summary, previous_report, None, None, history, {}, {"generated_at": "2026-10-25T10:00:00"}))
    assert "pts since" not in tiles and "+10 since last run" in tiles


def _run(day, coverage, loc, todo=None, **more):
    line = {"date": f"2026-10-{day:02d}T10:00:00", "mode": "whole", "odoo": 19, "coverage": coverage, "loc": loc,
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
