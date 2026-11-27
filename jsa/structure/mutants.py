"""The mutants of an addon, for 'jsa mutation': small changes of its code, every one compiled into a copy of the addon.

Not imported by Odoo: the python of a profile runs it (so the code it writes is the Python of that Odoo), jsa imports
it for its own tests. Standard library only.
    python mutants.py <addon folder> <folder of the copy> <skip: JSON list of method name globs>
writes <folder of the copy>/<addon>/ and <folder of the copy>/mutants.json.

A mutant is made by a rule at a place of a method's body; every applicable (place, rule) gives one, the same code gives
the same mutants (never random). In the copy, mutant k is active when jsa_mutant_switch.ACTIVE == k (`_jms.m(k)`); with
no mutant active the copy behaves like the original. Only the bodies of functions are changed: class bodies (fields),
decorators and default values run once, when Odoo imports the module.

The rules and what is left alone: RULES and LEFT_ALONE below (the report shows them).
"""
import ast
import copy
import fnmatch
import hashlib
import json
import shutil
import sys
from pathlib import Path

RULES_VERSION = 2  # a change of what is mutated: the results of another version are not reused
# What each rule changes, as the report shows it (report_html._mutation; `code` between backticks): (the rule = the
# `op` of its mutants, what it changes, an example). A unit test checks it names every rule of the Mutator below.
RULES = (
    ("comparison", "A comparison becomes its opposite: `<` and `>=`, `<=` and `>`, `==` and `!=`, `in` and `not in`, "
                   "`is` and `is not`. Never the boundary (`>` to `>=`): often a change that cannot matter.",
     "`qty > 10` → `qty <= 10`"),
    ("and/or", "`and` and `or` are swapped.", "`paid and done` → `paid or done`"),
    ("remove not", "A `not` is removed.", "`not done` → `done`"),
    ("negate condition", "A condition that is not a comparison, an and/or nor a `not` is negated: of an `if`, of "
                         "`a if c else b`, of the `if` of a comprehension.", "`if lines:` → `if not lines:`"),
    ("arithmetic", "`+` and `-`, `*` and `/` are swapped, so are `+=` and `-=`, `*=` and `/=` (not when a side is "
                   "text, a list or a tuple).", "`qty * price` → `qty / price`"),
    ("boolean", "`True` and `False` are swapped.", "`True` → `False`"),
    ("number", "A number changes: 0 becomes 1, 1 becomes 0, any other becomes itself + 1.", "`0.9` → `1.9`"),
    ("remove call", "A call alone on its line is removed.", "`order.action_confirm()` → removed"),
    ("remove assignment", "An assignment to a field or a key is removed.", "`self.note = text` → removed"),
    ("remove continue/break", "A `continue` or a `break` is removed.", "`continue` → removed"),
    ("empty return", "A value returned is emptied, only when its kind is sure (so the empty one is always valid): the "
                     "records of `browse`, `search`, `filtered`, `filtered_domain`, `sorted`, `mapped` → none, "
                     "`sum(x)` and `len(x)` → 0, a list comprehension → `[]`. Not `return res`: its kind is unknown.",
     "`return self.browse(ids)` → `return self.browse(ids)[:0]`"),
)
LEFT_ALONE = (
    "Only the code inside functions is changed (not the fields, decorators, default values), not the tests, "
    "migrations, hooks, manifests nor `__init__.py`. Never changed: text (strings, f-strings), `raise` and its message, "
    "logging, `_()`, `assert`, `ensure_one()`, `super()` calls, flush and invalidate. Left alone because changing them "
    "cannot matter: the id of `(0, 0, vals)`, the default of `.get(k, 0.0)`, the first value of a local variable, "
    "`return True` of a public method, the number in a comparison. Left alone because nobody tests them: the action a "
    "method returns to the web client, chatter messages and activities, names taken from a sequence, an `if` guarding "
    "only those. And the methods matching `[mutation] skip` of config.toml.")
SKIP_CALLS = {"_", "_lt", "print", "warn"}
LOGGERS = {"_logger", "logger", "logging", "_log"}
NO_DELETE = {"ensure_one", "super", "flush_model", "flush_recordset", "flush_all", "invalidate_recordset",
             "invalidate_model", "invalidate_all"}
OPPOSITE = {ast.Lt: ast.GtE, ast.LtE: ast.Gt, ast.Gt: ast.LtE, ast.GtE: ast.Lt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
            ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}
ARITHMETIC = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}
REMOVALS = ("remove call", "remove assignment", "remove continue/break")
RECORDS = {"browse", "search", "search_fetch", "filtered", "filtered_domain", "sorted", "mapped"}  # give records


