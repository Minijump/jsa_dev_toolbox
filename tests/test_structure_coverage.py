from pathlib import Path

from jsa.core.addons import DependencyGraph
from jsa.core.odoo import LogInfo
from jsa.structure import coverage
from jsa.structure.coverage import NOT_INSTALLABLE, NO_REPORT


GRAPH = DependencyGraph({
    "base": [], "mail": ["base"], "sale": ["mail"],
    "c_mail": ["mail"], "c_mail_child": ["c_mail"], "c_sale": ["sale", "c_mail"], "c_base": ["base"],
}, custom={"c_mail", "c_mail_child", "c_sale", "c_base"})


def test_dependency_graph():
    assert GRAPH.closure(["c_sale"]) == {"c_sale", "sale", "mail", "c_mail", "base"}
    assert GRAPH.dependencies("c_mail_child") == {"c_mail", "mail", "base"}
    assert GRAPH.dependencies("c_base") == {"base"}


def test_log_info():
    info = LogInfo()
    for line in [
        "INFO db odoo.modules.loading: Loading module base (1/3)",
        "INFO db odoo.modules.loading: Loading module c_mail (2/3)",
        "INFO db odoo.addons.c_mail.tests.test_x: Starting TestX.test_a ...",
        "INFO db odoo.addons.c_mail.tests.test_x: Starting TestX.test_b ...",
        "INFO db odoo.addons.c_mail.tests.test_x: Starting TestX.test_c ...",
        "ERROR db odoo.addons.c_mail.tests.test_x: FAIL: TestX.test_a",
        "ERROR db odoo.addons.c_mail.tests.test_x: ERROR: TestX.test_b",
        "INFO db odoo.modules.loading: Loading module c_broken (3/3)",
        "ERROR db odoo.modules.registry: Failed to load registry",
    ]:
        info.feed(line)
    assert info.failed_tests == {"c_mail": ["TestX.test_a", "TestX.test_b"]}
    assert info.tests["c_mail"] == 3
    assert info.failed and info.culprit == "c_broken"
    assert LogInfo(loaded=["x"], crashed=True).culprit is None  # a crash is never blamed on a module


