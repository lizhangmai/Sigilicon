"""Lazy public command entry point."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import sys


_COMMANDS = (
    "artifact-path",
    "flow",
    "ip",
)

_HELP = """usage: sigilicon [-h] {artifact-path,flow,ip} ...

Reusable EDA flow orchestration.

positional arguments:
  {artifact-path,flow,ip}

options:
  -h, --help            show this help message and exit
"""


def main(argv: Sequence[str] | None = None) -> int:
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
    if command == "flow":
        from sigilicon.cli.flow_core import main as flow_core_main

        return flow_core_main(arguments[1:])
    if command == "artifact-path":
        from sigilicon.cli.artifact_path import main as artifact_path_main

        return artifact_path_main(arguments[1:])
    if command == "ip":
        from sigilicon.cli.ip import main as ip_main

        return ip_main(arguments[1:])
    raise AssertionError(f"unhandled command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
