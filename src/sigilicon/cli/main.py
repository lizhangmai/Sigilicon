"""Lazy public command entry point."""

from __future__ import annotations

from collections.abc import Sequence
import sys


_HELP = """usage: sigilicon [-h] {artifact-path,candidate,read,execute,flow,layout,design,oa,ip} ...

Reusable EDA flow orchestration.

positional arguments:
  {artifact-path,candidate,read,execute,flow,layout,design,oa,ip}

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
    if arguments[0] == "artifact-path":
        from sigilicon.cli.artifact_path import main as artifact_path_main

        return artifact_path_main(arguments[1:])
    if arguments[0] == "candidate":
        from sigilicon.cli.candidate import main as candidate_main

        return candidate_main(arguments[1:])
    if arguments[0] == "read":
        from sigilicon.cli.agentic_read import main as agentic_read_main

        return agentic_read_main(arguments[1:])
    if arguments[0] == "execute":
        from sigilicon.cli.agentic_execute import main as agentic_execute_main

        return agentic_execute_main(arguments[1:])
    from sigilicon.cli.flow import main as flow_main

    return flow_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
