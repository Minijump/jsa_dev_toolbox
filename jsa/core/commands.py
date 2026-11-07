"""Calling a jsa command from another one, by its name: jsa's own commands and those of the plugins.

This is how a command can use what a plugin adds without knowing it: e.g. 'jsa restore' runs the commands named in
the [[restore.extra_steps]] of config.toml. The calling command asks typer for its context (a parameter
`ctx: typer.Context`) and gives it here.
"""
import shlex
from typing import Optional

import typer


def names(ctx: typer.Context) -> list[str]:
    """Names of the commands of jsa, plugins included."""
    root = ctx.find_root()
    return sorted(root.command.list_commands(root))


def name_of(command: str) -> str:
    """'set-cover --company 1' -> 'set-cover'"""
    return (shlex.split(command) or [""])[0]


def call(ctx: typer.Context, command: str, *arguments: str) -> Optional[str]:
    """Run a command written as after 'jsa' ("name", or "name --option value"), with `arguments` added at its end.
    Gives None when it worked, else why it did not: a command that fails must not stop the one calling it."""
    name, *written = shlex.split(command)
    root = ctx.find_root()
    target = root.command.get_command(root, name)
    if target is None:
        return f"unknown command '{name}'"
    try:
        # not "standalone": the command gives its exit code back instead of ending the program
        code = target.main(args=[*written, *arguments], prog_name=f"jsa {name}", standalone_mode=False)
    except SystemExit as e:
        code = e.code
    except typer.Abort:
        return "cancelled"
    except Exception as e:  # wrong arguments, or the code of a plugin failing in any way
        return e.format_message() if hasattr(e, "format_message") else repr(e)
    return f"exit code {code}" if isinstance(code, int) and code else None
