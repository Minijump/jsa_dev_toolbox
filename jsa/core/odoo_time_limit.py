"""Runs odoo-bin with a time limit on each test, and on each setUpClass / tearDownClass of a test class.

Not imported by jsa: the python of a profile runs it, in place of odoo-bin (see odoo.install):
    python odoo_time_limit.py <minutes> <path of odoo-bin> <arguments of odoo-bin...>

A test running longer is stopped with an error: it fails, the next tests run. A setUpClass running longer fails its
class: Odoo's test runner then runs none of its tests, each one is logged as failed (for any failed setUpClass). Uses
SIGALRM: it stops Python code (a loop, a wait on the
network), not a call blocked inside C (e.g. a database query waiting for a lock): jsa stops Odoo itself when its log
stays silent too long (odoo.install).
"""
import importlib.abc
import logging
import os
import runpy
import signal
import sys


class TestTimeout(Exception):
    """Raised in the test that took too long."""


class PatchAfterImport(importlib.abc.MetaPathFinder):
    """Changes modules right after Odoo imports them: imported earlier, Odoo's test framework reads options not
    parsed yet (Odoo 19 then runs no test)."""

    def __init__(self, patches: dict):
        self.patches = patches  # {module name: function changing the module}

    def find_spec(self, name, path, target=None):
        if name not in self.patches:
            return None
        for finder in sys.meta_path:
            spec = finder.find_spec(name, path, target) if finder is not self and hasattr(finder, "find_spec") else None
            if spec:
                break
        else:
            return None
        execute, patch = spec.loader.exec_module, self.patches[name]

        def execute_then_patch(module):
            execute(module)
            patch(module)

        spec.loader.exec_module = execute_then_patch
        return spec


def limit(function, seconds: int):
    """`function`, stopped by a TestTimeout when it runs longer than `seconds`."""
    def limited(*args, **kwargs):
        signal.alarm(seconds)
        try:
            return function(*args, **kwargs)
        finally:
            signal.alarm(0)
    return limited


def main():
    minutes, odoo_bin, arguments = float(sys.argv[1]), sys.argv[2], sys.argv[3:]
    os.environ['TZ'] = 'UTC'  # what odoo-bin does first
    sys.argv = [odoo_bin, *arguments]
    sys.path[0] = os.path.dirname(os.path.abspath(odoo_bin))  # not this folder: jsa's odoo.py would hide Odoo's package

    def expired(signum, frame):
        raise TestTimeout(f"stopped after {minutes:g} min (test_timeout of jsa's config.toml)")

    signal.signal(signal.SIGALRM, expired)
    seconds = max(1, round(minutes * 60))

    def patch_suite(suite):  # Odoo's own copy of unittest's suite (Odoo 16 and later)
        set_up = limit(suite.TestSuite._handleClassSetUp, seconds)

        def handle_class_setup(self, test, result):
            set_up(self, test, result)
            mark_not_run(self, type(test))

        suite.TestSuite._handleClassSetUp = handle_class_setup
        suite.TestSuite._tearDownPreviousClass = limit(suite.TestSuite._tearDownPreviousClass, seconds)

    reported = set()

    def mark_not_run(tests, test_class):
        """When the setUpClass of a class failed, Odoo runs none of its tests: each one is logged as failed, like a
        test that ran (what jsa reads in the log), besides the error of the setUpClass that Odoo logs."""
        if not getattr(test_class, '_classSetupFailed', False) or test_class in reported:
            return
        reported.add(test_class)
        logger = logging.getLogger(test_class.__module__)
        for test in tests:
            if type(test) is test_class:
                name = f"{test_class.__name__}.{getattr(test, '_testMethodName', '?')}"
                logger.info("Starting %s ...", name)
                logger.error("ERROR: %s\nnot run: the setUpClass of its class failed", name)

    def patch_result(result):
        start, stop = result.OdooTestResult.startTest, result.OdooTestResult.stopTest

        def start_test(self, test):  # the test, its setUp and tearDown run between these two
            start(self, test)
            signal.alarm(seconds)

        def stop_test(self, test):
            signal.alarm(0)
            stop(self, test)

        result.OdooTestResult.startTest, result.OdooTestResult.stopTest = start_test, stop_test

    sys.meta_path.insert(0, PatchAfterImport({'odoo.tests.suite': patch_suite, 'odoo.tests.result': patch_result}))
    runpy.run_path(odoo_bin, run_name="__main__")


if __name__ == "__main__":
    main()
