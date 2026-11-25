"""The codebase report page: one self-contained HTML file (no network, no external library).

Sections: totals, what needs attention, changes since the previous run, the security review (security.py), one
sortable table of the addons (details on click), the dependency graph (with or without standard modules) and the trend
of the measured runs.
Coverage is a continuous color from red (0%) to green (100%). States use a symbol + a label, never the color alone.
"""
import json
import math
from datetime import datetime
from html import escape as E
from typing import Optional

from . import report, security
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
.series[hidden]{display:none}
details.trend{border-top:1px solid var(--grid)}details.trend:first-child{border-top:none}
details.trend summary{display:grid;grid-template-columns:10px minmax(120px,290px) 80px 1fr;gap:14px;align-items:baseline;
padding:9px 0;cursor:pointer;list-style:none}
details.trend summary::-webkit-details-marker{display:none}
details.trend summary::before{content:"▸";color:var(--muted)}details.trend[open] summary::before{content:"▾"}
details.trend summary:hover .tname{color:var(--accent)}
.tname{font-weight:600}.tnow{font-weight:600;font-variant-numeric:tabular-nums}.tnote{color:var(--ink2);font-size:13px}
.chart{display:block;width:100%;height:auto;margin:0 0 10px}
.chart text{fill:var(--muted);font-size:11px}
.chart .last{fill:var(--ink);font-size:12px;font-weight:600}
.chart .event line{stroke:var(--ink2);stroke-dasharray:4 3}.chart .event text{fill:var(--ink2);font-size:11px}
.chart .gridline{stroke:var(--grid)}.chart .axisline{stroke:var(--axis)}
.chart polyline{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round}
.chart .dot{fill:var(--accent);stroke:var(--surface);stroke-width:2}.chart .hit{fill:transparent}
.chart .hit:hover+.dot{r:6}
details.trend svg.full,details.trend.from0 svg.zoomed{display:none}details.trend.from0 svg.full{display:block}
.axisbar{display:flex;justify-content:flex-end}button.axis{font-size:12px;padding:2px 8px}
ul.findings{padding:0;margin:8px 0 0}
.finding{list-style:none;margin:8px 0;padding:8px 12px;border:1px solid var(--grid);border-radius:8px}
.finding[hidden]{display:none}
.finding .why{color:var(--ink2);margin:2px 0}
.finding pre{font:12px ui-monospace,Consolas,monospace;background:var(--hover);padding:4px 8px;border-radius:4px;margin:4px 0 6px;
white-space:pre-wrap;word-break:break-word}
.tag{display:inline-block;font-size:11px;font-weight:600;border-radius:4px;padding:0 6px;margin-right:6px;background:var(--serious);color:#fff}
button.copy{font-size:12px;padding:2px 8px}
details.rule{border-top:1px solid var(--grid)}
details.rule>summary{display:grid;grid-template-columns:10px minmax(220px,400px) 120px 1fr;gap:12px;align-items:baseline;
padding:8px 0;cursor:pointer;list-style:none}
details.rule>summary::-webkit-details-marker{display:none}
details.rule>summary::before{content:"▸";color:var(--muted)}details.rule[open]>summary::before{content:"▾"}
details.rule>summary:hover .rname{color:var(--accent)}
.rname{font-weight:600}.rkind{color:var(--ink2);font-size:13px}.rcount{font-variant-numeric:tabular-nums}
details.rule>p{color:var(--ink2);margin:4px 0 6px 22px;max-width:900px}details.rule>ul.findings{margin-left:22px}
details.reviewed{margin:0 0 10px 22px}details.reviewed>summary{cursor:pointer}
.mutated h3{font-size:14px;margin:16px 0 4px}#mutation details>summary{cursor:pointer;margin-top:8px}
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
// ----- security review, mutation testing: the lines to copy into reviewed.toml
document.querySelectorAll('button.copy').forEach(btn => btn.addEventListener('click', () => {
  const text = btn.dataset.copy, label = btn.textContent;
  const done = () => { btn.textContent = 'Copied: paste it into reviewed.toml, then say why in reason'; setTimeout(() => btn.textContent = label, 2500); };
  const byHand = () => {  // file:// pages may not have the clipboard API
    const area = document.createElement('textarea');
    area.value = text; document.body.append(area); area.select();
    try { document.execCommand('copy'); done(); } catch (e) { window.prompt('Copy these lines:', text); }
    area.remove();
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, byHand); else byHand();
}));
const series = document.getElementById('series');  // the trend of one way of measuring at a time
if (series) {
  const pick = () => document.querySelectorAll('.series').forEach(b => b.hidden = b.dataset.key !== series.value);
  series.addEventListener('change', pick); pick();
}
// a chart zoomed on its values (at first) or from 0: the same in the trends of the other ways of measuring
document.querySelectorAll('details.trend button.axis').forEach(btn => btn.addEventListener('click', () => {
  const section = btn.closest('details.trend'), from0 = !section.classList.contains('from0');
  document.querySelectorAll(`details.trend[data-chart="${section.dataset.chart}"]`).forEach(d => {
    d.classList.toggle('from0', from0);
    d.querySelector('button.axis').textContent = from0 ? 'Zoom on the values' : 'Axis from 0';
  });
}));
// a chart folded or unfolded: the same in the trends of the other ways of measuring
document.querySelectorAll('details.trend').forEach(d => d.addEventListener('toggle', () =>
  document.querySelectorAll(`details.trend[data-chart="${d.dataset.chart}"]`).forEach(o => { if (o.open !== d.open) o.open = d.open; })));
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
    if coverage == NOT_INSTALLABLE and entry.get('statements'):
        return '<span class="muted" title="Its tests never ran: counted as 0% in the total">0%</span>'
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


def _delta(new, old, unit: str = "", decimals: int = 0, compared: str = "the previous report", short: bool = False) -> str:
    """'+300 since last run' ('+1' when short), the exact comparison in its tooltip; '' when nothing changed."""
    if new is None or old is None or new == old:
        return ""
    text = f"{new - old:+,.{decimals}f}{unit}" + ("" if short else " since last run")
    return f'<span title="Compared with {E(compared)}">{text}</span>'


def _tiles(summary: dict, old: Optional[dict], previous: Optional[dict] = None, series: Optional[tuple] = None) -> str:
    """`old`: the summary of the previous report, for what does not depend on how the tests were run (addons, lines
    of code). `previous`: the previous run measured the same way as this one (`series`), for the measures: none when
    this report did not measure anything, or when nothing was measured that way before.
    Each tile: a label, the value, at most two short lines (the change since the last run, a short note)."""
    old, previous = old or {}, previous or {}
    compared = f"the previous run measured the same way: {report.series_label(series)}" if series else ""
    measure = lambda key, unit="", decimals=0, short=False: _delta(summary[key], previous.get(key), unit, decimals, compared, short)
    report_delta = lambda key: _delta(summary[key], old.get(key))
    with_change = lambda text, change: text + (f" ({change})" if change else "")
    coverage = f"{summary['coverage']:.0f}%" if summary['coverage'] is not None else "–"
    passed = summary['tests'] - summary['failed']
    enterprise = summary.get('enterprise')
    tiles = [
        ("Addons", f"{summary['addons']}", [report_delta('addons')]),
        ("Lines of code", f"{summary['loc']:,}", [report_delta('loc'), "excl. tests, po, migrations"]),
        ("Coverage", coverage, [measure('coverage', " pts", 1), "branch coverage"]),
        ("Tests passed", f"{passed:,} / {summary['tests']:,}" if summary['tests'] else "–",
         [with_change(f"{summary['failed']} failed", measure('failed', short=True))]),
        ("Not installable", f"{summary['not_installable']}", [measure('not_installable')]),
        ("Enterprise dependencies", "–" if enterprise is None else f"{enterprise}",
         ["no enterprise folder in config.toml"] if enterprise is None else [report_delta('enterprise'), "addons using it directly"]),
    ]
    return '<div class="tiles">' + "".join(
        f'<div class="tile"><div class="label">{label}</div><div class="value">{value}</div>'
        + "".join(f'<div class="sub">{line}</div>' for line in lines if line) + '</div>'
        for label, value, lines in tiles) + '</div>'


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
                 'way than in the previous run (whole instead of one by one, or the reverse).</p>')
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


