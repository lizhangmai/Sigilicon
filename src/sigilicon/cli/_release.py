"""Implementation of the public immutable release commands."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from sigilicon.cli.common import emit_json, open_cli_project
from sigilicon.execution.runs import RunStore
from sigilicon.workflows import ip_packaging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon release",
        description="Plan, build, or audit one immutable IP release.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "build"):
        command = commands.add_parser(name)
        command.add_argument("selector", help="owner:release-operation[@variant]")
        command.add_argument("--project-root", type=Path)
        if name == "build":
            command.add_argument("--run-id")
    audit = commands.add_parser("audit")
    audit.add_argument("manifest", type=Path)
    return parser


def main(argv: Sequence[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "audit":
            emit_json(ip_packaging.audit_ip_release_manifest(args.manifest))
            return 0
        project = open_cli_project(args.project_root)
        plan = project.plan(args.selector)
        if len(plan.steps) != 1 or plan.steps[0].uses != "sigilicon.ip-release":
            raise ValueError("release command requires one sigilicon.ip-release step")
        if args.command == "plan":
            emit_json(plan.record)
            return 0
        result = project.run(plan, run_id=args.run_id)
        stored = RunStore(project.artifact_root).read(
            owner=plan.owner,
            operation=plan.operation,
            variant=plan.variant,
            run_id=result.run_id,
        )
        emit_json(stored.record)
        return 0 if result.status == "succeeded" else 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
