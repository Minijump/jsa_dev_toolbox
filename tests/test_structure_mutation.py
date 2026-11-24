import json
import sys
import types

import pytest
import typer

from jsa.core import config
from jsa.structure import mutants, mutation, report, report_html, security
from jsa.structure.coverage import Result

MODEL = '''import logging
from odoo import fields, models, _
_logger = logging.getLogger(__name__)


class Sale(models.Model):
    _name = 'x.sale'
    amount = fields.Float(default=0.0)

    def compute(self, qty, price):
        """A docstring."""
        total = 0.0
        if qty > 10 and price:
            total = qty * price - 1
        elif not price:
            total += 2
        for line in self:
            if line.skip:
                continue
            line.amount = total
        self.write({'state': 'done'})
        _logger.info("done %s", total)
        if total < 0:
            raise ValueError(_("negative"))
        return total

    def action_open(self):
        self.message_post(body="opened")
        self.name = self.env['ir.sequence'].next_by_code('x.sale')
        if self.partner_id:
            self.message_post(body="partner")
        return {'type': 'ir.actions.act_window', 'res_model': 'x.sale', 'target': 'new'}

    def _private(self):
        return self.env['x'].create([(0, 0, {'a': 1})]), {}.get('k', 0.0), True
'''


def mutate(source, skip=()):
    found = []
    text = mutants.mutate_source(source, "models/sale.py", found, skip)
    return text, mutants.identify(found)


def test_the_mutants_of_a_model():
    text, found = mutate(MODEL)
    compile(text, "sale.py", "exec")
    changes = [(m["function"], m["op"], m["original"], m["mutated"]) for m in found]
    assert changes == [
        ("Sale.compute", "comparison", "qty > 10", "qty <= 10"),  # the opposite, never the boundary
        ("Sale.compute", "and/or", "qty > 10 and price", "qty > 10 or price"),
        ("Sale.compute", "arithmetic", "qty * price", "qty / price"),
        ("Sale.compute", "number", "1", "0"),
        ("Sale.compute", "arithmetic", "qty * price - 1", "qty * price + 1"),
        ("Sale.compute", "remove not", "not price", "price"),
        ("Sale.compute", "number", "2", "3"),
        ("Sale.compute", "arithmetic", "total += 2", "total -= 2"),
        ("Sale.compute", "negate condition", "line.skip", "not line.skip"),
        ("Sale.compute", "remove continue/break", "continue", ""),
        ("Sale.compute", "remove assignment", "line.amount = total", ""),
        ("Sale.compute", "remove call", "self.write({'state': 'done'})", ""),
        ("Sale.compute", "comparison", "total < 0", "total >= 0"),
        # the id of an x2many command, the default of .get(), are left alone
        ("Sale._private", "number", "0", "1"), ("Sale._private", "number", "1", "0"),
        ("Sale._private", "boolean", "True", "False")]
    # left alone: the first value of a local variable (total = 0.0), the number of a comparison, field definitions,
    # docstring, logging, raise and its message; action_open: chatter, a sequence name, the action returned, and the
    # `if` guarding a chatter message only
    assert "import jsa_mutant_switch as _jms" in text.splitlines()[0] or "import jsa_mutant_switch as _jms" in text
    assert [m["line"] for m in found[:2]] == [13, 13] and len({m["id"] for m in found}) == len(found)


def test_skip_leaves_out_the_methods_matching():
    assert mutate(MODEL, ["Sale.compute"])[1][0]["function"] == "Sale._private"
    assert {m["function"] for m in mutate(MODEL, ["_priv*"])[1]} == {"Sale.compute"}


