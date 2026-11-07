import pytest
import typer

from jsa.uitests import commands, keyword_list

OUTPUT = """<robot><suite name="Odoo tests">
<suite name="Sales">
<test name="Confirm a quotation"><status status="PASS"/></test>
<test name="Wrong status"><kw name="Status Should Be"><status status="FAIL"/></kw><status status="FAIL">boom</status></test>
</suite><suite name="Contacts"><test name="Create a contact"><status status="PASS"/></test></suite>
</suite><errors><msg level="ERROR">Importing library 'broken.py' failed: SyntaxError</msg><msg level="WARN">w</msg></errors></robot>"""


def test_read_results(tmp_path):
    (tmp_path / "output.xml").write_text(OUTPUT)
    results = commands.read_results(tmp_path / "output.xml")
    assert (results.passed, results.failed, results.total) == (2, ["Wrong status"], 3)
    assert results.messages == ["boom"]
    assert results.report == tmp_path / "log.html"
    assert results.errors == ["Importing library 'broken.py' failed: SyntaxError"]  # e.g. a broken keyword file
    assert commands._failure(tmp_path / "output.xml") is None  # the first test passed
    (tmp_path / "output.xml").write_text('<robot><suite><test name="The browser starts"><kw><status status="FAIL"/></kw>'
                                         '<status status="FAIL">Error: no browser\nmore details</status></test></suite></robot>')
    assert commands._failure(tmp_path / "output.xml") == "Error: no browser"


def _registered(monkeypatch, tmp_path, requirements="robotframework==7.5\n"):
    env = tmp_path / ".env"
    (tmp_path / "requirements.txt").write_text(requirements)
    monkeypatch.setattr(commands, "ENV_DIR", env)
    monkeypatch.setattr(commands, "STAMP", env / "requirements.sha256")
    monkeypatch.setattr(commands, "REQUIREMENTS", tmp_path / "requirements.txt")
    app = typer.Typer()
    commands.register(app)
    return env, {c.name or c.callback.__name__ for c in app.registered_groups[0].typer_instance.registered_commands if not c.hidden}


def test_only_setup_exists_until_the_feature_is_set_up(monkeypatch, tmp_path):
    env, names = _registered(monkeypatch, tmp_path)
    assert names == {"setup"} and not commands.is_active()
    env.mkdir()  # a setup that did not finish is not "set up"
    assert not commands.is_active()
    (env / "requirements.sha256").write_text(commands._requirements_hash())
    _, names = _registered(monkeypatch, tmp_path)
    assert names == {"setup", "run", "report", "keywords", "remove"}
    assert commands.is_active() and commands.is_up_to_date()


def test_setup_is_done_only_when_the_browser_starts(monkeypatch, tmp_path, capsys):
    env, _ = _registered(monkeypatch, tmp_path)
    steps, browser = [], {"problem": "Error: browserType.launch: Executable doesn't exist at /x/chrome-headless-shell"}

    def step(label, command):
        steps.append(label.split()[0])
        # the download command "succeeds" even when it downloaded nothing: only what it prints tells it
        return "Downloading Chrome from https://cdn\n\x1b[2mError: connect ECONNREFUSED\x1b[22m\n  at node:net\nFailed to install browsers\n"

    monkeypatch.setattr(commands, "_create_environment", lambda: env.mkdir(exist_ok=True))
    monkeypatch.setattr(commands, "_step", step)
    monkeypatch.setattr(commands, "missing_system_libraries", lambda: [])
    monkeypatch.setattr(commands, "browser_problem", lambda: browser["problem"])

    with pytest.raises(typer.Exit):  # the browser was not downloaded (company proxy...): not set up, and the reason is shown
        commands.setup()
    out = capsys.readouterr().out
    assert not commands.is_active() and steps == ["Installing", "Downloading"]
    assert "does not start: Error: browserType.launch" in out and "     Error: connect ECONNREFUSED" in out and "HTTPS_PROXY" in out
    assert "at node:net" not in out

    browser["problem"] = None
    commands.setup()
    assert commands.is_up_to_date() and "ui tests ready" in capsys.readouterr().out
    del steps[:]
    commands.setup()  # nothing to do
    assert steps == [] and "already set up" in capsys.readouterr().out

    # set up by a version that did not check the browser: running the setup again downloads the browser only
    browser["problem"] = "Error: browserType.launch: Executable doesn't exist at /x/chrome-headless-shell"
    with pytest.raises(typer.Exit):
        commands.setup()
    assert steps == ["Downloading"] and commands.is_active()

    # system libraries are missing: set up, with the command to install them instead of "ready"
    del steps[:]
    (env / "requirements.sha256").unlink()
    monkeypatch.setattr(commands, "missing_system_libraries", lambda: ["libnss3.so"])
    commands.setup()
    out = capsys.readouterr().out
    assert commands.is_up_to_date() and "sudo apt install libnss3" in out and "ui tests ready" not in out