def call_name(node):
    func = getattr(node, "func", None)
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def is_logging(node):
    func = node.func
    while isinstance(func, ast.Attribute):
        func = func.value
    return isinstance(func, ast.Name) and func.id in LOGGERS


def of_no_interest(name):
    """Calls whose effect nobody tests and nobody needs to: chatter, activities, sequence names."""
    return bool(name) and (name.startswith(("message_post", "activity_")) or
                           name in ("message_notify", "message_subscribe", "message_unsubscribe", "next_by_code",
                                    "next_by_id"))


def stringy(node):
    return isinstance(node, (ast.JoinedStr, ast.List, ast.Tuple)) or (
        isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)))


def quiet(stmt):
    """A statement left alone, whole: what a method returns to the web client, chatter, a sequence name."""
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Return):
        value = stmt.value
        return isinstance(value, ast.Dict) or (isinstance(value, ast.Call) and call_name(value) == "dict")
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        return of_no_interest(call_name(stmt.value))
    if isinstance(stmt, (ast.Assign, ast.AugAssign)):
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        if any(isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == "action" for t in targets):
            return True
        return any(isinstance(n, ast.Call) and call_name(n) in ("next_by_code", "next_by_id") for n in ast.walk(stmt.value))
    return False


def is_action(node):
    """A dict with a 'type' key 'ir.actions...': a window action, a notification for the web client."""
    return isinstance(node, ast.Dict) and any(
        isinstance(k, ast.Constant) and k.value == "type" and isinstance(v, ast.Constant) and isinstance(v.value, str)
        and v.value.startswith("ir.actions") for k, v in zip(node.keys, node.values))


def constants_left_alone(fn):
    """Constants whose change gave equivalent mutants (no test could ever notice) in the prototype."""
    skip = set()
    public = not fn.name.startswith("_")
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare):  # x > 0 -> x > 1: a boundary
            skip.update(id(c) for c in [node.left, *node.comparators] if isinstance(c, ast.Constant))
        elif isinstance(node, ast.Call) and call_name(node) == "get" and len(node.args) >= 2:  # d.get(k, 0.0)
            skip.add(id(node.args[1]))
        elif isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == 3 and isinstance(node.elts[0], ast.Constant) \
                and node.elts[0].value in range(7):  # x2many command (0, 0, vals): the id is ignored
            skip.update(id(e) for e in node.elts[1:])
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            skip.add(id(node.value))  # total = 0.0: the first value of a local variable (often overwritten)
        elif isinstance(node, ast.Return) and public and isinstance(node.value, ast.Constant):
            skip.add(id(node.value))  # return True of a button: nobody reads it
    return skip


def emptied(value):
    """What a returned value becomes when emptied, only when its kind is sure (the empty value of that kind is then
    always valid where the value was: no crash, a test noticing nothing means no test looks at what is returned);
    None otherwise (`return res`, `return x.field`: its kind is unknown, emptying it could be a false alarm).
    Records (Odoo's methods giving records: `[:0]` of a recordset is the empty one of the same model, of a list `[]`;
    not when called on super() (left alone, like its calls) nor re.search, which gives a match), the builtins sum (one
    argument: sum(x, start) may not be a number) and len -> 0, a list comprehension -> []."""
    if isinstance(value, ast.ListComp):
        return lambda n: ast.List(elts=[], ctx=ast.Load())
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    if isinstance(func, ast.Name) and func.id in ("sum", "len") and len(value.args) == 1 and not value.keywords:
        return lambda n: ast.Constant(0)
    if isinstance(func, ast.Attribute) and func.attr in RECORDS and not (
            isinstance(func.value, ast.Call) and call_name(func.value) == "super") and not (
            isinstance(func.value, ast.Name) and func.value.id == "re"):
        return lambda n: ast.Subscript(value=n, slice=ast.Slice(upper=ast.Constant(0)), ctx=ast.Load())
    return None


def guard(k):
    return ast.Call(func=ast.Attribute(value=ast.Name("_jms", ast.Load()), attr="m", ctx=ast.Load()),
                    args=[ast.Constant(k)], keywords=[])


