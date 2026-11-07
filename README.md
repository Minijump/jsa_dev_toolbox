# jsa dev toolbox

A command line toolbox for Odoo developers: get a production database running locally in one command, see the state
of your custom addons (tests, coverage, dependencies), find the commit that broke a test.

## Install
Linux or WSL, Python 3.10+, PostgreSQL.
```bash
sudo apt install pipx postgresql-client graphviz && pipx ensurepath
git clone https://github.com/Minijump/jsa_dev_toolbox.git jsa_dev_toolbox && cd jsa_dev_toolbox
pipx install -e .
cp config.example.toml config.toml
```

## Set up
In `config.toml`: the folders of your addons (`[codebase] addons_roots`), one profile per Odoo installation
(`[profiles.19]`: its Python, `odoo-bin` and addons paths), and what a restore does to a database (the usual steps
are already there: 2FA off, every password `admin`, ...). Put your dumps in `dumps/`, then check it all with
`jsa doctor`.

## Commands
| Command | What it does | Options |
|---|---|---|
| **Databases** | | |
| `jsa restore <db> [zip]` | restores a backup zip of `dumps/` with its filestore, runs the restore steps, saves a snapshot, lists the addons to update | `--neutralize -p <profile>`: run `odoo-bin neutralize` (odoo.sh dumps already are)<br>`--ignore-sql-errors`: go on when the SQL restore reported errors<br>`--no-snapshot`: keep the previous snapshot<br>`--postgres-user`: system user for the superuser-only extensions (default `postgres`) |
| `jsa snapshot <db>` | saves a copy of the database and its filestore | |
| `jsa reset <db>` | puts the database back to its snapshot, in seconds | |
| `jsa clone <db> <new>` | copies a database and its filestore into a new one | `--no-snapshot`: do not save the copy as its snapshot |
| `jsa list` | the local databases | |
| `jsa remove <db>` | deletes a database, its filestore and its snapshot | |
| `jsa pending-upgrades <db>` | the addons whose code version differs from the installed one, with the `-u` argument to paste | |
| `jsa enterprise <db>` | the enterprise modules installed, and which of your modules need them | |
| `jsa exp-date <db>` | pushes the expiration date to 2135 (`restore` already does it) | |
| **Codebase** | | |
| `jsa structure` | the report of your addons: lines of code, dependency graph, enterprise dependencies, trends | `-c -p <profile>`: install and test the addons, measure their coverage<br>`-m whole`, `isolated` or `both`: all addons together (default), each one alone with its dependencies, or both<br>`--standard-tests`: also run the tests of the standard modules they need (long)<br>`-a`, `--all`: measure again the addons that did not change<br>`-o`, `--open`: open the report when it is ready |
| `jsa report` | opens the last report | |
| `jsa bisect <test> <good> [bad]` | finds the commit that broke a test (`/module.test_name`), even a test written today: it is taken from your checkout | `-p <profile>` (required): the Odoo that runs the test<br>`--fast`: install the other modules once and copy that database for each commit |
| **UI tests** (optional) | | |
| `jsa uitests setup` | downloads Robot Framework and a browser into `uitests/.env`; the commands below exist once it is done | |
| `jsa uitests run [tests]` | plays the tests in a browser, writes a report with a picture of each failure | `--url`: a running Odoo (default `http://localhost:8069`)<br>`-d`, `--db`: the database to test<br>`-p <profile>`: start that Odoo for the time of the tests<br>`-n`, `--new-db` (with `-p`): test on a fresh database with your addons and demo data<br>`--rebuild`: build that database again<br>`--keep-db`: keep the database of the run<br>`-w`, `--watch`: show the browser, slowed down<br>`-o`, `--open`: open the report at the end |
| `jsa uitests report` | opens the last report | |
| `jsa uitests keywords` | the keywords usable in a test | `--no-open`: write the list without opening it |
| `jsa uitests remove` | deletes what `setup` downloaded (your tests stay) | |
| **jsa** | | |
| `jsa doctor` | checks PostgreSQL, graphviz, `config.toml`, the profiles and the plugins | |
| `jsa version` | which code runs, and whether the repository has newer commits | `--no-fetch`: do not ask the remote |
| `jsa update` | pulls and reinstalls jsa | |

`--data-dir` (`restore`, `snapshot`, `reset`, `clone`, `remove`, `doctor`): Odoo's data folder, where the filestores
are, when it is not `~/.local/share/Odoo`. `jsa <command> --help` describes every argument and option.

## UI tests (proof of concept)
Acceptance tests written as plain lines of Odoo keywords (`Open Menu  Sales`, `Set Field  Customer  Gemini Furniture`,
`Click Button  Confirm`), played in a real browser. Optional: nothing is downloaded until `jsa uitests setup`.

## More
The [guide](docs/guide.md) covers the configuration, each command in detail, writing ui tests and keywords, your own
commands (plugins) and the development of jsa. License: [MIT](LICENSE).

## Roadmap
- **Improve user accceptance tests**