def _table(state: dict, enterprise: dict, reviewed: Optional[dict] = None, mutation: Optional[dict] = None) -> str:
    used_by = {name: sorted(n for n, e in state.items() if name in e.get('depends', [])) for name in state}
    to_review = {name: sum(1 for f in e.get('security') or [] if f['id'] not in (reviewed or {})) for name, e in state.items()}
    scores = {name: mutation_score(result) for name, result in (mutation or {}).items()}
    columns = [("name", "Addon", ""), ("rank", "Status", ""), ("coverage", "Coverage", ""), ("tests", "Tests", ""),
               ("failed", "Failed", "num"), ("loc", "Lines of code", "num"), ("usedby", "Used by", "num"),
               ("ent", "Enterprise", "num"), ("security", "Security", "num")] + (
              [("mutation", "Mutation", "num")] if mutation else [])
    head = "".join(f'<th data-key="{key}" class="{css}">{label}</th>' for key, label, css in columns)
    bodies = []
    for name, entry in sorted(state.items(), key=lambda item: (STATUS_RANK[report.status(item[1])], item[0])):
        state_name = report.status(entry)
        coverage = entry['coverage'] if isinstance(entry.get('coverage'), int) and entry['coverage'] >= 0 else -1
        failed = len(entry.get('failed') or [])
        ent = enterprise.get(name, {})
        ent_cell = f'<span class="st ent" title="{E(", ".join(ent))}"><b>◆</b>{len(ent)}</span>' if ent else ""
        value = scores.get(name)
        mutation_cell = (f'<td class="num" title="Changes of its code noticed by the tests (jsa mutation)">'
                         f'{"" if value is None else f"{value}%"}</td>') if mutation else ""
        bodies.append(
            f'<tbody class="addon" data-name="{E(name.lower())}" data-status="{state_name}" data-rank="{STATUS_RANK[state_name]}" '
            f'data-coverage="{coverage}" data-tests="{entry.get("tests") or 0}" data-failed="{failed}" data-loc="{entry.get("loc", 0)}" '
            f'data-usedby="{len(used_by[name])}" data-ent="{len(ent)}" data-security="{to_review[name]}" '
            f'data-mutation="{-1 if value is None else value}"><tr><td class="name">{E(name)}</td><td>{_status_html(state_name)}</td>'
            f'<td>{_coverage_html(entry)}</td><td>{_tests_html(entry)}</td><td class="num">{failed or ""}</td>'
            f'<td class="num">{entry.get("loc", 0):,}</td><td class="num">{len(used_by[name]) or ""}</td><td class="num">{ent_cell}</td>'
            f'<td class="num" title="Security findings to review">{to_review[name] or ""}</td>{mutation_cell}</tr>'
            f'<tr class="detail"><td colspan="{len(columns)}">{_detail(name, entry, state, used_by[name], ent)}</td></tr></tbody>')
    options = "".join(f'<option value="{name}">{label}</option>' for name, (label, _, _) in STATUS.items())
    options += '<option value="enterprise">Needs Odoo enterprise</option>'
    return (f'<section><h2>Addons ({len(state)})</h2><div class="controls">'
            '<input type="search" id="search" placeholder="Filter by name" aria-label="Filter addons by name">'
            f'<select id="only" aria-label="Filter by status"><option value="">All statuses</option>{options}</select>'
            '<span class="meta" style="margin:0">Click a row for the details, a column title to sort.</span></div>'
            f'<table id="addons"><thead><tr>{head}</tr></thead>{"".join(bodies)}</table></section>')


