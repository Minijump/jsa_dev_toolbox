"""Security review of the custom addons: the places of their code to look at, found by reading the files (no Odoo, no
database), so it takes seconds and runs with every 'jsa structure'.

Each rule is one function below, registered with @rule; its docstring is what the report says about it: what it looks
for, then (after "Left out:") what it does not look at. A finding is a place to review, not a proven flaw: when it is
fine, its id goes in reviewed.toml with the reason (the report gives the line to copy), and it leaves the list and the
chart. Its id stays the same when lines move: it is made of the rule, the addon, the file, what holds it (Class.method,
the id of an access right or of a template) and its code.
"""
import ast
import csv
import hashlib
import inspect
import io
import re
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

import typer

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib


@dataclass
class Finding:
    rule: str
    addon: str
    file: str  # path in the addon
    line: int
    where: str  # what holds it: Class.method, the id of an access right, of a template
    code: str  # its line of code
    why: str
    id: str = ""


@dataclass
class Rule:
    key: str
    title: str
    kind: str  # Access control (OWASP Top 10:2025 A01) or Injection (A05)
    check: Callable

    @property
    def text(self) -> str:
        """What the rule looks for, then what it leaves out: the docstring of its function."""
        return inspect.cleandoc(self.check.__doc__ or "")


RULES: list[Rule] = []


def rule(key: str, title: str, kind: str):
    def register(check):
        RULES.append(Rule(key, title, kind, check))
        return check
    return register


# ---------- the code read ----------

@dataclass
class File:
    addon: str
    path: str  # in the addon
    lines: list[str]
    tree: Optional[ast.Module] = None  # python files

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class Code:
    python: list[File] = field(default_factory=list)
    xml: list[File] = field(default_factory=list)
    access: list[File] = field(default_factory=list)  # ir.model.access.csv
    unreadable: list[str] = field(default_factory=list)  # python files that do not parse: not looked at


@dataclass
class Spot:
    """Where a rule found something: the line(s) of code (the first one is shown), what holds them, why it matters."""
    file: File
    lines: list[int]
    where: str
    why: str


def read_code(addons: dict[str, Path], files_of: Callable[[Path], Iterable[Path]]) -> Code:
    code = Code()
    for name, root in sorted(addons.items()):
        for path in sorted(files_of(root)):
            rel, suffix = path.relative_to(root).as_posix(), path.suffix.lower()
            if suffix not in ('.py', '.xml') and path.name != 'ir.model.access.csv' or 'upgrades' in rel.split('/')[:-1]:
                continue  # upgrades/ (like migrations/): scripts run by an update, nobody calls them
            text = path.read_text(encoding='utf-8', errors='ignore')
            if suffix == '.py':
                try:
                    with warnings.catch_warnings():  # "invalid escape sequence" of old code: not ours to say
                        warnings.simplefilter('ignore')
                        tree = ast.parse(text)
                except (SyntaxError, ValueError):
                    code.unreadable.append(f"{name}/{rel}")
                    continue
                code.python.append(File(name, rel, text.splitlines(), tree))
            else:
                (code.xml if suffix == '.xml' else code.access).append(File(name, rel, text.splitlines()))
    return code


def scan(addons: dict[str, Path], files_of: Callable[[Path], Iterable[Path]]) -> tuple[list[Finding], list[str]]:
    """The findings of every rule in the files `files_of` gives for each addon (the code: no tests, translations,
    migrations), and the python files that could not be read."""
    code = read_code(addons, files_of)
    findings, ids = [], set()
    for r in RULES:
        for spot in r.check(code):
            lines = [spot.file.lines[n - 1] if 0 < n <= len(spot.file.lines) else "" for n in spot.lines]
            text = "\n".join(" ".join(line.split()) for line in lines)
            base = hashlib.sha1(f"{r.key}|{spot.file.addon}|{spot.file.path}|{spot.where}|{text}".encode()).hexdigest()[:10]
            found_id, n = base, 1
            while found_id in ids:  # the same code twice in the same place
                n += 1
                found_id = f"{base}-{n}"
            ids.add(found_id)
            shown = lines[0].strip() if lines else ""
            findings.append(Finding(r.key, spot.file.addon, spot.file.path, spot.lines[0], spot.where,
                                    shown if len(shown) <= 200 else shown[:199] + "…", spot.why, found_id))
    return findings, code.unreadable


def by_rule(findings: Iterable[dict]) -> dict[str, list[str]]:
    """{rule: ids of its findings}: what a run of the history keeps."""
    out = {}
    for f in findings:
        out.setdefault(f['rule'], []).append(f['id'])
    return {key: sorted(ids) for key, ids in sorted(out.items())}


