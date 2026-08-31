"""Operator CLI for the current typed Flow core."""

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
    FlowContractError,
    ExecutionEnvironment,
    FlowExecutionError,
    ResolvedCapability,
    load_execution_environment,
)
from sigilicon.paths import discover_project_contract
from sigilicon.workflows.project import load_project
from sigilicon.workflows.project_runner import ProjectRunner, RunRequest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon flow",
        description="Plan and run current-schema typed design Flows.",
    )
    commands = parser.add_subparsers(dest="action", required=True)

    list_parser = commands.add_parser("list", help="list cataloged Flows")
    list_parser.add_argument("--project-root", type=Path)
    list_parser.add_argument("--owner", required=True)

    show = commands.add_parser("show", help="show one cataloged Flow selection")
    show.add_argument("--project-root", type=Path)
    show.add_argument("--owner", required=True)
    show.add_argument("--flow", required=True)
    show.add_argument("--profile")

    for name, help_text in (
        ("plan", "resolve and validate a source-only Flow plan"),
        ("graph", "render the resolved dependency graph as DOT"),
        ("preflight", "check adapters and an explicit current-site environment"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--project-root", type=Path)
        command.add_argument("--owner", required=True)
        command.add_argument("--flow", required=True)
        command.add_argument("--target", required=True)
        command.add_argument("--profile")
        if name == "preflight":
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

    run = commands.add_parser("run", help="execute a resolved Flow plan")
    run.add_argument("--project-root", type=Path)
    run.add_argument("--owner", required=True)
    run.add_argument("--flow", required=True)
    run.add_argument("--target", required=True)
    run.add_argument("--profile")
    run.add_argument("--environment", type=Path)
    run.add_argument(
        "--capability",
        action="append",
        default=[],
        metavar="NAME[=COMMAND]",
        help=(
            "attest one current-process capability; resolve COMMAND from PATH "
            "when supplied"
        ),
    )
    run.add_argument("--run-id")

    for name, help_text in (
        ("status", "read a persisted Flow Result"),
        ("clean", "remove exactly one manifest-owned Flow Run"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--project-root", type=Path)
        command.add_argument("owner")
        command.add_argument("flow")
        command.add_argument("target")
        command.add_argument("run_id")
    return parser


def _flow_status_exit(payload: dict[str, Any]) -> int:
    nodes = payload.get("nodes")
    if isinstance(nodes, dict) and any(
        isinstance(node, dict) and node.get("execution_status") == "cancelled"
        for node in nodes.values()
    ):
        return 130
    return 0 if payload.get("status") == "accepted" else 1


def _execution_environment(
    args: argparse.Namespace,
) -> ExecutionEnvironment:
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
    project_root = getattr(args, "project_root", None)
    if project_root is None:
        try:
            project_contract = discover_project_contract()
        except RuntimeError as exc:
            raise FlowContractError(str(exc)) from exc
    else:
        project_contract = project_root.resolve() / "sigilicon.toml"
    return load_project(project_contract)


def _project_runner(args: argparse.Namespace) -> ProjectRunner:
    return ProjectRunner(_project(args), args.owner)


def main(
    argv: Sequence[str] | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(arguments)
    try:
        if args.action == "list":
            project = _project_runner(args)
            catalog = project.catalog()
            emit_json(
                [
                    {
                        "flow": entry.flow_id,
                        "default_profile": entry.default_profile,
                        "profiles": sorted(entry.profiles),
                    }
                    for entry in catalog.entries
                ]
            )
            return 0
        if args.action == "show":
            project = _project_runner(args)
            emit_json(project.describe(flow=args.flow, profile=args.profile))
            return 0
        if args.action in {"plan", "graph", "preflight", "run"}:
            project = _project_runner(args)
            resolved = project.plan(
                RunRequest.flow(
                    args.flow,
                    args.target,
                    getattr(args, "profile", None),
                ),
            )
            if args.action == "plan":
                emit_json(resolved.record)
                return 0
            if args.action == "graph":
                print(f'digraph "{resolved.flow}:{resolved.target}" {{')
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
            environment = _execution_environment(
                args,
            )
            result = resolved.run(
                environment,
                run_id=args.run_id,
            )
            payload = resolved.read_result(result.run_id)
            emit_json(payload)
            return _flow_status_exit(payload)
        if args.action == "status":
            project = _project_runner(args)
            resolved = project.plan(
                RunRequest.flow(
                    args.flow,
                    args.target,
                    getattr(args, "profile", None),
                ),
            )
            payload = resolved.read_result(args.run_id)
            emit_json(payload)
            return _flow_status_exit(payload)
        if args.action == "clean":
            project = _project_runner(args)
            resolved = project.plan(
                RunRequest.flow(
                    args.flow,
                    args.target,
                    getattr(args, "profile", None),
                ),
            )
            resolved.clean(args.run_id)
            emit_json(
                {
                    "schema": 1,
                    "contract_kind": "flow-clean-result",
                    "status": "cleaned",
                    "owner": args.owner,
                    "flow": args.flow,
                    "target": args.target,
                    "run_id": args.run_id,
                }
            )
            return 0
        raise AssertionError(f"unhandled Flow command: {args.action}")
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