def _change(new: int, gone: int) -> str:
    """'+2 new, −1 gone' since the previous report ('' when nothing changed)."""
    parts = ([f'<span class="neg">+{new} new</span>'] if new else []) + ([f'<span class="pos">−{gone} gone</span>'] if gone else [])
    return ", ".join(parts)


def _security(state: dict, reviewed: dict, before: Optional[dict], off=()) -> str:
    """The security review, one fold per rule: in its summary, how many findings to review and the change since the
    previous report (+ new, − gone); inside, what it looks for (the docstring of its function), its findings (the new
    ones first), those reviewed (folded, with their reason). Then the stale lines of reviewed.toml and the rules
    switched off in config.toml. `before`: {id: rule} of the previous report, None if it had no security review."""
    if not any('security' in e for e in state.values()):
        return ""
    findings = [f for e in state.values() for f in e.get('security') or []]
    current = {f['id'] for f in findings}
    is_new = lambda f: before is not None and f['id'] not in before
    place = lambda f: (f['addon'], f['file'], f['line'])
    stale = sorted(reviewed.keys() - current)

    def item(f: dict, reason: Optional[str] = None) -> str:
        where = f" · {E(f['where'])}" if f.get('where') else ""
        end = (f'<div class="why">Reviewed: {E(reason)}</div>' if reason is not None else
               f'<button class="copy" data-copy="{E(security.review_entry(f))}">Copy for reviewed.toml</button>')
        tag = '<span class="tag">new</span>' if reason is None and is_new(f) else ""
        return (f'<li class="finding">{tag}<strong>{E(f["addon"])}</strong> · <code>{E(f["file"])}:{f["line"]}</code>'
                f'{where}<div class="why">{E(f["why"])}</div><pre>{E(f["code"])}</pre>{end}</li>')

    folds, total, total_new, total_gone = [], 0, 0, 0
    for r in security.RULES:
        if r.key in off:
            continue
        mine = [f for f in findings if f['rule'] == r.key]
        to_review = sorted((f for f in mine if f['id'] not in reviewed), key=lambda f: (not is_new(f), *place(f)))
        done = sorted((f for f in mine if f['id'] in reviewed), key=place)
        new = sum(1 for f in to_review if is_new(f))
        gone = sum(1 for i, rule in (before or {}).items() if rule == r.key and i not in current and i not in reviewed)
        total, total_new, total_gone = total + len(to_review), total_new + new, total_gone + gone
        change = _change(new, gone)
        count = ((f"{len(to_review)} to review" if to_review else "nothing to review") + (f" ({change})" if change else "")
                 + (f" · {len(done)} reviewed" if done else ""))
        body = ("".join(f"<p>{E(' '.join(p.split()))}</p>" for p in r.text.split("\n\n"))
                + f'<p class="meta">Its key: <code>{r.key}</code>. To switch it off, in config.toml: '
                  f'<code>[security] skip = ["{r.key}"]</code></p>')
        if to_review:
            body += f'<ul class="findings">{"".join(item(f) for f in to_review)}</ul>'
        if done:
            body += (f'<details class="reviewed"><summary>Reviewed ({len(done)})</summary><ul class="findings">'
                     + "".join(item(f, reviewed[f['id']]) for f in done) + '</ul></details>')
        folds.append(f'<details class="rule" data-rule="{r.key}"><summary><span class="rname">{E(r.title)}</span>'
                     f'<span class="rkind">{E(r.kind)}</span><span class="rcount">{count}</span></summary>{body}</details>')
    body = ('<p class="meta" style="margin:0 0 10px">Places of your code to look at, found by reading the files: not '
            'proof of a flaw. Click a rule for what it looks for, what it leaves out, and its findings. When a finding '
            'is fine, copy its lines into <code>reviewed.toml</code> (next to <code>config.toml</code>) and say why in '
            '<code>reason</code>: it leaves the list and the chart.</p>' + "".join(folds))
    if stale:
        body += (f'<p class="meta">{len(stale)} line(s) of <code>reviewed.toml</code> match no finding anymore (the code '
                 f'changed or was removed), they can go: {", ".join(f"<code>{E(i)}</code>" for i in stale)}</p>')
    if off:
        body += ('<p class="meta">Switched off in config.toml (<code>[security] skip</code>): '
                 + ", ".join(f"<code>{E(key)}</code>" for key in off) + '</p>')
    change = _change(total_new, total_gone)
    return (f'<section id="security"><h2>Security review ({total} to review'
            + (f': {change} since the previous report' if change else '') + f')</h2>{body}</section>')


