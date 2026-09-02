"""Resolve the canonical project artifact layout for non-Python owners."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sigilicon.paths import ProjectContext


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon artifact-path",
        description="Resolve paths through the canonical ArtifactLayout.",
    )
    commands = parser.add_subparsers(dest="kind", required=True)

    exported = commands.add_parser("export", help="resolve one named export")
    exported.add_argument("owner")
    exported.add_argument("name")
    exported.add_argument("components", nargs="*")
    exported.add_argument("--project-root", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    context = ProjectContext.from_project_root(args.project_root)
    print(context.artifacts.export(args.owner, args.name, *args.components))
    return 0
