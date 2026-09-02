"""Implementation of the public immutable release commands."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from sigilicon.cli.common import emit_json
from sigilicon.paths import discover_project_contract
from sigilicon.project import Project
from sigilicon.workflows import ip_packaging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon release",
        description="Plan, build, or audit one immutable IP release.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "build"):
        command = commands.add_parser(name)
        command.add_argument("contract", type=Path)
        command.add_argument("--maturity")
        command.add_argument("--project-root", type=Path)
    audit = commands.add_parser("audit")
    audit.add_argument("manifest", type=Path)
    return parser


def main(argv: Sequence[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "audit":
            emit_json(ip_packaging.audit_ip_release_manifest(args.manifest))
            return 0
        root = (
            discover_project_contract().parent
            if args.project_root is None
            else args.project_root
        )
        project = Project.open(root)
        contract = args.contract
        if not contract.is_absolute():
            contract = project.project_root / contract
        action = (
            ip_packaging.plan_ip_release
            if args.command == "plan"
            else ip_packaging.build_ip_release
        )
        emit_json(action(contract, project=project, maturity=args.maturity))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
