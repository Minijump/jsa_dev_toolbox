"""Plugin: put back the PDF files of the quotation headers and footers (Odoo 18 and later, quotation builder).

A plugin made of a folder: its code (this file), and its own files next to it, so nothing goes in config.toml:

    plugins/quote_pdfs/
        __init__.py               this code (part of the toolbox)
        documents.toml            which file for which header or footer (yours: copy documents.toml.sample)
        cover.pdf, terms.pdf...   the PDF files (yours)

The problem: the headers and footers added to quotations (Sales > Configuration > Headers/Footers) are files of the
filestore. A dump without its filestore (odoo.sh) keeps the records but not their files, and printing a quotation
that uses one fails. Each quotation remembers which headers and footers it uses: uploading new ones by hand leaves
the existing quotations on the broken ones. This command puts the files back into the existing records instead, so
every quotation using them prints again. The ids of the records: 'jsa quote-pdfs <db> -p <profile> --list'.

After each restore, with a step of 'jsa restore' (in config.toml):

    [[restore.extra_steps]]
    label = "Quotation headers and footers put back"
    command = "quote-pdfs -p <profile>"
    requires = ["quotation_document"]
    odoo_versions = [19]
"""
import json
import subprocess
from pathlib import Path

import typer

from jsa import config

FOLDER = Path(__file__).resolve().parent  # the PDF files are here
DOCUMENTS = FOLDER / "documents.toml"

# what runs inside Odoo ('odoo-bin shell'): it writes the files, then describes every header and footer
ODOO_SCRIPT = r'''
import base64, json, os
import odoo

def say(**info):
    print("JSA " + json.dumps(info), flush=True)

if odoo.release.version_info[0] < 18:
    say(error=f"Odoo {odoo.release.version}: the headers and footers are records of their own from Odoo 18 on")
elif "quotation.document" not in env:
    say(error="the quotation builder (sale_pdf_quote_builder) is not installed in this database")
else:
    Document = env["quotation.document"].with_context(active_test=False)
    for doc_id, path in json.loads(os.environ["JSA_QUOTE_PDFS"]).items():
        doc = Document.browse(int(doc_id)).exists()
        if not doc:
            say(id=int(doc_id), problem="no header or footer has this id")
            continue
        try:
            with open(path, "rb") as f, env.cr.savepoint():
                doc.write({"datas": base64.b64encode(f.read())})  # the same record: the quotations using it keep it
        except Exception as e:
            say(id=doc.id, name=doc.name, problem=(str(e).strip().splitlines() or [repr(e)])[0])
            continue
        say(id=doc.id, name=doc.name, put=os.path.basename(path))
    env.cr.commit()
    for doc in Document.search([]):
        say(id=doc.id, name=doc.name, type=doc.document_type, active=doc.active, missing=not doc.datas,
            quotations=env["sale.order"].search_count([("quotation_document_ids", "in", doc.id)]))
'''


def files_to_put() -> dict[int, Path]:
    """{id of the header or footer: its file}, from documents.toml, checked before Odoo is started."""
    if not DOCUMENTS.exists():
        typer.echo(f"❌ No {DOCUMENTS}: copy documents.toml.sample (next to it) and give a file to each header or footer")
        raise typer.Exit(code=1)
    files = {}
    for key, name in config.load_config(path=DOCUMENTS).items():
        if not str(key).isdigit() or not isinstance(name, str):
            typer.echo(f"❌ {DOCUMENTS}: '{key}' must be the id of a header or footer, followed by = \"its file.pdf\"")
            raise typer.Exit(code=1)
        files[int(key)] = FOLDER / name
    if not files:
        typer.echo(f"❌ {DOCUMENTS} gives no file: one line per header or footer, like 12 = \"cover.pdf\"")
        raise typer.Exit(code=1)
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        typer.echo(f"❌ File(s) not found: {', '.join(missing)} (put them in {FOLDER})")
        raise typer.Exit(code=1)
    return files


def run_in_odoo(profile: dict, db_name: str, data_dir: Path, files: dict[int, Path]) -> list[dict]:
    env = {**config.odoo_env(profile), "JSA_QUOTE_PDFS": json.dumps({str(k): str(v) for k, v in files.items()})}
    res = subprocess.run([profile['python'], profile['odoo_bin'], "shell", "-d", db_name, "--addons-path",
                          ",".join(profile['addons_paths']), "--data-dir", str(data_dir), "--log-level=warn"],
                         input=ODOO_SCRIPT, env=env, capture_output=True, text=True, errors="replace")
    said = [json.loads(line[4:]) for line in res.stdout.splitlines() if line.startswith("JSA ")]
    if res.returncode != 0 or not said:
        typer.echo(f"❌ Odoo could not run on '{db_name}':\n{(res.stdout + res.stderr).strip()[-1500:]}")
        raise typer.Exit(code=1)
    return said


def quote_pdfs(
    db_name: str = typer.Argument(..., help="Database to repair"),
    profile: str = typer.Option(..., "--profile", "-p", help="Profile of config.toml: the Odoo of the database"),
    only_list: bool = typer.Option(False, "--list", help="Only list the headers and footers, with their id and whether their file is there"),
    data_dir: Path = typer.Option(config.DEFAULT_DATA_DIR, "--data-dir", help="Odoo data_dir, where the filestores are"),
):
    """Put back the PDF files of the quotation headers and footers, lost with a dump without filestore."""
    files = {} if only_list else files_to_put()
    typer.echo(f"🔄 {'Looking at' if only_list else 'Putting back'} the quotation headers and footers of '{db_name}'...")
    said = run_in_odoo(config.load_profile(profile), db_name, data_dir, files)
    if said[0].get('error'):
        typer.echo(f"⚠️ Nothing done: {said[0]['error']}")
        raise typer.Exit(code=1)
    documents = [info for info in said if 'type' in info]
    used = {info['id']: info['quotations'] for info in documents}
    problems = [info for info in said if 'problem' in info]
    for info in said:
        if 'put' in info:
            typer.echo(f"✅ '{info['name']}' (id {info['id']}): {info['put']} put back, used by {used.get(info['id'], 0)} quotation(s)")
    for info in problems:
        typer.echo(f"❌ id {info['id']}{' ' + repr(info['name']) if info.get('name') else ''}: {info['problem']}")
    shown = documents if only_list else [info for info in documents if info['missing']]
    if shown:
        typer.echo("📄 Headers and footers:" if only_list else
                   f"⚠️ {len(shown)} header(s)/footer(s) still without their file: printing a quotation using one fails")
        for info in shown:
            state = "file missing" if info['missing'] else "file ok"
            typer.echo(f"   id {info['id']:<5} {info['type']:<6} {state:<14} used by {info['quotations']:>4} quotation(s)  "
                       f"{info['name']}{'' if info['active'] else ' (archived)'}")
        if not only_list:
            typer.echo(f"   Give them a file in {DOCUMENTS} (id = \"file.pdf\", the file in that folder)")
    elif documents:
        typer.echo("✅ Every header and footer has its file")
    if problems:
        raise typer.Exit(code=1)


def register(app: typer.Typer):
    app.command("quote-pdfs")(quote_pdfs)
