"""Operator CLI for the shared authorized agentic execution Interface."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from sigilicon.cli.common import emit_json
from sigilicon.workflows.agentic_execution import AgenticExecutionBudget
from sigilicon.workflows.project import bind_agentic_execution


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon execute",
        description="Execute only launcher-approved target-operation plans.",
    )
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--grant", type=Path, required=True)
    parser.add_argument("--environment", type=Path)
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("target-run", help="execute and wait for one approved plan")
    run.add_argument("plan_identity")
    run.add_argument("--maximum-seconds", type=int, required=True)
    run.add_argument("--maximum-nodes", type=int, required=True)
    cancel = commands.add_parser("run-cancel", help="cancel an exactly owned live run")
    cancel.add_argument("run_id")
    inspect = commands.add_parser("run-inspect", help="inspect one authorized managed run")
    inspect.add_argument("run_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        interface = bind_agentic_execution(
            args.project_root,
            grant_contract=args.grant,
            environment_contract=args.environment,
        )
        if args.action == "target-run":
            budget = AgenticExecutionBudget(
                maximum_seconds=args.maximum_seconds,
                maximum_nodes=args.maximum_nodes,
            )
            submitted = interface.run_target(
                plan_identity=args.plan_identity,
                budget=budget,
                wait=False,
            )
            run_id = submitted["data"]["management"]["run_id"]
            try:
                payload = interface.wait_run(run_id)
            except KeyboardInterrupt:
                payload = interface.cancel_run(run_id=run_id)
            payload = dict(payload)
            payload["operation"] = "target.run"
        elif args.action == "run-cancel":
            payload = interface.cancel_run(run_id=args.run_id)
        else:
            payload = interface.inspect_run(run_id=args.run_id)
        emit_json(payload)
        status = payload["data"]["management"]["status"]
        if status == "accepted":
            return 0
        if status in {"cancelled", "budget-exhausted"}:
            return 130
        return 1 if status in {"failed", "uncertain"} else 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
