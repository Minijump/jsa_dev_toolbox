"""Runs the tests of an addon once per mutant, for 'jsa mutation'. Not imported by jsa: the python of a profile runs it
(its Odoo must be importable: PYTHONPATH), with the copy of the addon holding the mutants installed in the database.
    python mutation_driver.py <arguments.json>
arguments: db, addons_path, data_dir, module, mutants (mutants.json), slice [i, n] (the mutants whose index % n == i),
log (Odoo's log file), out (the results). Linux only (fork).

Odoo is loaded once. The tests of the addon run twice as they are (the reference): a test failing in either run is left
out (failing, or flaky); each test records which mutants its code reached (jsa_mutant_switch.REC), its class setup too.
Then, for each mutant, a copy of this process (fork) runs only the tests that reach it, the mutant active: some test
fails = the mutant is killed (the tests noticed), none = it survived. The database is never changed: Odoo's tests roll
back what they do, and each copy ends with its process. Browser tests (HttpCase: tours, pages) are left out: they need
Odoo's web server.
Progress on stdout, one line per mutant: "JSA <index> <status>".
"""
import json
import os
import pickle
import select
import signal
import sys
import threading
import time
import traceback

import jsa_mutant_switch as switch


def main(arguments_file):
    args = json.load(open(arguments_file))
    import odoo
    from odoo.tools import config
    config.parse_config(["-d", args["db"], "--addons-path", args["addons_path"], "--data-dir", args["data_dir"],
                         "--test-tags", f"/{args['module']}", "--logfile", args["log"], "--log-level", "info"])
    from odoo import sql_db
    from odoo.modules import module as odoo_module
    from odoo.tests import loader
    from odoo.tests.common import HttpCase
    from odoo.tests.result import OdooTestResult
    from odoo.tests.suite import OdooSuite
    try:
        from odoo.orm.registry import Registry  # Odoo 19
    except ImportError:
        from odoo.modules.registry import Registry

    class Result(OdooTestResult):
        """The tests that failed, and (record) the mutants each test reached, and each class setup."""
        def __init__(self, record):
            super().__init__()
            self.record, self.failed, self.reached, self.setup, self.times = record, set(), {}, {}, {}

        def startTest(self, test):
            super().startTest(test)
            if self.record:
                switch.REC = set()
            self.started = time.time()

        def stopTest(self, test):
            super().stopTest(test)
            self.times[test.id()] = time.time() - self.started
            if self.record:
                self.reached[test.id()] = sorted(switch.REC)
                switch.REC = None

        def addError(self, test, err):
            super().addError(test, err)
            self.failed.add(test_id(test))

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self.failed.add(test_id(test))

    set_up = OdooSuite._handleClassSetUp

    def handle_class_setup(self, test, result):
        if getattr(result, "record", False) and result._previousTestClass is not type(test):
            switch.REC = set()
            set_up(self, test, result)
            result.setup[class_id(type(test))] = sorted(switch.REC)
            switch.REC = None
        else:
            set_up(self, test, result)

    OdooSuite._handleClassSetUp = handle_class_setup

    def tests():
        found = []
        for position in ("at_install", "post_install"):
            found += [t for t in loader.make_suite([args["module"]], position) if not isinstance(t, HttpCase)]
        return found

    def browser_tests():
        return sum(1 for position in ("at_install", "post_install")
                   for t in loader.make_suite([args["module"]], position) if isinstance(t, HttpCase))

    def run(chosen, record=False):
        odoo_module.current_test = True
        result = Result(record)
        OdooSuite(chosen)(result)
        odoo_module.current_test = False
        return result

    threading.current_thread().dbname = args["db"]
    Registry(args["db"])  # loaded once: each copy (fork) has it

    def reference():
        result = run(tests(), record=True)
        return {"failed": sorted(result.failed), "reached": result.reached, "setup": result.setup, "times": result.times}

    started = time.time()
    status, first = in_child(reference, 3600, sql_db)
    if status != "ok":
        raise SystemExit(f"the tests of {args['module']} could not run: {first}")
    status, second = in_child(reference, 3600, sql_db)
    flaky = set(first["failed"]) ^ set(second["failed"] if status == "ok" else first["failed"])
    left_out = set(first["failed"]) | flaky
    usable = [t for t in tests() if t.id() not in left_out and f"class:{class_id(type(t))}" not in left_out]
    reference_seconds = (time.time() - started) / 2

    results = []
    for mutant in json.load(open(args["mutants"])):
        k = mutant["index"]
        if k % args["slice"][1] != args["slice"][0]:
            continue
        chosen = [t.id() for t in usable if k in first["reached"].get(t.id(), ())
                  or k in first["setup"].get(class_id(type(t)), ())]
        if not chosen:
            results.append({"index": k, "status": "not reached", "killers": [], "tests": []})
            print(f"JSA {k} not reached", flush=True)
            continue
        budget = 10 + 3 * sum(first["times"].get(name, 1) for name in chosen)

        def one():
            Registry(args["db"]).clear_all_caches()  # nothing the original code put in a cache is reused
            switch.ACTIVE = k
            return sorted(run([t for t in tests() if t.id() in chosen]).failed)

        began = time.time()
        status, failed = in_child(one, budget, sql_db)
        classes = {f"class:{name.rsplit('.', 1)[0]}" for name in chosen}
        killers = [name for name in failed if name in chosen or name in classes] if status == "ok" else []
        verdict = ("killed" if killers else "survived") if status == "ok" else "killed"
        results.append({"index": k, "status": verdict, "killers": killers, "tests": chosen,
                        "why": None if status == "ok" else status, "seconds": round(time.time() - began, 2)})
        print(f"JSA {k} {verdict}", flush=True)

    json.dump({"tests": [t.id() for t in tests()], "left_out": sorted(left_out), "flaky": sorted(flaky),
               "browser_tests": browser_tests(), "reference_seconds": round(reference_seconds, 1), "mutants": results},
              open(args["out"], "w"), indent=1)


def test_id(test):
    test = getattr(test, "test_case", test)  # a subTest
    name = test.id()
    if name.startswith(("setUpClass (", "tearDownClass (")):  # a class that failed: "setUpClass (module.Class)"
        return "class:" + name.split("(", 1)[1].rstrip(")")
    return name


def class_id(cls):
    return f"{cls.__module__}.{cls.__qualname__}"


def in_child(function, timeout, sql_db):
    """function() in a copy of this process (fork): ('ok', what it gave) | ('crash', why) | ('timeout', None). The
    copy opens its own connections to the database (those of this process are closed first)."""
    sql_db.close_all()
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read)
        try:
            data = pickle.dumps(("ok", function()))
        except BaseException:
            data = pickle.dumps(("crash", traceback.format_exc()[-1000:]))
        with os.fdopen(write, "wb") as f:
            f.write(data)
        os._exit(0)
    os.close(write)
    chunks, deadline = [], time.time() + timeout
    while True:
        left = deadline - time.time()
        if left <= 0 or not select.select([read], [], [], left)[0]:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            os.close(read)
            return "timeout", None
        chunk = os.read(read, 1 << 16)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read)
    os.waitpid(pid, 0)
    return pickle.loads(b"".join(chunks)) if chunks else ("crash", "the copy of Odoo died")


if __name__ == "__main__":
    main(sys.argv[1])
