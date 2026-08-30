"""Inspect one canonical design source without touching OpenAccess."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sigilicon.cli.common import die, emit_json
from sigilicon.paths import discover_project_contract
from sigilicon.workflows.design_lifecycle import inspect_design
from sigilicon.workflows.project import load_project


def main(
    argv: Sequence[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("topology",),
        default="topology",
    )
    args = parser.parse_args(argv)
    project = load_project(discover_project_contract(__file__))
    try:
        inspection = inspect_design(args.design, project=project)
        emit_json(inspection.as_dict())
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