REMOVALS = ("remove call", "remove assignment", "remove continue/break")  # mutants.REMOVALS


def mutation_counts(result: dict) -> dict:
    """How many changes of 'jsa mutation' the tests noticed (killed), did not (survived), never ran (not reached)."""
    statuses = [m.get('status') for m in result.get('mutants', [])]
    return {status: statuses.count(status) for status in ('killed', 'survived', 'not reached')}


def mutation_score(result: dict) -> Optional[int]:
    """Of the changes the tests reach, the share they notice (killed / (killed + survived)); None when none."""
    counts = mutation_counts(result)
    reached = counts['killed'] + counts['survived']
    return round(100 * counts['killed'] / reached) if reached else None


def _change_html(mutant: dict) -> str:
    """'If <code>move._post()</code> is removed, no test fails.'"""
    short = lambda text: E(text if len(text) <= 160 else text[:159] + "…")
    if mutant.get('op') in REMOVALS:
        return f"If <code>{short(mutant['original'])}</code> is removed, no test fails."
    return f"If <code>{short(mutant['original'])}</code> becomes <code>{short(mutant['mutated'])}</code>, no test fails."


def mutation_entry(addon: str, mutant: dict) -> str:
    """The lines to copy into reviewed.toml for a change that cannot matter (ASCII: pasted in any editor)."""
    return (f"[[mutation]]\nid = \"{mutant['id']}\"  # {addon}/{mutant['file']}:{mutant['line']} | {mutant['function']} | "
            f"{mutant['op']}\nreason = \"\"\n")


