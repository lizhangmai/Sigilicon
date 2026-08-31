"""Lazy public command entry point."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import sys


_COMMANDS = (
    "artifact-path",
    "candidate",
    "read",
    "execute",
    "experimental",
    "flow",
    "oa",
    "ip",
)

_HELP = """usage: sigilicon [-h] {artifact-path,candidate,read,execute,experimental,flow,oa,ip} ...

Reusable EDA flow orchestration.

positional arguments:
  {artifact-path,candidate,read,execute,experimental,flow,oa,ip}

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
    if command == "candidate":
        from sigilicon.cli.candidate import main as candidate_main

        return candidate_main(arguments[1:])
    if command == "read":
        from sigilicon.cli.agentic_read import main as agentic_read_main

        return agentic_read_main(arguments[1:])
    if command == "execute":
        from sigilicon.cli.agentic_execute import main as agentic_execute_main

        return agentic_execute_main(arguments[1:])
    if command == "experimental":
        from sigilicon.cli.experimental import main as experimental_main

        return experimental_main(arguments[1:])
    if command == "oa" or command == "ip":
        from sigilicon.cli.flow import main as flow_main

        return flow_main(arguments)
    raise AssertionError(f"unhandled command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
