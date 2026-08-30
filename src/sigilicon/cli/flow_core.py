"""Operator CLI for the current typed Flow core."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
import sys
from typing import Any

from sigilicon.cli.common import emit_json
from sigilicon.flow import (
    FlowContractError,
    FlowEngine,
    ExecutionEnvironment,
    ExecutionProfile,
    FlowExecutionError,
    FlowRegistry,
    load_execution_environment,
    resolve_catalog_selection,
)
from sigilicon.paths import discover_project_contract
from sigilicon.workflows.builtin import build_flow_registry
from sigilicon.workflows.project import load_project
from sigilicon.workflows.project_flow import (
    ProjectFlow,
    ProjectFlowPlan,
    project_workflow_registry,
)


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

    run = commands.add_parser("run", help="execute a resolved Flow plan")
    run.add_argument("--project-root", type=Path)
    run.add_argument("--owner", required=True)
    run.add_argument("--flow", required=True)
    run.add_argument("--target", required=True)
    run.add_argument("--profile")
    run.add_argument("--environment", type=Path)
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


def _spec_payload(spec: Any, profile: ExecutionProfile) -> dict[str, Any]:
    return {
        "schema": 1,
        "contract_kind": "flow-summary",
        "owner": spec.owner,
        "flow": spec.flow_id,
        "nodes": [node.node_id for node in spec.nodes],
        "targets": [target.target_id for target in spec.targets],
        "policies": [policy.policy_id for policy in spec.policies],
        "execution_profile": profile.profile_id,
    }


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
    profile: ExecutionProfile,
    factory: Callable[[ExecutionProfile], ExecutionEnvironment],
) -> ExecutionEnvironment:
    contract = getattr(args, "environment", None)
    return (
        factory(profile)
        if contract is None
        else load_execution_environment(contract)
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
    try:
        return load_project(project_contract)
    except ValueError as exc:
        raise FlowContractError(str(exc)) from exc


def _project_flow(
    args: argparse.Namespace,
    *,
    registry_factory: Callable[[Any, Path], FlowRegistry] | None,
) -> ProjectFlow:
    try:
        return ProjectFlow(
            _project(args),
            args.owner,
            project_workflow_registry if registry_factory is None else registry_factory,
        )
    except ValueError as exc:
        raise FlowContractError(str(exc)) from exc


def _catalog(project: ProjectFlow) -> Any:
    try:
        return project.catalog()
    except ValueError as exc:
        raise FlowContractError(str(exc)) from exc


def _resolved_plan(
    args: argparse.Namespace,
    registry_factory: Callable[[Any, Path], FlowRegistry] | None,
) -> tuple[ProjectFlowPlan, ProjectFlow]:
    project = _project_flow(args, registry_factory=registry_factory)
    try:
        planned = project.plan(
            flow=args.flow,
            target=args.target,
            profile=args.profile,
        )
    except ValueError as exc:
        raise FlowContractError(str(exc)) from exc
    return planned, project


def main(
    argv: Sequence[str] | None = None,
    *,
    registry_factory: Callable[[Any, Path], FlowRegistry] | None = None,
    environment_factory: Callable[[ExecutionProfile], ExecutionEnvironment] = (
        lambda _profile: ExecutionEnvironment()
    ),
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(arguments)
    try:
        if args.action == "list":
            project = _project_flow(args, registry_factory=registry_factory)
            catalog = _catalog(project)
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
            project = _project_flow(args, registry_factory=registry_factory)
            selection = resolve_catalog_selection(
                _catalog(project),
                flow_id=args.flow,
                profile_id=args.profile,
            )
            emit_json(_spec_payload(selection.spec, selection.profile))
            return 0
        if args.action in {"plan", "graph", "preflight", "run"}:
            resolved, project = _resolved_plan(args, registry_factory)
            engine = resolved.engine
            plan = resolved.plan
            if args.action == "plan":
                emit_json(resolved.record)
                return 0
            if args.action == "graph":
                print(f'digraph "{plan.spec.flow_id}:{plan.target.target_id}" {{')
                for planned in plan.nodes:
                    print(f'  "{planned.node.node_id}";')
                    for dependency in planned.dependencies:
                        print(f'  "{dependency}" -> "{planned.node.node_id}";')
                print("}")
                return 0
            if args.action == "preflight":
                preflight = engine.preflight(
                    plan,
                    _execution_environment(
                        args,
                        plan.profile,
                        environment_factory,
                    ),
                )
                emit_json(engine.preflight_record(plan, preflight))
                return 0 if preflight.status == "ready" else 2
            environment = _execution_environment(
                args,
                plan.profile,
                environment_factory,
            )
            result = project.run(
                resolved,
                environment,
                run_id=args.run_id,
            )
            payload = engine.read_run_result(
                artifact_root=project.project.artifact_root,
                owner=result.owner,
                flow_id=result.flow_id,
                target=result.target,
                run_id=result.run_id,
            )
            emit_json(payload)
            return _flow_status_exit(payload)
        if args.action == "status":
            project = _project(args)
            engine = FlowEngine(build_flow_registry())
            payload = engine.read_run_result(
                artifact_root=project.artifact_root,
                owner=args.owner,
                flow_id=args.flow,
                target=args.target,
                run_id=args.run_id,
            )
            emit_json(payload)
            return _flow_status_exit(payload)
        if args.action == "clean":
            project = _project(args)
            engine = FlowEngine(build_flow_registry())
            engine.clean_run(
                artifact_root=project.artifact_root,
                owner=args.owner,
                flow_id=args.flow,
                target=args.target,
                run_id=args.run_id,
            )
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
    except FlowContractError as exc:
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