def _mutation(results: dict, reviewed: dict) -> str:
    """What 'jsa mutation' found: per addon, its score, the changes of its code that no test noticed (grouped by
    function, as sentences), those reviewed folded, its score run after run."""
    if not results:
        return ""
    blocks, to_review_total = [], 0
    for addon, result in sorted(results.items()):
        counts, value = mutation_counts(result), mutation_score(result)
        survivors = sorted((m for m in result.get('mutants', []) if m.get('status') == 'survived'),
                           key=lambda m: (m['file'], m['line']))
        to_review = [m for m in survivors if m['id'] not in reviewed]
        done = [m for m in survivors if m['id'] in reviewed]
        to_review_total += len(to_review)
        when = E(str(result.get('date', ''))[:16].replace('T', ' '))
        notes = []
        if result.get('browser_tests'):
            notes.append(f"{result['browser_tests']} browser test(s) (tours) not run: what only they check counts as not "
                         "noticed")
        if result.get('left_out'):
            notes.append("left out, failing or flaky without any change: " + ", ".join(f"<code>{E(t)}</code>"
                                                                                       for t in result['left_out']))
        head = (f'<h3>{E(addon)} · {value if value is not None else "–"}{"%" if value is not None else ""} of the changes '
                f'reached noticed</h3><p class="meta" style="margin:0 0 6px">{counts["killed"]} change(s) noticed by the '
                f'tests, {counts["survived"]} not noticed, {counts["not reached"]} not reached by any test (the coverage '
                f'shows those) · {result.get("tests", 0)} tests · measured {when}'
                + (f' with Odoo {result["odoo"]}' if result.get('odoo') else '')
                + (f' in {_seconds(result["seconds"])}' if result.get('seconds') else '') + '</p>'
                + "".join(f'<p class="meta" style="margin:0 0 6px">{note}</p>' for note in notes))

        def item(m: dict, reason: Optional[str] = None) -> str:
            end = (f'<div class="why">Reviewed: {E(reason)}</div>' if reason is not None else
                   f'<button class="copy" data-copy="{E(mutation_entry(addon, m))}">Copy for reviewed.toml</button>')
            again = ' · checked by a real install (its code also runs while installing)' if m.get('checked') else ''
            return (f'<li class="finding"><strong>{E(m["function"])}</strong> · <code>{E(m["file"])}:{m["line"]}</code>'
                    f'{again}<div>{_change_html(m)}</div>{end}</li>')

        body = head + (f'<ul class="findings">{"".join(item(m) for m in to_review)}</ul>' if to_review else
                       '<p class="meta">Every change the tests reach is noticed.</p>' if not done else '')
        if done:
            body += (f'<details><summary>Reviewed ({len(done)})</summary><ul class="findings">'
                     + "".join(item(m, reviewed[m['id']]) for m in done) + '</ul></details>')
        runs = [(str(r.get('date', ''))[:16].replace('T', ' '), round(100 * r['killed'] / (r['killed'] + r['survived'])))
                for r in result.get('runs', []) if r.get('killed', 0) + r.get('survived', 0)]
        if len(runs) >= 2:
            body += '<p class="meta" style="margin:12px 0 0">Its score, run after run</p>' + line_chart(
                f"Score of {addon}", runs, "%", 100, True)
        blocks.append(f'<div class="mutated">{body}</div>')
    return ('<section id="mutation"><h2>Untested behaviour: mutation testing'
            + (f' ({to_review_total} change(s) not noticed)' if to_review_total else '') + '</h2>'
            '<p class="meta" style="margin:0 0 10px"><code>jsa mutation</code> changes the code of an addon one small '
            'change at a time (<code>&gt;</code> becomes <code>&lt;=</code>, a call is removed, ...) and runs the tests '
            'that reach it: a change no test notices is behaviour the tests run without checking it. When one cannot '
            'matter, copy its lines into <code>reviewed.toml</code> and say why in <code>reason</code>.</p>'
            + "".join(blocks) + '</section>')


def _seconds(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m{seconds % 60:02d}s"


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


def number(value: float) -> str:
    """12,480 / 70.4: a value as shown in a label."""
    return f"{int(value):,}" if value == int(value) else f"{value:,.1f}"


def axis_number(value: float) -> str:
    """12.5k: short, for the axis."""
    return f"{value / 1000:,.4g}k" if abs(value) >= 10000 else f"{value:,.6g}"


def zoomed_axis(values: list[float], whole: bool = False) -> tuple[float, float, float]:
    """(low, high, step) of an axis covering the values with round ticks, instead of starting at 0. `whole`: counts,
    whose ticks are whole numbers (no "0.5 findings" when they are all 0)."""
    low, high = min(values), max(values)
    span = (high - low) or max(abs(high) * 0.02, 1)  # a flat line: a narrow band around it
    magnitude = 10 ** math.floor(math.log10(span / 2))
    if whole:
        magnitude = max(1, magnitude)
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10)
                if m * magnitude >= span / 2 and (not whole or float(m * magnitude).is_integer()))
    low, high = math.floor(low / step) * step, math.ceil(high / step) * step
    return low, (high if high > low else low + step), step


