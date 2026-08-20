"""Inspect one canonical design source without touching OpenAccess."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sigilicon.cli.common import die, emit_json
from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.workflows.design_lifecycle import inspect_design


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
    root = discover_project_context(__file__).project_root
    try:
        inspection = inspect_design(args.design, project_root=root)
        emit_json(inspection.as_dict())
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