# ---------- reading python ----------

def call_name(node: ast.AST) -> Optional[str]:
    func = getattr(node, 'func', None)
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def methods(f: File) -> Iterator[tuple[Optional[ast.ClassDef], ast.FunctionDef]]:
    """The functions of a file: (its class or None, the function)."""
    for node in f.tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield node, item
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield None, node


def holder(cls: Optional[ast.ClassDef], fn: ast.FunctionDef) -> str:
    return f"{cls.name}.{fn.name}" if cls else fn.name


def class_value(cls: ast.ClassDef, name: str):
    """A constant set in the body of a class (_name = 'x.y'), the first item of a list."""
    for node in cls.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            value = node.value
            if isinstance(value, (ast.List, ast.Tuple)) and value.elts:
                value = value.elts[0]
            if isinstance(value, ast.Constant):
                return value.value
    return None


def model_kind(cls: ast.ClassDef) -> Optional[str]:
    """'Model', 'TransientModel' or 'AbstractModel' for a class of the Odoo ORM, None for anything else."""
    for base in cls.bases:
        name = base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else None
        if name in ('Model', 'TransientModel', 'AbstractModel'):
            return name
    return 'Model' if class_value(cls, '_name') or class_value(cls, '_inherit') else None


def model_name(cls: ast.ClassDef) -> str:
    return class_value(cls, '_name') or class_value(cls, '_inherit') or cls.name


def decorated(fn: ast.FunctionDef, name: str) -> Optional[ast.AST]:
    """The decorator of that name (@api.private, @http.route(...)), None if it has none."""
    for d in fn.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        if (isinstance(target, ast.Attribute) and target.attr == name) or (isinstance(target, ast.Name) and target.id == name):
            return d
    return None


def sudo_lines(fn: ast.FunctionDef) -> list[int]:
    return sorted({n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and call_name(n) == 'sudo'})


def route_of(fn: ast.FunctionDef) -> Optional[dict]:
    """The arguments of its @http.route(...), None when it is not a web route."""
    d = decorated(fn, 'route')
    if not isinstance(d, ast.Call):
        return None
    args = {k.arg: k.value for k in d.keywords if k.arg}
    if d.args and 'route' not in args:
        args['route'] = d.args[0]
    args['line'] = d.lineno
    return args


def constant(node: Optional[ast.AST], default=None):
    return node.value if isinstance(node, ast.Constant) else default


def paths_of(route: dict) -> str:
    value = route.get('route')
    items = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
    return ", ".join(str(constant(v)) for v in items if isinstance(constant(v), str)) or "(path not read)"


def assignments(fn: ast.FunctionDef) -> dict[str, list[ast.AST]]:
    """{variable: the values given to it in the function} (`+=` included)."""
    found = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).append(node.value)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and isinstance(node.target, ast.Name) and node.value:
            found.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, (ast.For, ast.comprehension)) and isinstance(node.target, ast.Name):
            found.setdefault(node.target.id, []).append(node.iter)  # `for name in names`: its values are those of names
    return found


TEXT_METHODS = {'strip', 'lstrip', 'rstrip', 'lower', 'upper', 'replace', 'str', 'repr'}  # the text of their value


def carries_outside_text(node: ast.AST, values: dict, seen: frozenset = frozenset()) -> bool:
    """Whether a piece of a query (or of HTML) may hold text that does not come from the code itself: a variable or
    an argument, a field of a record (self.name), what .get() reads (the context, the parameters of a request). Not:
    constants, table names (self._table), ids, what a method returns (self._select(): the code of a report, not
    followed), Odoo's SQL()."""
    if isinstance(node, ast.Constant):
        return False
    if isinstance(node, ast.Attribute):
        return not (node.attr.endswith('_table') or node.attr in ('id', 'ids'))
    if isinstance(node, ast.JoinedStr):
        return any(carries_outside_text(v.value, values, seen) for v in node.values if isinstance(v, ast.FormattedValue))
    if isinstance(node, ast.FormattedValue):
        return carries_outside_text(node.value, values, seen)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
        return carries_outside_text(node.left, values, seen) or carries_outside_text(node.right, values, seen)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(carries_outside_text(e, values, seen) for e in node.elts)
    if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
        return carries_outside_text(node.elt, values, seen)
    if isinstance(node, ast.IfExp):
        return carries_outside_text(node.body, values, seen) or carries_outside_text(node.orelse, values, seen)
    if isinstance(node, ast.Call):
        name = call_name(node)
        if name == 'get':  # the context, the parameters of a request, a dict of values
            return True
        if name in ('format', 'join') and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Call) and call_name(node.func.value) == 'SQL':  # psycopg2's sql.SQL().format()
                return False
            return carries_outside_text(node.func.value, values, seen) or any(
                carries_outside_text(a, values, seen) for a in [*node.args, *(k.value for k in node.keywords)])
        if name in TEXT_METHODS:
            receiver = [node.func.value] if isinstance(node.func, ast.Attribute) else []
            return any(carries_outside_text(a, values, seen) for a in [*receiver, *node.args])
        return False  # what a method returns: not followed (the SQL of a report, len(), SQL(), a translation)
    if isinstance(node, ast.Name):
        if node.id in seen or node.id not in values:
            return node.id not in seen  # an argument of the method, a loop variable: not known, may be anything
        return any(carries_outside_text(v, values, seen | {node.id}) for v in values[node.id])
    return True