def line_chart(title: str, points: list[tuple], unit: str = "", y_max: Optional[float] = None,
               zoom: bool = False, events: list[tuple[float, str]] = (), css: str = "") -> str:
    """Line chart of one measure over the runs (the section around it names it). points: (label, value[, tooltip]).
    Each point has a tooltip (its label unless given), the first and last labels are on the x axis, the last
    value is labeled. zoom: the axis covers the values instead of starting at 0 (+2% on 80,000 lines of code would
    look flat). events: (position on the x axis, counted in runs; what happened), drawn as dashed vertical lines.
    As wide as the page's sections (1200 = their width on a large screen: the text keeps its size there).
    css: more classes of the <svg>."""
    width, height, left, right, top, bottom = 1200, 200, 48, 72, 26, 24
    values = [p[1] for p in points]
    if zoom:
        low, high, step = zoomed_axis(values, whole=not unit)  # no unit: a count
        ticks = [low + i * step for i in range(round((high - low) / step) + 1)]
    else:
        low, high = 0, y_max or max(1, *values)
        ticks = [0, high / 2, high] if unit or high % 2 == 0 else [0, high]  # no half tick for counts
    x = lambda i: left + i * (width - left - right) / max(len(points) - 1, 1)
    y = lambda v: top + (1 - (v - low) / (high - low)) * (height - top - bottom)
    out = [f'<svg class="chart{" " + css if css else ""}" viewBox="0 0 {width} {height}" width="{width}" role="img" '
           f'aria-label="{E(title)}">']
    for tick in ticks:
        css = "axisline" if tick == ticks[0] else "gridline"
        out.append(f'<line class="{css}" x1="{left}" x2="{width - right}" y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>'
                   f'<text x="{left - 6}" y="{y(tick) + 4:.1f}" text-anchor="end">{axis_number(tick)}{unit}</text>')
    for position, text in events:
        if 0 <= position <= len(points) - 1:
            at, near_end = x(position), x(position) > left + 0.7 * (width - left - right)
            short = text if len(text) <= 40 else text[:39] + "…"
            out.append(f'<g class="event"><title>{E(text)}</title><line x1="{at:.1f}" x2="{at:.1f}" y1="{top - 6}" '
                       f'y2="{height - bottom}"/><text x="{at + (-5 if near_end else 5):.1f}" y="{top - 9}" '
                       f'text-anchor="{"end" if near_end else "start"}">{E(short)}</text></g>')
    out.append('<polyline points="' + " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values)) + '"/>')
    for i, (label, value, *tooltip) in enumerate(points):
        tip = f"<title>{E(tooltip[0] if tooltip else label)}: {number(value)}{unit}</title>"
        out.append(f'<circle class="hit" cx="{x(i):.1f}" cy="{y(value):.1f}" r="12">{tip}</circle>'
                   f'<circle class="dot" cx="{x(i):.1f}" cy="{y(value):.1f}" r="4">{tip}</circle>')
    last_label, last_value = points[-1][:2]
    out.append(f'<text class="last" x="{x(len(points) - 1) + 9:.1f}" y="{y(last_value) + 4:.1f}">{number(last_value)}{unit}</text>')
    out.append(f'<text x="{left}" y="{height - 6}">{E(points[0][0])}</text>')
    if len(points) > 1:
        out.append(f'<text x="{width - right}" y="{height - 6}" text-anchor="end">{E(last_label)}</text>')
    return "".join(out) + '</svg>'


# the charts of the trend, in their default order: (key in the history = name in config.toml, title, unit). Each one
# is zoomed on its values at first (80% -> 82% must show), its axis from 0 one click away (the user, 2026-11-24)
TREND_CHARTS = (
    ('coverage', "Coverage", "%"),
    ('loc', "Lines of code", ""),
    ('tests', "Tests", ""),
    ('failed', "Failed tests", ""),
    ('security', "Security findings to review", ""),
    ('not_installable', "Not installable addons", ""),
    ('no_tests', "Addons without tests", ""),
    ('addons', "Addons", ""),
    ('enterprise', "Enterprise dependencies", ""),
)
TRENDS_OPEN = ('coverage', 'loc')  # unfolded at first, unless [report] trends_open of config.toml says otherwise


def code_date(run: dict):
    """When the measured code was written: the date of its last commit (recorded by the runs since 2026-11-21), else
    the date of the run. Measuring old commits places them in the past."""
    return report.parse_date(run.get('code_date') or run.get('date'))


def event_positions(dates: list, events: list[dict]) -> list[tuple[float, str]]:
    """Where each event falls on an x axis with one step per run: between the two runs around its date."""
    found = []
    for event in events:
        when = report.parse_date(event['date'])
        for i in range(len(dates) - 1):
            if dates[i] <= when <= dates[i + 1]:
                span = (dates[i + 1] - dates[i]).total_seconds()
                found.append((i + ((when - dates[i]).total_seconds() / span if span else 0), str(event['event'])))
                break
    return found


def _trend_section(key: str, title: str, unit: str, is_open: bool, runs: list[dict], events: list[dict],
                   value_of, custom: bool = False, detail=None) -> str:
    """One chart in its foldable section, titled with its last value. The runs are in the order of their code_date.
    Drawn twice: zoomed on its values (shown) and from 0 (class 'full'), a button swaps them (class 'from0' on the
    section, see JS). custom: a chart said to come later when no run recorded it yet (a measure of config.toml, the
    security review; the other charts are left out). detail: more about a run for the tooltip of its point."""
    points = [(r, value_of(r)) for r in runs if value_of(r) is not None]
    head = f'<details class="trend" data-chart="{key}"{" open" if is_open else ""}><summary><span class="tname">{E(title)}</span>'
    if not points:
        return (head + '<span class="tnow">–</span><span class="tnote">recorded from the next run measuring the coverage (-c)</span>'
                       '</summary></details>') if custom else ""
    label = lambda r: str(r.get('code_date') or r.get('date', ''))[:16].replace('T', ' ')
    head += f'<span class="tnow">{number(points[-1][1])}{unit}</span>'
    if len(points) == 1:
        return head + f'<span class="tnote">one run so far ({E(label(points[0][0]))})</span></summary></details>'
    # the tooltip gives the Odoo version, and both dates for a run measured after its code was written (an old commit)
    tip = lambda r: label(r) + (f" · Odoo {r['odoo']}" if r.get('odoo') else "") + (
        f" (measured {str(r['date'])[:10]})" if r.get('code_date') and str(r['code_date'])[:10] != str(r.get('date', ''))[:10] else "") + (
        f" · {detail(r)}" if detail and detail(r) else "")
    dates = [code_date(r) for r, _ in points]
    marks = event_positions(dates, events + version_changes([r for r, _ in points])) if all(dates) else []
    chart = lambda zoom: line_chart(title, [(label(r), v, tip(r)) for r, v in points], unit, 100 if unit == "%" else None,
                                    zoom, marks, "zoomed" if zoom else "full")
    return (head + '</summary><div class="axisbar"><button type="button" class="axis" title="The axis of this chart starts '
            'at 0, or covers only its values">Axis from 0</button></div>' + chart(True) + chart(False) + '</details>')


def version_changes(runs: list[dict]) -> list[dict]:
    """An event between two runs on different Odoo versions ('Odoo 17 → 19'), halfway between their dates."""
    found = []
    for before, after in zip(runs, runs[1:]):
        if before.get('odoo') and after.get('odoo') and before['odoo'] != after['odoo']:
            start, end = code_date(before), code_date(after)
            if start and end:
                found.append({'date': (start + (end - start) / 2).isoformat(), 'event': f"Odoo {before['odoo']} → {after['odoo']}"})
    return found


def open_findings(run: dict, reviewed: dict, off=()) -> Optional[int]:
    """The security findings of a run not reviewed (with today's reviewed.toml), those of the rules switched off left
    out, None when it has no review."""
    found = run.get('security')
    return None if found is None else sum(1 for rule, ids in found.items() if rule not in off for i in ids if i not in reviewed)


def _findings_detail(run: dict, reviewed: dict, off=()) -> str:
    """The findings to review of a run, rule by rule (tooltip of a point of the security chart)."""
    titles = {r.key: r.title for r in security.RULES}
    counts = [(titles.get(key, key), sum(1 for i in ids if i not in reviewed))
              for key, ids in (run.get('security') or {}).items() if key not in off]
    return ", ".join(f"{title}: {n}" for title, n in counts if n)


def _trend(history: list[dict], measures: list[str] = (), choice: Optional[dict] = None,
           reviewed: Optional[dict] = None, off=()) -> str:
    """One trend per method (whole, or one by one): runs measured differently do not compare. Every Odoo version on
    the same curve (a dashed line where it changes); when the runs span several versions, each one alone too, one
    choice away. `measures`: the labels of [[report.measures]]. `choice`: [report] trends (the charts shown: by default all, the
    measures last) and trends_open (unfolded at first: by default coverage and lines of code). The unfolded charts
    come first in the order of trends_open, then the others in the order of trends. `reviewed`: the security findings
    found fine (reviewed.toml): the security chart counts the others, in every run."""
    choice, reviewed = choice or {}, reviewed or {}
    listed = choice.get('trends', [key for key, *_ in TREND_CHARTS] + list(measures))
    opened = [name for name in choice.get('trends_open', TRENDS_OPEN) if name in listed]
    unfolded = set(opened)
    # the unfolded charts first, in the order of trends_open, then the folded ones in the order of trends
    shown_charts = opened + [name for name in listed if name not in unfolded]
    if not shown_charts:
        return ""
    runs, events = report.runs_and_events(history)
    measured = [r for r in runs if r.get('coverage') is not None]
    series = {}  # (method, None: every Odoo version | one version): its runs
    for run in measured:
        series.setdefault((report.series_of(run), None), []).append(run)
    for method, _ in list(series):
        versions = sorted({r.get('odoo') for r in series[(method, None)] if r.get('odoo')})
        if len(versions) > 1:
            for version in versions:
                series[(method, version)] = [r for r in series[(method, None)] if r.get('odoo') == version]
    for lines in series.values():  # in the order of their code, whatever the order they were measured in
        lines.sort(key=lambda r: code_date(r) or datetime.min)
    drawn = {key: lines[-30:] for key, lines in series.items() if len(lines) >= 2}
    if not drawn:
        return ""
    latest = (report.series_of(measured[-1]), None)
    shown = latest if latest in drawn else max((key for key in drawn if key[1] is None), default=next(iter(drawn)),
                                               key=lambda key: measured.index(drawn[key][-1]))
    keys = sorted(drawn, key=lambda key: (key != shown, key[0], key[1] or 0))
    label = lambda key: report.series_label(*key)
    select = ('<div class="controls"><select id="series" aria-label="Which runs">' + "".join(
        f'<option value="{i}">{E(label(key))} ({len(series[key])} runs)</option>' for i, key in enumerate(keys))
        + '</select></div>') if len(keys) > 1 else f'<p class="meta">{E(label(shown))}</p>'
    builtin = {key: (title, unit) for key, title, unit in TREND_CHARTS}

    def section(name: str, lines: list[dict]) -> str:
        if name == 'security':
            title, unit = builtin[name]
            return _trend_section(name, title, unit, name in unfolded, lines, events,
                                  lambda r: open_findings(r, reviewed, off), custom=True,
                                  detail=lambda r: _findings_detail(r, reviewed, off))
        if name in builtin:
            title, unit = builtin[name]
            return _trend_section(name, title, unit, name in unfolded, lines, events, lambda r: r.get(name))
        if name in measures:  # data-chart: an id, the label may hold any character
            return _trend_section(f"m{list(measures).index(name)}", name, "", name in unfolded, lines, events,
                                  lambda r: (r.get('measures') or {}).get(name), custom=True)
        return ""  # a measure removed from config.toml

    sections = lambda lines: "".join(section(name, lines) for name in shown_charts)
    blocks = "".join(f'<div class="series" data-key="{i}"{"" if key == shown else " hidden"}>{sections(drawn[key])}</div>'
                     for i, key in enumerate(keys))
    return ('<section id="trends"><h2>Trend of the measured runs</h2>' + select + blocks
            + '<p class="meta" style="margin:8px 0 0">One point per run that measured the coverage, with the runs measured the '
            'same way only (whole, or one by one), every Odoo version together (a dashed line where it changes), at the date '
            'of the code measured: its last commit '
            '(the date of the run when it had uncommitted changes), so measuring old commits fills the past. '
            'Click a title to fold or unfold its chart. Each chart is zoomed on its values: "Axis from 0" draws it from '
            '0. To forget a '
            'run, delete its line in <code>history.jsonl</code>; to mark an event (a release, someone joining the team), add '
            'a line there: <code>{"date": "2026-11-01", "event": "Release 2.0"}</code>.</p></section>')


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
    runs = report.runs_and_events(history)[0]
    previous = next((r for r in reversed(runs[:-1]) if report.series_of(r) == series), None) if series else None
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
        _tiles(summary, old_summary, previous, series), _attention(state, meta.get('standard_tests')), _standard(meta.get('standard_tests')), _enterprise(enterprise), _changes(diff, old_date),
        _security(state, meta.get('reviewed') or {}, meta.get('security_before'), meta.get('security_off') or []),
        _mutation(meta.get('mutation') or {}, meta.get('reviewed_changes') or {}),
        _table(state, enterprise, meta.get('reviewed'), meta.get('mutation')),
        _graph(graphs), _trend(history, meta.get('measures') or [], meta.get('trends'), meta.get('reviewed'),
                               meta.get('security_off') or []),
        '<footer>Files next to this report: <code>state.json</code> (last results, reused for unchanged addons), '
        '<code>history.jsonl</code> (one line per measured run), <code>coverage_odoo.log</code> (full Odoo log of the last measure).</footer>',
        '</main><script type="application/json" id="data">', json.dumps(data).replace("</", "<\\/"), '</script>',
        f'<script>{JS}</script></body></html>',
    ])
