"""The codebase report page: one self-contained HTML file (no network, no external library).

Sections: totals, what needs attention, then folded the trend of the commits recorded by jsa backfill, the security
review (security.py), mutation testing, the dependencies on Odoo enterprise, the tests of the standard modules; then
one sortable table of the addons (details on click) and the dependency graph (with or without standard modules).
Coverage is a continuous color from red (0%) to green (100%). States use a symbol + a label, never the color alone.
"""
import json
import math
from collections import Counter
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
--hover:rgba(11,11,11,.04);--ent:#4a3aa7;--line2:#b8641f}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--accent:#3987e5;--hover:rgba(255,255,255,.05);--ent:#9085e9;--line2:#e8964f}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1280px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:22px;margin:0}h2{font-size:16px;margin:0 0 12px}
.meta{color:var(--ink2);margin:4px 0 20px}.meta.small{font-size:12px;margin:-16px 0 20px}
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
.cov{white-space:nowrap;font-variant-numeric:tabular-nums}.whole{display:block;color:var(--muted);font-size:12px}
.pos{color:var(--good)}.neg{color:var(--critical)}
.depgraph{background:#fcfcfb;border:1px solid var(--border);border-radius:8px;overflow:auto;max-height:78vh;padding:8px}
.depgraph svg{display:block;height:auto}
.ent b{color:var(--ent)}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--ink2);font-size:12px;margin:10px 0 0}
.legend .grad{display:inline-block;width:120px;height:12px;border-radius:3px;vertical-align:-2px;margin:0 5px;background:linear-gradient(90deg,hsl(0,55%,58%),hsl(60,55%,58%),hsl(120,55%,58%))}
.legend .dot{background:#ec835a;color:#fff;font-weight:700;font-size:10px;line-height:14px;text-align:center;font-style:normal;border-color:#ec835a}
.legend i{display:inline-block;width:14px;height:14px;border-radius:50%;vertical-align:-3px;margin-right:4px;border:1px solid #898781}
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
.chart polyline.l1{stroke:var(--line2);stroke-dasharray:7 4}.chart .dot.l1{fill:var(--line2)}.chart .last.l1{fill:var(--line2)}
.lines{display:flex;gap:16px;color:var(--ink2);font-size:12px;margin-right:auto}.lines i{display:inline-block;width:20px;border-top:2px solid var(--accent);vertical-align:4px;margin-right:6px}.lines i.l1{border-top:2px dashed var(--line2)}
details.trend svg.full,details.trend.from0 svg.zoomed{display:none}details.trend.from0 svg.full{display:block}
.axisbar{display:flex;justify-content:flex-end;align-items:center}button.axis{font-size:12px;padding:2px 8px}
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
details.rule.mutated>summary{grid-template-columns:10px minmax(180px,320px) 340px 1fr}.mbody{margin-left:22px}
details.part{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:0 18px;margin-bottom:16px}
details.part>summary{display:flex;gap:16px;align-items:baseline;flex-wrap:wrap;padding:14px 0;cursor:pointer;list-style:none}
details.part>summary::-webkit-details-marker{display:none}
details.part>summary::before{content:"▸";color:var(--muted)}details.part[open]>summary::before{content:"▾"}
details.part>summary h2{margin:0}details.part>summary:hover h2{color:var(--accent)}
.psum{color:var(--ink2)}.pbody{padding:0 0 16px}
a{color:var(--accent)}
.whole.bad{color:var(--serious)}
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
// ----- the sections that fold: a link to one (#security) opens it
const openPart = id => { const d = id && document.getElementById(id); if (d && d.tagName === 'DETAILS') d.open = true; };
document.addEventListener('click', e => { const a = e.target.closest('a[href^="#"]'); if (a) openPart(a.getAttribute('href').slice(1)); });
window.addEventListener('hashchange', () => openPart(location.hash.slice(1))); openPart(location.hash.slice(1));
// a chart zoomed on its values (at first) or from 0
document.querySelectorAll('details.trend button.axis').forEach(btn => btn.addEventListener('click', () => {
  const section = btn.closest('details.trend'), from0 = section.classList.toggle('from0');
  btn.textContent = from0 ? 'Zoom on the values' : 'Axis from 0';
}));
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


def _percent(measure: dict) -> Optional[str]:
    """'72%' of a measure (per module, or whole), '0%' when its tests never ran (not installable, counted as 0% in the
    total), None when there is no percentage."""
    coverage = measure.get('coverage')
    if coverage == NOT_INSTALLABLE:
        return "0%" if measure.get('statements') else None
    return f"{coverage}%" if isinstance(coverage, int) and coverage >= 0 else None


def _coverage_html(entry: dict, change: Optional[int] = None) -> str:
    """The coverage per module (a meter, its change since the report compared with), then the whole one."""
    coverage, whole = entry.get('coverage'), _percent(entry.get('whole') or {})
    whole = f'<span class="whole" title="With all the addons installed and tested together">whole {whole}</span>' if whole else ""
    change = (f' <span class="{"pos" if change > 0 else "neg"}" title="Since the report compared with">{change:+d}</span>'
              if change else "")
    if coverage == NOT_INSTALLABLE and entry.get('statements'):
        return '<span class="muted" title="Its tests never ran: counted as 0% in the total">0%</span>' + whole
    if coverage is None or coverage == NOT_INSTALLABLE:
        return '<span class="muted">–</span>' + whole
    if coverage == NO_REPORT:
        return '<span class="muted">no python code</span>'
    return (f'<span class="cov"><span class="meter"><span style="width:{coverage}%;background:{coverage_color(coverage)}"></span>'
            f'</span>{coverage}%{change}</span>{whole}')


def _tests_html(entry: dict, newly: int = 0) -> str:
    """Status, tests and failures in one cell (the user, 2026-11-26: they were in three columns): per module, `newly`
    failing since the report compared with, then the whole run when it differs."""
    status = report.status(entry)
    if status == 'not_measured':
        return '<span class="st muted"><b>–</b>not measured</span>'
    if status == 'not_installable':
        return '<span class="st critical"><b>✖</b>not installable</span>'
    tests, failed, whole = entry.get('tests') or 0, entry.get('failed') or [], entry.get('whole') or {}
    together = whole.get('failed')
    if whole.get('coverage') == NOT_INSTALLABLE:
        line = 'whole: not installable'
    elif together is not None and sorted(together) != sorted(failed):
        line = f'whole: {len(together)} failed'  # a test failing only with all the addons, or passing only with them
    else:
        line = ''
    line = (f'<span class="whole{" bad" if status == "failing" and not failed else ""}" title="With all the addons '
            f'installed and tested together">{line}</span>') if line else ''
    if failed:
        main = (f'<span class="st serious"><b>⚠</b>{len(failed)} of {tests} failed</span>'
                + (f' <span class="neg" title="Failing since the report compared with">+{newly}</span>' if newly else ''))
    elif tests:
        main = f'<span class="st good"><b>✔</b>{tests} passed</span>'
    else:
        main = '<span class="st muted"><b>○</b>no tests</span>'
    return main + line


def _tip(entry: dict) -> str:
    """Tooltip of an addon in the graph."""
    state = report.status(entry)
    parts = [STATUS[state][0].lower()]
    if isinstance(entry.get('coverage'), int) and entry['coverage'] >= 0:
        whole = _percent(entry.get('whole') or {})
        parts.append(f"{entry['coverage']}% coverage" + (f" (whole {whole})" if whole else ""))
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


def _tiles(summary: dict, old: Optional[dict], earlier: list[dict] = ()) -> str:
    """`old`: the summary of the previous report, for what does not depend on the tests (addons, lines of code).
    `earlier`: the runs before this one, for the measures (none when this report did not measure anything): each is
    compared with the most recent recorded commit (jsa backfill) that has it.
    Each tile: a label, the value (per module), at most two short lines (the change since the last run, the value
    whole: all the addons installed and tested together)."""
    old = old or {}
    last = lambda key: next((r[key] for r in reversed(earlier) if r.get(key) is not None), None)
    measure = lambda key, unit="", decimals=0, short=False: _delta(summary.get(key), last(key), unit, decimals,
                                                                   "the most recent recorded commit", short)
    report_delta = lambda key: _delta(summary[key], old.get(key))
    with_change = lambda text, change: text + (f" ({change})" if change else "")
    whole = lambda key, text: (with_change(f"whole: {text.format(summary[key])}", measure(key, " pts" if "%" in text else "",
                                                                                         1 if "%" in text else 0, True))
                               if summary.get(key) is not None else "")
    coverage = f"{summary['coverage']:.0f}%" if summary['coverage'] is not None else "–"
    passed = summary['tests'] - summary['failed']
    enterprise = summary.get('enterprise')
    tiles = [
        ("Addons", f"{summary['addons']}", [report_delta('addons')]),
        ("Lines of code", f"{summary['loc']:,}", [report_delta('loc'), "excl. tests, po, migrations"]),
        ("Coverage", coverage, [measure('coverage', " pts", 1), whole('coverage_whole', "{:.0f}%")],
         "Branch coverage of the code inside functions, per module: each addon alone with its dependencies and its own "
         "tests. Whole: all the addons installed and tested together"),
        ("Tests passed", f"{passed:,} / {summary['tests']:,}" if summary['tests'] else "–",
         [with_change(f"{summary['failed']} failed", measure('failed', short=True)), whole('failed_whole', "{} failed")],
         "Per module; whole: the tests failing with all the addons installed together"),
        ("Not installable", f"{summary['not_installable']}", [measure('not_installable'), whole('not_installable_whole', "{}")],
         "Per module (alone with its dependencies); whole: with all the addons"),
        ("Enterprise dependencies", "–" if enterprise is None else f"{enterprise}",
         ["no enterprise folder in config.toml"] if enterprise is None else [report_delta('enterprise'), "addons using it directly"]),
    ]
    return '<div class="tiles">' + "".join(
        f'<div class="tile"{f" title={chr(34)}{E(tip[0])}{chr(34)}" if tip else ""}><div class="label">{label}</div>'
        f'<div class="value">{value}</div>' + "".join(f'<div class="sub">{line}</div>' for line in lines if line) + '</div>'
        for label, value, lines, *tip in tiles) + '</div>'


def _part(key: str, title: str, summary: str, body: str, is_open: bool = False) -> str:
    """A section of the page that folds (the user, 2026-11-26: what needs attention on top, then the detailed
    sections, each folded with a summary of its content; the table and the graph stay open). A link to #key opens it."""
    return (f'<details class="part" id="{key}"{" open" if is_open else ""}><summary><h2>{E(title)}</h2>'
            f'<span class="psum">{summary}</span></summary><div class="pbody">{body}</div></details>')


def _attention(state: dict, summary: dict, old_summary: Optional[dict], diff: Optional[dict], since: Optional[str],
               standard: Optional[dict] = None, findings: tuple = ({}, 0), survivors: Optional[dict] = None) -> str:
    """Needs attention, on top and open: what got worse since the report compared with (tagged new, first), then
    what is still wrong, and what got better in one line. `findings`: ({rule title: count} of the security findings
    new since then and not reviewed, how many are gone); `survivors`: {addon: count} of the changes no test notices,
    new since the previous 'jsa mutation' of that addon and not reviewed. The sections below and the table say more."""
    diff, new = diff or {}, '<span class="tag">new</span>'
    names = lambda tests, fresh=(): ", ".join(f"{new if t in fresh else ''}<code>{E(t)}</code>" for t in tests)
    newly, newly_whole = dict(diff.get('newly_failing', [])), dict(diff.get('newly_failing_whole', []))
    broken = set(diff.get('could_to_cant', [])) | set(diff.get('added', []))
    items = []  # (something new in it, html)
    for name, entry in sorted(state.items()):
        status, whole = report.status(entry), entry.get('whole') or {}
        if status == 'not_installable':
            items.append((name in broken, f'{_status_html("not_installable")} {new if name in broken else ""}'
                                          f'<strong>{E(name)}</strong>: {E(entry.get("note") or "could not be installed")}'))
        elif status == 'failing':
            failed, fresh_whole = entry.get('failed') or [], name in diff.get('whole_could_to_cant', [])
            parts = [names(failed, newly.get(name, []))] if failed else []
            if whole.get('coverage') == NOT_INSTALLABLE:
                parts.append(f"{new if fresh_whole else ''}cannot be installed with all the addons "
                             f"({E(whole.get('note') or 'see the log')})")
            elif only := [t for t in whole.get('failed') or [] if t not in failed]:
                parts.append("with all the addons (whole): " + names(only, newly_whole.get(name, [])))
            items.append((bool(newly.get(name) or newly_whole.get(name) or fresh_whole),
                          f'{_status_html("failing")} <strong>{E(name)}</strong>: {"; ".join(parts)}'))
    down = '<span class="st critical"><b>▼</b>Coverage down</span>'
    for key, label in (('coverage', 'per module'), ('coverage_whole', 'whole')):
        was, now = (old_summary or {}).get(key), summary.get(key)
        if was is not None and now is not None and now < was - 0.05 and not diff.get('not_comparable'):
            items.append((True, f'{down} {new}in total, {label}: {was:.1f}% → {now:.1f}% '
                                f'(<span class="neg">{now - was:+.1f} pts</span>)'))
    for name, _, change, was, now in diff.get('metrics_changed', []):
        if change and change < 0:
            items.append((True, f'{down} {new}<strong>{E(name)}</strong>: {was}% → {now}% '
                                f'(<span class="neg">{change:+d} pts</span>)'))
    fresh_findings, gone_findings = findings
    if fresh_findings:
        items.append((True, f'<span class="st serious"><b>⚠</b>Security review</span> {new}<a href="#security">{sum(fresh_findings.values())} new security finding(s) to '
                            'review</a>: ' + ", ".join(f"{E(title)} ({n})" for title, n in fresh_findings.items())))
    if survivors:
        items.append((True, f'<span class="st serious"><b>⚠</b>Mutation testing</span> {new}<a href="#mutation">{sum(survivors.values())} new change(s) no test notices</a>: '
                            + ", ".join(f"{E(addon)} ({n})" for addon, n in sorted(survivors.items()))))
    for name, modules in diff.get('enterprise_added', []):
        items.append((True, f'<span class="st ent"><b>◆</b></span>{new}<strong>{E(name)}</strong> now depends on Odoo '
                            f'enterprise: {E(", ".join(modules))}'))
    for name in diff.get('added', []):
        if report.status(state.get(name, {})) == 'no_tests':
            items.append((True, f'{_status_html("no_tests")} {new}<strong>{E(name)}</strong>: a new addon without tests'))
    for module, result in sorted(((standard or {}).get('modules') or {}).items()):
        if result['failed']:
            items.append((False, f'{_status_html("failing")} <strong>{E(module)}</strong> (standard module, with your '
                                 f'addons): {names(result["failed"])}'))
    better = []
    if diff.get('fixed'):
        better.append(f"{sum(len(t) for _, t in diff['fixed'])} test(s) fixed ({E(', '.join(n for n, _ in diff['fixed']))})")
    if diff.get('cant_to_could'):
        better.append("installable again: " + E(", ".join(diff['cant_to_could'])))
    if up := [(n, c) for n, _, c, *_ in diff.get('metrics_changed', []) if c and c > 0]:
        better.append("coverage up: " + ", ".join(f"{E(n)} +{c} pts" for n, c in up))
    if gone_findings:
        better.append(f"{gone_findings} security finding(s) gone")
    items.sort(key=lambda item: not item[0])  # what is new first
    fresh = sum(1 for is_new, _ in items if is_new)
    when = f"since the report of {E(since[:16].replace('T', ' '))}" if since and diff else ""
    told = ([f'<span class="neg">{fresh} new</span>'] if fresh else [f"nothing new"] if when else []) + (
        [f"{len(items) - fresh} still to fix"] if len(items) > fresh else [])
    body = (f'<ul>{"".join(f"<li>{html}</li>" for _, html in items)}</ul>' if items else
            '<p class="meta" style="margin:0">Nothing needs attention.</p>')
    if better:
        body += f'<p class="meta" style="margin:10px 0 0"><span class="pos">Better</span> {when}: {"; ".join(better)}.</p>'
    return _part('attention', 'Needs attention', (" · ".join(told) or "nothing") + (f" ({when})" if when else ""),
                 body, is_open=True)


def _standard(standard: Optional[dict]) -> str:
    """The tests of the standard modules, run with the custom addons installed (like odoo.sh)."""
    if not standard:
        return ""
    modules = standard.get('modules') or {}
    tests, failed = sum(r['tests'] for r in modules.values()), sum(len(r['failed']) for r in modules.values())
    when = E(standard.get('date', '')[:16].replace('T', ' '))
    failing = {module: r['failed'] for module, r in sorted(modules.items()) if r['failed']}
    summary = (f"{tests:,} tests in {len(modules)} standard modules, "
               + (f'<span class="neg">{failed} failed</span>' if failed else "all passed") + f" ({when})")
    body = ('<p class="meta" style="margin:0 0 8px">Run with your addons installed, like odoo.sh '
            '(<code>jsa structure -c --standard-tests</code>).</p>'
            + ('<ul>' + "".join(f"<li><strong>{E(module)}</strong>: " + ", ".join(f"<code>{E(t)}</code>" for t in tests)
                                + "</li>" for module, tests in failing.items()) + '</ul>' if failing else ''))
    return _part('standard', 'Tests of the standard modules', summary, body)


def _enterprise(enterprise: dict[str, dict[str, list[str]]], added: Optional[dict] = None) -> str:
    """Which Odoo enterprise modules the custom addons need: the work to do to run on the community edition.
    `added`: {addon: the enterprise modules it depends on directly since the report compared with}."""
    if not enterprise:
        return ""
    added = added or {}
    direct, indirect = {}, {}
    for addon, modules in enterprise.items():
        for module, via in modules.items():
            (indirect if via else direct).setdefault(module, []).append(addon)
    tag = lambda addon, module: '<span class="tag">new</span>' if module in added.get(addon, []) else ''
    rows = "".join(
        f'<tr><td><span class="st ent"><b>◆</b>{E(module)}</span></td>'
        f'<td>{", ".join(tag(addon, module) + E(addon) for addon in sorted(direct[module]))}</td>'
        f'<td>{E(", ".join(sorted(indirect.get(module, [])))) or "–"}</td></tr>' for module in sorted(direct))
    count = sum(len(modules) for modules in added.values())
    summary = f"{len(direct)} modules, {len(enterprise)} addons" + (f' (<span class="neg">+{count} new</span>)' if count else "")
    return _part('enterprise', 'Dependencies on Odoo enterprise', summary,
                 '<p class="meta" style="margin:0 0 8px">⚠ These addons would not work on the community edition of Odoo.</p>'
                 '<table class="plain"><thead><tr><th>Enterprise module</th><th>Custom addons depending on it</th>'
                 f'<th>Addons needing it through another custom addon</th></tr></thead><tbody>{rows}</tbody></table>')


def _detail(name: str, entry: dict, state: dict, used_by: list[str], enterprise: dict[str, list[str]]) -> str:
    parts = []
    if entry.get('note'):
        parts.append(f"<h3>Why it is not installable</h3>{E(entry['note'])}")
    if entry.get('failed'):
        parts.append("<h3>Failed tests</h3><ul>" + "".join(f"<li><code>{E(t)}</code></li>" for t in entry['failed']) + "</ul>")
    whole, failed = entry.get('whole') or {}, entry.get('failed') or []
    if whole.get('coverage') == NOT_INSTALLABLE and entry.get('coverage') != NOT_INSTALLABLE:
        parts.append(f"<h3>With all the addons (whole)</h3>Cannot be installed with them: {E(whole.get('note') or 'see the log')}")
    elif only := [t for t in whole.get('failed') or [] if t not in failed]:
        parts.append("<h3>Failing only with all the addons (whole)</h3><ul>"
                     + "".join(f"<li><code>{E(t)}</code></li>" for t in only) + "</ul>")
    if passing := [t for t in failed if 'failed' in whole and t not in whole['failed']]:
        parts.append("<h3>Passing with all the addons (whole)</h3>" + ", ".join(f"<code>{E(t)}</code>" for t in passing))
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


def _table(state: dict, enterprise: dict, reviewed: Optional[dict] = None, mutation: Optional[dict] = None,
           diff: Optional[dict] = None, security_before: Optional[dict] = None) -> str:
    """The addons, not folded (the user, 2026-11-26: Status, Tests and Failed in one column, Mutation next to Coverage,
    no Used by column). What changed since the report compared with: a new addon tagged, its coverage +/-, the tests
    failing since, the security findings new since."""
    diff, reviewed = diff or {}, reviewed or {}
    used_by = {name: sorted(n for n, e in state.items() if name in e.get('depends', [])) for name in state}
    open_ = lambda e: [f for f in e.get('security') or [] if f['id'] not in reviewed]
    to_review = {name: len(open_(e)) for name, e in state.items()}
    fresh = {name: sum(1 for f in open_(e) if security_before is not None and f['id'] not in security_before)
             for name, e in state.items()}
    scores = {name: (mutation_score(result), mutation_score_all(result)) for name, result in (mutation or {}).items()}
    coverage_change = {name: change for name, _, change, *_ in diff.get('metrics_changed', []) if change}
    newly = {name: len(tests) for name, tests in diff.get('newly_failing', [])}
    columns = ([("name", "Addon", ""), ("coverage", "Coverage", "")] + ([("mutation", "Mutation", "num")] if mutation else [])
               + [("tests", "Tests", ""), ("loc", "Lines of code", "num"), ("ent", "Enterprise", "num"),
                  ("security", "Security", "num")])
    head = "".join(f'<th data-key="{key}" class="{css}">{label}</th>' for key, label, css in columns)
    bodies = []
    for name, entry in sorted(state.items(), key=lambda item: (STATUS_RANK[report.status(item[1])], item[0])):
        state_name = report.status(entry)
        coverage = entry['coverage'] if isinstance(entry.get('coverage'), int) and entry['coverage'] >= 0 else -1
        failed = len(entry.get('failed') or [])
        ent = enterprise.get(name, {})
        reached, everything = scores.get(name, (None, None))
        mutation_cell = (f'<td class="num" title="Changes of its code noticed by the tests (jsa mutation): of those its '
                         f'tests reach, and of all">{"" if reached is None else f"{reached}%"}'
                         + (f'<span class="whole">{everything}% of all</span>' if everything is not None else "")
                         + '</td>') if mutation else ""
        security_cell = (f'{to_review[name] or ""}' + (f' <span class="neg" title="New since the report compared with">'
                                                       f'+{fresh[name]}</span>' if fresh[name] else ''))
        new_tag = '<span class="tag">new</span>' if name in diff.get('added', []) else ''
        bodies.append(
            f'<tbody class="addon" data-name="{E(name.lower())}" data-status="{state_name}" data-coverage="{coverage}" '
            f'data-mutation="{-1 if reached is None else reached}" data-tests="{STATUS_RANK[state_name] * 10000 - failed}" '
            f'data-loc="{entry.get("loc", 0)}" data-ent="{len(ent)}" data-security="{to_review[name]}">'
            f'<tr><td class="name">{new_tag}{E(name)}</td><td>{_coverage_html(entry, coverage_change.get(name))}</td>'
            f'{mutation_cell}<td>{_tests_html(entry, newly.get(name, 0))}</td><td class="num">{entry.get("loc", 0):,}</td>'
            f'<td class="num" title="{E(", ".join(ent))}">{len(ent) or ""}</td>'
            f'<td class="num" title="Security findings to review">{security_cell}</td></tr>'
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

    folds, total, total_new, total_gone, total_done = [], 0, 0, 0, 0
    for r in security.RULES:
        if r.key in off:
            continue
        mine = [f for f in findings if f['rule'] == r.key]
        to_review = sorted((f for f in mine if f['id'] not in reviewed), key=lambda f: (not is_new(f), *place(f)))
        done = sorted((f for f in mine if f['id'] in reviewed), key=place)
        new = sum(1 for f in to_review if is_new(f))
        gone = sum(1 for i, rule in (before or {}).items() if rule == r.key and i not in current and i not in reviewed)
        total, total_new, total_gone = total + len(to_review), total_new + new, total_gone + gone
        total_done += len(done)
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
    return _part('security', 'Security review', (f"{total} to review" if total else "nothing to review")
                 + (f" ({change})" if change else "") + (f" · {total_done} reviewed" if total_done else ""), body)


REMOVALS = ("remove call", "remove assignment", "remove continue/break")  # mutants.REMOVALS


def mutation_counts(result: dict) -> dict:
    """How many changes of 'jsa mutation' the tests noticed (killed), did not (survived), never ran (not reached)."""
    statuses = [m.get('status') for m in result.get('mutants', [])]
    return {status: statuses.count(status) for status in ('killed', 'survived', 'not reached')}


def mutation_score(result: dict) -> Optional[int]:
    """Of the changes the tests reach, the share they notice (killed / (killed + survived)): how well they check what
    they run; None when none."""
    counts = mutation_counts(result)
    reached = counts['killed'] + counts['survived']
    return round(100 * counts['killed'] / reached) if reached else None


def mutation_score_all(result: dict) -> Optional[int]:
    """Of all the changes, the share the tests notice (killed / all): the changes in code no test runs count as not
    noticed (what PIT and Infection call the mutation score); None when none."""
    counts = mutation_counts(result)
    total = sum(counts.values())
    return round(100 * counts['killed'] / total) if total else None


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
    """What 'jsa mutation' found, one fold per addon: its two scores (of the changes its tests reach, of all its
    changes), the changes no test noticed as sentences (those new since its previous run first, tagged), those
    reviewed folded, its scores run after run."""
    if not results:
        return ""
    folds, total, total_new = [], 0, 0
    score = lambda value: "–" if value is None else f"{value}%"
    for addon, result in sorted(results.items()):
        counts = mutation_counts(result)
        survivors = sorted((m for m in result.get('mutants', []) if m.get('status') == 'survived'),
                           key=lambda m: (not m.get('new'), m['file'], m['line']))
        to_review = [m for m in survivors if m['id'] not in reviewed]
        done = [m for m in survivors if m['id'] in reviewed]
        fresh = sum(1 for m in to_review if m.get('new'))
        total, total_new = total + len(to_review), total_new + fresh
        when = E(str(result.get('date', ''))[:16].replace('T', ' '))
        notes = []
        if result.get('browser_tests'):
            notes.append(f"{result['browser_tests']} browser test(s) (tours) not run: what only they check counts as not "
                         "noticed")
        if result.get('left_out'):
            notes.append("left out, failing or flaky without any change: " + ", ".join(f"<code>{E(t)}</code>"
                                                                                       for t in result['left_out']))

        def item(m: dict, reason: Optional[str] = None) -> str:
            end = (f'<div class="why">Reviewed: {E(reason)}</div>' if reason is not None else
                   f'<button class="copy" data-copy="{E(mutation_entry(addon, m))}">Copy for reviewed.toml</button>')
            again = ' · checked by a real install (its code also runs while installing)' if m.get('checked') else ''
            tag = '<span class="tag">new</span>' if m.get('new') and reason is None else ''
            return (f'<li class="finding">{tag}<strong>{E(m["function"])}</strong> · <code>{E(m["file"])}:{m["line"]}</code>'
                    f'{again}<div>{_change_html(m)}</div>{end}</li>')

        body = (f'<p class="meta" style="margin:0 0 6px">{counts["killed"]} change(s) noticed by the tests, '
                f'{counts["survived"]} not noticed, {counts["not reached"]} not reached by any test (the coverage shows '
                f'those) · {result.get("tests", 0)} tests · measured {when}'
                + (f' with Odoo {result["odoo"]}' if result.get('odoo') else '')
                + (f' in {_seconds(result["seconds"])}' if result.get('seconds') else '') + '</p>'
                + "".join(f'<p class="meta" style="margin:0 0 6px">{note}</p>' for note in notes))
        body += (f'<ul class="findings">{"".join(item(m) for m in to_review)}</ul>' if to_review else
                 '<p class="meta">Every change the tests reach is noticed.</p>' if not done else '')
        if done:
            body += (f'<details class="reviewed"><summary>Reviewed ({len(done)})</summary><ul class="findings">'
                     + "".join(item(m, reviewed[m['id']]) for m in done) + '</ul></details>')
        runs = [(str(r.get('date', ''))[:16].replace('T', ' '),
                 (round(100 * r['killed'] / (r['killed'] + r['survived'])),
                  round(100 * r['killed'] / (r['killed'] + r['survived'] + r.get('not reached', 0)))))
                for r in result.get('runs', []) if r.get('killed', 0) + r.get('survived', 0)]
        if len(runs) >= 2:
            names = ("of the changes reached", "of all the changes")
            body += ('<p class="meta" style="margin:12px 0 0">Its scores, run after run '
                     '<span class="lines">' + "".join(f'<span><i{" class=" + chr(34) + "l1" + chr(34) if n else ""}></i>'
                                                      f'{name}</span>' for n, name in enumerate(names)) + '</span></p>'
                     + line_chart(f"Scores of {addon}", runs, "%", 100, True, names=names))
        count = ((f"{len(to_review)} not noticed" if to_review else "nothing to review")
                 + (f' (<span class="neg">+{fresh} new</span>)' if fresh else "") + (f" · {len(done)} reviewed" if done else ""))
        folds.append(f'<details class="rule mutated" data-addon="{E(addon)}"><summary><span class="rname">{E(addon)}</span>'
                     f'<span class="rkind">{score(mutation_score(result))} of the changes reached · '
                     f'{score(mutation_score_all(result))} of all</span><span class="rcount">{count}</span></summary>'
                     f'<div class="mbody">{body}</div></details>')
    summary = ((f"{total} change(s) no test notices" if total else "every change the tests reach is noticed")
               + (f' (<span class="neg">+{total_new} new</span>)' if total_new else "") + f" · {len(results)} addon(s)")
    intro = ('<p class="meta" style="margin:0 0 10px"><code>jsa mutation</code> changes the code of an addon one small '
             'change at a time (<code>&gt;</code> becomes <code>&lt;=</code>, a call is removed, ...) and runs the tests '
             'that reach it: a change no test notices is behaviour the tests run without checking it. Two scores: of '
             'the changes the tests reach (how well they check what they run), and of all the changes (those in code no '
             'test runs count as not noticed: what other tools call the mutation score). When a change cannot matter, '
             'copy its lines into <code>reviewed.toml</code> and say why in <code>reason</code>.</p>')
    return _part('mutation', 'Mutation testing: untested behaviour', summary, intro + "".join(folds))


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


LINES = ("per module", "whole")  # the two lines of a chart measured both ways


def line_chart(title: str, points: list[tuple], unit: str = "", y_max: Optional[float] = None,
               zoom: bool = False, events: list[tuple[float, str]] = (), css: str = "", names: tuple = ()) -> str:
    """Line chart of a measure over the runs (the section around it names it). points: (label, value[, tooltip]).
    With `names`, several lines: the value of a point is then a tuple, one value per line (None: that run did not
    measure it, the line goes straight to its next point); the second line is dashed (the legend is the section's).
    Each point has a tooltip (its label unless given), the first and last labels are on the x axis, the last
    value of each line is labeled. zoom: the axis covers the values instead of starting at 0 (+2% on 80,000 lines of
    code would look flat). events: (position on the x axis, counted in runs; what happened), drawn as dashed vertical
    lines. As wide as the page's sections (1200 = their width on a large screen: the text keeps its size there).
    css: more classes of the <svg>."""
    width, height, left, right, top, bottom = 1200, 200, 48, 72, 26, 24
    lines = [[p[1][n] for p in points] for n in range(len(names))] if names else [[p[1] for p in points]]
    values = [v for line in lines for v in line if v is not None]
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
    lasts = []  # (line, x, y, text) of the last value of each line
    for n, line in enumerate(lines):
        drawn = [(i, v) for i, v in enumerate(line) if v is not None]
        if drawn:
            out.append(f'<polyline{f" class={chr(34)}l{n}{chr(34)}" if n else ""} points="'
                       + " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in drawn) + '"/>')
            lasts.append([n, x(drawn[-1][0]) + 9, y(drawn[-1][1]) + 4, f"{number(drawn[-1][1])}{unit}"])
    for i, (label, _, *tooltip) in enumerate(points):
        shown = [(names[n] + " " if names else "", line[i]) for n, line in enumerate(lines) if line[i] is not None]
        tip = (f"<title>{E(tooltip[0] if tooltip else label)}: "
               + ", ".join(f"{E(name)}{number(v)}{unit}" for name, v in shown) + "</title>")
        for n, line in enumerate(lines):
            if line[i] is not None:
                out.append(f'<circle class="hit" cx="{x(i):.1f}" cy="{y(line[i]):.1f}" r="12">{tip}</circle>'
                           f'<circle class="dot{f" l{n}" if n else ""}" cx="{x(i):.1f}" cy="{y(line[i]):.1f}" r="4">{tip}</circle>')
    if len(lasts) == 2 and abs(lasts[0][2] - lasts[1][2]) < 13:  # labels apart
        middle = (lasts[0][2] + lasts[1][2]) / 2
        upper, lower = sorted(lasts, key=lambda last: last[2])
        upper[2], lower[2] = middle - 7, middle + 7
    for n, at_x, at_y, text in lasts:
        out.append(f'<text class="last{f" l{n}" if n else ""}" x="{at_x:.1f}" y="{at_y:.1f}">{text}</text>')
    out.append(f'<text x="{left}" y="{height - 6}">{E(points[0][0])}</text>')
    if len(points) > 1:
        out.append(f'<text x="{width - right}" y="{height - 6}" text-anchor="end">{E(points[-1][0])}</text>')
    return "".join(out) + '</svg>'


# the charts of the trend, in their default order: (key in the history = name in config.toml, title, unit, measured
# both ways: a second line, whole, from '<key>_whole'). Each one is zoomed on its values at first (80% -> 82% must
# show), its axis from 0 one click away (the user, 2026-11-24)
TREND_CHARTS = (
    ('coverage', "Coverage", "%", True),
    ('loc', "Lines of code", "", False),
    ('tests', "Tests", "", False),
    ('failed', "Failed tests", "", True),
    ('security', "Security findings to review", "", False),
    ('not_installable', "Not installable addons", "", True),
    ('no_tests', "Addons without tests", "", False),
    ('addons', "Addons", "", False),
    ('enterprise', "Enterprise dependencies", "", False),
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
                   value_of, custom: bool = False, detail=None, names: tuple = ()) -> str:
    """One chart in its foldable section, titled with its last value. The runs are in the order of their code_date.
    Drawn twice: zoomed on its values (shown) and from 0 (class 'full'), a button swaps them (class 'from0' on the
    section, see JS). custom: a chart said to come later when no run recorded it yet (a measure of config.toml, the
    security review; the other charts are left out). detail: more about a run for the tooltip of its point.
    names: the lines of a chart measured both ways (LINES): value_of gives a tuple, one value per line."""
    has = (lambda v: any(x is not None for x in v)) if names else (lambda v: v is not None)
    points = [(r, value_of(r)) for r in runs if has(value_of(r))]
    head = f'<details class="trend" data-chart="{key}"{" open" if is_open else ""}><summary><span class="tname">{E(title)}</span>'
    if not points:
        return (head + '<span class="tnow">–</span><span class="tnote">counted on the recorded commits by the next jsa backfill</span>'
                       '</summary></details>') if custom else ""
    label = lambda r: str(r.get('code_date') or r.get('date', ''))[:16].replace('T', ' ')
    if names:  # the last value of each line: per module in the title, whole next to it
        last = [next((v[n] for _, v in reversed(points) if v[n] is not None), None) for n in range(len(names))]
        head += (f'<span class="tnow">{"–" if last[0] is None else f"{number(last[0])}{unit}"}</span>'
                 + "".join(f'<span class="tnote">{E(name)} {number(v)}{unit}</span>'
                           for name, v in zip(names[1:], last[1:]) if v is not None))
    else:
        head += f'<span class="tnow">{number(points[-1][1])}{unit}</span>'
    if len(points) == 1:
        return head + f'<span class="tnote">one run so far ({E(label(points[0][0]))})</span></summary></details>'
    # the tooltip gives the Odoo version, and both dates for a run measured after its code was written (an old commit)
    tip = lambda r: label(r) + (f" · {r['commit'][:9]}" if r.get('commit') else "") + (f" · Odoo {r['odoo']}" if r.get('odoo') else "") + (
        f" (measured {str(r['date'])[:10]})" if r.get('code_date') and r.get('date') and str(r['code_date'])[:10] != str(r['date'])[:10] else "") + (
        f" · {detail(r)}" if detail and detail(r) else "")
    dates = [code_date(r) for r, _ in points]
    marks = event_positions(dates, list(events) + version_changes([r for r, _ in points])) if all(dates) else []
    chart = lambda zoom: line_chart(title, [(label(r), v, tip(r)) for r, v in points], unit, 100 if unit == "%" else None,
                                    zoom, marks, "zoomed" if zoom else "full", names)
    legend = ('<span class="lines">' + "".join(f'<span><i{f" class={chr(34)}l{n}{chr(34)}" if n else ""}></i>{E(name)}</span>'
                                               for n, name in enumerate(names)) + '</span>') if names else ""
    return (head + f'</summary><div class="axisbar">{legend}<button type="button" class="axis" title="The axis of this chart '
            'starts at 0, or covers only its values">Axis from 0</button></div>' + chart(True) + chart(False) + '</details>')


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


def _trend(runs: list[dict], events: list[dict] = (), measures: list[str] = (), choice: Optional[dict] = None,
           reviewed: Optional[dict] = None, off=(), hidden: int = 0) -> str:
    """The trend of the commits recorded by jsa backfill (`runs`, those of the branch; `hidden`: those of other
    branches), on the same charts whatever their Odoo version (a dashed line where it changes); coverage, failed
    tests and not installable addons have two lines, per module and whole. `events`: {date, event} (backfill.toml).
    `measures`: the labels of [[report.measures]]. `choice`: [report] trends (the charts shown: by default all, the
    measures last) and trends_open (unfolded at first: by default coverage and lines of code). The unfolded charts
    come first in the order of trends_open, then the others in the order of trends. `reviewed`: the security findings
    found fine (reviewed.toml): the security chart counts the others, in every run."""
    choice, reviewed = choice or {}, reviewed or {}
    listed = choice.get('trends', [key for key, *_ in TREND_CHARTS] + list(measures))
    opened = [name for name in choice.get('trends_open', TRENDS_OPEN) if name in listed]
    unfolded = set(opened)
    # the unfolded charts first, in the order of trends_open, then the folded ones in the order of trends
    shown_charts = opened + [name for name in listed if name not in unfolded]
    runs = sorted(runs, key=lambda r: code_date(r) or datetime.min)  # in the order of their code
    count, runs = len(runs), runs[-30:]
    elsewhere = f" ({hidden} recorded in other branches not shown)" if hidden else ""
    how = ('The trend shows the commits recorded with <code>jsa backfill</code>: list in <code>backfill.toml</code> '
           'the commits to record (a release, the first of each month...) and the events (<code>[[events]]</code> date '
           'and event, or <code>event</code> on a commit), then run <code>jsa backfill</code>. '
           '<code>jsa structure</code> never adds to it.')
    if not shown_charts:
        return ""
    if len(runs) < 2:
        return _part('trends', 'Trend of the recorded commits',
                     (f"{count} commit recorded" if count else "no commit recorded yet") + elsewhere,
                     f'<p class="meta" style="margin:0">{how} Two recorded commits make a trend.</p>')
    builtin = {key: (title, unit, both) for key, title, unit, both in TREND_CHARTS}

    def section(name: str) -> str:
        if name == 'security':
            title, unit, _ = builtin[name]
            return _trend_section(name, title, unit, name in unfolded, runs, events,
                                  lambda r: open_findings(r, reviewed, off), custom=True,
                                  detail=lambda r: _findings_detail(r, reviewed, off))
        if name in builtin:
            title, unit, both = builtin[name]
            if both:
                return _trend_section(name, title, unit, name in unfolded, runs, events,
                                      lambda r: (r.get(name), r.get(f"{name}_whole")), names=LINES)
            return _trend_section(name, title, unit, name in unfolded, runs, events, lambda r: r.get(name))
        if name in measures:  # data-chart: an id, the label may hold any character
            return _trend_section(f"m{list(measures).index(name)}", name, "", name in unfolded, runs, events,
                                  lambda r: (r.get('measures') or {}).get(name), custom=True)
        return ""  # a measure removed from config.toml

    last, day = runs[-1], lambda r: str(r.get('code_date') or r.get('date', ''))[:10]
    facts = [f"{count} commits recorded, from {day(runs[0])} to {day(last)}{elsewhere}"]
    if last.get('coverage') is not None:
        facts.append(f"coverage {number(last['coverage'])}%" + (f" (whole {number(last['coverage_whole'])}%)"
                                                               if last.get('coverage_whole') is not None else ""))
    if last.get('loc') is not None:
        facts.append(f"{last['loc']:,} lines of code")
    return _part('trends', 'Trend of the recorded commits', " · ".join(facts), "".join(section(name) for name in shown_charts)
            + f'<p class="meta" style="margin:8px 0 0">One point per recorded commit of this branch, at its date, every '
            'Odoo version together (a dashed line where it changes). Two lines for what is measured both ways: '
            '<strong>per module</strong> (each addon alone with its dependencies, its own tests) and <strong>whole</strong> '
            '(all the addons installed and tested together, like production). Click a title to fold or unfold its chart. '
            f'Each chart is zoomed on its values: "Axis from 0" draws it from 0. {how} To forget a commit, take it out of '
            '<code>backfill.toml</code>.</p>')


def render(state: dict, summary: dict, old_summary: Optional[dict], diff: Optional[dict], old_date: Optional[str],
           runs: list[dict], events: list[dict], graphs: dict, meta: dict) -> str:
    """`runs`: the commits recorded by jsa backfill in the branch of the code, `events` those of backfill.toml."""
    measured = lambda e: isinstance(e.get('coverage'), int) and e['coverage'] >= 0
    enterprise = report.enterprise_dependencies(state)
    data = {'addons': {name: {'status': report.status(e), 'color': coverage_color(e['coverage']) if measured(e) else None,
                              'tip': _tip(e)} for name, e in state.items()},
            'enterprise': sorted({module for e in state.values() for module in e.get('enterprise') or []}),
            'duplicates': graphs.get('duplicates', [])}
    when = meta['generated_at'][:16].replace('T', ' ')
    # measured now: the measures are compared with the most recent recorded commit of the branch (not the commit of
    # the code itself when it has no change: it is the same code)
    branch = meta.get('branch') or {}
    runs = sorted(runs, key=lambda r: code_date(r) or datetime.min)
    earlier = [r for r in runs if not (r['commit'] == branch.get('commit') and not branch.get('dirty'))] \
        if meta.get('measured') else []
    where = (f'<p class="meta small">{"branch <strong>" + E(branch["branch"]) + "</strong> · " if branch.get("branch") else ""}'
             f'commit {E(branch["commit"][:9])} of {E(branch["date"][:16].replace("T", " "))}'
             + (' · with uncommitted changes' if branch.get('dirty') else '') + '</p>') if branch.get('commit') else ""
    if meta.get('measured'):
        how = (f"coverage measured per module and whole with profile <strong>{E(str(meta['profile']))}</strong>")
    elif summary['measured']:
        how = "coverage not measured in this run: results of the last measure shown for the unchanged addons"
    else:
        how = "coverage not measured (run with <code>-c -p &lt;profile&gt;</code>)"
    # what is new since the report compared with: security findings (by rule) and their number gone; the changes no
    # test notices, new since the previous 'jsa mutation' of their addon
    reviewed, before, off = meta.get('reviewed') or {}, meta.get('security_before'), meta.get('security_off') or []
    titles = {r.key: r.title for r in security.RULES}
    current = {f['id'] for e in state.values() for f in e.get('security') or []}
    fresh_findings = Counter(titles.get(f['rule'], f['rule']) for e in state.values() for f in e.get('security') or []
                             if before is not None and f['id'] not in before and f['id'] not in reviewed)
    gone_findings = sum(1 for i, rule in (before or {}).items() if i not in current and i not in reviewed and rule not in off)
    survivors = {addon: n for addon, result in (meta.get('mutation') or {}).items()
                 if (n := sum(1 for m in result.get('mutants', []) if m.get('status') == 'survived' and m.get('new')
                              and m['id'] not in (meta.get('reviewed_changes') or {})))}
    return "".join([
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>Codebase report</title><style>{CSS}</style><style id="graph-css">{GRAPH_CSS}</style></head><body><main>',
        f'<h1>Codebase report</h1><p class="meta">Generated {E(when)} · {how}</p>{where}',
        _tiles(summary, old_summary, earlier),
        _attention(state, summary, old_summary, diff, old_date, meta.get('standard_tests'),
                   (dict(fresh_findings), gone_findings), survivors),
        _trend(runs, events, meta.get('measures') or [], meta.get('trends'), reviewed, off, meta.get('hidden_runs') or 0),
        _security(state, reviewed, before, off),
        _mutation(meta.get('mutation') or {}, meta.get('reviewed_changes') or {}),
        _enterprise(enterprise, dict((diff or {}).get('enterprise_added', []))), _standard(meta.get('standard_tests')),
        _table(state, enterprise, reviewed, meta.get('mutation'), diff, before), _graph(graphs),
        '<footer>Files next to this report: <code>state.json</code> (last results, reused for unchanged addons), '
        '<code>runs/</code> (one file per commit recorded by <code>jsa backfill</code>: the trend), '
        '<code>coverage_odoo.log</code> (full Odoo log of the last measure).</footer>',
        '</main><script type="application/json" id="data">', json.dumps(data).replace("</", "<\\/"), '</script>',
        f'<script>{JS}</script></body></html>',
    ])