def built_with_values(node: ast.AST, values: dict, seen: frozenset = frozenset()) -> bool:
    """A string built in Python with values that may come from outside: an f-string, %, + or .format(), written
    there or in a variable of the method. A query received as an argument, or returned by a call, is not followed.
    `seen`: the variables being followed (query = query + "..." refers to itself: its previous values are looked at)."""
    if isinstance(node, ast.Name):
        return node.id not in seen and any(built_with_values(v, values, seen | {node.id}) for v in values.get(node.id, []))
    formatting = isinstance(node, (ast.JoinedStr, ast.BinOp, ast.IfExp)) or (
        isinstance(node, ast.Call) and call_name(node) == 'format' and isinstance(node.func, ast.Attribute))
    return formatting and carries_outside_text(node, values, seen)


# ---------- the rules ----------

@rule("sudo_rpc", "sudo() in a method callable over RPC", "Access control")
def sudo_in_rpc_methods(code: Code):
    """A public method of a model (its name does not start with "_") calling sudo(). Anyone who can read a record of
    the model can call its public methods from outside the web client, with the arguments they choose, and what
    follows sudo() runs with every right: check that the method verifies who calls it and what it receives.

    Left out: methods whose name starts with "_" and those marked @api.private (they cannot be called over RPC), the
    web controllers (see the route rules), with_user()."""
    for f in code.python:
        for cls, fn in methods(f):
            if cls is None or not model_kind(cls) or fn.name.startswith('_') or decorated(fn, 'private') or route_of(fn):
                continue
            lines = sudo_lines(fn)
            if lines:
                more = f" ({len(lines)} lines)" if len(lines) > 1 else ""
                yield Spot(f, lines, holder(cls, fn), f"sudo() in {fn.name}{more}, a public method of {model_name(cls)}: "
                                                      "callable over RPC by anyone who can read its records")


@rule("route_sudo", "sudo() in a public web route", "Access control")
def sudo_in_public_routes(code: Code):
    """A web route open to visitors who are not logged in (auth='public' or auth='none') calling sudo(): anyone on
    the internet can reach it, and what follows sudo() runs with every right. Check what it reads from the request and
    what it gives back.

    Left out: routes for logged-in users (auth='user', the default), and a route redefined with @http.route() without
    arguments (it keeps the auth of the route it overrides, which is not read here)."""
    for f in code.python:
        for cls, fn in methods(f):
            route = route_of(fn)
            auth = constant(route.get('auth'), 'user') if route else None
            if auth in ('public', 'none') and (lines := sudo_lines(fn)):
                yield Spot(f, lines, holder(cls, fn), f"sudo() in the route {paths_of(route)} (auth='{auth}'): reachable "
                                                      "without logging in")


@rule("csrf_off", "Web route without CSRF protection", "Access control")
def routes_without_csrf(code: Code):
    """A web route of type 'http' declared with csrf=False: another site can make the browser of a logged-in user send
    it a form (cross-site request forgery). Normal for a webhook called by another server (a payment provider), which
    must then check who calls it (a signature).

    Left out: routes of type 'json' or 'jsonrpc', and routes accepting only GET: Odoo does not check CSRF there."""
    for f in code.python:
        for cls, fn in methods(f):
            route = route_of(fn)
            if not route or constant(route.get('csrf'), True) is not False:
                continue
            kind = constant(route.get('type'), 'http')
            accepted = route.get('methods')
            only_get = isinstance(accepted, (ast.List, ast.Tuple)) and {str(constant(m, '')).upper() for m in accepted.elts} <= {'GET', 'HEAD'}
            if kind == 'http' and not only_get:
                yield Spot(f, [route['line']], holder(cls, fn), f"csrf=False on the route {paths_of(route)}: check who "
                                                                "can call it")