def test_without_a_mutant_on_the_copy_behaves_like_the_original(monkeypatch):
    source = (
        "def price(qty, unit):\n"
        "    total = 0\n"
        "    if qty > 10:\n"
        "        total = qty * unit * 0.9\n"
        "    else:\n"
        "        total = qty * unit\n"
        "    return round(total, 2)\n")
    text, found = mutate(source)
    switch = types.SimpleNamespace(active=0)
    switch.m = lambda k: switch.active == k
    monkeypatch.setitem(sys.modules, "jsa_mutant_switch", switch)
    original, mutated = {}, {}
    exec(source, original)
    exec(text, mutated)
    # (a price with 3 decimals: without it, round(total, 2) -> round(total, 3) changes nothing, a test that misses it)
    cases = [(qty, unit) for qty in (0, 5, 10, 11, 20) for unit in (1, 2.5, 1.234)]
    assert [mutated["price"](*c) for c in cases] == [original["price"](*c) for c in cases]
    changed = []
    for mutant in found:  # each mutant on: the function does something else, for some input
        switch.active = mutant["index"]
        changed.append(any(mutated["price"](*c) != original["price"](*c) for c in cases))
    assert all(changed), [m for m, c in zip(found, changed) if not c]


def test_a_mutant_keeps_its_id_when_lines_move_and_a_new_one_when_its_code_changes():
    _, before = mutate(MODEL)
    _, moved = mutate("# a comment\n\n" + MODEL)
    assert [m["id"] for m in moved] == [m["id"] for m in before] and moved[0]["line"] == before[0]["line"] + 2
    _, changed = mutate(MODEL.replace("if total < 0:", "if total < 1:"))
    assert changed[-4]["id"] != before[-4]["id"] and changed[0]["id"] == before[0]["id"]
    twice = "def f(a, b):\n    return a > 1, a > 1\n"
    assert len({m["id"] for m in mutate(twice)[1]}) == 2  # the same change twice in a function: its rank tells them apart


def test_the_files_mutated_are_the_code_of_the_addon(tmp_path):
    addon = tmp_path / "my_addon"
    for rel in ("__init__.py", "__manifest__.py", "models/__init__.py", "models/sale.py", "tests/test_sale.py",
                "migrations/2.0/post.py", "hooks.py", "wizard/assign.py"):
        (addon / rel).parent.mkdir(parents=True, exist_ok=True)
        (addon / rel).write_text("x = 1\n")
    assert [rel for _, rel in mutants.code_files(addon)] == ["models/sale.py", "wizard/assign.py"]
    (addon / "models" / "sale.py").write_text(MODEL)
    out = tmp_path / "copies"
    mutants.main(str(addon), str(out), json.dumps(["Sale._private"]))
    found = json.loads((out / "mutants.json").read_text())
    assert len(found) == 13 and all(m["function"] == "Sale.compute" for m in found)
    assert "_jms.m(" in (out / "my_addon" / "models" / "sale.py").read_text()
    assert (out / "my_addon" / "tests" / "test_sale.py").read_text() == "x = 1\n"  # copied, not mutated


def test_settings_tags_and_names(capsys):
    assert mutation.settings({}) == {"addons": [], "skip": []}
    assert mutation.settings({"mutation": {"addons": ["a"], "skip": ["action_*"]}}) == {"addons": ["a"], "skip": ["action_*"]}
    for wrong, why in (({"mutation": {"addon": ["a"]}}, "has the key(s) addon, which do not exist"),
                       ({"mutation": {"skip": "x"}}, "skip must be a list of names")):
        with pytest.raises(typer.Exit):
            mutation.settings(wrong)
        assert why in capsys.readouterr().out
    assert mutation.test_tags("m", ["odoo.addons.m.tests.test_x.TestX.test_a", "odoo.addons.m.tests.test_y.TestY.test_b"]) \
        == "/m:TestX.test_a,/m:TestY.test_b"
    assert mutation.short_names(["odoo.addons.m.tests.test_x.TestX.test_a", "class:odoo.addons.m.tests.test_y.TestY"]) \
        == ["TestX.test_a", "TestY"]


