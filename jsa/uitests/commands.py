"""uitests: acceptance tests written with Odoo keywords (Robot Framework), played in a real browser.

An optional feature, like an app that is not installed yet: the code is here, but nothing is downloaded and only
'jsa uitests setup' exists until you set it up. Its packages and its browser then live in uitests/.env, never in
jsa's own environment, and 'jsa update' keeps them up to date. 'jsa uitests remove' deletes them.

Two places: jsa/uitests/ is the public part (basic keywords, example tests); uitests/ at the root of the toolbox is
yours and ignored by git: your keyword files (*.py, *.resource; also those of the folders of [uitests] keywords_dirs
in config.toml), like the commands in plugins/, plus what is generated (results/, .env/).

Other commands can run the tests with run_tests().
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import typer

from ..core import config, odoo, ui
from . import keyword_list, testdb

# the public part, read from the toolbox folder (a non-editable install only copies the python packages)
PUBLIC_DIR = config.TOOLBOX_DIR / "jsa" / "uitests"
KEYWORDS_DIR = PUBLIC_DIR / "keywords"  # the basic keywords and the toolkit to write keywords
EXAMPLES_DIR = PUBLIC_DIR / "examples"  # the example tests
REQUIREMENTS = PUBLIC_DIR / "requirements.txt"
# your part, ignored by git
USER_DIR = config.TOOLBOX_DIR / "uitests"  # your own keyword files
USER_TESTS_DIR = USER_DIR / "tests"  # your own tests
KEYWORD_SUFFIXES = (".py", ".resource")  # keywords written in Python / keywords made of other keywords
KEYWORD_FILES_VARIABLE = "JSA_UITESTS_KEYWORDS"  # how keywords/prerun.py receives your keyword files
RESULTS_DIR = USER_DIR / "results"
ENV_DIR = USER_DIR / ".env"  # everything downloaded by the setup: python packages, browser
BROWSERS_DIR = ENV_DIR / "browsers"
STAMP = ENV_DIR / "requirements.sha256"  # the requirements the environment was installed from
GET_PIP = "https://bootstrap.pypa.io/get-pip.py"  # pip's official installer
# system libraries the browser needs and the Ubuntu package providing each
SYSTEM_PACKAGES = {"libnss3.so": "libnss3", "libnssutil3.so": "libnss3", "libsmime3.so": "libnss3", "libnspr4.so": "libnspr4",
                   "libasound.so.2": "libasound2t64"}
# a test that only starts the browser, the way the tests do
BROWSER_CHECK = """*** Settings ***
Library    Browser

*** Test Cases ***
The browser starts
    New Browser    chromium    headless=True
    Close Browser
