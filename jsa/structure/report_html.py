"""The codebase report page: one self-contained HTML file (no network, no external library).

Sections: totals, what needs attention, changes since the previous run, one sortable table of the addons (details on
click), the dependency graph (with or without standard modules) and the trend of the measured runs.
Coverage is a continuous color from red (0%) to green (100%). States use a symbol + a label, never the color alone.
"""
import json
from html import escape as E
from typing import Optional

from . import report
from .coverage import NO_REPORT, NOT_INSTALLABLE

# status: (label, symbol, css class)
STATUS = {
    'not_installable': ("Not installable", "✖", "critical"),
    'failing': ("Failing tests", "⚠", "serious"),
    'no_tests': ("No tests", "○", "muted"),
    'ok': ("Tests pass", "✔", "good"),
    'not_measured': ("Not measured", "–", "muted"),
}
STATUS_RANK = {name: rank for rank, name in enumerate(STATUS)}
def coverage_color(coverage: float) -> str:
    """Continuous scale from red (0%) through yellow to green (100%): graph nodes, table meters and legend."""
    return f"hsl({round(1.2 * max(0, min(100, coverage)))},55%,58%)"


# style of the dependency graph, relative to the <svg class="dg">: also embedded in the downloaded image
GRAPH_CSS = """
.dg .node ellipse,.dg .node polygon{fill:#fcfcfb;stroke:#898781;stroke-width:1;cursor:pointer}
.dg .node text{fill:#0b0b0b;cursor:pointer}
.dg .edge path{stroke:#898781}.dg .edge polygon{stroke:#898781;fill:#898781}
.dg .edge.dup path{stroke:#b4b2a9;stroke-dasharray:2 3}.dg .edge.dup polygon{stroke:#b4b2a9;fill:#b4b2a9}
.dg .not_installable ellipse{stroke:#d03b3b;stroke-width:3}
.dg .not_measured ellipse{stroke-dasharray:4 3}
.dg .std polygon{fill:#f0efec;stroke:#c3c2b7}.dg .std text{fill:#52514e}
.dg .ent polygon{fill:#ebe9f8;stroke:#4a3aa7;stroke-width:2}.dg .ent text{fill:#2f2578}
.dg .edge.ent path{stroke:#4a3aa7;stroke-dasharray:5 3}.dg .edge.ent polygon{stroke:#4a3aa7;fill:#4a3aa7}
.dg .flag circle{fill:#ec835a;stroke:#fcfcfb;stroke-width:1.5}
.dg .flag text{fill:#fff;font-size:12px;font-weight:700;font-family:Helvetica,Arial,sans-serif}
.dg.focus .node:not(.on),.dg.focus .edge:not(.on){opacity:.12}
.dg .edge.on path{stroke:#0b0b0b;stroke-width:2}.dg .edge.on polygon{stroke:#0b0b0b;fill:#0b0b0b}
"""

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;
--axis:#c3c2b7;--border:rgba(11,11,11,.10);--accent:#2a78d6;--good:#0ca30c;--serious:#ec835a;--critical:#d03b3b;
--hover:rgba(11,11,11,.04);--ent:#4a3aa7}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--accent:#3987e5;--hover:rgba(255,255,255,.05);--ent:#9085e9}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1280px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:22px;margin:0}h2{font-size:16px;margin:0 0 12px}
.meta{color:var(--ink2);margin:4px 0 20px}
section{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;margin-bottom:16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.tile .label{color:var(--ink2);font-size:13px}.tile .value{font-size:28px;font-weight:600;margin:2px 0}
.tile .sub{color:var(--muted);font-size:12px}
.st{white-space:nowrap}.st b{font-weight:400;display:inline-block;width:1.3em;text-align:center}
.good b{color:var(--good)}.serious b{color:var(--serious)}.critical b{color:var(--critical)}.muted b{color:var(--muted)}
ul{margin:0;padding-left:20px}li{margin:3px 0}
code{font:12px ui-monospace,Consolas,monospace;background:var(--hover);padding:1px 4px;border-radius:4px}
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
input[type=search],select,button{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--axis);
border-radius:6px;padding:5px 9px}
button{cursor:pointer}button:hover{background:var(--hover)}
table{border-collapse:collapse;width:100%}
table.plain th{cursor:default}
th{text-align:left;color:var(--ink2);font-weight:600;font-size:13px;padding:7px 10px;border-bottom:1px solid var(--axis);
cursor:pointer;user-select:none;white-space:nowrap}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums}
th[aria-sort=ascending]::after{content:" ▲";font-size:10px}th[aria-sort=descending]::after{content:" ▼";font-size:10px}
td{padding:7px 10px;border-bottom:1px solid var(--grid);vertical-align:middle}
tbody.addon>tr:first-child{cursor:pointer}tbody.addon>tr:first-child:hover{background:var(--hover)}
tbody.addon>tr.detail{display:none}tbody.addon.open>tr.detail{display:table-row}
tbody.addon.open>tr:first-child td{border-bottom:none}
.detail td{background:var(--hover);color:var(--ink2);padding:10px 14px 14px 34px}
.detail h3{font-size:13px;margin:10px 0 4px;color:var(--ink)}.detail h3:first-child{margin-top:0}
.name::before{content:"▸";display:inline-block;width:16px;color:var(--muted)}
.open .name::before{content:"▾"}
.meter{display:inline-block;width:110px;height:8px;border-radius:4px;background:var(--grid);vertical-align:middle;
margin-right:8px;overflow:hidden}
.meter span{display:block;height:100%;border-radius:4px}
.cov{white-space:nowrap;font-variant-numeric:tabular-nums}
.pos{color:var(--good)}.neg{color:var(--critical)}
.depgraph{background:#fcfcfb;border:1px solid var(--border);border-radius:8px;overflow:auto;max-height:78vh;padding:8px}
.depgraph svg{display:block;height:auto}
.ent b{color:var(--ent)}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--ink2);font-size:12px;margin:10px 0 0}
.legend .grad{display:inline-block;width:120px;height:12px;border-radius:3px;vertical-align:-2px;margin:0 5px;background:linear-gradient(90deg,hsl(0,55%,58%),hsl(60,55%,58%),hsl(120,55%,58%))}
.legend .dot{background:#ec835a;color:#fff;font-weight:700;font-size:10px;line-height:14px;text-align:center;font-style:normal;border-color:#ec835a}
.legend i{display:inline-block;width:14px;height:14px;border-radius:50%;vertical-align:-3px;margin-right:4px;border:1px solid #898781}
.charts{display:flex;gap:24px;flex-wrap:wrap}
.series[hidden]{display:none}
.chart text{fill:var(--muted);font-size:11px}.chart .title{fill:var(--ink);font-size:13px;font-weight:600}
.chart .last{fill:var(--ink);font-size:12px;font-weight:600}
.chart .gridline{stroke:var(--grid)}.chart .axisline{stroke:var(--axis)}
.chart polyline{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round}
.chart .dot{fill:var(--accent);stroke:var(--surface);stroke-width:2}.chart .hit{fill:transparent}
.chart .hit:hover+.dot{r:6}
footer{color:var(--muted);font-size:12px;margin-top:8px}
"""

JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
// ----- table: open details, sort, filter
const table = document.getElementById('addons');
if (table) {
  table.addEventListener('click', e => {
    const row = e.target.closest('tbody.addon > tr:first-child');
    if (row) row.parentNode.classList.toggle('open');
  });
  table.querySelectorAll('th').forEach(th => th.addEventListener('click', () => {
    const key = th.dataset.key, asc = th.getAttribute('aria-sort') !== 'ascending';
    table.querySelectorAll('th').forEach(o => o.removeAttribute('aria-sort'));
    th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
    const value = b => key === 'name' ? b.dataset.name : parseFloat(b.dataset[key]);
    [...table.querySelectorAll('tbody.addon')]
      .sort((a, b) => (value(a) > value(b) ? 1 : value(a) < value(b) ? -1 : 0) * (asc ? 1 : -1))
      .forEach(b => table.appendChild(b));
  }));
  const search = document.getElementById('search'), only = document.getElementById('only');
  const filter = () => table.querySelectorAll('tbody.addon').forEach(b => {
    const match = only.value === 'enterprise' ? b.dataset.ent > 0 : (!only.value || b.dataset.status === only.value);
    const shown = b.dataset.name.includes(search.value.trim().toLowerCase()) && match;
    b.style.display = shown ? '' : 'none';
  });
  search.addEventListener('input', filter); only.addEventListener('change', filter);
}
// ----- graph (graphviz names its root group "graph": the container is "depgraph", the styled svg is "dg")
const SVG = 'http://www.w3.org/2000/svg', DUPLICATES = new Set(D.duplicates);
document.querySelectorAll('.depgraph').forEach(box => {
  const svg = box.querySelector('svg'), nodes = {}, edges = [];
  svg.classList.add('dg');
  box.querySelectorAll('g.node').forEach(g => {
    const name = g.querySelector('title').textContent, a = D.addons[name];
    nodes[name] = g;
    if (!a) {
      const ent = D.enterprise.includes(name);
      g.classList.add('std'); g.classList.toggle('ent', ent);
      if (ent) g.querySelector('text').textContent = '◆ ' + name;
      g.querySelector('title').textContent = name + (ent ? ' (Odoo enterprise module)' : ' (standard module)');
      return;
    }
    g.classList.add(a.status);
    const shape = g.querySelector('ellipse'), text = g.querySelector('text');
    if (a.color) shape.style.fill = a.color;  // coverage: red -> green
    if (a.status === 'not_installable') text.textContent = '✖ ' + text.textContent;
    if (a.status === 'failing') {  // a flag on the addon, its color still shows the coverage
      const flag = document.createElementNS(SVG, 'g'), dot = document.createElementNS(SVG, 'circle'), mark = document.createElementNS(SVG, 'text');
      const x = +shape.getAttribute('cx') + shape.getAttribute('rx') * 0.72, y = +shape.getAttribute('cy') - shape.getAttribute('ry') * 0.72;
      flag.setAttribute('class', 'flag');
      dot.setAttribute('cx', x); dot.setAttribute('cy', y); dot.setAttribute('r', 8);
      mark.setAttribute('x', x); mark.setAttribute('y', y + 4); mark.setAttribute('text-anchor', 'middle'); mark.textContent = '!';
      flag.append(dot, mark); g.append(flag);
    }
    g.querySelector('title').textContent = name + ' - ' + a.tip;
  });
  box.querySelectorAll('g.edge').forEach(g => {
    const [from, to] = g.querySelector('title').textContent.split('->');
    edges.push({g, from, to});
    g.classList.toggle('ent', D.enterprise.includes(to));
    g.classList.toggle('dup', DUPLICATES.has(from + '->' + to));
  });
  const focus = names => {
    svg.classList.toggle('focus', names.size > 0);
    Object.entries(nodes).forEach(([n, g]) => g.classList.toggle('on', names.has(n)));
    edges.forEach(e => e.g.classList.toggle('on', names.has(e.from) && names.has(e.to)));
  };
  let current = null;
  box.addEventListener('click', e => {
    const g = e.target.closest('g.node');
    const name = g && Object.keys(nodes).find(n => nodes[n] === g);
    if (!name || name === current) { current = null; return focus(new Set()); }
    current = name;  // the addon, what it depends on and what depends on it
    focus(new Set([name, ...edges.filter(x => x.from === name).map(x => x.to), ...edges.filter(x => x.to === name).map(x => x.from)]));
  });
  box.find = text => focus(new Set(text ? Object.keys(nodes).filter(n => n.includes(text)) : []));
  const natural = parseFloat(svg.getAttribute('width')) || 800;
  svg.removeAttribute('height');
  box.zoom = factor => {  // 0: natural size, shrunk to fit the page if needed
    box.scale = factor ? (box.scale || 1) * factor : 1;
    svg.style.width = natural * box.scale + 'pt';
    svg.style.maxWidth = factor ? 'none' : '100%';
  };
  box.zoom(0);
});
const series = document.getElementById('series');  // the trend of one way of measuring at a time
if (series) {
  const pick = () => document.querySelectorAll('.series').forEach(b => b.hidden = b.dataset.key !== series.value);
  series.addEventListener('change', pick); pick();
}
const graphs = () => [...document.querySelectorAll('.depgraph')];
const std = document.getElementById('std'), dup = document.getElementById('dup');
if (std) {
  const show = () => {  // one picture per choice: standard modules or not, duplicate links or not
    const id = 'graph-' + (std.checked ? 'all' : 'custom') + (dup.checked ? '-dup' : '');
    graphs().forEach(b => b.style.display = b.id === id ? '' : 'none');
    const count = D.duplicates.filter(link => std.checked || link.split('->')[1] in D.addons).length;
    document.getElementById('dupcount').textContent = ` (${count})`;
  };
  std.addEventListener('change', show); dup.addEventListener('change', show); show();
  document.getElementById('gsearch').addEventListener('input', e => graphs().forEach(b => b.find(e.target.value.trim().toLowerCase())));
  document.querySelectorAll('[data-zoom]').forEach(btn => btn.addEventListener('click', () => graphs().forEach(b => b.zoom(parseFloat(btn.dataset.zoom)))));
  // the graph as shown (standard modules or not, current selection) as a PNG image, twice its size to stay sharp
  document.getElementById('download').addEventListener('click', () => {
    const svg = graphs().find(b => b.style.display !== 'none').querySelector('svg'), copy = svg.cloneNode(true);
    const {width, height} = svg.viewBox.baseVal, style = document.createElementNS(SVG, 'style');
    style.textContent = document.getElementById('graph-css').textContent;
    copy.removeAttribute('style'); copy.setAttribute('width', width); copy.setAttribute('height', height); copy.prepend(style);
    const image = new Image();
    image.onload = () => {
      const canvas = document.createElement('canvas'), link = document.createElement('a');
      canvas.width = width * 2; canvas.height = height * 2;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#fcfcfb'; ctx.fillRect(0, 0, canvas.width, canvas.height); ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
      link.download = 'dependency_graph.png'; link.href = canvas.toDataURL('image/png'); link.click();
    };
    image.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(copy));
  });
}
"""


def _status_html(name: str) -> str:
    label, symbol, css = STATUS[name]
    return f'<span class="st {css}"><b>{symbol}</b>{label}</span>'


def _coverage_html(entry: dict) -> str:
    coverage = entry.get('coverage')
    if coverage is None or coverage == NOT_INSTALLABLE:
        return '<span class="muted">–</span>'
    if coverage == NO_REPORT:
        return '<span class="muted">no python code</span>'
    return (f'<span class="cov"><span class="meter"><span style="width:{coverage}%;background:{coverage_color(coverage)}"></span>'
            f'</span>{coverage}%</span>')


def _tests_html(entry: dict) -> str:
    if report.status(entry) in ('not_measured', 'not_installable'):
        return '–'
    tests, failed = entry.get('tests') or 0, len(entry.get('failed') or [])
    if not tests:
        return 'no tests'
    return f'{tests - failed} passed' + (f' · <span class="st serious"><b>⚠</b>{failed} failed</span>' if failed else '')


def _tip(entry: dict) -> str:
    """Tooltip of an addon in the graph."""
    state = report.status(entry)
    parts = [STATUS[state][0].lower()]
    if isinstance(entry.get('coverage'), int) and entry['coverage'] >= 0:
        parts.append(f"{entry['coverage']}% coverage")
    if state in ('ok', 'failing'):
        tests = entry.get('tests', 0)
        parts.append(f"{tests} test{'s' if tests != 1 else ''}" + (f", {len(entry['failed'])} failed" if entry.get('failed') else ""))
    return ", ".join(parts + [f"{entry.get('loc', 0):,} lines"])


def _delta(new, old, unit: str = "", decimals: int = 0, since: str = "previous run") -> str:
    if new is None or old is None or new == old:
        return ""
    return f" ({new - old:+,.{decimals}f}{unit} since {since})"


def _tiles(summary: dict, old: Optional[dict], previous: Optional[dict] = None, series: Optional[tuple] = None) -> str:
    """`old`: the summary of the previous report, for what does not depend on how the tests were run (addons, lines
    of code). `previous`: the previous run measured the same way as this one (`series`), for the measures: none when
    this report did not measure anything, or when nothing was measured that way before."""
    old, previous = old or {}, previous or {}
    since = f"the previous run, {report.series_label(series)}" if series else ""
    measure = lambda key, unit="", decimals=0: _delta(summary[key], previous.get(key), unit, decimals, since)
    coverage = f"{summary['coverage']:.0f}%" if summary['coverage'] is not None else "–"
    passed = summary['tests'] - summary['failed']
    tiles = [
        ("Addons", f"{summary['addons']}", f"{summary['measured']} measured" + _delta(summary['addons'], old.get('addons'))),
        ("Lines of code", f"{summary['loc']:,}", "without tests, translations, migrations" + _delta(summary['loc'], old.get('loc'))),
        ("Coverage", coverage, "of the measured code, lines + branches" + measure('coverage', " pts", 1)),
        ("Tests passed", f"{passed:,} / {summary['tests']:,}" if summary['tests'] else "–", f"{summary['failed']} failed" + measure('failed')),
        ("Not installable", f"{summary['not_installable']}", "addons that could not be installed" + measure('not_installable')),
        ("Without tests", f"{summary['no_tests']}", "installed addons with no test" + measure('no_tests')),
        ("Enterprise dependencies", "–" if summary.get('enterprise') is None else f"{summary['enterprise']}",
         "no enterprise folder in the profiles of config.toml" if summary.get('enterprise') is None else
         "addons depending directly on Odoo enterprise" + _delta(summary['enterprise'], old.get('enterprise'))),
    ]
    return '<div class="tiles">' + "".join(
        f'<div class="tile"><div class="label">{label}</div><div class="value">{value}</div><div class="sub">{sub}</div></div>'
        for label, value, sub in tiles) + '</div>'


def _attention(state: dict, standard: Optional[dict] = None) -> str:
    items = []
    for name, entry in state.items():
        if report.status(entry) == 'not_installable':
            items.append(f'<li>{_status_html("not_installable")} <strong>{E(name)}</strong>: {E(entry.get("note") or "could not be installed")}</li>')
    for name, entry in state.items():
        if report.status(entry) == 'failing':
            tests = ", ".join(f"<code>{E(t)}</code>" for t in entry.get('failed') or [])
            together = E(entry['together']) if entry.get('together') and 'test(s) fail' not in entry['together'] else ""
            items.append(f'<li>{_status_html("failing")} <strong>{E(name)}</strong>: {"; ".join(x for x in (tests, together) if x)}</li>')
    for module, result in sorted(((standard or {}).get('modules') or {}).items()):
        if result['failed']:
            tests = ", ".join(f"<code>{E(t)}</code>" for t in result['failed'])
            items.append(f'<li>{_status_html("failing")} <strong>{E(module)}</strong> (standard module, with your addons): {tests}</li>')
    if not items:
        return ""
    return f'<section><h2>Needs attention ({len(items)})</h2><ul>{"".join(items)}</ul></section>'


def _standard(standard: Optional[dict]) -> str:
    """The tests of the standard modules, run with the custom addons installed (like odoo.sh)."""
    if not standard:
        return ""
    modules = standard.get('modules') or {}
    tests, failed = sum(r['tests'] for r in modules.values()), sum(len(r['failed']) for r in modules.values())
    when = E(standard.get('date', '')[:16].replace('T', ' '))
    failing = [m for m, r in sorted(modules.items()) if r['failed']]
    body = (f"{tests:,} tests in {len(modules)} standard modules, run with your addons installed ({when}): "
            + (f'<span class="st serious"><b>⚠</b>{failed} failed</span> in {E(", ".join(failing))}' if failed else "all passed"))
    return f'<section><h2>Tests of the standard modules</h2><span class="meta">{body}</span></section>'


def _enterprise(enterprise: dict[str, dict[str, list[str]]]) -> str:
    """Which Odoo enterprise modules the custom addons need: the work to do to run on the community edition."""
    if not enterprise:
        return ""
    direct, indirect = {}, {}
    for addon, modules in enterprise.items():
        for module, via in modules.items():
            (indirect if via else direct).setdefault(module, []).append(addon)
    rows = "".join(
        f'<tr><td><span class="st ent"><b>◆</b>{E(module)}</span></td><td>{E(", ".join(sorted(direct[module])))}</td>'
        f'<td>{E(", ".join(sorted(indirect.get(module, [])))) or "–"}</td></tr>' for module in sorted(direct))
    return (f'<section><h2>Dependencies on Odoo enterprise ({len(direct)} modules, {len(enterprise)} addons)</h2>'
            '<p class="meta" style="margin:0 0 8px">⚠ These addons would not work on the community edition of Odoo.</p>'
            '<table class="plain"><thead><tr><th>Enterprise module</th><th>Custom addons depending on it</th>'
            f'<th>Addons needing it through another custom addon</th></tr></thead><tbody>{rows}</tbody></table></section>')


def _changes(diff: Optional[dict], old_date: Optional[str]) -> str:
    if diff is None:
        return ""
    def names(pairs):
        return "; ".join(f"<strong>{E(n)}</strong> ({', '.join(f'<code>{E(t)}</code>' for t in tests)})" for n, tests in pairs)

    items = []
    if diff['could_to_cant']:
        items.append(f"✖ Not installable anymore: <strong>{E(', '.join(diff['could_to_cant']))}</strong>")
    if diff['newly_failing']:
        items.append(f"⚠ Tests failing now: {names(diff['newly_failing'])}")
    if diff['cant_to_could']:
        items.append(f"✔ Installable again: <strong>{E(', '.join(diff['cant_to_could']))}</strong>")
    if diff['fixed']:
        items.append(f"✔ Tests fixed: {names(diff['fixed'])}")
    if diff.get('enterprise_added'):
        items.append("◆ New dependencies on Odoo enterprise: " + "; ".join(
            f"<strong>{E(n)}</strong> ({E(', '.join(modules))})" for n, modules in diff['enterprise_added']))
    if diff['added']:
        items.append(f"New addons: <strong>{E(', '.join(diff['added']))}</strong>")
    if diff['removed']:
        items.append(f"Removed addons: <strong>{E(', '.join(diff['removed']))}</strong>")
    for name, loc, cov, old_cov, new_cov in diff['metrics_changed']:
        parts = []
        if loc:
            parts.append(f"{loc:+,} lines")
        if cov:
            parts.append(f'coverage {old_cov}% → {new_cov}% (<span class="{"pos" if cov > 0 else "neg"}">{cov:+d} pts</span>)')
        items.append(f"<strong>{E(name)}</strong>: {', '.join(parts)}")
    for name, added, removed in diff['deps_changed']:
        parts = ([f"now depends on {E(', '.join(added))}"] if added else []) + ([f"no longer depends on {E(', '.join(removed))}"] if removed else [])
        items.append(f"<strong>{E(name)}</strong>: {'; '.join(parts)}")
    since = f" ({E(old_date[:16].replace('T', ' '))})" if old_date else ""
    body = f'<ul>{"".join(f"<li>{i}</li>" for i in items)}</ul>' if items else ""
    if diff.get('not_comparable'):
        count = len(diff['not_comparable'])
        body += (f'<p class="meta">Coverage and tests of {count} addon{"s" if count > 1 else ""} not compared: measured another '
                 'way than in the previous run (another Odoo version, or whole instead of one by one).</p>')
    body = body or '<span class="meta">Nothing changed.</span>'
    return f'<section><h2>Changes since the previous run{since}</h2>{body}</section>'


def _detail(name: str, entry: dict, state: dict, used_by: list[str], enterprise: dict[str, list[str]]) -> str:
    parts = []
    if entry.get('note'):
        parts.append(f"<h3>Why it is not installable</h3>{E(entry['note'])}")
    if entry.get('together'):
        parts.append(f"<h3>With all the other addons installed</h3>{E(entry['together'])}")
    if entry.get('failed'):
        parts.append("<h3>Failed tests</h3><ul>" + "".join(f"<li><code>{E(t)}</code></li>" for t in entry['failed']) + "</ul>")
    if entry.get('files'):
        parts.append("<h3>Lines not covered by tests</h3><ul>" + "".join(
            f"<li><code>{E(f)}</code>: {E(lines)}</li>" for f, lines in sorted(entry['files'].items())) + "</ul>")
    if enterprise:
        parts.append("<h3>Odoo enterprise modules it needs</h3>" + ", ".join(
            f"{E(module)} ({'direct' if not via else 'through ' + E(', '.join(via))})" for module, via in enterprise.items()))
    custom = [d for d in entry.get('depends', []) if d in state]
    standard = [d for d in entry.get('depends', []) if d not in state]
    parts.append(f"<h3>Dependencies</h3>Custom: {E(', '.join(custom)) or 'none'}<br>Standard: {E(', '.join(standard)) or 'none'}"
                 f"<br>Used by: {E(', '.join(used_by)) or 'no other custom addon'}")
    return "".join(parts)


def _table(state: dict, enterprise: dict) -> str:
    used_by = {name: sorted(n for n, e in state.items() if name in e.get('depends', [])) for name in state}
    columns = [("name", "Addon", ""), ("rank", "Status", ""), ("coverage", "Coverage", ""), ("tests", "Tests", ""),
               ("failed", "Failed", "num"), ("loc", "Lines of code", "num"), ("usedby", "Used by", "num"),
               ("ent", "Enterprise", "num")]
    head = "".join(f'<th data-key="{key}" class="{css}">{label}</th>' for key, label, css in columns)
    bodies = []
    for name, entry in sorted(state.items(), key=lambda item: (STATUS_RANK[report.status(item[1])], item[0])):
        state_name = report.status(entry)
        coverage = entry['coverage'] if isinstance(entry.get('coverage'), int) and entry['coverage'] >= 0 else -1
        failed = len(entry.get('failed') or [])
        ent = enterprise.get(name, {})
        ent_cell = f'<span class="st ent" title="{E(", ".join(ent))}"><b>◆</b>{len(ent)}</span>' if ent else ""
        bodies.append(
            f'<tbody class="addon" data-name="{E(name.lower())}" data-status="{state_name}" data-rank="{STATUS_RANK[state_name]}" '
            f'data-coverage="{coverage}" data-tests="{entry.get("tests") or 0}" data-failed="{failed}" data-loc="{entry.get("loc", 0)}" '
            f'data-usedby="{len(used_by[name])}" data-ent="{len(ent)}"><tr><td class="name">{E(name)}</td><td>{_status_html(state_name)}</td>'
            f'<td>{_coverage_html(entry)}</td><td>{_tests_html(entry)}</td><td class="num">{failed or ""}</td>'
            f'<td class="num">{entry.get("loc", 0):,}</td><td class="num">{len(used_by[name]) or ""}</td><td class="num">{ent_cell}</td></tr>'
            f'<tr class="detail"><td colspan="{len(columns)}">{_detail(name, entry, state, used_by[name], ent)}</td></tr></tbody>')
    options = "".join(f'<option value="{name}">{label}</option>' for name, (label, _, _) in STATUS.items())
    options += '<option value="enterprise">Needs Odoo enterprise</option>'
    return (f'<section><h2>Addons ({len(state)})</h2><div class="controls">'
            '<input type="search" id="search" placeholder="Filter by name" aria-label="Filter addons by name">'
            f'<select id="only" aria-label="Filter by status"><option value="">All statuses</option>{options}</select>'
            '<span class="meta" style="margin:0">Click a row for the details, a column title to sort.</span></div>'
            f'<table id="addons"><thead><tr>{head}</tr></thead>{"".join(bodies)}</table></section>')


def _graph(graphs: dict) -> str:
    if not graphs.get('custom'):
        return ('<section><h2>Dependency graph</h2><span class="meta">Not drawn: graphviz is not installed '
                '(<code>sudo apt install graphviz</code>, then run the command again).</span></section>')
    return ('<section><h2>Dependency graph</h2><div class="controls">'
            '<label><input type="checkbox" id="std"> Show standard modules</label>'
            '<label title="A link A → C is a duplicate when the graph already shows another path, e.g. A → B → C">'
            '<input type="checkbox" id="dup"> Show duplicate links<span id="dupcount"></span></label>'
            '<input type="search" id="gsearch" placeholder="Find an addon" aria-label="Find an addon in the graph">'
            '<button data-zoom="0.8" aria-label="Zoom out">−</button><button data-zoom="1.25" aria-label="Zoom in">+</button>'
            '<button data-zoom="0">Fit</button><button id="download">Download image</button>'
            '<span class="meta" style="margin:0">An arrow goes from an addon to what it depends on. Click an addon to isolate its links.</span></div>'
            + "".join(f'<div class="depgraph" id="graph-{key.replace("_", "-")}">{graphs.get(key) or ""}</div>'
                      for key in ('custom', 'all', 'custom_dup', 'all_dup')) +
            '<div class="legend"><span>Coverage 0%<span class="grad"></span>100%</span>'
            '<span><i style="background:#fcfcfb;border-style:dashed"></i>not measured</span>'
            '<span><i style="background:#fcfcfb;border:3px solid #d03b3b"></i>✖ not installable</span>'
            '<span><i class="dot">!</i>failing tests</span>'
            '<span><i style="background:#f0efec;border-radius:2px;border-color:#c3c2b7"></i>standard module</span>'
            '<span><i style="background:#ebe9f8;border-radius:2px;border:2px solid #4a3aa7"></i>◆ Odoo enterprise module</span>'
            '<span>Size of an addon: its lines of code</span>'
            '<span>⋯ duplicate link: A → C while A → B → C already shows it (hidden unless asked)</span></div></section>')


def line_chart(title: str, points: list[tuple[str, float]], unit: str = "", y_max: Optional[float] = None) -> str:
    """Small line chart of one series (the title names it). Each point has a tooltip; the last value is labeled."""
    width, height, left, right, top, bottom = 440, 170, 42, 52, 30, 24
    y_max = y_max or max(1, *(v for _, v in points))
    x = lambda i: left + i * (width - left - right) / max(len(points) - 1, 1)
    y = lambda v: top + (1 - v / y_max) * (height - top - bottom)
    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" width="{width}" role="img" aria-label="{E(title)}">',
           f'<text class="title" x="0" y="14">{E(title)}</text>']
    for tick in (0, y_max / 2, y_max) if unit or y_max % 2 == 0 else (0, y_max):  # no half tick for counts
        css = "axisline" if tick == 0 else "gridline"
        out.append(f'<line class="{css}" x1="{left}" x2="{width - right}" y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>'
                   f'<text x="{left - 6}" y="{y(tick) + 4:.1f}" text-anchor="end">{tick:g}{unit}</text>')
    out.append('<polyline points="' + " ".join(f"{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(points)) + '"/>')
    for i, (label, value) in enumerate(points):
        out.append(f'<circle class="hit" cx="{x(i):.1f}" cy="{y(value):.1f}" r="12"><title>{E(label)}: {value:g}{unit}</title></circle>'
                   f'<circle class="dot" cx="{x(i):.1f}" cy="{y(value):.1f}" r="4"><title>{E(label)}: {value:g}{unit}</title></circle>')
    last_label, last_value = points[-1]
    out.append(f'<text class="last" x="{x(len(points) - 1) + 9:.1f}" y="{y(last_value) + 4:.1f}">{last_value:g}{unit}</text>')
    out.append(f'<text x="{left}" y="{height - 6}">{E(points[0][0])}</text>')
    if len(points) > 1:
        out.append(f'<text x="{width - right}" y="{height - 6}" text-anchor="end">{E(last_label)}</text>')
    return "".join(out) + '</svg>'


def _trend(history: list[dict]) -> str:
    """One trend per way of measuring (Odoo version, whole or one by one): runs measured differently do not compare."""
    measured = [r for r in history if r.get('coverage') is not None]
    series = {}
    for run in measured:
        series.setdefault(report.series_of(run), []).append(run)
    drawn = {key: runs[-30:] for key, runs in series.items() if len(runs) >= 2}
    if not drawn:
        return ""
    latest = report.series_of(measured[-1])
    shown = latest if latest in drawn else max(drawn, key=lambda key: measured.index(drawn[key][-1]))
    label = lambda r: str(r.get('date', ''))[:16].replace('T', ' ')
    keys = sorted(drawn, key=lambda key: (key != shown, report.series_label(key)))
    select = ('<div class="controls"><select id="series" aria-label="Which runs">' + "".join(
        f'<option value="{i}">{E(report.series_label(key))} ({len(series[key])} runs)</option>' for i, key in enumerate(keys))
        + '</select></div>') if len(keys) > 1 else f'<p class="meta">{E(report.series_label(shown))}</p>'
    blocks = "".join(
        f'<div class="series charts" data-key="{i}"{"" if key == shown else " hidden"}>'
        + line_chart("Coverage", [(label(r), r['coverage']) for r in drawn[key]], "%", 100)
        + line_chart("Failed tests", [(label(r), r.get('failed', 0)) for r in drawn[key]])
        + line_chart("Not installable addons", [(label(r), r.get('not_installable', 0)) for r in drawn[key]]) + '</div>'
        for i, key in enumerate(keys))
    return ('<section><h2>Trend of the measured runs</h2>' + select + blocks
            + '<p class="meta" style="margin:8px 0 0">One point per run that measured the coverage, with the runs measured the '
            'same way only (same Odoo version; whole, or one by one). To forget a run, delete its line in '
            '<code>history.jsonl</code>.</p></section>')


def render(state: dict, summary: dict, old_summary: Optional[dict], diff: Optional[dict], old_date: Optional[str],
           history: list[dict], graphs: dict, meta: dict) -> str:
    measured = lambda e: isinstance(e.get('coverage'), int) and e['coverage'] >= 0
    enterprise = report.enterprise_dependencies(state)
    data = {'addons': {name: {'status': report.status(e), 'color': coverage_color(e['coverage']) if measured(e) else None,
                              'tip': _tip(e)} for name, e in state.items()},
            'enterprise': sorted({module for e in state.values() for module in e.get('enterprise') or []}),
            'duplicates': graphs.get('duplicates', [])}
    when = meta['generated_at'][:16].replace('T', ' ')
    # measured now: the measures are compared with the previous run measured the same way (the last line of the
    # history is this run)
    series = report.series_of(meta) if meta.get('mode') else None
    previous = next((r for r in reversed(history[:-1]) if report.series_of(r) == series), None) if series else None
    if meta.get('mode'):
        how = f"coverage measured in mode <strong>{E(meta['mode'])}</strong> with profile <strong>{E(str(meta['profile']))}</strong>"
    elif summary['measured']:
        how = "coverage not measured in this run: results of the last measure shown for the unchanged addons"
    else:
        how = "coverage not measured (run with <code>-c -p &lt;profile&gt;</code>)"
    return "".join([
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>Codebase report</title><style>{CSS}</style><style id="graph-css">{GRAPH_CSS}</style></head><body><main>',
        f'<h1>Codebase report</h1><p class="meta">Generated {E(when)} · {how}</p>',
        _tiles(summary, old_summary, previous, series), _attention(state, meta.get('standard_tests')), _standard(meta.get('standard_tests')), _enterprise(enterprise), _changes(diff, old_date), _table(state, enterprise),
        _graph(graphs), _trend(history),
        '<footer>Files next to this report: <code>state.json</code> (last results, reused for unchanged addons), '
        '<code>history.jsonl</code> (one line per measured run), <code>coverage_odoo.log</code> (full Odoo log of the last measure).</footer>',
        '</main><script type="application/json" id="data">', json.dumps(data).replace("</", "<\\/"), '</script>',
        f'<script>{JS}</script></body></html>',
    ])
