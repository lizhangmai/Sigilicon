"""Operator CLI for the Project execution seam."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sys

from sigilicon.artifacts import read_nofollow_text
from sigilicon.cli.common import emit_json, open_cli_project
from sigilicon.execution import (
    ContractError,
    ExecutionError,
    RunStore,
    RunStoreError,
)
from sigilicon.execution.operations import parse_selector
from sigilicon.project import Project


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon flow",
        description="Plan, preflight, run, read, audit, or clean one owner operation.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "preflight", "run"):
        command = commands.add_parser(name)
        command.add_argument("selector", nargs="?", help="owner:operation[@variant]")
        command.add_argument("--project-root", type=Path)
        if name in {"preflight", "run"}:
            command.add_argument("--plan-file", type=Path)
        if name == "run":
            command.add_argument("--run-id")
    for name in ("status", "audit", "clean"):
        command = commands.add_parser(name)
        command.add_argument("selector", help="owner:operation[@variant]")
        command.add_argument("run_id")
        command.add_argument("--project-root", type=Path)
    return parser


def _project(args: argparse.Namespace) -> Project:
    return open_cli_project(args.project_root)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        project = _project(args)
        runs = RunStore(project.artifact_root)
        if args.command == "status":
            owner, operation, variant = parse_selector(args.selector)
            stored = runs.read(
                owner=owner,
                operation=operation,
                variant=variant,
                run_id=args.run_id,
            )
            emit_json(stored.record)
            return 0 if stored.status == "succeeded" else 1
        if args.command == "audit":
            owner, operation, variant = parse_selector(args.selector)
            runs.audit(
                owner=owner,
                operation=operation,
                variant=variant,
                run_id=args.run_id,
            )
            emit_json(
                {
                    "schema": 1,
                    "contract_kind": "run-audit-result",
                    "status": "verified",
                    "owner": owner,
                    "operation": operation,
                    "variant": variant,
                    "run_id": args.run_id,
                }
            )
            return 0
        if args.command == "clean":
            owner, operation, variant = parse_selector(args.selector)
            runs.clean(
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
        plan_file = getattr(args, "plan_file", None)
        if (args.selector is None) == (plan_file is None):
            raise ContractError(
                "plan/preflight/run requires exactly one selector or --plan-file"
            )
        if plan_file is None:
            plan = project.plan(args.selector)
        else:
            try:
                record = json.loads(read_nofollow_text(plan_file))
            except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError) as exc:
                raise ContractError(f"cannot read execution plan: {plan_file}") from exc
            if not isinstance(record, dict):
                raise ContractError("execution plan file must contain a JSON object")
            plan = project.plan(record)
        if args.command == "plan":
            emit_json(plan.record)
            return 0
        if args.command == "preflight":
            checked = project.preflight(plan)
            emit_json(checked.record)
            return 0 if checked.ready else 2
        result = project.run(plan, run_id=args.run_id)
        stored = runs.read(
            owner=plan.owner,
            operation=plan.operation,
            variant=plan.variant,
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
