import json
import textwrap

import pytest
import typer

from jsa.core import config
from jsa.structure import codebase, report, report_html, security

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

MODELS = '''
from markupsafe import Markup
from odoo import api, fields, models


class Sale(models.Model):
    _name = 'x.sale'
    _inherit = ['mail.thread']
    company_id = fields.Many2one('res.company')
    note = fields.Html(sanitize=False)
    body = fields.Html()

    def action_confirm(self):
        self.sudo().write({'state': 'done'})

    def _private(self):
        return self.sudo()

    @api.private
    def hidden(self):
        return self.sudo()

    def search_by_name(self, name):
        self.env.cr.execute("SELECT id FROM x_sale WHERE name = '%s'" % name)
        query = f"SELECT id FROM {self._table} WHERE id = %s"
        self.env.cr.execute(query, [self.id])
        self.env.cr.execute("SELECT 1 FROM x WHERE id = %s", (name,))
        self.env.cr.execute("SELECT 1 FROM x WHERE id IN %s" % (tuple(self.ids),))
        self.env.cr.execute("SELECT %s FROM x" % self._select())
        where = " AND active"
        where = where + " AND 1 = 1"
        self.env.cr.execute("SELECT 1 FROM x WHERE id = %s" + where, [self.id])
        q2 = "SELECT id FROM x WHERE "
        q2 += "name = " + name
        self._cr.execute(q2)
        self.env.cr.execute(f"SELECT 1 FROM x WHERE ref = '{self.env.context.get('ref')}'")

    def init(self):
        self.env.cr.execute("CREATE VIEW %s AS (%s)" % (self._table, self.name))

    def html(self, value):
        a = Markup("<b>%s</b>") % value
        b = Markup("<b>%s</b>" % value)
        c = Markup(value)
        return eval(value), a, b, c


class Wizard(models.TransientModel):
    _name = 'x.wizard'
    company_id = fields.Many2one('res.company')

    def action_go(self):
        return self.sudo()


class Covered(models.Model):
    _name = 'x.covered'
    company_id = fields.Many2one(comodel_name='res.company')


class Extended(models.Model):
    _inherit = 'res.partner'
    company_id = fields.Many2one('res.company')
'''

CONTROLLERS = '''
from odoo import http
from odoo.http import request, route


class Main(http.Controller):
    @http.route('/x/public', type='http', auth='public', csrf=False)
    def public(self, **kw):
        return request.env['x.sale'].sudo().search([])

    @route(['/x/user'], auth='user')
    def user(self):
        return request.env['x.sale'].sudo().search([])

    @http.route('/x/json', type='json', auth='none', csrf=False)
    def json_route(self):
        return 1

    @http.route('/x/get', type='http', auth='public', methods=['GET'], csrf=False)
    def get_route(self):
        return 1
'''

ACCESS = """id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
access_sale_all,sale all,model_x_sale,,1,1,0,0
access_sale_read,sale read,model_x_sale,,1,0,0,0
access_sale_user,sale user,model_x_sale,base.group_user,1,1,1,1
"""

RULES_XML = """<odoo>
  <record id="rule_covered" model="ir.rule">
    <field name="model_id" ref="model_x_covered"/>
    <field name="domain_force">[('company_id', 'in', company_ids)]</field>
  </record>
  <record id="rule_sale_admin" model="ir.rule">
    <field name="model_id" ref="model_x_sale"/>
    <field name="domain_force">[(1, '=', 1)]</field>
  </record>
</odoo>
"""

TEMPLATES = """<odoo>
  <template id="page">
    <div t-raw="value"/>
    <div t-out="value"/>
  </template>
</odoo>
"""


def make_addon(root, name="my_addon", models=MODELS):
    addon = root / name
    for folder in ("models", "controllers", "security", "views", "tests", "upgrades/2.0"):
        (addon / folder).mkdir(parents=True, exist_ok=True)
    (addon / "__manifest__.py").write_text("{'depends': ['base']}")
    (addon / "models" / "sale.py").write_text(models)
    (addon / "controllers" / "main.py").write_text(CONTROLLERS)
    (addon / "security" / "ir.model.access.csv").write_text(ACCESS)
    (addon / "security" / "rules.xml").write_text(RULES_XML)
    (addon / "views" / "templates.xml").write_text(TEMPLATES)
    # not code: never looked at
    (addon / "tests" / "test_sale.py").write_text("def test(): eval('1'); self.sudo()\n")
    (addon / "upgrades" / "2.0" / "post-x.py").write_text("def migrate(cr, v): cr.execute('x %s' % v)\n")
    return addon


def scan(root):
    addons = {p.name: p for p in root.iterdir() if (p / "__manifest__.py").exists()}
    return security.scan(addons, codebase.code_files)


