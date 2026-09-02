"""Operator CLI for the Project execution seam."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import sys

from sigilicon.cli.common import emit_json
from sigilicon.execution import ContractError, ExecutionError, Resources, RunStoreError
from sigilicon.paths import discover_project_contract
from sigilicon.project import Project


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon flow",
        description="Plan, preflight, run, read, or clean one owner operation.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "preflight", "run"):
        command = commands.add_parser(name)
        command.add_argument("selector", help="owner:operation[@variant]")
        command.add_argument("--project-root", type=Path)
        if name in {"preflight", "run"}:
            command.add_argument("--capability", action="append", default=[])
        if name == "run":
            command.add_argument("--run-id")
    for name in ("status", "clean"):
        command = commands.add_parser(name)
        command.add_argument("selector", help="owner:operation[@variant]")
        command.add_argument("run_id")
        command.add_argument("--project-root", type=Path)
    return parser


def _project(args: argparse.Namespace) -> Project:
    root = args.project_root
    if root is None:
        return Project.open(discover_project_contract().parent)
    return Project.open(root)


def _resources(args: argparse.Namespace) -> Resources:
    capabilities = frozenset(args.capability)
    if len(capabilities) != len(args.capability) or any(not item for item in capabilities):
        raise ContractError("--capability values must be unique and non-empty")
    return Resources(capabilities, dict(os.environ))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        project = _project(args)
        if args.command == "status":
            from sigilicon.execution.operations import parse_selector

            owner, operation, variant = parse_selector(args.selector)
            stored = project.runs.read(
                owner=owner,
                operation=operation,
                variant=variant,
                run_id=args.run_id,
            )
            emit_json(stored.record)
            return 0 if stored.status == "succeeded" else 1
        if args.command == "clean":
            from sigilicon.execution.operations import parse_selector

            owner, operation, variant = parse_selector(args.selector)
            project.runs.clean(
                owner=owner,
                operation=operation,
                variant=variant,
                run_id=args.run_id,
            )
            emit_json(
                {
                    "schema": 1,
                    "contract_kind": "run-clean-result",
                    "status": "cleaned",
                    "owner": owner,
                    "operation": operation,
                    "variant": variant,
                    "run_id": args.run_id,
                }
            )
            return 0
        plan = project.plan(args.selector)
        if args.command == "plan":
            emit_json(plan.record)
            return 0
        resources = _resources(args)
        if args.command == "preflight":
            checked = project.preflight(plan, resources)
            emit_json(checked.record)
            return 0 if checked.ready else 2
        result = project.run(plan, resources, run_id=args.run_id)
        stored = project.runs.read(
            owner=result.owner,
            operation=result.operation,
            variant=result.variant,
            run_id=result.run_id,
        )
        emit_json(stored.record)
        return 0 if result.status == "succeeded" else 1
    except (ContractError, RunStoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ExecutionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Sigilicon defect: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