@rule("access_everyone", "Access right given to everyone", "Access control")
def access_for_everyone(code: Code):
    """A line of ir.model.access.csv with no group that allows to write, create or delete: it applies to every user,
    portal users and website visitors included.

    Left out: read access alone, and the access rights written in XML files."""
    for f in code.access:
        reader = csv.DictReader(io.StringIO(f.text))
        for row in reader:
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            group = next((row[k] for k in ('group_id:id', 'group_id/id', 'group_id') if k in row), None)
            allowed = [name for perm, name in (('perm_write', 'write'), ('perm_create', 'create'), ('perm_unlink', 'delete'))
                       if row.get(perm, '').lower() in ('1', 'true')]
            if group == "" and allowed:
                model = row.get('model_id:id') or row.get('model_id/id') or row.get('model_id') or "?"
                yield Spot(f, [reader.line_num], row.get('id') or f"line {reader.line_num}",
                           f"no group: every user, portal and public ones included, can {', '.join(allowed)} ({model})")


@rule("company_rule", "Model with a company and no company rule", "Access control")
def models_without_company_rule(code: Code):
    """A model of your addons with a company_id field (a link to res.company) and no record rule (ir.rule) on it that
    mentions the company: in a database with several companies, its records are visible from the other companies.

    The rules are looked for in the XML files of all your addons. Left out: wizards (TransientModel) and abstract
    models (no records of their own), models only extended (_inherit without a new _name)."""
    covered = set()  # the xml ids of the models having a rule about the company: model_sale_order
    for f in code.xml:
        try:
            root = ET.fromstring(f.text)
        except ET.ParseError:
            continue
        for record in root.iter('record'):
            if record.get('model') != 'ir.rule':
                continue
            fields = {fld.get('name'): fld for fld in record.iter('field')}
            model, domain = fields.get('model_id'), fields.get('domain_force')
            text = "" if domain is None else (domain.get('eval') or "") + (domain.text or "")
            if model is not None and model.get('ref') and 'company' in text:
                covered.add(model.get('ref').split('.')[-1])
    for f in code.python:
        for cls in [n for n in f.tree.body if isinstance(n, ast.ClassDef)]:
            name = class_value(cls, '_name')
            if model_kind(cls) != 'Model' or not isinstance(name, str) or "model_" + name.replace('.', '_') in covered:
                continue
            for node in cls.body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'company_id' for t in node.targets) \
                        and isinstance(node.value, ast.Call) and call_name(node.value) == 'Many2one':
                    comodel = constant(node.value.args[0]) if node.value.args else None
                    comodel = comodel or constant(next((k.value for k in node.value.keywords if k.arg == 'comodel_name'), None))
                    if comodel == 'res.company':
                        yield Spot(f, [node.lineno], cls.name, f"{name} has a company_id and no record rule about the "
                                                               "company: visible from the other companies")


