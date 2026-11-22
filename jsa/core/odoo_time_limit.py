"""Runs odoo-bin with a time limit on each test, and on each setUpClass / tearDownClass of a test class.

Not imported by jsa: the python of a profile runs it, in place of odoo-bin (see odoo.install):
    python odoo_time_limit.py <minutes> <path of odoo-bin> <arguments of odoo-bin...>

A test running longer is stopped with an error: it fails, the next tests run. A setUpClass running longer fails its
class: Odoo's test runner then runs none of its tests, each one is logged as failed (for any failed setUpClass).
The error is not an Exception: a test catching every Exception (a retry loop) cannot swallow it. Uses SIGALRM: it
stops Python code (a loop, a wait on the network), not a call blocked inside C (e.g. a database query waiting for a
lock): jsa stops Odoo itself when no test starts for too long (odoo.install).
"""
import importlib.abc
import logging
import os
import runpy
import signal
import sys
from contextlib import contextmanager


class TestTimeout(BaseException):
    """Raised in the test that took too long. Not an Exception, so that the test's own `except Exception` cannot
    swallow it: Odoo's test runner records any error of a test (a bare except)."""


class ClassTimeout(Exception):
    """The same in a setUpClass or tearDownClass: Odoo's suite records only an Exception there."""


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


@contextmanager
def recorded_by_the_suite(test_class, method: str):
    """While it lasts, `test_class.<method>` (setUpClass, tearDownClass) turns a TestTimeout into a ClassTimeout:
    the suite records the failure of the class instead of stopping."""
    if test_class is None or not hasattr(test_class, method):
        yield
        return
    own, bound = vars(test_class).get(method), getattr(test_class, method)

    def converted():
        try:
            return bound()
        except TestTimeout as e:
            raise ClassTimeout(str(e)) from None

    setattr(test_class, method, converted)
    try:
        yield
    finally:
        if own is not None:
            setattr(test_class, method, own)
        else:
            delattr(test_class, method)


def class_step(handle, method: str, class_of, seconds: int):
    """`handle` (the suite running a setUpClass or a tearDownClass), stopped after `seconds`."""
    def handled(self, test, result):
        with recorded_by_the_suite(class_of(test, result), method):
            signal.alarm(seconds)
            try:
                return handle(self, test, result)
            finally:
                signal.alarm(0)
    return handled


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
        set_up = class_step(suite.TestSuite._handleClassSetUp, 'setUpClass', lambda test, result: type(test), seconds)
        tear_down = class_step(suite.TestSuite._tearDownPreviousClass, 'tearDownClass',
                               lambda test, result: getattr(result, '_previousTestClass', None), seconds)

        def handle_class_setup(self, test, result):
            test_class = type(test)
            if getattr(result, '_previousTestClass', None) is not test_class:  # else Odoo does nothing
                tests = [getattr(t, '_testMethodName', '?') for t in self if type(t) is test_class]
                announce(test_class, f"setUpClass {test_class.__name__}: {' '.join(tests)}")
            set_up(self, test, result)
            mark_not_run(self, test_class)

        def tear_down_previous_class(self, test, result):
            previous = getattr(result, '_previousTestClass', None)
            if previous is not None and previous is not type(test) and not getattr(previous, '_classSetupFailed', False):
                announce(previous, f"tearDownClass {previous.__name__}")
            tear_down(self, test, result)

        suite.TestSuite._handleClassSetUp = handle_class_setup
        suite.TestSuite._tearDownPreviousClass = tear_down_previous_class

    def announce(test_class, what: str):
        """What runs between two tests, for jsa (odoo.CLASS_STEP_RE): if Odoo stops answering there, jsa skips it."""
        logging.getLogger(test_class.__module__).info("jsa: %s", what)

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