"""
BROWSER_MISSING = "Executable doesn't exist"  # what a test fails with when the browser was not downloaded
ANSI_COLORS = re.compile(r"\x1b\[[0-9;]*m")
# The browser's downloader only trusts its own list of certificates: on a company network that presents its own
# certificate, the download is refused. It is then given the certificates this computer trusts (see
# trust_known_certificates): those of this Linux and, in WSL, those of Windows.
CERTIFICATES = "trusted_certificates.pem"  # in uitests/.env
SYSTEM_CERTIFICATES = Path("/etc/ssl/certs/ca-certificates.crt")  # Debian, Ubuntu
WINDOWS_CERTIFICATES = """Get-ChildItem Cert:\\LocalMachine\\Root, Cert:\\CurrentUser\\Root | ForEach-Object {
    '-----BEGIN CERTIFICATE-----'
    [Convert]::ToBase64String($_.RawData, 'InsertLineBreaks')
    '-----END CERTIFICATE-----'
}"""


# ---------- the feature's own environment ----------

def is_active() -> bool:
    """Set up, i.e. a setup went to its end (the stamp is written last)."""
    return STAMP.exists()


def _tool(name: str) -> str:
    return str(ENV_DIR / "bin" / name)


def _environment() -> dict[str, str]:
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS_DIR)}
    if (ENV_DIR / CERTIFICATES).exists():
        env.setdefault("NODE_EXTRA_CA_CERTS", str(ENV_DIR / CERTIFICATES))
    return env


def trust_known_certificates() -> int:
    """Write in uitests/.env the certificates this computer trusts, for the downloads of the setup (only them: nothing
    is changed on the system, and certificates are still checked). Gives the number of certificates found."""
    found = []
    if SYSTEM_CERTIFICATES.is_file():
        found.append(SYSTEM_CERTIFICATES.read_text(errors="replace"))
    powershell = shutil.which("powershell.exe")  # WSL
    if powershell:
        script = base64.b64encode(WINDOWS_CERTIFICATES.encode("utf-16-le")).decode()
        try:
            res = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", script],
                                 capture_output=True, text=True, errors="replace", timeout=120)
            found.append(res.stdout.replace("\r", ""))
        except (OSError, subprocess.TimeoutExpired):
            pass
    certificates = "\n".join(found)
    count = certificates.count("-----BEGIN CERTIFICATE-----")
    if count:
        (ENV_DIR / CERTIFICATES).write_text(certificates)
    return count


def _requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def is_up_to_date() -> bool:
    return is_active() and STAMP.exists() and STAMP.read_text().strip() == _requirements_hash()


def _step(label: str, command: list[str]) -> str:
    """Run a step of the setup, and give what it printed."""
    typer.echo(f"🔄 {label}...")
    res = subprocess.run(command, env=_environment(), capture_output=True, text=True, errors="replace")
    if res.returncode != 0:
        typer.echo(f"❌ {label} failed:\n{(res.stdout + res.stderr).strip()[-2000:]}")
        raise typer.Exit(code=1)
    return res.stdout + res.stderr


def missing_system_libraries() -> list[str]:
    """Libraries the downloaded browser needs and the system does not have."""
    browsers = sorted(BROWSERS_DIR.glob("chromium-*/chrome-linux*/chrome"))
    if not browsers or not shutil.which("ldd"):
        return []
    out = subprocess.run(["ldd", str(browsers[-1])], capture_output=True, text=True).stdout
    return sorted({line.split()[0] for line in out.splitlines() if "not found" in line})


def _create_environment():
    """A python environment of its own for the feature, in uitests/.env."""
    typer.echo(f"🔄 Creating {ENV_DIR}...")
    shutil.rmtree(ENV_DIR, ignore_errors=True)  # leftovers of a setup that did not finish
    if subprocess.run([sys.executable, "-m", "venv", str(ENV_DIR)], capture_output=True).returncode == 0:
        return
    # this python cannot put pip in an environment (Ubuntu without python3-venv): create it bare, then install pip in it
    shutil.rmtree(ENV_DIR, ignore_errors=True)
    _step("Creating it without pip", [sys.executable, "-m", "venv", "--without-pip", str(ENV_DIR)])
    installer = ENV_DIR / "get-pip.py"
    try:
        urllib.request.urlretrieve(GET_PIP, installer)
    except OSError as e:
        typer.echo(f"❌ Could not download pip ({e}). Install python3-venv (sudo apt install python3-venv) and try again")
        raise typer.Exit(code=1)
    _step("Installing pip in it", [_tool("python"), str(installer), "--quiet"])
    installer.unlink()


def _failure(output_xml: Path) -> Optional[str]:
    """First line of the error of the first test of a Robot Framework output.xml, None if it passed."""
    status = ET.parse(output_xml).getroot().find(".//test/status")
    if status is not None and status.get("status") == "PASS":
        return None
    message = (status.text if status is not None else "") or ""
    return (message.strip().splitlines() or ["unknown error"])[0]


def browser_problem() -> Optional[str]:
    """Why the browser of the tests cannot start, None when it does. It is started once, the way the tests start it:
    this also catches a download that did not happen (the download command reports a success even then)."""
    with tempfile.TemporaryDirectory(prefix="jsa_browser_") as tmp:
        test = Path(tmp) / "browser.robot"
        test.write_text(BROWSER_CHECK)
        try:
            res = subprocess.run([_tool("python"), "-m", "robot", "--outputdir", tmp, "--log", "NONE", "--report", "NONE",
                                  "--console", "none", str(test)], env=_environment(), cwd=tmp, capture_output=True,
                                 text=True, errors="replace", timeout=180)
        except (OSError, subprocess.TimeoutExpired) as e:
            return str(e)
        output = Path(tmp) / "output.xml"
        if not output.exists():
            return ((res.stdout + res.stderr).strip().splitlines() or ["Robot Framework did not run"])[-1]
        return _failure(output)


def _download_browser() -> bool:
    """Download the browser of the tests and check that it starts. False: it needs system libraries first."""
    download = [_tool("rfbrowser"), "install", "chromium"]
    said = _step("Downloading the browser (a few hundred MB the first time)", download)
    if "certificate" in said.lower() and "NODE_EXTRA_CA_CERTS" not in os.environ:
        count = trust_known_certificates()
        if count:
            typer.echo(f"🔐 The download was refused because of a certificate (company network?): trying again with the "
                       f"{count} certificates this computer trusts")
            said = _step("Downloading the browser", download)
    missing = missing_system_libraries()
    if missing:
        packages = sorted({SYSTEM_PACKAGES.get(lib, lib) for lib in missing})
        typer.echo(f"⚠️ The browser needs system libraries that are not installed: {', '.join(missing)}")
        typer.echo(f"   Install them once with: sudo apt install {' '.join(packages)}")
        return False
    problem = browser_problem()
    if not problem:
        return True
    typer.echo(f"❌ The browser of the ui tests does not start: {problem}")
    # the download reports a success even when nothing could be downloaded: what it printed tells why
    lines = [ANSI_COLORS.sub("", line).strip() for line in said.splitlines()]
    reasons = list(dict.fromkeys(line for line in lines if line.startswith(("Error", "Downloading", "Failed"))))
    if reasons:
        typer.echo("   What its download said:")
        for line in reasons[:8]:
            typer.echo(f"     {line[:300]}")
        typer.echo("   On a company network, the download may need HTTPS_PROXY (a proxy) or NODE_EXTRA_CA_CERTS (the file of "
                   "the company's certificate) in your terminal. Then run: jsa uitests setup")
    raise typer.Exit(code=1)


def install() -> bool:
    """Create or update uitests/.env from jsa/uitests/requirements.txt. False: the browser needs system libraries."""
    if not is_active():
        _create_environment()
    _step("Installing Robot Framework and its browser library", [_tool("pip"), "install", "--quiet", "-r", str(REQUIREMENTS)])
    starts = _download_browser()
    STAMP.write_text(_requirements_hash())
    return starts


def setup():
    """Set up the ui tests: download Robot Framework and a browser into uitests/.env (nothing else is touched).
    Run it again to repair them: it checks that the browser starts, and downloads it again if it does not."""
    if is_up_to_date():
        typer.echo("🔄 Checking that the browser of the ui tests starts...")
        problem = browser_problem()
        if not problem:
            typer.echo("✅ The ui tests are already set up")
            return
        typer.echo(f"⚠️ It does not: {problem}")
        starts = _download_browser()
    else:
        starts = install()
    if starts:
        typer.echo("✅ ui tests ready. Run them with: jsa uitests run   (see jsa uitests --help)")


def sync():
    """Update uitests/.env if the feature is set up and its requirements changed (called by 'jsa update')."""
    if is_active() and not is_up_to_date():
        install()
        typer.echo("✅ ui tests environment updated")


def remove():
    """Deactivate the ui tests: delete everything their setup downloaded (the tests themselves are kept)."""
    if not typer.confirm(f"Delete {ENV_DIR} (packages and browser of the ui tests)?"):
        typer.echo("❌ Cancelled.")
        return
    shutil.rmtree(ENV_DIR)
    typer.echo("✅ ui tests deactivated. Set them up again with: jsa uitests setup")


# ---------- keywords ----------

def keyword_files(conf: dict) -> list[Path]:
    """Your own keyword files: those placed directly in uitests/ and in the folders of [uitests] keywords_dirs.
    A file whose name starts with "_" is not a keyword file (a helper module for the others)."""
    folders = [Path(d).expanduser() for d in conf.get('uitests', {}).get('keywords_dirs', [])]
    for folder in folders:
        if not folder.is_dir():
            typer.echo(f"⚠️ [uitests] keywords_dirs: {folder} not found")

    def files_of(folder: Path) -> list[Path]:
        if not folder.is_dir():
            return []
        return [f for f in sorted(folder.iterdir()) if f.suffix in KEYWORD_SUFFIXES and not f.name.startswith('_')]

    return [f for folder in [USER_DIR, *folders] for f in files_of(folder)]


def _python_paths(files: list[Path]) -> list[str]:
    """Where the keywords are imported from: the public folder (basic keywords, toolkit), then the folders of your
    keyword files, so that they can share helper modules."""
    folders = [KEYWORDS_DIR] + sorted({f.parent for f in files})
    return [arg for folder in folders for arg in ("--pythonpath", str(folder))]


def keyword_docs(files: list[Path]) -> list[dict]:
    """Documentation of the basic keywords, then of each of your keyword files, as read by Robot Framework's
    libdoc: [{title, source, resource (a .resource file: its keywords win over the others), keywords}]."""
    docs = []
    with tempfile.TemporaryDirectory(prefix="jsa_keywords_") as tmp:
        for i, source in enumerate(["OdooKeywords", *files]):
            out = Path(tmp) / f"{i}.json"
            res = subprocess.run([_tool("python"), "-m", "robot.libdoc", *_python_paths(files), str(source), str(out)],
                                 capture_output=True, text=True, errors="replace")
            if res.returncode != 0 or not out.exists():
                lines = [line.strip() for line in (res.stdout + res.stderr).splitlines() if line.strip()]
                reason = next((line for line in lines if "failed:" in line), lines[-1] if lines else "unknown error")
                typer.echo(f"❌ The keywords of {source} could not be read: {reason}")
                continue
            title = "Basic keywords" if i == 0 else f"Your keywords: {Path(source).name}"
            docs.append({'title': title, 'source': str(source), 'resource': str(source).endswith(".resource"),
                         'keywords': json.loads(out.read_text(encoding="utf-8"))["keywords"]})
    return docs


# ---------- running ----------

@dataclass
class TestResults:
    __test__ = False  # not a pytest class
    passed: int = 0
    failed: list[str] = field(default_factory=list)  # names of the failed tests
    messages: list[str] = field(default_factory=list)  # why each of them failed (first line)
    errors: list[str] = field(default_factory=list)  # problems outside the tests, e.g. a keyword file that cannot be loaded
    report: Optional[Path] = None

    @property
    def total(self) -> int:
        return self.passed + len(self.failed)


def read_results(output_xml: Path) -> TestResults:
    """Passed/failed tests from Robot Framework's output.xml."""
    results = TestResults(report=output_xml.parent / "log.html")
    root = ET.parse(output_xml).getroot()
    # the first line says what is wrong; what follows is Python's traceback and search path
    results.errors = [(msg.text or "").strip().splitlines()[0] for msg in root.iterfind("errors/msg")
                      if msg.get("level") == "ERROR" and (msg.text or "").strip()]
    for test in root.iter("test"):
        status = test.find("status")
        if status is not None and status.get("status") == "PASS":
            results.passed += 1
        else:
            results.failed.append(test.get("name"))
            message = ((status.text if status is not None else "") or "").strip()
            results.messages.append(message.splitlines()[0] if message else "")
    return results


