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
    load_catalog_selection,
    load_execution_environment,
    load_flow_catalog,
)
from sigilicon.paths import discover_project_context
from sigilicon.workflows.builtin import builtin_workflow_registry
from sigilicon.workflows.project_flow import (
    ProjectFlow,
    ProjectFlowPlan,
    project_owner_binding_for_root,
    project_workflow_registry,
    project_workflow_registry_for_owner_root,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon flow",
        description="Plan and run current-schema typed design Flows.",
    )
    commands = parser.add_subparsers(dest="action", required=True)

    list_parser = commands.add_parser("list", help="list cataloged Flows")
    list_parser.add_argument("catalog", type=Path, nargs="?")
    list_parser.add_argument("--owner-root", type=Path)
    list_parser.add_argument("--project-root", type=Path)
    list_parser.add_argument("--owner")

    show = commands.add_parser("show", help="show one cataloged Flow selection")
    show.add_argument("catalog", type=Path, nargs="?")
    show.add_argument("flow_positional", metavar="flow", nargs="?")
    show.add_argument("--owner-root", type=Path)
    show.add_argument("--project-root", type=Path)
    show.add_argument("--owner")
    show.add_argument("--flow", dest="flow_option")
    show.add_argument("--profile")

    for name, help_text in (
        ("plan", "resolve and validate a source-only Flow plan"),
        ("graph", "render the resolved dependency graph as DOT"),
        ("preflight", "check adapters and an explicit current-site environment"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("catalog", type=Path, nargs="?")
        command.add_argument("flow_positional", metavar="flow", nargs="?")
        command.add_argument("target_positional", metavar="target", nargs="?")
        command.add_argument("--owner-root", type=Path)
        command.add_argument("--project-root", type=Path)
        command.add_argument("--owner")
        command.add_argument("--flow", dest="flow_option")
        command.add_argument("--target", dest="target_option")
        command.add_argument("--profile")
        if name == "preflight":
            command.add_argument("--environment", type=Path)

    run = commands.add_parser("run", help="execute a resolved Flow plan")
    run.add_argument("catalog", type=Path, nargs="?")
    run.add_argument("flow_positional", metavar="flow", nargs="?")
    run.add_argument("target_positional", metavar="target", nargs="?")
    run.add_argument("--owner-root", type=Path)
    run.add_argument("--project-root", type=Path)
    run.add_argument("--owner")
    run.add_argument("--flow", dest="flow_option")
    run.add_argument("--target", dest="target_option")
    run.add_argument("--profile")
    run.add_argument("--environment", type=Path)
    run.add_argument("--artifact-root", type=Path)
    run.add_argument("--run-id")

    for name, help_text in (
        ("status", "read a persisted Flow Result"),
        ("clean", "remove exactly one manifest-owned Flow Run"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--artifact-root", type=Path, required=True)
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


def _flow_registry(
    args: argparse.Namespace,
    registry_factory: Callable[[Path | None], FlowRegistry] | None,
) -> FlowRegistry:
    try:
        owner_root = getattr(args, "owner_root", None)
        if registry_factory is not None:
            return registry_factory(owner_root)
        project_root = getattr(args, "project_root", None)
        if project_root is not None:
            return project_workflow_registry(
                project_root,
                owner_root,
            )
        if owner_root is not None:
            return project_workflow_registry_for_owner_root(owner_root)
        try:
            project = discover_project_context()
        except RuntimeError:
            return builtin_workflow_registry()
        return project_workflow_registry(project.project_root, None)
    except ValueError as exc:
        raise FlowContractError(str(exc)) from exc


def _project_flow(args: argparse.Namespace) -> ProjectFlow | None:
    owner = getattr(args, "owner", None)
    if owner is None:
        return None
    if getattr(args, "catalog", None) is not None or getattr(
        args, "owner_root", None
    ) is not None:
        raise FlowContractError(
            "semantic --owner selection cannot be mixed with catalog or --owner-root"
        )
    project_root = getattr(args, "project_root", None)
    if project_root is None:
        try:
            project_root = discover_project_context().project_root
        except RuntimeError as exc:
            raise FlowContractError(str(exc)) from exc
    try:
        return ProjectFlow.from_project_root(project_root, owner=owner)
    except ValueError as exc:
        raise FlowContractError(str(exc)) from exc


def _selection_value(
    args: argparse.Namespace,
    name: str,
    *,
    project: bool,
) -> str:
    option = getattr(args, f"{name}_option", None)
    positional = getattr(args, f"{name}_positional", None)
    if project and positional is not None:
        raise FlowContractError(
            f"semantic --owner selection requires --{name}, not positional {name}"
        )
    if not project and option is not None:
        raise FlowContractError(
            f"path-based Flow selection requires positional {name}, not --{name}"
        )
    value = option if project else positional
    if not isinstance(value, str) or not value:
        form = f"--{name}" if project else name
        raise FlowContractError(f"Flow {form} selection is required")
    return value


def _resolved_plan(
    args: argparse.Namespace,
    registry_factory: Callable[[Path | None], FlowRegistry] | None,
) -> tuple[ProjectFlowPlan, ProjectFlow | None]:
    project = _project_flow(args)
    if project is not None:
        try:
            planned = project.plan(
                flow=_selection_value(args, "flow", project=True),
                target=_selection_value(args, "target", project=True),
                profile=args.profile,
            )
        except ValueError as exc:
            raise FlowContractError(str(exc)) from exc
        return planned, project
    if args.catalog is None or args.owner_root is None:
        raise FlowContractError(
            "path-based Flow selection requires catalog and --owner-root"
        )
    try:
        binding = project_owner_binding_for_root(args.owner_root)
    except ValueError as exc:
        raise FlowContractError(str(exc)) from exc
    if binding is not None:
        repository, owner = binding
        canonical_catalog = repository.owner_flow_catalog(owner)
        if args.catalog.resolve() != canonical_catalog:
            raise FlowContractError(
                "project Flow path selection must use the selected owner's "
                f"canonical catalog: {canonical_catalog}"
            )
    flow = _selection_value(args, "flow", project=False)
    target = _selection_value(args, "target", project=False)
    engine = FlowEngine(_flow_registry(args, registry_factory))
    selection = load_catalog_selection(
        args.catalog,
        owner_root=args.owner_root,
        flow_id=flow,
        profile_id=args.profile,
    )
    return ProjectFlowPlan(
        engine,
        engine.plan(selection.spec, target, selection.profile),
    ), None


def main(
    argv: Sequence[str] | None = None,
    *,
    registry_factory: Callable[[Path | None], FlowRegistry] | None = None,
    environment_factory: Callable[[ExecutionProfile], ExecutionEnvironment] = (
        lambda _profile: ExecutionEnvironment()
    ),
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] and arguments[0] in {"layout", "design", "oa", "ip"}:
        from sigilicon.cli.flow import main as legacy_flow_main

        return legacy_flow_main(arguments)
    args = _parser().parse_args(arguments)
    try:
        if args.action == "list":
            project = _project_flow(args)
            if project is not None:
                catalog = project.catalog()
            else:
                if args.catalog is None or args.owner_root is None:
                    raise FlowContractError(
                        "path-based Flow selection requires catalog and --owner-root"
                    )
                catalog = load_flow_catalog(args.catalog, owner_root=args.owner_root)
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
            project = _project_flow(args)
            flow = _selection_value(
                args,
                "flow",
                project=project is not None,
            )
            catalog = args.catalog
            owner_root = args.owner_root
            if project is not None:
                catalog = project.catalog_path
                owner_root = project.owner.root
            if catalog is None or owner_root is None:
                raise FlowContractError(
                    "path-based Flow selection requires catalog and --owner-root"
                )
            selection = load_catalog_selection(
                catalog,
                owner_root=owner_root,
                flow_id=flow,
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
            if project is not None and args.artifact_root is None:
                result = project.run(
                    resolved,
                    environment,
                    run_id=args.run_id,
                )
            else:
                if args.artifact_root is None:
                    raise FlowContractError(
                        "path-based Flow run requires --artifact-root"
                    )
                result = engine.run(
                    plan,
                    artifact_root=args.artifact_root,
                    environment=environment,
                    run_id=args.run_id,
                )
            payload = engine.read_run_result(
                artifact_root=(
                    args.artifact_root
                    if args.artifact_root is not None
                    else project.repository.project.artifact_root
                ),
                owner=result.owner,
                flow_id=result.flow_id,
                target=result.target,
                run_id=result.run_id,
            )
            emit_json(payload)
            return _flow_status_exit(payload)
        if args.action == "status":
            engine = FlowEngine(builtin_workflow_registry())
            payload = engine.read_run_result(
                artifact_root=args.artifact_root,
                owner=args.owner,
                flow_id=args.flow,
                target=args.target,
                run_id=args.run_id,
            )
            emit_json(payload)
            return _flow_status_exit(payload)
        if args.action == "clean":
            engine = FlowEngine(builtin_workflow_registry())
            engine.clean_run(
                artifact_root=args.artifact_root,
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
