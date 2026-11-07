import pytest
import typer

from jsa.core.addons import DependencyGraph
from jsa.core.odoo import LogInfo
from jsa.uitests import testdb


def make_addon(folder, name, manifest="{'name': 'x', 'depends': ['base']}"):
    addon = folder / name
    addon.mkdir(parents=True)
    (addon / "__manifest__.py").write_text(manifest)
    (addon / "models.py").write_text("x = 1")
    return addon


def test_modules_come_from_the_config_else_from_the_custom_addons(tmp_path):
    make_addon(tmp_path, "b_addon")
    make_addon(tmp_path, "a_addon")
    make_addon(tmp_path, "old_addon", "{'installable': False}")
    conf = {"codebase": {"addons_roots": [str(tmp_path), str(tmp_path / "missing")]}}
    assert testdb.requested_modules(conf) == ["a_addon", "b_addon"]
    assert testdb.requested_modules({**conf, "uitests": {"modules": ["sale_management", "a_addon"]}}) == ["a_addon", "sale_management"]
    assert testdb.requested_modules({}) == []
    assert testdb.names("19") == ("uitests_19__template", "uitests_19")
    assert testdb.names("odoo-19.ent") == ("uitests_odoo_19_ent__template", "uitests_odoo_19_ent")


def test_template_is_rebuilt_when_what_it_holds_changes(tmp_path):
    odoo_root, mine = tmp_path / "odoo", tmp_path / "mine"
    make_addon(odoo_root / "addons", "sale")
    make_addon(odoo_root / "odoo" / "addons", "base")
    addon = make_addon(mine, "my_addon", "{'depends': ['sale']}")
    profile = {"odoo_root": str(odoo_root), "enterprise": None, "addons_paths": [str(mine), str(odoo_root / "addons")]}
    # your addons are those that are not in the folders of Odoo
    custom = testdb.custom_addons(profile, ["my_addon", "sale", "base", "not_there"])
    assert custom == {"my_addon": addon}

    built = testdb.fingerprint(["my_addon"], custom, True, "commit1")
    assert testdb.fingerprint(["my_addon"], custom, True, "commit1") == built  # nothing changed: the template is reused
    assert testdb.fingerprint(["my_addon", "sale"], custom, True, "commit1") != built  # another list of modules
    assert testdb.fingerprint(["my_addon"], custom, False, "commit1") != built  # without demo data
    assert testdb.fingerprint(["my_addon"], custom, True, "commit2") != built  # Odoo was updated
    (addon / "models.py").write_text("x = 2")
    assert testdb.fingerprint(["my_addon"], custom, True, "commit1") != built  # the code of a custom addon changed


class Databases:
    """What would be installed where, without Odoo. `broken`: the addons that fail to install."""
    def __init__(self, graph, broken=()):
        self.graph, self.broken, self.dbs, self.installs = graph, set(broken), {}, []

    def install(self, db, modules, label):
        self.installs.append((db, tuple(modules)))
        failing = sorted(self.broken & self.graph.closure(modules))
        if failing:
            return LogInfo(loaded=[failing[0]], registry_failed=True)
        self.dbs[db] |= set(modules)
        return LogInfo()

    def copy(self, source, target):
        assert target not in self.dbs, f"{target} already exists"
        self.dbs[target] = set(self.dbs[source]) if source else set()

    def drop(self, db):
        self.dbs.pop(db, None)

    def build(self, modules, custom):
        return testdb.install_modules("tpl", modules, dict.fromkeys(custom), self.graph, self.install, self.copy, self.drop)


GRAPH = DependencyGraph({"sale": ["base"], "contacts": ["base"], "a": ["sale"], "b": ["a"], "c": ["contacts"]})


def test_standard_modules_first_then_the_custom_addons():
    dbs = Databases(GRAPH)
    assert dbs.build(["b", "c"], ["a", "b", "c"]) == []
    assert dbs.installs == [("tpl_std", ("base", "contacts", "sale")), ("tpl", ("a", "b", "c"))]
    assert dbs.dbs == {"tpl": {"base", "contacts", "sale", "a", "b", "c"}}  # only the template is left

    dbs = Databases(GRAPH)  # only standard modules: installed directly in the template
    assert dbs.build(["sale"], []) == []
    assert dbs.installs == [("tpl", ("base", "sale"))] and dbs.dbs == {"tpl": {"base", "sale"}}


def test_a_custom_addon_that_fails_is_left_out_with_those_depending_on_it(capsys):
    dbs = Databases(GRAPH, broken=["a"])
    assert dbs.build(["b", "c"], ["a", "b", "c"]) == ["a", "b"]
    # the standard modules are not installed again: the second try starts from a copy of them
    assert dbs.installs == [("tpl_std", ("base", "contacts", "sale")), ("tpl", ("a", "b", "c")), ("tpl", ("c",))]
    assert dbs.dbs == {"tpl": {"base", "contacts", "sale", "c"}}
    assert "a failed to install: left out, with 1 addon(s) depending on it" in capsys.readouterr().out

    dbs = Databases(GRAPH, broken=["c"])  # every custom addon fails: the standard modules alone
    assert dbs.build(["c"], ["c"]) == ["c"] and dbs.dbs == {"tpl": {"base", "contacts"}}

    dbs = Databases(GRAPH, broken=["sale"])  # a standard module fails: nothing can be built
    with pytest.raises(typer.Exit):
        dbs.build(["b"], ["a", "b"])
    assert dbs.dbs == {}
