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
                       "tests": 5, "failed": 1, "not_installable": 1, "no_tests": 1, "enterprise": None}
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
    assert diff["not_comparable"] == ["a", "c"]
    assert diff["metrics_changed"] == [("a", 2, None, 80, 60), ("b", 0, 5, 50, 55)]  # a: its lines only, not its coverage
    assert diff["newly_failing"] == [] and diff["fixed"] == [("b", ["T.x"])] and diff["could_to_cant"] == []
    page = report_html._changes(diff, "2026-10-25T10:00:00")
    assert "Coverage and tests of 2 addons not compared: measured another way" in page and "+2 lines" in page
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


def test_one_trend_per_way_of_measuring(tmp_path):
    assert report.series_of({"mode": "isolated", "odoo": 19}) == report.series_of({"mode": "both", "odoo": 19}) == (19, "one by one")
    assert report.series_of({"mode": "whole", "odoo": 19}) == (19, "whole")
    assert report.series_of({"mode": "whole", "profile": "17"}) == (None, "whole")  # recorded before the version was
    assert report.series_label((None, "whole")) == "Odoo version unknown · whole"
    run = lambda mode, version, coverage, day: {"date": f"2026-10-{day:02d}T10:00", "mode": mode, "odoo": version,
                                                "coverage": coverage, "failed": 0, "not_installable": 0}
    history = [run("whole", 17, 50, 1), run("whole", 17, 52, 2), run("whole", 19, 70, 3), run("isolated", 19, 40, 4),
               run("whole", 19, 72, 5), run("both", 19, 41, 6)]
    page = report_html._trend(history)
    options = page[page.index("<select"):page.index("</select>")]
    # the series of the last run first (shown), each with only its own runs
    assert options.index("Odoo 19 · one by one (isolated, both) (2 runs)") < options.index("Odoo 17 · whole (2 runs)")
    assert "Odoo 19 · whole (2 runs)" in options
    shown = page[page.index('data-key="0">'):page.index('data-key="1"')]
    assert "41%" in shown and "72%" not in shown and "hidden" not in shown[:20]
    assert page.count(" hidden>") == 2
    assert ".series[hidden]{display:none}" in report_html.CSS  # the charts are display:flex, which beats "hidden"
    assert report_html._trend([run("whole", 19, 70, 1)]) == ""  # one run: no trend yet
    single = report_html._trend([run("whole", 19, 70, 1), run("whole", 19, 71, 2)])
    assert "<select" not in single and "Odoo 19 · whole" in single
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
    summary = {**history[-1], "tests": 9, "measured": 2, "enterprise": None}
    previous_report = run("whole", 70.0, 0, 100)
    page = report_html.render({}, summary, previous_report, None, None, history, {},
                              {"generated_at": "2026-10-25T10:00:00", "mode": "both", "profile": "19", "odoo": 19})
    tiles = page[page.index('<div class="tiles">'):page.index('</div></div></div>', page.index('Without tests'))]
    # coverage and failures: against the previous isolated run (same method), not against the whole run just before
    assert "(+3.0 pts since the previous run, Odoo 19 · one by one (isolated, both))" in tiles
    assert "(-1 since the previous run, Odoo 19 · one by one (isolated, both))" in tiles
    assert "-27" not in tiles
    assert "(+10 since previous run)" in tiles  # lines of code: against the previous report, whatever its method
    # a report that measured nothing compares no measure
    page = report_html.render({}, summary, previous_report, None, None, history, {}, {"generated_at": "2026-10-25T10:00:00"})
    assert "pts since" not in page and "(+10 since previous run)" in page


def test_state_history_and_report_files(tmp_path):
    assert report.load_state(tmp_path) == ({}, None)  # no previous run
    results = {"a": Result(60, tests=5, failed=["T.new"], statements=100, covered=60, files={"models/x.py": "3-5"}), "new": None}
    for _ in range(2):  # two measured runs: the trend appears
        report.write_reports({"a": 15, "new": 3}, {"a": ["base"], "new": []}, results, tmp_path, run={"mode": "whole", "profile": "17"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["history.jsonl", "report.html", "state.json"]
    state, _ = report.load_state(tmp_path)
    assert state["a"]["failed"] == ["T.new"] and state["new"]["coverage"] is None
    assert Result.from_state(state["a"]) == results["a"]
    assert len(report.load_history(tmp_path / "history.jsonl")) == 2
    (tmp_path / "history.jsonl").write_text("not json\n" + (tmp_path / "history.jsonl").read_text())
    assert len(report.load_history(tmp_path / "history.jsonl")) == 2  # a line broken by hand is ignored
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    for expected in ("Needs attention", "T.new", "models/x.py", "3-5", "Trend of the measured runs", "60%", "Not measured"):
        assert expected in html


def test_coverage_color_is_continuous():
    colors = [report_html.coverage_color(c) for c in (0, 50, 80, 100)]
    assert colors == ["hsl(0,55%,58%)", "hsl(60,55%,58%)", "hsl(96,55%,58%)", "hsl(120,55%,58%)"]  # 80% is not 100%