def odoo_version(url: str) -> int:
    """Major version of the Odoo answering at `url`."""
    request = urllib.request.Request(f"{url}/web/webclient/version_info", data=b'{"jsonrpc": "2.0", "params": {}}',
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(json.load(response)["result"]["server_version_info"][0])
    except (OSError, KeyError, ValueError) as e:
        typer.echo(f"❌ No Odoo answers at {url} ({e})")
        raise typer.Exit(code=1)


@contextmanager
def started_odoo(profile: dict, db_name: str, data_dir: Optional[Path] = None):
    """Run the Odoo of a profile on a database for the time of the tests. Gives its url."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    log = RESULTS_DIR / "odoo.log"
    typer.echo(f"🚀 Starting Odoo on '{db_name}' (log: {log})...")
    with odoo.serving(profile, db_name, log, data_dir) as url:
        if not url:
            typer.echo(f"❌ Odoo did not start, see {log}")
            raise typer.Exit(code=1)
        yield url


def default_tests(settings: dict) -> list[Path]:
    """The tests played when none is given: the folder of [uitests] tests in config.toml, else yours (uitests/tests,
    once it holds a test), else the examples."""
    if settings.get('tests'):
        return [Path(settings['tests']).expanduser()]
    if USER_TESTS_DIR.is_dir() and any(USER_TESTS_DIR.rglob("*.robot")):
        return [USER_TESTS_DIR]
    return [EXAMPLES_DIR]


def screens_version(version: int, available: list[int]) -> int:
    """The odoo_<version>.py whose screens are used for a version of Odoo, like the keywords choose it: the most
    recent one that is not newer than it, else the oldest there is."""
    usable = [v for v in available if v <= version]
    return max(usable) if usable else min(available)


def described_versions() -> list[int]:
    return sorted(int(p.stem.split("_")[1]) for p in KEYWORDS_DIR.glob("odoo_[0-9]*.py"))


def run_tests(url: str, db_name: str = "", password: str = "admin", tests: Optional[list[Path]] = None,
              watch: bool = False, quiet: bool = False, keywords: Optional[list[Path]] = None) -> TestResults:
    """Play the tests on the Odoo at `url`. For other commands too: quiet=True shows nothing.
    `keywords`: your own keyword files (see keyword_files), usable in the tests like the basic ones."""
    url = url.rstrip("/")
    keywords = keywords or []
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    version, described = odoo_version(url), described_versions()
    used = screens_version(version, described)
    if used != version and not quiet:
        typer.echo(f"⚠️ The screens of Odoo {version} are not described: those of Odoo {used} are used "
                   f"(jsa/uitests/keywords/odoo_{used}.py), a keyword may not find what it looks for. To describe them, copy "
                   f"that file to odoo_{version}.py and adapt it.")
    command = [_tool("python"), "-m", "robot", "--outputdir", str(RESULTS_DIR), *_python_paths(keywords),
               "--prerunmodifier", "prerun.AddOdooKeywords", "--name", "Odoo tests", "--console", "none" if quiet else "verbose",
               "--variable", f"ODOO_URL:{url}", "--variable", f"ODOO_DB:{db_name}", "--variable", f"ODOO_PASSWORD:{password}",
               "--variable", f"ODOO_VERSION:{version}",
               # watching: a visible browser, slowed down so that a person can follow
               "--variable", f"HEADLESS:{not watch}", "--variable", f"SLOWMO:{'0.6s' if watch else '0s'}"]
    environment = {**_environment(), KEYWORD_FILES_VARIABLE: os.pathsep.join(str(f) for f in keywords)}
    subprocess.run(command + [str(t) for t in (tests or [EXAMPLES_DIR])], env=environment,
                   capture_output=quiet, cwd=RESULTS_DIR)
    output = RESULTS_DIR / "output.xml"
    if not output.exists():
        typer.echo("❌ The tests could not be run")
        raise typer.Exit(code=1)
    return read_results(output)


def run(
    tests: Optional[list[Path]] = typer.Argument(None, help="Test files or folders (default: [uitests] tests of config.toml, else your tests in uitests/tests, else the examples of jsa/uitests/examples)"),
    url: Optional[str] = typer.Option(None, "--url", help="Address of a running Odoo (default: [uitests] url of config.toml, else http://localhost:8069)"),
    db_name: Optional[str] = typer.Option(None, "--db", "-d", help="Database to test (needed when the Odoo has several, and with --profile)"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Start the Odoo of this profile for the time of the tests (on --db, or with --new-db), instead of using --url"),
    new_db: bool = typer.Option(False, "--new-db", "-n", help="With --profile: test on a fresh database made for the tests, with your custom addons (or [uitests] modules of config.toml) and demo data. Built once, then copied for each run"),
    rebuild: bool = typer.Option(False, "--rebuild", help="With --new-db: build the test database again, even if nothing changed"),
    keep_db: bool = typer.Option(False, "--keep-db", help="With --new-db: keep the database of the run, to look at what the tests left (the next run replaces it)"),
    watch: bool = typer.Option(False, "--watch", "-w", help="Show the browser and slow it down, to watch the tests being played"),
    open_it: bool = typer.Option(False, "--open", "-o", help="Open the report in the browser at the end"),
):
    """Play the ui tests in a browser, then write a report with the steps of each test (and a screenshot of each failure)."""
    conf = config.load_config(required=bool(profile))
    settings = conf.get('uitests', {})
    db_name = db_name or settings.get('db', '')
    password = settings.get('password', 'admin')
    own_keywords = keyword_files(conf)
    if own_keywords:
        typer.echo(f"🔑 {len(own_keywords)} keyword file(s) of your own: {', '.join(f.name for f in own_keywords)}")
    tests = tests or default_tests(settings)
    if tests == [USER_TESTS_DIR]:
        typer.echo(f"🧪 Playing your tests: {USER_TESTS_DIR}")
    if new_db:
        if not profile:
            typer.echo("❌ --new-db needs --profile: the Odoo to build the test database with.")
            raise typer.Exit(code=1)
        odoo_profile = config.load_profile(profile, conf)
        db_name = testdb.prepare(profile, odoo_profile, conf, RESULTS_DIR / "odoo_build.log", rebuild)
        try:
            with started_odoo(odoo_profile, db_name, testdb.DATA_DIR) as started_url:
                results = run_tests(started_url, db_name, password, tests, watch, keywords=own_keywords)
        finally:
            if keep_db:
                typer.echo(f"💾 The database of this run is kept: '{db_name}' (the next run with --new-db replaces it)")
            else:
                testdb.drop(profile)
    elif profile:
        if not db_name:
            typer.echo("❌ --db (or --new-db) is required with --profile.")
            raise typer.Exit(code=1)
        with started_odoo(config.load_profile(profile, conf), db_name) as started_url:
            results = run_tests(started_url, db_name, password, tests, watch, keywords=own_keywords)
    else:
        results = run_tests(url or settings.get('url', 'http://localhost:8069'), db_name, password, tests, watch,
                            keywords=own_keywords)

    for error in results.errors:
        typer.echo(f"❌ {error}")
    for name in results.failed:
        typer.echo(f"❌ {name}")
    if any(BROWSER_MISSING in message for message in results.messages):
        typer.echo("💡 The browser of the ui tests is not there (its download did not work): run 'jsa uitests setup' to get it")
    typer.echo(f"{'✅' if not results.failed else '⚠️'} {results.passed}/{results.total} tests passed. Report: {results.report}")
    if open_it:
        open_report()
    else:
        typer.echo("   Open it with: jsa uitests report")
    if results.failed:
        raise typer.Exit(code=1)


def open_report():
    """Open the report of the last run in the browser."""
    if not (RESULTS_DIR / "log.html").exists():
        typer.echo("❌ No report yet: run 'jsa uitests run' first")
        raise typer.Exit(code=1)
    ui.open_file(RESULTS_DIR / "log.html")


def keywords(open_it: bool = typer.Option(True, "--open/--no-open", help="Open the list in the browser")):
    """Write the list of the keywords usable in a test (the basic ones and yours), with an example for each."""
    docs = keyword_docs(keyword_files(config.load_config(required=False)))
    conflicts, replacements = keyword_list.clashes(docs)
    for name, titles in conflicts.items():
        typer.echo(f"⚠️ The keyword '{name}' exists in {' and in '.join(titles)}: a test using it fails, rename one of them")
    for name, title in replacements.items():
        typer.echo(f"ℹ️ The keyword '{name}' of '{title}' replaces the other keyword(s) with that name")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    page = RESULTS_DIR / "keywords.html"
    page.write_text(keyword_list.render(docs), encoding="utf-8")
    typer.echo(f"✅ {sum(len(d['keywords']) for d in docs)} keywords listed in {page}")
    if open_it:
        ui.open_file(page)


def register(app: typer.Typer):
    group = typer.Typer(no_args_is_help=True, help="Acceptance tests played in a browser (optional feature: 'jsa uitests setup').")
    group.command()(setup)
    group.command(hidden=True)(sync)
    if is_active():  # the other commands only exist once the feature is set up
        group.command()(run)
        group.command("report")(open_report)
        group.command()(keywords)
        group.command()(remove)
    app.add_typer(group, name="uitests")