def test_every_rule_finds_what_it_says_and_leaves_out_the_rest(tmp_path):
    make_addon(tmp_path)
    findings, unreadable = scan(tmp_path)
    assert unreadable == []
    found = {}
    for f in findings:
        found.setdefault(f.rule, []).append(f)
    places = lambda key: sorted((f.where, f.code) for f in found.get(key, []))
    # public methods of models (wizards included), not "_private", not @api.private, not the controllers
    assert places("sudo_rpc") == [("Sale.action_confirm", "self.sudo().write({'state': 'done'})"),
                                  ("Wizard.action_go", "return self.sudo()")]
    assert "callable over RPC" in found["sudo_rpc"][0].why and "x.sale" in found["sudo_rpc"][0].why
    # auth='public' with sudo(); auth='user' left out
    assert places("route_sudo") == [("Main.public", "return request.env['x.sale'].sudo().search([])")]
    assert "/x/public" in found["route_sudo"][0].why
    # csrf=False on an http route; json routes and GET-only routes left out
    assert [f.where for f in found["csrf_off"]] == ["Main.public"]
    # no group + write: everyone; read alone, or a group, left out
    assert places("access_everyone") == [("access_sale_all", "access_sale_all,sale all,model_x_sale,,1,1,0,0")]
    assert "can write (model_x_sale)" in found["access_everyone"][0].why
    # a company and no rule mentioning it; a rule covering it, a wizard, an extended model left out
    assert [f.where for f in found["company_rule"]] == ["Sale"]
    # built with a value from outside: an argument, the context; not the table, the ids, what a method returns,
    # constants added together, parameters, init()
    assert places("sql_built") == [
        ("Sale.search_by_name", 'self._cr.execute(q2)'),
        ("Sale.search_by_name", 'self.env.cr.execute("SELECT id FROM x_sale WHERE name = \'%s\'" % name)'),
        ("Sale.search_by_name", 'self.env.cr.execute(f"SELECT 1 FROM x WHERE ref = \'{self.env.context.get(\'ref\')}\'")')]
    # Markup() around a formatted string, an Html field not sanitized, t-raw; not Markup(...) % value, Markup(value)
    assert places("html_unescaped") == [("Sale.html", 'b = Markup("<b>%s</b>" % value)'),
                                        ("Sale.note", "note = fields.Html(sanitize=False)"),
                                        ("page", '<div t-raw="value"/>')]
    assert [f.where for f in found["eval"]] == ["Sale.html"]
    assert all(f.file in ("models/sale.py", "controllers/main.py", "security/ir.model.access.csv",
                          "views/templates.xml") for f in findings)  # not the tests, not upgrades/
    assert len({f.id for f in findings}) == len(findings)
    # every rule says what it looks for, then what it leaves out
    assert all(r.text and r.title and r.kind in ("Access control", "Injection") for r in security.RULES)
    assert [r.key for r in security.RULES] == ["sudo_rpc", "route_sudo", "csrf_off", "access_everyone", "company_rule",
                                               "sql_built", "html_unescaped", "eval"]


def test_a_finding_keeps_its_id_when_lines_move_and_gets_a_new_one_when_its_code_changes(tmp_path):
    make_addon(tmp_path)
    before = {(f.rule, f.where, f.code): f for f in scan(tmp_path)[0]}
    (tmp_path / "my_addon" / "models" / "sale.py").write_text("\n\n# moved down\n" + MODELS.replace(
        "self.sudo().write({'state': 'done'})", "self.sudo().write({'state': 'cancel'})"))
    after = {(f.rule, f.where, f.code): f for f in scan(tmp_path)[0]}
    moved = next(f for f in after.values() if f.rule == "eval")
    old = next(f for f in before.values() if f.rule == "eval")
    assert moved.line == old.line + 3 and moved.id == old.id
    confirm = lambda found: next(f for f in found.values() if f.where == "Sale.action_confirm")
    assert confirm(after).id != confirm(before).id  # the code changed: to look at again


def test_the_same_code_twice_in_the_same_place_gets_two_ids(tmp_path):
    make_addon(tmp_path)
    (tmp_path / "my_addon" / "views" / "templates.xml").write_text(TEMPLATES.replace(
        '<div t-out="value"/>', '<div t-raw="value"/>'))
    ids = [f.id for f in scan(tmp_path)[0] if f.rule == "html_unescaped" and f.where == "page"]
    assert len(ids) == 2 and ids[1] == ids[0] + "-2"


def test_a_python_file_that_does_not_parse_is_said(tmp_path):
    make_addon(tmp_path)
    (tmp_path / "my_addon" / "models" / "broken.py").write_text("def x(:\n")
    assert scan(tmp_path)[1] == ["my_addon/models/broken.py"]


def test_reviewed_file(tmp_path, capsys):
    path = tmp_path / "reviewed.toml"
    assert security.load_reviewed(path) == {}  # none yet
    entry = security.review_entry({"rule": "sudo_rpc", "id": "abc123", "addon": "a", "file": "models/x.py",
                                   "where": "X.action"})
    assert entry.startswith('[[security]]\nid = "abc123"  # sudo() in a method callable over RPC | a/models/x.py | X.action\n')
    path.write_text(entry.replace('reason = ""', 'reason = "only managers see the button"'), encoding="utf-8")
    assert security.load_reviewed(path) == {"abc123": "only managers see the button"}
    for text, why in ((entry, "say in reason why it is fine"),
                      ('[[security]]\nid = "a"\nreason = "b"\nwhy = "c"\n', "has the key(s) why, which do not exist"),
                      ('[[security]]\nreason = "b"\n', "needs the id (the report gives the lines to copy)"),
                      ('security = "x"\n', "write each one as a [[security]] table"),
                      ('[[security]\n', "not valid TOML")):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(typer.Exit):
            security.load_reviewed(path)
        assert why in capsys.readouterr().out