class Mutator(ast.NodeTransformer):
    """Puts the mutants of one function into it (switches), recording each one in `found`."""

    def __init__(self, file, function, found, skip):
        self.file, self.function, self.found, self.skip = file, function, found, skip

    def new(self, node, op, original, mutated):
        k = len(self.found) + 1
        self.found.append({"index": k, "file": self.file, "line": node.lineno, "function": self.function, "op": op,
                           "original": original, "mutated": mutated})
        return k

    def expr(self, node, op, make):
        """node, or make(node) when its mutant is active (make applied to the already mutated children)."""
        before = copy.deepcopy(node)
        node = self.generic_visit(node)
        k = self.new(node, op, ast.unparse(before), ast.unparse(make(before)))
        return ast.copy_location(ast.IfExp(test=guard(k), body=make(copy.deepcopy(node)), orelse=node), node)

    def stmt(self, node, op, make=None):
        """The statement replaced (make), or removed (no make), when its mutant is active."""
        before = copy.deepcopy(node)
        node = self.generic_visit(node)
        k = self.new(node, op, ast.unparse(before), ast.unparse(make(before)) if make else "")
        if make is None:
            return ast.copy_location(ast.If(test=ast.UnaryOp(op=ast.Not(), operand=guard(k)), body=[node], orelse=[]), node)
        return ast.copy_location(ast.If(test=guard(k), body=[make(copy.deepcopy(node))], orelse=[node]), node)

    def body(self, statements):
        return [s if quiet(s) else self.visit(s) for s in statements]

    # ---- left alone
    def visit_Call(self, node):
        if call_name(node) in SKIP_CALLS or is_logging(node) or of_no_interest(call_name(node)):
            return node
        return self.generic_visit(node)

    def visit_JoinedStr(self, node):
        return node

    def visit_Raise(self, node):  # the condition leading to it is mutated, not the error
        return node

    def visit_Assert(self, node):
        return node

    def visit_Subscript(self, node):  # vals['key']: not the key
        node.value = self.visit(node.value)
        return node

    def visit_Dict(self, node):  # keys are names of fields
        if is_action(node):
            return node
        node.values = [self.visit(v) for v in node.values]
        return node

    def visit_Lambda(self, node):
        node.body = self.visit(node.body)
        return node

    def visit_FunctionDef(self, node):  # a function inside the method: its body only
        node.body = self.body(node.body)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    # ---- expressions
    def visit_Compare(self, node):
        if len(node.ops) != 1 or type(node.ops[0]) not in OPPOSITE:
            return self.generic_visit(node)
        return self.expr(node, "comparison", lambda n: ast.Compare(left=n.left, ops=[OPPOSITE[type(n.ops[0])]()],
                                                                   comparators=n.comparators))

    def visit_BoolOp(self, node):
        return self.expr(node, "and/or", lambda n: ast.BoolOp(op=ast.Or() if isinstance(n.op, ast.And) else ast.And(),
                                                              values=n.values))

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            return self.expr(node, "remove not", lambda n: n.operand)
        return self.generic_visit(node)

    def visit_BinOp(self, node):
        if type(node.op) not in ARITHMETIC or stringy(node.left) or stringy(node.right):
            return self.generic_visit(node)
        return self.expr(node, "arithmetic", lambda n: ast.BinOp(left=n.left, op=ARITHMETIC[type(n.op)](), right=n.right))

    def visit_Constant(self, node):
        if id(node) in self.skip:
            return node
        value = node.value
        if isinstance(value, bool):
            return self.expr(node, "boolean", lambda n: ast.Constant(not value))
        if isinstance(value, (int, float)):
            new = 1 if value == 0 else 0 if value == 1 else value + 1
            return self.expr(node, "number", lambda n: ast.Constant(type(value)(new)))
        return node

    def condition(self, test):
        """A condition: its comparisons, and/or, not have their own rules; anything else (`if lines`) is negated."""
        if isinstance(test, (ast.Compare, ast.BoolOp, ast.UnaryOp)):
            return self.visit(test)
        return self.expr(test, "negate condition", lambda n: ast.UnaryOp(op=ast.Not(), operand=n))

    def visit_IfExp(self, node):  # a if c else b
        node.test = self.condition(node.test)
        node.body, node.orelse = self.visit(node.body), self.visit(node.orelse)
        return node

    def visit_comprehension(self, node):  # [l for l in lines if l.active]: not the target
        node.iter = self.visit(node.iter)
        node.ifs = [self.condition(test) for test in node.ifs]
        return node

    # ---- statements
    def visit_If(self, node):
        if all(quiet(s) for s in node.body) and not node.orelse:  # it only guards something of no interest
            return node
        node.test = self.condition(node.test)
        node.body, node.orelse = self.body(node.body), self.body(node.orelse)
        return node

    def visit_Return(self, node):
        empty = emptied(node.value)
        if empty is None:
            return self.generic_visit(node)
        node.value = self.expr(node.value, "empty return", empty)  # its inner parts keep their own mutants
        return node

    def visit_For(self, node):
        node.iter = self.visit(node.iter)
        node.body, node.orelse = self.body(node.body), self.body(node.orelse)
        return node

    def visit_While(self, node):
        node.test = self.visit(node.test)
        node.body, node.orelse = self.body(node.body), self.body(node.orelse)
        return node

    def visit_With(self, node):
        node.items = [self.visit(item) for item in node.items]
        node.body = self.body(node.body)
        return node

    def visit_Try(self, node):
        node.body, node.orelse, node.finalbody = self.body(node.body), self.body(node.orelse), self.body(node.finalbody)
        for handler in node.handlers:
            handler.body = self.body(handler.body)
        return node

    def visit_Expr(self, node):
        value = node.value
        if not isinstance(value, ast.Call):
            return node
        if is_logging(value) or call_name(value) in SKIP_CALLS | NO_DELETE or (
                isinstance(value.func, ast.Attribute) and isinstance(value.func.value, ast.Call)
                and call_name(value.func.value) == "super"):
            return self.generic_visit(node)
        return self.stmt(node, "remove call")

    def visit_Assign(self, node):
        if all(isinstance(t, (ast.Attribute, ast.Subscript)) for t in node.targets):
            return self.stmt(node, "remove assignment")
        return self.generic_visit(node)

    def visit_AugAssign(self, node):
        if type(node.op) not in ARITHMETIC:
            return self.generic_visit(node)
        return self.stmt(node, "arithmetic", lambda n: ast.AugAssign(target=n.target, op=ARITHMETIC[type(n.op)](),
                                                                     value=n.value))

    def visit_Continue(self, node):
        return self.stmt(node, "remove continue/break")

    visit_Break = visit_Continue


