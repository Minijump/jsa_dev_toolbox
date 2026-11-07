"""The list of the keywords usable in a ui test: one page for the test writers, basic keywords then private ones."""
from html import escape as E

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--grid:#e1e0d9;--axis:#c3c2b7;
--border:rgba(11,11,11,.10);--code:rgba(11,11,11,.06)}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--code:rgba(255,255,255,.08)}}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 4px}
.meta{color:var(--ink2);margin:0 0 12px}
section{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;margin-bottom:16px}
pre,code{font:12.5px ui-monospace,Consolas,monospace;background:var(--code);border-radius:4px}
code{padding:1px 5px}pre{padding:10px 12px;margin:8px 0 0;overflow:auto}
input[type=search]{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--axis);border-radius:6px;
padding:6px 10px;width:280px;margin-bottom:16px}
table{border-collapse:collapse;width:100%}
th{text-align:left;color:var(--ink2);font-size:13px;padding:7px 10px;border-bottom:1px solid var(--axis)}
td{padding:9px 10px;border-bottom:1px solid var(--grid);vertical-align:top}
td.name{font-weight:600;white-space:nowrap}td.values{white-space:nowrap}
td p{margin:0 0 6px}td p:last-child{margin:0}
"""

HOW_TO = """A test is a name, then one keyword per line under it. A keyword and its values are separated by at least
two spaces. A value written <code>name=value</code> fills the column or option called <code>name</code>.
<pre>*** Test Cases ***
Confirm a quotation
    Login As    admin
    Open Menu    Sales / Orders / Quotations
    Create Record
    Set Field    Customer    Gemini Furniture
    Add Line    Product=Office Chair    Quantity=2
    Click Button    Confirm
    Status Should Be    Sales Order</pre>"""

JS = """
const search = document.getElementById('search');
search.addEventListener('input', () => {
  const text = search.value.trim().toLowerCase();
  document.querySelectorAll('tbody tr').forEach(row => row.style.display = row.textContent.toLowerCase().includes(text) ? '' : 'none');
});
"""


def clashes(docs: list[dict]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Keywords defined more than once: (conflicts {name: titles of the files}, replacements {name: title of the winner}).
    Robot Framework uses the keyword of a .resource file before the ones written in Python, so a single .resource
    keyword replaces the others (that is how a basic keyword is adapted). Anything else is a conflict: a test using
    the keyword fails."""
    seen = {}
    for doc in docs:
        for kw in doc['keywords']:
            seen.setdefault(kw['name'].lower(), (kw['name'], []))[1].append(doc)
    conflicts, replacements = {}, {}
    for name, definitions in seen.values():
        resources = [d for d in definitions if d.get('resource')]
        if len(definitions) > 1 and len(resources) == 1:
            replacements[name] = resources[0]['title']
        elif len(definitions) > 1:
            conflicts[name] = [d['title'] for d in definitions]
    return conflicts, replacements


def _values(kw: dict) -> str:
    return " ".join(f"<code>{E(arg.get('repr') or arg.get('name') or '')}</code>" for arg in kw.get('args', []))


def _section(doc: dict, replacements: dict[str, str]) -> str:
    winners = {name.lower(): title for name, title in replacements.items()}

    def note(kw):
        winner = winners.get(kw['name'].lower())
        if not winner:
            return ""
        return '<p class="meta">Replaces the other keyword with this name.</p>' if winner == doc['title'] else \
            f'<p class="meta">Replaced by the one of "{E(winner)}".</p>'

    rows = "".join(
        f'<tr><td class="name">{E(kw["name"])}</td><td class="values">{_values(kw)}</td><td>{kw.get("doc") or ""}{note(kw)}</td></tr>'
        for kw in sorted(doc['keywords'], key=lambda kw: kw['name']))
    source = "" if doc['source'] == "OdooKeywords" else f'<p class="meta">{E(doc["source"])}</p>'
    return (f'<section><h2>{E(doc["title"])} ({len(doc["keywords"])})</h2>{source}<table><thead><tr><th>Keyword</th>'
            f'<th>Values</th><th>What it does</th></tr></thead><tbody>{rows}</tbody></table></section>')


def render(docs: list[dict]) -> str:
    """`docs`: [{title, source, keywords: [{name, args: [{repr}], doc (html)}]}], the keyword's doc being trusted html
    (written by Robot Framework's libdoc from the documentation of the keywords)."""
    return "".join([
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>Keywords</title><style>{CSS}</style></head><body><main><h1>Keywords for the ui tests</h1>',
        f'<p class="meta">{sum(len(d["keywords"]) for d in docs)} keywords</p><section><h2>How to write a test</h2>{HOW_TO}</section>',
        '<input type="search" id="search" placeholder="Find a keyword" aria-label="Find a keyword">',
        *(_section(doc, clashes(docs)[1]) for doc in docs),
        f'</main><script>{JS}</script></body></html>',
    ])