def test_the_report_lists_the_findings_and_the_chart_counts_those_not_reviewed(tmp_path, monkeypatch, capsys):
    roots, out = tmp_path / "addons", tmp_path / "out"
    make_addon(roots)
    reviewed = tmp_path / "reviewed.toml"
    monkeypatch.setattr(config, "REVIEWED_FILE", reviewed)
    conf = {"codebase": {"addons_roots": [str(roots)]}}
    codebase.analyse(conf, [roots], out)
    said = capsys.readouterr().out
    assert "🔒 Security review: 13 to review\n" in said  # the first time: nothing is new
    state, _ = report.load_state(out)
    findings = state["my_addon"]["security"]
    assert len(findings) == 13 and {"rule", "addon", "file", "line", "where", "code", "why", "id"} <= set(findings[0])
    page = (out / "report.html").read_text(encoding="utf-8")
    assert '<section id="security"><h2>Security review (13 to review)</h2>' in page
    assert 'class="tag"' not in page  # nothing new the first time
    assert "Left out: methods whose name starts with" in page  # what each rule does, in the page
    assert page.count('<button class="copy"') == 13 and 'data-security="13"' in page

    # one reviewed, one new finding in the code, one line of reviewed.toml matching nothing
    sudo = next(f for f in findings if f["rule"] == "sudo_rpc" and f["where"] == "Sale.action_confirm")
    reviewed.write_text(security.review_entry(sudo).replace('reason = ""', 'reason = "checked: managers only"')
                        + '[[security]]\nid = "0000000000"\nreason = "gone"\n', encoding="utf-8")
    (roots / "my_addon" / "models" / "other.py").write_text("x = eval('1')\n")
    codebase.analyse(conf, [roots], out)
    assert "🔒 Security review: 13 to review, 1 new (1 reviewed)" in capsys.readouterr().out
    page = (out / "report.html").read_text(encoding="utf-8")
    assert '<h2>Security review (13 to review, 1 new)</h2>' in page
    new = page.index('<span class="tag">new</span>')
    assert new < page.index("Reviewed (1)") and "models/other.py" in page[new:new + 300]  # the new one first
    assert "Reviewed: checked: managers only" in page
    assert "match no finding anymore" in page and "<code>0000000000</code>" in page

    # the history keeps the ids of each rule: the chart counts those not reviewed, with today's reviewed.toml
    state, _ = report.load_state(out)
    run = {"date": "2026-11-24T10:00:00", "mode": "whole", "odoo": 19, "coverage": 50.0, "loc": 10,
           **{"security": report.summarize(state)["security"]}}
    assert sum(len(ids) for ids in run["security"].values()) == 14
    assert report_html.open_findings(run, {sudo["id"]: "x"}) == 13 and report_html.open_findings({}, {}) is None
    chart = report_html._trend([run, {**run, "date": "2026-11-25T10:00:00"}], [], None, {sudo["id"]: "x"})
    section = chart[chart.index('data-chart="security"'):]
    assert '<span class="tname">Security findings to review</span><span class="tnow">13</span>' in section
    assert "sudo() in a method callable over RPC: 1" in section  # the tooltip: rule by rule
    old_runs = report_html._trend([{**run, "security": None}, {**run, "security": None}], [])
    assert "recorded from the next run measuring the coverage (-c)" in old_runs[old_runs.index('data-chart="security"'):]


def test_counts_have_whole_ticks():
    assert report_html.zoomed_axis([0, 0], whole=True) == (0, 1, 1)  # not 0 to 0.5
    assert report_html.zoomed_axis([3, 4], whole=True)[2] == 1
    assert report_html.zoomed_axis([12000, 12480], whole=True) == (12000, 12500, 250)
    assert report_html.zoomed_axis([0, 0])[2] == 0.5  # a measure in %: halves are fine


def test_backfill_adds_the_review_to_the_commits_already_measured():
    from plugins.backfill import missing_security, with_security
    history = [{"commit": "a", "mode": "whole"}, {"commit": "a", "mode": "isolated", "security": {"eval": ["x"]}},
               {"commit": "b", "mode": "whole", "security": None}, {"date": "2026-01-01", "event": "e", "commit": "a"},
               {"mode": "whole"}]
    assert missing_security(history, {"a", "b"}) == {"a", "b"}
    assert missing_security(history, {"b"}) == {"b"}
    found = {"a": {"sql_built": ["y"]}}
    assert with_security(history[0], found) == {"commit": "a", "mode": "whole", "security": {"sql_built": ["y"]}}
    assert with_security(history[1], found) == history[1]  # already reviewed: kept
    assert with_security(history[3], found) == history[3]  # an event