def skipped(holder, skip):
    """Whether a function (Class.method or function) matches one of the `skip` globs of config.toml."""
    name = holder.rsplit(".", 1)[-1]
    return any(fnmatch.fnmatchcase(holder, pattern) or fnmatch.fnmatchcase(name, pattern) for pattern in skip)


def mutate_source(text, rel, found, skip=()):
    """The source of a file with its mutants compiled in (switches), the mutants added to `found`."""
    tree = ast.parse(text)
    for owner in [n for n in tree.body if isinstance(n, ast.ClassDef)] + [tree]:
        for fn in owner.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            holder = fn.name if owner is tree else f"{owner.name}.{fn.name}"
            if skipped(holder, skip):
                continue
            mutator = Mutator(rel, holder, found, constants_left_alone(fn))
            doc = fn.body[:1] if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant) else []
            fn.body = doc + mutator.body(fn.body[len(doc):]) or [ast.Pass()]
    position = 0  # the switch is imported after the docstring and the __future__ imports
    while position < len(tree.body) and (
            isinstance(tree.body[position], ast.ImportFrom) and tree.body[position].module == "__future__"
            or position == 0 and isinstance(tree.body[0], ast.Expr)):
        position += 1
    tree.body.insert(position, ast.Import(names=[ast.alias(name="jsa_mutant_switch", asname="_jms")]))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def identify(found):
    """The id of each mutant: stays the same when lines move (file, function, rule, the change, its rank among the
    same change in that function)."""
    seen = {}
    for mutant in found:
        key = (mutant["file"], mutant["function"], mutant["op"], mutant["original"], mutant["mutated"])
        seen[key] = seen.get(key, 0) + 1
        mutant["id"] = hashlib.sha1("|".join(map(str, (*key, seen[key]))).encode()).hexdigest()[:10]
    return found


def code_files(addon):
    """The python files mutated: not the tests, migrations, manifests, __init__.py, hooks (like the coverage)."""
    for path in sorted(addon.rglob("*.py")):
        rel = path.relative_to(addon)
        if rel.parts[0] in ("tests", "migrations", "upgrades") or path.name in ("__manifest__.py", "__init__.py") \
                or path.name in ("hooks.py", "hook.py") or "hooks" in rel.parts[:-1]:
            continue
        yield path, rel.as_posix()


def main(addon, out, skip="[]"):
    addon, out, skip = Path(addon), Path(out), json.loads(skip)
    copy_of = out / addon.name
    if copy_of.exists():
        shutil.rmtree(copy_of)
    shutil.copytree(addon, copy_of, ignore=shutil.ignore_patterns("__pycache__"))
    found = []
    for path, rel in code_files(addon):
        try:
            text = mutate_source(path.read_text(encoding="utf-8"), rel, found, skip)
        except SyntaxError as e:
            print(f"not mutated (syntax error): {rel}: {e}", file=sys.stderr)
            continue
        compile(text, str(path), "exec")
        (copy_of / rel).write_text(text, encoding="utf-8")
    (out / "mutants.json").write_text(json.dumps(identify(found), indent=1), encoding="utf-8")
    print(len(found))


if __name__ == "__main__":
    main(*sys.argv[1:])