RESULT = {
    "addon": "my_addon", "date": "2026-11-24T10:00:00", "odoo": 19, "seconds": 125, "tests": 4, "browser_tests": 1,
    "left_out": ["TestX.test_flaky"],
    "mutants": [
        {"id": "aaaaaaaaaa", "file": "models/sale.py", "line": 21, "function": "Sale.compute", "op": "remove call",
         "original": "self.write({'state': 'done'})", "mutated": "", "status": "survived", "killers": []},
        {"id": "bbbbbbbbbb", "file": "models/sale.py", "line": 13, "function": "Sale.compute", "op": "comparison",
         "original": "qty > 10", "mutated": "qty <= 10", "status": "survived", "killers": [], "checked": True},
        {"id": "cccccccccc", "file": "models/sale.py", "line": 14, "function": "Sale.compute", "op": "arithmetic",
         "original": "qty * price", "mutated": "qty / price", "status": "killed", "killers": ["TestX.test_a"]},
        {"id": "dddddddddd", "file": "models/sale.py", "line": 35, "function": "Sale._private", "op": "boolean",
         "original": "True", "mutated": "False", "status": "not reached", "killers": []}],
    "runs": [{"date": "2026-11-20T10:00:00", "killed": 1, "survived": 3, "not reached": 1},
             {"date": "2026-11-24T10:00:00", "killed": 1, "survived": 2, "not reached": 1}]}


def test_the_report_lists_the_changes_no_test_notices(tmp_path):
    assert report_html.mutation_counts(RESULT) == {"killed": 1, "survived": 2, "not reached": 1}
    assert report_html.mutation_score(RESULT) == 33 and report_html.mutation_score({"mutants": []}) is None
    (tmp_path / "mutation").mkdir()
    (tmp_path / "mutation" / "my_addon.json").write_text(json.dumps(RESULT))
    (tmp_path / "mutation" / "my_addon_odoo.log").write_text("not json")
    assert list(report.load_mutation(tmp_path)) == ["my_addon"]
    results = {"my_addon": Result(60, tests=4, statements=10, covered=6), "other": Result(50, tests=1, statements=2, covered=1)}
    report.write_reports({"my_addon": 40, "other": 5}, {"my_addon": ["base"], "other": []}, results, tmp_path,
                         reviewed_changes={"bbbbbbbbbb": "the boundary has its own test elsewhere"})
    page = (tmp_path / "report.html").read_text(encoding="utf-8")
    section = page[page.index('<section id="mutation">'):page.index("</section>", page.index('<section id="mutation">'))]
    assert "<h2>Untested behaviour: mutation testing (1 change(s) not noticed)</h2>" in section
    assert "my_addon · 33% of the changes reached noticed" in section
    assert "If <code>self.write({&#x27;state&#x27;: &#x27;done&#x27;})</code> is removed, no test fails." in section
    assert "Reviewed: the boundary has its own test elsewhere" in section and "checked by a real install" in section
    assert "1 browser test(s) (tours) not run" in section and "<code>TestX.test_flaky</code>" in section
    assert section.count('<button class="copy"') == 1 and 'aria-label="Score of my_addon"' in section  # two runs: a chart
    entry = report_html.mutation_entry("my_addon", RESULT["mutants"][0])
    assert entry.startswith('[[mutation]]\nid = "aaaaaaaaaa"  # my_addon/models/sale.py:21 | Sale.compute | remove call\n')
    assert 'data-key="mutation"' in page and 'data-mutation="33"' in page and 'data-mutation="-1"' in page


def test_reviewed_changes_in_their_own_section(tmp_path, capsys):
    path = tmp_path / "reviewed.toml"
    path.write_text('[[security]]\nid = "s"\nreason = "fine"\n\n[[mutation]]\nid = "m"\nreason = "cannot matter"\n',
                    encoding="utf-8")
    assert security.load_reviewed(path) == {"s": "fine"}
    assert security.load_reviewed(path, "mutation") == {"m": "cannot matter"}
    path.write_text('[[mutation]]\nid = "m"\n', encoding="utf-8")
    with pytest.raises(typer.Exit):
        security.load_reviewed(path, "mutation")
    assert "[[mutation]] #1 (m): say in reason why it is fine" in capsys.readouterr().out


def test_mutation_needs_linux_and_addons(monkeypatch, capsys):
    monkeypatch.setattr(mutation.sys, "platform", "win32")
    with pytest.raises(typer.Exit):
        mutation.mutation(["a"], "19", 0, False)
    assert "runs on Linux only" in capsys.readouterr().out
    monkeypatch.setattr(mutation.sys, "platform", "linux")
    monkeypatch.setattr(config, "load_config", lambda required=True: {})
    with pytest.raises(typer.Exit):
        mutation.mutation(None, "19", 0, False)
    assert "Which addons?" in capsys.readouterr().out
