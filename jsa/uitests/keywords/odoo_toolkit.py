"""What a keyword needs to act on Odoo's screens: used by the basic keywords (OdooKeywords.py) and by yours.

    from robot.api.deco import keyword
    from odoo_toolkit import odoo

    @keyword
    def confirm_quotation_for(customer):
        odoo.run("Open Menu", "Sales / Orders / Quotations")   # any keyword: basic, yours, or of the Browser library
        odoo.run("Create Record")
        odoo.run("Set Field", "Customer", customer)
        odoo.run("Click Button", "Confirm")
        odoo.expect("The status", "Sales Order", lambda: odoo.run("Get Text", odoo.selector("status")))

Everything specific to an Odoo version (where the buttons and fields are) is in odoo_<version>.py.
"""
import importlib
import json
import re
import time
from pathlib import Path

from robot.libraries.BuiltIn import BuiltIn

CHECK_TIMEOUT = 10  # seconds a check waits for the expected value: the screen may still be refreshing
ACTION_DELAY = 0.3  # seconds left to Odoo to send its requests after an action, before waiting for their end
# Count the requests the page is waiting for (Odoo's permanent connection to the server, for notifications, is left
# out: it never ends). Installed once per page.
COUNT_REQUESTS = r"""() => {
  if (window.__jsa) return;
  const state = window.__jsa = {pending: 0, last: Date.now()};
  const permanent = url => /websocket|longpolling|\/bus\//.test(String(url));
  const start = () => { state.pending++; state.last = Date.now(); };
  const end = () => { state.pending = Math.max(0, state.pending - 1); state.last = Date.now(); };
  const fetch0 = window.fetch;
  window.fetch = function (input, ...rest) {
    if (permanent(input && input.url || input)) return fetch0.call(this, input, ...rest);
    start();
    return fetch0.call(this, input, ...rest).finally(end);
  };
  const open0 = XMLHttpRequest.prototype.open, send0 = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) { this.__jsaPermanent = permanent(url); return open0.call(this, method, url, ...rest); };
  XMLHttpRequest.prototype.send = function (...args) {
    if (!this.__jsaPermanent) { start(); this.addEventListener('loadend', end, {once: true}); }
    return send0.apply(this, args);
  };
}"""
IS_QUIET = "() => !window.__jsa || (window.__jsa.pending === 0 && Date.now() - window.__jsa.last > 150)"


class Toolkit:

    def __init__(self):
        self._screens = None

    def run(self, name, *args):
        """Run a keyword by its name: a basic Odoo keyword, one of yours, or one of the Browser library
        (https://marketsquare.github.io/robotframework-browser/Browser.html). It shows as a step in the report."""
        # Robot Framework reads "\" as an escape in the values it is given: keep ours as they are
        args = [a.replace("\\", "\\\\") if isinstance(a, str) else a for a in args]
        return BuiltIn().run_keyword(name, *args)

    def variable(self, name, default=None):
        """A setting of the run: ODOO_URL, ODOO_DB, ODOO_VERSION, HEADLESS..."""
        return BuiltIn().get_variable_value("${%s}" % name, default)

    @property
    def version(self) -> int:
        """Major version of the tested Odoo (0 if unknown)."""
        return int(float(self.variable("ODOO_VERSION", 0)) or 0)

    @property
    def screens(self):
        """Selectors of the tested Odoo version: the most recent odoo_<version>.py that is not newer than it, else the
        oldest there is ('jsa uitests run' says when the tested version has no file of its own)."""
        if self._screens is None:
            versions = sorted(int(p.stem.split("_")[1]) for p in Path(__file__).parent.glob("odoo_[0-9]*.py"))
            usable = [v for v in versions if v <= self.version]
            self._screens = importlib.import_module(f"odoo_{usable[-1] if usable else versions[0]}")
        return self._screens

    def selector(self, name, text=None):
        """A selector of odoo_<version>.py, with its {text} replaced."""
        selector = self.screens.SELECTORS[name]
        return selector.replace("{text}", str(text).replace('"', '\\"')) if text is not None else selector

    def count(self, selector) -> int:
        """Number of elements on the screen matching a selector."""
        return self.run("Get Element Count", selector)

    def ready(self):
        """Wait until Odoo has finished what the last action started: no request to the server running, nothing
        loading. Call it after each action: without it, a check made right after a click would read the screen
        before it changed."""
        self.run("Evaluate JavaScript", None, COUNT_REQUESTS)
        time.sleep(ACTION_DELAY)
        busy = json.dumps(self.selector("busy"))
        self.run("Wait For Function", f"() => ({IS_QUIET})() && !document.querySelector({busy})", "timeout=30s")

    def fail(self, message):
        """Fail the test with a message for the user, and a picture of the screen in the report."""
        try:
            self.run("Take Screenshot", "EMBED")
        except Exception:
            pass  # no page to take a picture of
        raise AssertionError(message)

    def evaluate(self, function, argument):
        """Call a JavaScript function in the page with one argument. It returns an object; {error: "..."} fails the test."""
        result = self.run("Evaluate JavaScript", None, f"() => ({function})({json.dumps(argument)})")
        if result.get("error"):
            self.fail(result["error"])
        return result

    def field(self, label) -> dict:
        """The field of the form on screen with this label: {"id": of its input, "kind": text, many2one, selection,
        boolean, date or readonly, "value": what it shows}."""
        return self.evaluate(self.screens.FIND_FIELD, label)

    def expect(self, what, expected, read):
        """Check that `read()` gives the expected text, waiting a little for the screen to be up to date.
        `what` names the thing in the failure message: 'The status is "Quotation", expected "Sales Order"'."""
        expected = re.sub(r"\s+", " ", str(expected)).strip()
        deadline = time.monotonic() + CHECK_TIMEOUT
        while True:
            actual = re.sub(r"\s+", " ", str(read())).strip()
            if actual == expected:
                return
            if time.monotonic() > deadline:
                self.fail(f'{what} is "{actual}", expected "{expected}"')
            time.sleep(0.3)

    def choose(self, input_selector, value):
        """Type in a field proposing records (customer, product...) and pick the proposal."""
        self.run("Fill Text", input_selector, value)
        exact, close = self.selector("choice_exact", value), self.selector("choice", value)
        self.run("Wait For Elements State", close, "visible", "timeout=10s")
        self.run("Click", exact if self.count(exact) else close)
        self.ready()


odoo = Toolkit()
