"""Check a design catalog or the active repository source contracts."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sigilicon.cli.common import die, emit_json
from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.workflows.design_catalog import inspect_design_catalog
from sigilicon.workflows.repository_checks import inspect_repository_designs


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        help="check one explicit design catalog",
    )
    parser.add_argument(
        "--repository",
        type=Path,
        help="run the checks declared by a repository check manifest",
    )
    args = parser.parse_args(argv)
    context = discover_project_context(__file__)
    root = context.project_root
    try:
        if (args.catalog is None) == (args.repository is None):
            parser.error("select exactly one of --catalog or --repository")
        if args.repository is not None:
            report = inspect_repository_designs(
                args.repository,
                project_root=root,
                artifact_root=context.artifact_root,
            )
        else:
            assert args.catalog is not None
            _, report = inspect_design_catalog(args.catalog, project_root=root)
    except (OSError, RuntimeError, ValueError) as error:
        die(f"ERROR: {error}")
    emit_json(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
