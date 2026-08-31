"""Explicit-project stdio launcher for the Sigilicon Native MCP Server."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon-mcp",
        description="Run the capability-filtered Sigilicon Native MCP Server over stdio.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        required=True,
        help="explicit repository root containing sigilicon.toml",
    )
    parser.add_argument(
        "--execution-grant",
        type=Path,
        help="launcher-only canonical approval contract enabling execution tools",
    )
    parser.add_argument(
        "--environment",
        type=Path,
        help="launcher-only current-site execution environment contract",
    )
    parser.add_argument(
        "--history-only",
        action="store_true",
        help="serve persisted Run inspection without loading current catalogs",
    )
    return parser


async def _serve(server: Any) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        import anyio

        from sigilicon.integrations.mcp.server import create_server
    except ModuleNotFoundError as exc:
        if exc.name == "mcp":
            print(
                "ERROR: install the optional 'sigilicon[mcp]' dependency",
                file=sys.stderr,
            )
            return 2
        raise

    try:
        from sigilicon.workflows.project import (
            bind_agentic_execution,
            bind_agentic_read,
            bind_run_read,
        )

        execution = None
        if args.history_only:
            if args.environment is not None or args.execution_grant is not None:
                raise ValueError("history-only MCP does not accept execution bindings")
            interface = bind_run_read(args.project_root)
        elif args.execution_grant is None:
            if args.environment is not None:
                raise ValueError("execution environment requires an execution grant")
            interface = bind_agentic_read(args.project_root)
        else:
            execution = bind_agentic_execution(
                args.project_root,
                grant_contract=args.execution_grant,
                environment_contract=args.environment,
            )
            interface = execution.read
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: cannot bind Sigilicon project: {exc}", file=sys.stderr)
        return 2
    anyio.run(_serve, create_server(execution or interface))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
