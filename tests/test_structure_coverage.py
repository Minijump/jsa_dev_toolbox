from pathlib import Path

from jsa.core import odoo
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


def test_a_lock_waited_for_too_long_errs_and_a_long_query_is_cancelled(monkeypatch):
    # PostgreSQL gives up waiting for a lock after the time limit of a test (libpq reads PGOPTIONS)
    assert odoo.with_lock_timeout({"PATH": "x"}, 10)["PGOPTIONS"] == "-c lock_timeout=600000"
    assert odoo.with_lock_timeout({"PGOPTIONS": "-c work_mem=64MB"}, 0.5)["PGOPTIONS"] == "-c work_mem=64MB -c lock_timeout=30000"
    assert odoo.with_lock_timeout({}, 0.001)["PGOPTIONS"] == "-c lock_timeout=1000"  # never less than a second
    import subprocess
    asked = []
    monkeypatch.setattr(odoo.subprocess, "run", lambda cmd, **kw: asked.append(cmd[-1]) or
                        subprocess.CompletedProcess(cmd, 0, stdout="2\n", stderr=""))
    assert odoo.cancel_queries("cov19_abc_whole") == 2
    assert "pg_cancel_backend" in asked[0] and "datname = 'cov19_abc_whole'" in asked[0] and "state = 'active'" in asked[0]
    monkeypatch.setattr(odoo.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="denied"))
    assert odoo.cancel_queries("db") == 0  # not allowed (another role): Odoo is killed as before


def test_reading_odoo_ends_when_it_exits_even_if_a_process_it_started_keeps_its_output():
    import subprocess
    import sys
    import time
    # like Odoo leaving the browser of a stopped tour behind: the output stays open 8 s after Odoo has gone
    script = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)']); "
              "print('first'); print('last')")
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    start = time.monotonic()
    assert [line.strip() for line in odoo.lines_of(proc, grace=0.5)] == ["first", "last"]  # everything it wrote
    assert time.monotonic() - start < 5  # not the 8 s of the process left behind


def test_the_stop_of_a_test_cannot_be_swallowed_and_fails_its_class():
    from jsa.core import odoo_time_limit as limit
    caught = []
    try:  # a test with a retry loop catching every Exception: the stop goes through
        try:
            raise limit.TestTimeout("stopped")
        except Exception:
            caught.append("swallowed")
    except limit.TestTimeout:
        caught.append("stopped")
    assert caught == ["stopped"]

    class Parent:
        @classmethod
        def setUpClass(cls):
            raise limit.TestTimeout("stopped after 1 min")

    class Hanging(Parent):
        pass

    class Own(Parent):
        @classmethod
        def setUpClass(cls):
            super().setUpClass()

    for test_class in (Hanging, Own):
        before = vars(test_class).get('setUpClass')
        with limit.recorded_by_the_suite(test_class, 'setUpClass'):
            try:  # what Odoo's suite does: it records an Exception as the failure of the class
                test_class.setUpClass()
            except Exception as e:
                assert isinstance(e, limit.ClassTimeout) and "1 min" in str(e)
        assert vars(test_class).get('setUpClass') is before  # put back as it was


def test_a_class_whose_setup_failed_and_odoo_stopped_answering():
    info = LogInfo()
    for line in [  # as Odoo 17 and 19 log them (the setUpClass by its test suite, its tests by odoo_time_limit.py)
        "INFO db odoo.addons.c_mail.tests.test_x: Starting TestFine.test_a ...",
        "ERROR db odoo.tests.suite: ERROR: setUpClass (odoo.addons.c_mail.tests.test_hang.TestAHangingSetup)",
        "INFO db odoo.addons.c_mail.tests.test_hang: Starting TestAHangingSetup.test_one ...",
        "ERROR db odoo.addons.c_mail.tests.test_hang: ERROR: TestAHangingSetup.test_one",
        "ERROR db odoo.tests.suite: ERROR: tearDownClass (odoo.addons.c_sale.tests.test_y.TestY)",
        "INFO db odoo.addons.c_sale.tests.test_y: Starting TestY.test_b ...",
    ]:
        info.feed(line)
    assert info.failed_tests == {"c_mail": ["TestAHangingSetup.setUpClass", "TestAHangingSetup.test_one"],
                                 "c_sale": ["TestY.tearDownClass"]}
    # the tests of the class are failed, test_a passed: 3 tests, 2 failed
    assert info.tests == {"c_mail": 3, "c_sale": 2}
    assert not info.failed and info.culprit is None and info.testing == "c_sale"
    assert LogInfo(loaded=["x"], crashed=True).culprit is None  # a crash is never blamed on a module


GRAPH_SALE = DependencyGraph({"base": [], "c_mail": ["base"], "c_sale": ["base"], "c_sale_child": ["c_sale"]},
                             custom={"c_mail", "c_sale", "c_sale_child"})


def killed(*lines) -> LogInfo:
    """What jsa knows of a run it killed (Odoo stopped answering) after these lines."""
    info = LogInfo()
    for line in lines:
        info.feed(line)
    info.timed_out = True
    return info


