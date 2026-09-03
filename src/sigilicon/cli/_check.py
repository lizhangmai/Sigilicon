"""Implementation of the public project check command."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from sigilicon.cli.common import emit_json, open_cli_project
from sigilicon.workflows.repository_checks import inspect_repository_designs


def _summary(report: dict[str, object]) -> dict[str, object]:
    configuration = report["configuration"]
    components = report["components"]
    operations = report["operations"]
    if not isinstance(configuration, dict):
        raise TypeError("repository check configuration result is malformed")
    if not isinstance(components, dict) or not isinstance(operations, dict):
        raise TypeError("repository check owner result is malformed")
    return {
        "passed": report["passed"],
        "project": report["project"],
        "configuration": {
            name: configuration[name]
            for name in (
                "documents",
                "contracts",
                "native_documents",
                "owners",
            )
        },
        "components": {
            name: {
                key: value[key]
                for key in ("kind", "lifecycle")
            }
            for name, value in components.items()
        },
        "operations": {
            name: len(value)
            for name, value in operations.items()
        },
        "releases": sorted(report["ip_releases"]),
        "oa_assemblies": sorted(report["oa_assemblies"]),
        "platforms": sorted(report["platforms"]),
    }


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sigilicon check",
        description="Check the active project's source and integration contracts.",
    )
    parser.add_argument("--project-root", type=Path)
    parser.add_argument(
        "--full",
        action="store_true",
        help="emit the complete resolved contract graph",
    )
    args = parser.parse_args(argv)
    try:
        report = inspect_repository_designs(open_cli_project(args.project_root))
        emit_json(report if args.full else _summary(report))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