def execute_calls(fn: ast.FunctionDef) -> Iterator[ast.Call]:
    """cr.execute(...), self.env.cr.execute(...), self._cr.execute(...), cursor.execute(...)."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and call_name(node) == 'execute' and node.args and isinstance(node.func, ast.Attribute):
            on = node.func.value
            name = on.attr if isinstance(on, ast.Attribute) else on.id if isinstance(on, ast.Name) else None
            if name in ('cr', '_cr', 'cursor', 'cur'):
                yield node


@rule("sql_built", "SQL query built from values", "Injection")
def sql_from_strings(code: Code):
    """A query given to cr.execute() built in Python with values in it (an f-string, %, + or .format()), there or in a
    variable of the same method: a value coming from a user can change the query (SQL injection). Pass the values as
    parameters, cr.execute("... WHERE id = %s", [value]), or build the query with Odoo's SQL().

    Left out: table names (self._table), ids, Odoo's SQL(), what a method returns (the SQL of a report: self._select()),
    a query received as an argument (where it is built is not followed), and init(), _auto_init(), _init_column() (they
    run when the module is installed or updated, with no value from a user)."""
    for f in code.python:
        for cls, fn in methods(f):
            if fn.name in ('init', '_auto_init', '_init_column'):
                continue
            values = None
            for call in execute_calls(fn):
                values = values if values is not None else assignments(fn)
                if built_with_values(call.args[0], values):
                    yield Spot(f, [call.lineno], holder(cls, fn), f"cr.execute() in {fn.name} runs a query built with values")


XML_HOLDER = re.compile(r'<(?:template|record)\b[^>]*\bid="([^"]+)"|\bt-name="([^"]+)"')


@rule("html_unescaped", "HTML not escaped", "Injection")
def unescaped_html(code: Code):
    """HTML that Odoo will not escape, so text typed by a user can run a script in the browser of whoever reads it
    (XSS): Markup() around a string built with values (an f-string, %, + or .format(): Markup("<b>%s</b>") % value
    escapes the value, Markup("<b>%s</b>" % value) does not), t-raw in a view or a template, an Html field declared
    with sanitize=False.

    Left out: Markup() of a constant, of a translation, of what a method returns or of a variable received as an
    argument."""
    for f in code.python:
        for cls, fn in methods(f):
            values = None
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and call_name(node) == 'Markup' and node.args:
                    values = values if values is not None else assignments(fn)
                    if built_with_values(node.args[0], values):
                        yield Spot(f, [node.lineno], holder(cls, fn), f"Markup() in {fn.name} around a string built with "
                                                                      "values: they are not escaped")
        for cls in [n for n in f.tree.body if isinstance(n, ast.ClassDef)]:
            for node in cls.body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and call_name(node.value) == 'Html' \
                        and any(k.arg == 'sanitize' and constant(k.value, True) is False for k in node.value.keywords):
                    target = next((t.id for t in node.targets if isinstance(t, ast.Name)), "?")
                    yield Spot(f, [node.lineno], f"{cls.name}.{target}", f"the Html field {target} of {model_name(cls)} is "
                                                                         "not sanitized: what users write in it is kept as is")
    for f in code.xml:
        place = ""
        for number, line in enumerate(f.lines, 1):
            if m := XML_HOLDER.search(line):
                place = m.group(1) or m.group(2)
            if re.search(r'\bt-raw\s*=', line):
                yield Spot(f, [number], place, f"t-raw in {place or 'this file'}: the value is put in the page without "
                                               "being escaped (t-out escapes it)")


@rule("eval", "eval() or exec()", "Injection")
def eval_exec(code: Code):
    """Python's eval() or exec(): if what they run can come from a user, that user runs code on the server. Odoo's
    safe_eval() is made for that.

    Left out: safe_eval() and ast.literal_eval()."""
    for f in code.python:
        for node in ast.walk(f.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('eval', 'exec'):
                fn = next((fn for cls, fn in methods(f) if fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno)), None)
                cls = next((c for c in f.tree.body if isinstance(c, ast.ClassDef) and fn in c.body), None) if fn else None
                yield Spot(f, [node.lineno], holder(cls, fn) if fn else "", f"{node.func.id}() runs Python code given as text")


# ---------- reviewed.toml: the findings looked at and found fine ----------

REVIEW_KEYS = ('id', 'reason')


def load_reviewed(path: Path, section: str = 'security') -> dict[str, str]:
    """{id: why it is fine}: the [[security]] tables of reviewed.toml (or those of another section: [[mutation]], the
    changes no test notices that cannot matter). A mistake stops the command (before anything is measured)."""
    def refuse(why: str):
        typer.echo(f"❌ {path.name}: {why}")
        raise typer.Exit(code=1)

    try:
        data = tomllib.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        refuse(f"not valid TOML: {e}")
    items = data.get(section, [])
    if not isinstance(items, list):
        refuse(f"write each one as a [[{section}]] table: id = \"...\", reason = \"...\"")
    reviewed = {}
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            refuse(f"[[{section}]] #{number} must be a table: id = \"...\", reason = \"...\"")
        unknown = sorted(set(item) - set(REVIEW_KEYS))
        if unknown:
            refuse(f"[[{section}]] #{number} has the key(s) {', '.join(unknown)}, which do not exist: use id and reason")
        found_id, reason = item.get('id'), item.get('reason')
        if not isinstance(found_id, str) or not found_id.strip():
            refuse(f"[[{section}]] #{number} needs the id (the report gives the lines to copy)")
        if not isinstance(reason, str) or not reason.strip():
            refuse(f"[[{section}]] #{number} ({found_id}): say in reason why it is fine")
        reviewed[found_id.strip()] = reason.strip()
    return reviewed


def review_entry(finding: dict) -> str:
    """The lines to copy into reviewed.toml for a finding."""
    title = next((r.title for r in RULES if r.key == finding['rule']), finding['rule'])
    where = f" | {finding['where']}" if finding.get('where') else ""  # ASCII: pasted in any editor
    return (f"[[security]]\nid = \"{finding['id']}\"  # {title} | {finding['addon']}/{finding['file']}{where}\n"
            "reason = \"\"\n")