def test_a_stuck_test_is_skipped_and_the_tests_run_again():
    tries = []

    def attempt(modules, skipped):
        tries.append((list(modules), [s.spec for s in skipped]))
        if len(tries) == 1:  # stuck in a test of c_sale (Python could not stop it): killed
            return killed("INFO db odoo.addons.c_sale.tests.test_y: Starting TestY.test_stuck ...")
        info = LogInfo()
        for line in ("INFO db odoo.addons.c_sale.tests.test_y: Starting TestY.test_fine ...",
                     "INFO db odoo.addons.c_mail.tests.test_x: Starting TestX.test_a ..."):
            info.feed(line)
        return info

    last, left_out = odoo.install_leaving_out(["c_mail", "c_sale", "c_sale_child"], GRAPH_SALE, attempt)
    # the same addons again, without that test: the other tests of c_sale run, it is installed and measured
    assert tries == [(["c_mail", "c_sale", "c_sale_child"], []), (["c_mail", "c_sale", "c_sale_child"], ["/c_sale:TestY.test_stuck"])]
    assert left_out == {} and not last.failed
    assert last.failed_tests == {"c_sale": ["TestY.test_stuck"]} and last.tests["c_sale"] == 2  # counted, failed


def test_a_stuck_setup_class_skips_its_class_and_an_install_that_hangs_leaves_the_addon_out():
    stuck = killed("INFO db odoo.addons.c_sale.tests.test_y: Starting TestY.test_fine ...",
                   "INFO db odoo.addons.c_sale.tests.test_z: jsa: setUpClass TestZ: test_one test_two").stuck
    assert (stuck.module, stuck.spec) == ("c_sale", "/c_sale:TestZ")  # the whole class: its setUpClass never ended
    assert stuck.failed == ["TestZ.setUpClass", "TestZ.test_one", "TestZ.test_two"]
    # killed while installing (no test running): nothing to skip, the addon is left out with those depending on it
    install = killed("INFO db odoo.addons.c_mail.tests.test_x: Starting TestX.test_a ...",
                     "INFO db odoo.modules.loading: Loading module c_sale (3/4)")
    assert install.stuck is None and install.culprit == "c_sale"
    tries = []

    def attempt(modules, skipped):
        tries.append(list(modules))
        return install if len(tries) == 1 else LogInfo()

    last, left_out = odoo.install_leaving_out(["c_mail", "c_sale", "c_sale_child"], GRAPH_SALE, attempt)
    assert tries == [["c_mail", "c_sale", "c_sale_child"], ["c_mail"]] and not last.failed
    assert left_out == {"c_sale": "Odoo stopped answering during its tests", "c_sale_child": "depends on c_sale"}


def test_isolated_skips_a_stuck_test_of_an_addon():
    class StuckOnce(FakeRunner):
        def run(self, db, install, label, test=(), sources=(), measure=True, skip=()):
            info = super().run(db, install, label, test, sources, measure, skip)
            if test and not skip:  # the first try of its tests: stuck in one of them
                info = killed(f"INFO db odoo.addons.{test[0]}.tests.t: Starting TestA.test_stuck ...")
            self.skips = getattr(self, 'skips', []) + ([[s.spec for s in skip]] if test else [])
            return info

    runner = StuckOnce()
    results = coverage.run_isolated({"c_base": Path("c_base")}, GRAPH, runner)
    assert runner.skips == [[], ["/c_base:TestA.test_stuck"]]  # tried again without it
    assert results["c_base"].coverage != NOT_INSTALLABLE and results["c_base"].failed == ["TestA.test_stuck"]
    assert runner.dbs == {}  # the database of the stopped try is dropped too


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

    def run(self, db, install, label, test=(), sources=(), measure=True, skip=()):
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


def test_whole_leaves_out_before_the_install_an_addon_needing_a_module_found_nowhere():
    graph = DependencyGraph({**GRAPH.depends, "c_lost": ["no_such_module"], "c_lost_child": ["c_lost"]},
                            custom=GRAPH.custom | {"c_lost", "c_lost_child"})
    addons = {m: Path(m) for m in ("c_base", "c_lost", "c_lost_child")}
    runner = FakeRunner()
    results, _ = coverage.run_whole(addons, graph, runner)
    assert results["c_lost"].note == results["c_lost_child"].note == "needs no_such_module, in no addons path"
    assert results["c_base"].coverage == NO_REPORT  # measured: the others are not stopped by it
    assert [install for _, install, _ in runner.installs] == [("base",), ("c_base",)]  # never tried to install it
    lost_only = {m: Path(m) for m in ("c_lost",)}
    assert coverage.run_whole(lost_only, graph, FakeRunner())[0]["c_lost"].coverage == NOT_INSTALLABLE


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
    runner.demo_args, runner.time_limit = ["--with-demo"], 7
    runner.run("db", ["a"], "label", test=["a"], sources=[tmp_path / "a"])
    assert seen["time_limit"] == 7  # each test and setUpClass limited (see odoo.install)
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
