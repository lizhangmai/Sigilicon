"""Lazy public command entry point."""

from __future__ import annotations

from collections.abc import Sequence
import sys


_HELP = """usage: sigilicon [-h] {flow,layout,design,oa,ip} ...

Reusable EDA flow orchestration.

positional arguments:
  {flow,layout,design,oa,ip}

options:
  -h, --help            show this help message and exit
"""


def main(argv: Sequence[str] | None = None) -> int:
    """Show dependency-free help or lazily load the complete command tree."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments == ["-h"] or arguments == ["--help"]:
        print(_HELP, end="")
        return 0
    if arguments[0] == "flow":
        from sigilicon.cli.flow_core import main as flow_core_main

        return flow_core_main(arguments[1:])
    from sigilicon.cli.flow import main as flow_main

    return flow_main(arguments)