def test_download_refused_for_a_certificate_is_tried_again(monkeypatch, tmp_path, capsys):
    env, _ = _registered(monkeypatch, tmp_path)
    env.mkdir()
    system = tmp_path / "ca-certificates.crt"
    system.write_text("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(commands, "SYSTEM_CERTIFICATES", system)
    monkeypatch.setattr(commands.shutil, "which", lambda tool: None)  # not in WSL: no Windows certificates to add
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)
    downloads = []

    def step(label, command):
        downloads.append(commands._environment().get("NODE_EXTRA_CA_CERTS"))
        # a company network presents its own certificate, that the downloader does not know
        return "Error: unable to get local issuer certificate\nFailed to install browsers\n" if len(downloads) == 1 else "done"

    monkeypatch.setattr(commands, "_step", step)
    monkeypatch.setattr(commands, "missing_system_libraries", lambda: [])
    monkeypatch.setattr(commands, "browser_problem", lambda: None)
    assert commands._download_browser()
    assert downloads == [None, str(env / "trusted_certificates.pem")]  # second try: with the computer's certificates
    assert "trying again with the 1 certificates" in capsys.readouterr().out


def test_environment_is_outdated_when_requirements_change(monkeypatch, tmp_path):
    env, _ = _registered(monkeypatch, tmp_path)
    env.mkdir()
    (env / "requirements.sha256").write_text(commands._requirements_hash())
    (tmp_path / "requirements.txt").write_text("robotframework==8.0\n")
    assert commands.is_active() and not commands.is_up_to_date()  # 'jsa update' will reinstall


def test_own_keyword_files(monkeypatch, tmp_path, capsys):
    mine, extra = tmp_path / "uitests", tmp_path / "company"
    for folder in (mine, extra, mine / "results", mine / ".env", mine / "tests"):
        folder.mkdir()
    for name in ("b.resource", "a.py", "_helpers.py", "example_keywords.py.sample", "notes.txt"):
        (mine / name).write_text("")
    (mine / ".env" / "installed.py").write_text("")  # what jsa generates next to your files is not a keyword file
    (extra / "company.py").write_text("")
    monkeypatch.setattr(commands, "USER_DIR", mine)
    conf = {"uitests": {"keywords_dirs": [str(extra), str(tmp_path / "missing")]}}
    files = commands.keyword_files(conf)
    assert [f.name for f in files] == ["a.py", "b.resource", "company.py"]  # no helper, no sample, no other file
    assert "missing not found" in capsys.readouterr().out
    assert commands.keyword_files({}) == [mine / "a.py", mine / "b.resource"]
    # the public folder first (basic keywords, toolkit), then the folders of your files
    paths = commands._python_paths(files)
    assert paths[:2] == ["--pythonpath", str(commands.KEYWORDS_DIR)] and set(paths[3::2]) == {str(mine), str(extra)}
    # only the files placed directly in uitests/: those of its subfolders (your tests...) are not keyword files
    (mine / "tests" / "steps.py").write_text("")
    assert commands.keyword_files({}) == [mine / "a.py", mine / "b.resource"]


def test_own_tests_are_played_by_default_once_there_is_one(monkeypatch, tmp_path):
    own = tmp_path / "uitests" / "tests"
    monkeypatch.setattr(commands, "USER_TESTS_DIR", own)
    assert commands.default_tests({}) == [commands.EXAMPLES_DIR]  # nothing of yours yet: the examples
    (own / "orders").mkdir(parents=True)
    assert commands.default_tests({}) == [commands.EXAMPLES_DIR]  # an empty folder is not a test
    (own / "orders" / "quotation.robot").write_text("*** Test Cases ***")
    assert commands.default_tests({}) == [own]
    assert commands.default_tests({"tests": str(tmp_path / "elsewhere")}) == [tmp_path / "elsewhere"]  # config.toml first


def test_screens_of_the_tested_version():
    assert commands.screens_version(19, [17, 19]) == 19
    assert commands.screens_version(20, [17, 19]) == 19  # a newer Odoo: the most recent description
    assert commands.screens_version(18, [17, 19]) == 17
    assert commands.screens_version(16, [17, 19]) == 17  # older than all: the oldest
    assert 19 in commands.described_versions()


def test_public_part_is_in_the_package_folder():
    for path in (commands.KEYWORDS_DIR / "OdooKeywords.py", commands.KEYWORDS_DIR / "odoo_toolkit.py",
                 commands.EXAMPLES_DIR / "contacts.robot", commands.REQUIREMENTS):
        assert path.is_file(), path


def _doc(title, names, resource=False):
    return {"title": title, "source": title, "resource": resource,
            "keywords": [{"name": n, "args": [{"repr": "value=1"}], "doc": f"<p>Does {n}</p>"} for n in names]}


def test_keyword_clashes_and_page():
    docs = [_doc("Basic keywords", ["Login As", "Save Record", "Open Menu"]),
            _doc("Your keywords: a.py", ["Open Menu", "Approve Order"]),
            _doc("Your keywords: b.resource", ["save record", "Create Contact"], resource=True)]
    conflicts, replacements = keyword_list.clashes(docs)
    assert conflicts == {"Open Menu": ["Basic keywords", "Your keywords: a.py"]}  # two python keywords: a test fails
    assert replacements == {"Save Record": "Your keywords: b.resource"}  # a .resource keyword replaces the basic one
    page = keyword_list.render(docs)
    for expected in ("Basic keywords (3)", "Your keywords: a.py (2)", "Approve Order", "<code>value=1</code>",
                     "<p>Does Create Contact</p>", "Replaces the other keyword with this name",
                     'Replaced by the one of "Your keywords: b.resource"'):
        assert expected in page
