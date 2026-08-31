"""Operator CLI for the single owner target interface."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from sigilicon.cli.common import emit_json
from sigilicon.flow import (
    ExecutionEnvironment,
    FlowContractError,
    FlowExecutionError,
    ResolvedCapability,
    load_execution_environment,
)
from sigilicon.paths import discover_project_contract
from sigilicon.workflows.project import bind_run_store, load_project
from sigilicon.workflows.project_runner import ProjectRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon flow",
        description="Plan and run owner target operations.",
    )
    commands = parser.add_subparsers(dest="action", required=True)

    listing = commands.add_parser("list", help="list owner targets")
    listing.add_argument("--project-root", type=Path)
    listing.add_argument("--owner", required=True)

    show = commands.add_parser("show", help="show a target or target operation")
    show.add_argument("--project-root", type=Path)
    show.add_argument("--owner", required=True)
    show.add_argument("--target", required=True)
    show.add_argument("--operation")

    for name, help_text in (
        ("plan", "resolve and validate a source-only target plan"),
        ("graph", "render the target operation dependency graph as DOT"),
        ("preflight", "check a target operation against the site environment"),
        ("run", "execute a target operation"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--project-root", type=Path)
        command.add_argument("--owner", required=True)
        command.add_argument("--target", required=True)
        command.add_argument("--operation", required=True)
        if name in {"preflight", "run"}:
            command.add_argument("--environment", type=Path)
            command.add_argument(
                "--capability",
                action="append",
                default=[],
                metavar="NAME[=COMMAND]",
                help=(
                    "attest one current-process capability; resolve COMMAND "
                    "from PATH when supplied"
                ),
            )
        if name == "run":
            command.add_argument("--run-id")

    for name, help_text in (
        ("status", "read a persisted target operation result"),
        ("clean", "remove exactly one manifest-owned run"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--project-root", type=Path)
        command.add_argument("owner")
        command.add_argument("target")
        command.add_argument("operation")
        command.add_argument("run_id")
    return parser


def _result_exit(payload: dict[str, Any]) -> int:
    nodes = payload.get("nodes")
    if isinstance(nodes, dict) and any(
        isinstance(node, dict) and node.get("execution_status") == "cancelled"
        for node in nodes.values()
    ):
        return 130
    return 0 if payload.get("status") == "accepted" else 1


def _execution_environment(args: argparse.Namespace) -> ExecutionEnvironment:
    contract = getattr(args, "environment", None)
    base = (
        ExecutionEnvironment()
        if contract is None
        else load_execution_environment(contract)
    )
    capabilities = dict(base.capabilities)
    for declaration in getattr(args, "capability", ()):
        name, separator, command = declaration.partition("=")
        if not name or (separator and not command):
            raise FlowContractError(
                "--capability must use NAME or NAME=COMMAND syntax"
            )
        if name in capabilities:
            raise FlowContractError(f"duplicate current-site capability: {name!r}")
        executable = None
        if separator:
            resolved = shutil.which(command)
            if resolved is None:
                raise FlowContractError(
                    f"capability command is unavailable: {command!r}"
                )
            executable = Path(os.path.abspath(resolved))
        capabilities[name] = ResolvedCapability(
            identity=f"current-process:{name}",
            executable=executable,
        )
    return ExecutionEnvironment(
        capabilities=capabilities,
        platform_assets=base.platform_assets,
    )


def _project(args: argparse.Namespace) -> Any:
    return load_project(_project_contract(args))


def _project_contract(args: argparse.Namespace) -> Path:
    project_root = getattr(args, "project_root", None)
    if project_root is None:
        try:
            return discover_project_contract()
        except RuntimeError as exc:
            raise FlowContractError(str(exc)) from exc
    return project_root.resolve() / "sigilicon.toml"


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(arguments)
    try:
        if args.action in {"status", "clean"}:
            runs = bind_run_store(_project_contract(args).parent)
        if args.action == "status":
            payload = runs.read(
                owner=args.owner,
                target=args.target,
                operation=args.operation,
                run_id=args.run_id,
            )
            emit_json(payload)
            return _result_exit(payload)
        if args.action == "clean":
            runs.clean(
                owner=args.owner,
                target=args.target,
                operation=args.operation,
                run_id=args.run_id,
            )
            emit_json(
                {
                    "schema": 1,
                    "contract_kind": "project-run-clean-result",
                    "status": "cleaned",
                    "owner": args.owner,
                    "target": args.target,
                    "operation": args.operation,
                    "run_id": args.run_id,
                }
            )
            return 0
        project = _project(args)
        runs = project.runs
        runner = ProjectRunner(project, args.owner)
        if args.action == "list":
            emit_json(runner.targets())
            return 0
        if args.action == "show":
            emit_json(runner.describe(args.target, args.operation))
            return 0
        if args.action in {"plan", "graph", "preflight", "run"}:
            resolved = runner.plan(args.target, args.operation)
            if args.action == "plan":
                emit_json(resolved.record)
                return 0
            if args.action == "graph":
                print(f'digraph "{resolved.target}:{resolved.operation}" {{')
                for node, dependencies in resolved.graph:
                    print(f'  "{node}";')
                    for dependency in dependencies:
                        print(f'  "{dependency}" -> "{node}";')
                print("}")
                return 0
            if args.action == "preflight":
                preflight = resolved.preflight(_execution_environment(args))
                emit_json(resolved.preflight_record(preflight))
                return 0 if preflight.status == "ready" else 2
            result = resolved.run(
                _execution_environment(args),
                run_id=args.run_id,
            )
            payload = runs.read(
                owner=resolved.owner,
                target=resolved.target,
                operation=resolved.operation,
                run_id=result.run_id,
            )
            emit_json(payload)
            return _result_exit(payload)
        raise AssertionError(f"unhandled target command: {args.action}")
    except (FlowContractError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except FlowExecutionError as exc:
        print(str(exc), file=sys.stderr)
        return 2 if args.action in {"status", "clean"} else 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Sigilicon defect: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
