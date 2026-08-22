"""Resolve the canonical project artifact layout for non-Python owners."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import uuid

from sigilicon.paths import ProjectContext


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon artifact-path",
        description="Resolve paths through the canonical ArtifactLayout.",
    )
    commands = parser.add_subparsers(dest="kind", required=True)

    run = commands.add_parser("run", help="resolve one disposable execution")
    run.add_argument("owner")
    run.add_argument("target")
    run.add_argument("flow")
    run.add_argument("variant")
    run.add_argument("--project-root", type=Path, default=Path.cwd())
    run.add_argument("--identity")
    run.add_argument("--create", action="store_true")
    run.add_argument(
        "--role",
        choices=("inputs", "work", "outputs", "logs"),
        help="print one standard role directory instead of the run root",
    )

    exported = commands.add_parser("export", help="resolve one named export")
    exported.add_argument("owner")
    exported.add_argument("name")
    exported.add_argument("components", nargs="*")
    exported.add_argument("--project-root", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    context = ProjectContext.from_project_root(args.project_root)
    if args.kind == "export":
        print(context.artifacts.export(args.owner, args.name, *args.components))
        return 0

    execution = context.artifacts.execution(
        owner=args.owner,
        target=args.target,
        flow=args.flow,
        variant=args.variant,
        identity=args.identity or uuid.uuid4().hex,
        artifact_kind="external_execution",
        identity_kind="run_id",
    )
    if args.create:
        execution.create()
    print(execution.role(args.role) if args.role else execution.root)
    return 0
