"""Implementation of the public interactive OA commands."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
import sys
from typing import Any

from sigilicon.cli.common import emit_json
from sigilicon.execution.model import Resources
from sigilicon.paths import ProjectContext, discover_project_contract
from sigilicon.project import Project
from sigilicon.workflows.virtuoso_operations import close_cell, open_project_cell


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon oa",
        description="Open or close an interactive Virtuoso cell view.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    opened = commands.add_parser("open")
    opened.add_argument("library")
    opened.add_argument("cell")
    opened.add_argument("view", nargs="?", default="schematic")
    opened.add_argument("--project-root", type=Path)
    closed = commands.add_parser("close")
    closed.add_argument("library")
    closed.add_argument("cell")
    closed.add_argument("view", nargs="?")
    closed.add_argument("--project-root", type=Path)
    closed.add_argument("--json", action="store_true")
    return parser


def main(
    argv: Sequence[str],
    *,
    client_factory: Callable[[Resources], Any] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        root = (
            discover_project_contract().parent
            if args.project_root is None
            else args.project_root
        )
        project = Project.open(root)
        paths = ProjectContext.from_roots(
            project.project_root,
            artifact_root=project.artifact_root,
            workspace_root=project.workspace_root,
        )
        if client_factory is None:
            from sigilicon.workflows.oa_client import get_client

            client_factory = get_client
        from sigilicon.workflows.oa_client import bind_client

        resources = project.resources()
        client = bind_client(client_factory(resources), resources)
        if args.command == "open":
            open_project_cell(client, paths, args.library, args.cell, args.view)
            print(f"opened {args.library}/{args.cell}/{args.view}")
            return 0
        result = close_cell(client, paths, args.library, args.cell, args.view)
        if args.json:
            emit_json(
                {
                    "library": args.library,
                    "cell": args.cell,
                    "view": args.view,
                    "closed": result.closed,
                    "remaining": result.remaining,
                }
            )
        else:
            suffix = "" if args.view is None else f"/{args.view}"
            print(
                f"closed {result.closed} window(s) for "
                f"{args.library}/{args.cell}{suffix}; {result.remaining} remain"
            )
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Sigilicon defect: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
