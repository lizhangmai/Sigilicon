"""Implementation of the public project check command."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from sigilicon.cli.common import emit_json, open_cli_project
from sigilicon.workflows.repository_checks import inspect_repository_designs


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sigilicon check",
        description="Check the active project's source and integration contracts.",
    )
    parser.add_argument("--project-root", type=Path)
    args = parser.parse_args(argv)
    try:
        emit_json(inspect_repository_designs(open_cli_project(args.project_root)))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
