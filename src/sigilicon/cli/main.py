"""Lazy public command entry point."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import sys
from typing import Any


_COMMANDS = ("check", "flow", "oa", "release")

_HELP = """usage: sigilicon [-h] {check,flow,oa,release} ...

Reusable EDA flow orchestration.

positional arguments:
  {check,flow,oa,release}
    check               validate the active project
    flow                plan, run, inspect, or clean an owner operation
    oa                  open or close a Virtuoso cell view
    release             plan, build, or audit an IP release

options:
  -h, --help            show this help message and exit
"""


def main(
    argv: Sequence[str] | None = None,
    *,
    oa_client_factory: Callable[[Any], Any] | None = None,
) -> int:
    """Show dependency-free help or lazily load the complete command tree."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments == ["-h"] or arguments == ["--help"]:
        print(_HELP, end="")
        return 0
    command = arguments[0]
    if command not in _COMMANDS:
        parser = argparse.ArgumentParser(prog="sigilicon")
        parser.add_argument("command", choices=_COMMANDS)
        # Let argparse provide the stable usage/error contract for commands
        # that are no longer part of the public CLI.  In particular, do not
        # route an unknown command to the project workflow parser.
        parser.parse_args([command])
        raise AssertionError("argparse rejected an unknown command without exiting")
    if command == "check":
        from sigilicon.cli._check import main as command_main
    elif command == "flow":
        from sigilicon.cli._flow import main as command_main
    elif command == "oa":
        from sigilicon.cli._oa import main as oa_main

        return oa_main(arguments[1:], client_factory=oa_client_factory)
    elif command == "release":
        from sigilicon.cli._release import main as command_main
    else:  # pragma: no cover - _COMMANDS and dispatch stay closed together
        raise AssertionError(f"unhandled command: {command}")
    return command_main(arguments[1:])


if __name__ == "__main__":
    raise SystemExit(main())
