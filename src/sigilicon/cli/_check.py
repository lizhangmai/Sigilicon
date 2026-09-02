"""Implementation of the public project check command."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from sigilicon.cli.common import emit_json
from sigilicon.paths import discover_project_contract
from sigilicon.project import Project
from sigilicon.workflows.repository_checks import inspect_repository_designs


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sigilicon check",
        description="Check the active project's source and integration contracts.",
    )
    parser.add_argument("--project-root", type=Path)
    args = parser.parse_args(argv)
    try:
        root = (
            discover_project_contract().parent
            if args.project_root is None
            else args.project_root
        )
        emit_json(inspect_repository_designs(Project.open(root)))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