def test_module_coverage(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    summary = lambda lines, stmts, br, nbr: {"summary": {"covered_lines": lines, "num_statements": stmts,
                                                         "covered_branches": br, "num_branches": nbr}}
    data = {"files": {str(a / "models" / "x.py"): {**summary(8, 10, 1, 4), "missing_lines": [3, 4, 9]},
                      str(a / "models" / "y.py"): summary(10, 10, 0, 0),
                      str(tmp_path / "c" / "z.py"): summary(0, 5, 0, 0)}}
    result = coverage.module_coverage(data, {"a": a, "b": b})
    assert result["a"] == {"coverage": 79, "statements": 24, "covered": 19, "files": {"models/x.py": "3-4, 9"}}
    assert result["b"]["coverage"] == NO_REPORT
    assert coverage.display_percent(999, 1000) == 99 and coverage.display_percent(1, 1000) == 1


class FakeRunner:
    """Records what would be installed where, without Odoo. `failing`: {module: [tests that fail]}."""
    def __init__(self, broken=(), failing=None):
        self.broken, self.dbs, self.installs, self.copies = set(broken), {}, [], []
        self.failing, self.tested = failing or {}, []

    def new_db(self, name, template=None):
        self.dbs[name] = set(self.dbs[template]) if template else set()
        if template:
            self.copies.append((template, name))
        return name

    def run(self, db, install, label, test=(), sources=(), measure=True):
        self.installs.append((db, tuple(sorted(install)), bool(test)))
        self.tested.append((tuple(test), measure))
        broken = self.broken & GRAPH.closure(install)
        if broken:
            return LogInfo(loaded=[broken.pop()], registry_failed=True)
        self.dbs[db] |= GRAPH.closure(install)
        info = LogInfo()
        for module in test:
            info.tests[module] += 2
            if module in self.failing:
                info.failed_tests[module] = list(self.failing[module])
        return info

    def drop(self, db):
        del self.dbs[db]

    def coverage_json(self):
        return {}

    def installed(self, db, modules):
        return self.dbs[db] & set(modules)


def test_isolated_smart_snapshots():
    addons = {m: Path(m) for m in ("c_base", "c_mail", "c_mail_child", "c_sale")}
    runner = FakeRunner()
    results = coverage.run_isolated(addons, GRAPH, runner)
    installs = [(install, test) for _, install, test in runner.installs]
    # each dependency set is installed once, on top of the biggest snapshot it contains
    assert installs == [
        (("base",), False), (("c_base",), True),           # c_base: fresh db with base
        (("mail",), False), (("c_mail",), True),            # c_mail: base snapshot + mail
        (("c_mail",), False), (("c_mail_child",), True),    # c_mail_child: base+mail snapshot + c_mail
        (("sale",), False), (("c_sale",), True),            # c_sale: base+mail+c_mail snapshot + sale
    ]
    assert all(r.coverage == NO_REPORT for r in results.values())  # fake runner measures nothing
    assert runner.dbs == {}  # every database dropped


def test_isolated_failed_dependency():
    addons = {m: Path(m) for m in ("c_base", "c_mail", "c_mail_child")}
    results = coverage.run_isolated(addons, GRAPH, FakeRunner(broken={"mail"}))
    assert results["c_base"].coverage == NO_REPORT
    assert results["c_mail"].coverage == results["c_mail_child"].coverage == NOT_INSTALLABLE
    assert "mail" in results["c_mail"].note


def test_whole_retries_without_broken_addon():
    addons = {m: Path(m) for m in ("c_base", "c_mail", "c_mail_child")}
    runner = FakeRunner(broken={"c_mail"})
    results, standard = coverage.run_whole(addons, GRAPH, runner)
    assert standard is None  # the tests of the standard modules were not asked for
    assert results["c_mail"].note == "install failed"
    assert results["c_mail_child"].note == "depends on c_mail"
    assert results["c_base"].coverage == NO_REPORT  # measured in the retry
    tests = [install for _, install, test in runner.installs if test]
    assert tests == [("c_base", "c_mail", "c_mail_child"), ("c_base",)]


def test_whole_with_the_tests_of_the_standard_modules():
    addons = {m: Path(m) for m in ("c_base", "c_mail")}
    runner = FakeRunner(failing={"mail": ["TestMail.test_send"], "c_mail": ["TestMine.test_x"]})
    results, standard = coverage.run_whole(addons, GRAPH, runner, standard_tests=True)
    # no snapshot of the standard modules: they are installed and tested in the same run as the custom addons
    assert [install for _, install, _ in runner.installs] == [("c_base", "c_mail")]
    assert runner.tested == [(("c_base", "c_mail", "base", "mail"), True)]  # the coverage is still the custom one
    assert standard == {"base": {"tests": 2, "failed": []}, "mail": {"tests": 2, "failed": ["TestMail.test_send"]}}
    assert results["c_mail"].failed == ["TestMine.test_x"] and results["c_base"].tests == 2


def test_both_keeps_the_measures_alone_and_adds_what_breaks_together():
    alone = {"a": coverage.Result(80, tests=3, failed=["T.test_alone"]), "b": coverage.Result(60, tests=1),
             "c": coverage.Result(NOT_INSTALLABLE, note="install failed"), "d": coverage.Result(50, tests=2)}
    together = {"a": coverage.Result(NO_REPORT, tests=3, failed=["T.test_alone", "T.test_with_others"]),
                "b": coverage.Result(NOT_INSTALLABLE, note="depends on x"), "d": coverage.Result(NO_REPORT, tests=2)}
    merged = coverage.merge_together(alone, together)
    assert merged["a"].coverage == 80 and merged["a"].failed == ["T.test_alone", "T.test_with_others (with all the addons)"]
    assert merged["a"].together == "1 test(s) fail only when all the addons are installed"
    assert merged["b"].coverage == 60 and merged["b"].together == "cannot be installed with all the other addons (depends on x)"
    assert merged["c"].coverage == NOT_INSTALLABLE and not merged["c"].together  # already reported alone
    assert merged["d"] == alone["d"]  # nothing breaks: the measure alone, as it was
    # a kept result is merged again with the next run together: what came from the previous one goes away first
    again = coverage.merge_together(merged, {"a": coverage.Result(NO_REPORT, tests=3, failed=["T.test_alone"])})
    assert again["a"].failed == ["T.test_alone"] and not again["a"].together


def test_hooks_and_migrations_are_not_measured(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(coverage.odoo, "install", lambda *args, **kwargs: seen.update(kwargs) or LogInfo())
    runner = coverage.OdooRunner.__new__(coverage.OdooRunner)
    runner.profile, runner.data_dir, runner.log_file, runner.work_dir = {}, tmp_path, tmp_path / "log", tmp_path
    runner.demo_args = ["--with-demo"]
    runner.run("db", ["a"], "label", test=["a"], sources=[tmp_path / "a"])
    omitted = seen["python_args"][seen["python_args"].index("--omit") + 1].split(",")
    for pattern in ("*/hooks.py", "*/migrations/*", "*/upgrades/*", "*/tests/*"):
        assert pattern in omitted
    assert seen["odoo_args"][0] == "--with-demo"  # the demo data, on every version
    runner.run("db", ["a"], "label", test=["a"], measure=False)  # tests without coverage: odoo-bin alone
    assert seen["python_args"] == [] and "--test-enable" in seen["odoo_args"]


def test_demo_data_options_follow_what_odoo_knows(monkeypatch):
    for knows_with_demo, demo, expected in ((True, True, ["--with-demo"]), (False, True, []),
                                            (True, False, []), (False, False, ["--without-demo=all"])):
        monkeypatch.setattr(coverage.odoo, "supports", lambda profile, option: knows_with_demo)
        assert coverage.odoo.demo_arguments({}, demo) == expected
