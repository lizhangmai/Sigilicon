"""Stable project workflow command line."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
import sys
from typing import Any

from sigilicon.cli.common import add_json_arg, die, emit_json
from sigilicon.flow import (
    ExecutionEnvironment,
    ResolvedCapability,
    resolve_catalog_selection,
)
from sigilicon.paths import discover_project_contract
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.design_targets import (
    DesignTarget,
    execute_design_target,
    load_design_target_catalog,
)
from sigilicon.workflows.ip_integration import (
    check_ip_integration,
    ip_catalog_contract_path,
    plan_ip_integration,
)
from sigilicon.workflows.ip_packaging import (
    audit_ip_release,
    build_ip_release,
    plan_ip_release,
    publish_ip_release,
)
from sigilicon.workflows.layout_targets import (
    LayoutTarget,
    load_layout_target_catalog,
)
from sigilicon.workflows.layout_generation import (
    execute_layout_generation_spec,
    plan_layout_spec,
)
from sigilicon.workflows.layout_verification import execute_layout_verification_set
from sigilicon.workflows.oa_check import UnavailableBridge
from sigilicon.workflows.project import load_project
from sigilicon.workflows.project_flow import ProjectFlow
from sigilicon.workflows.project_oa import ProjectOaWorkflow


def _target_payload(target: LayoutTarget) -> dict[str, object]:
    return {
        "name": target.name,
        "owner": target.owner,
        "description": target.description,
        "spec": target.spec_relative.as_posix(),
        "actions": list(target.actions),
    }


def _design_target_payload(target: DesignTarget) -> dict[str, object]:
    return {
        "name": target.name,
        "owner": target.owner,
        "description": target.description,
        "kind": target.kind,
        "entrypoint": target.entrypoint,
        "spec_argument": target.spec_argument,
        "spec": (
            target.spec_relative.as_posix()
            if target.spec_relative is not None
            else None
        ),
        "modes": {mode.name: list(mode.default_args) for mode in target.modes},
    }


def _print_oa_check_summary(payload: dict[str, Any]) -> None:
    """Print a compact operator summary; ``--json`` remains the full report."""

    source = payload.get("source_contract")
    source = source if isinstance(source, dict) else {}
    parity = payload.get("parity")
    parity = parity if isinstance(parity, dict) else {}
    live = payload.get("live")
    live = live if isinstance(live, dict) else {}
    ownership = payload.get("ownership")
    ownership = ownership if isinstance(ownership, dict) else {}
    library_ownership = ownership.get("library")
    library_ownership = (
        library_ownership if isinstance(library_ownership, dict) else {}
    )
    locks = payload.get("locks")
    locks = locks if isinstance(locks, dict) else {}
    flow_lock = locks.get("flow_operation_lock")
    flow_lock = flow_lock if isinstance(flow_lock, dict) else {}
    process = live.get("process")
    process = process if isinstance(process, dict) else {}

    def _size(value: Any) -> int:
        return len(value) if isinstance(value, (list, dict, tuple, set)) else 0

    print(f"OA check: {payload.get('status', 'uncertain')}")
    print(
        "  contract: "
        f"{'pass' if source.get('passed') else 'fail'} "
        f"library={source.get('library', '-')} "
        f"cells={source.get('cell_count', '-')} "
        f"views={source.get('view_count', '-')} "
        f"testbenches={source.get('testbench_count', '-')}"
    )
    print(
        "  ownership: "
        f"{'pass' if library_ownership.get('passed') else 'fail'} "
        f"path={library_ownership.get('registered_path', '-')}"
    )
    print(
        "  OA parity: "
        f"{'pass' if parity.get('passed') else 'fail'} "
        f"missing={_size(parity.get('missing_cells')) + _size(parity.get('missing_views'))} "
        f"extra={_size(parity.get('extra_cells')) + _size(parity.get('extra_views'))} "
        f"modified={_size(parity.get('stale_or_modified_views'))}"
    )
    print(
        "  Bridge/process: "
        f"sessions={_size(live.get('active_maestro_sessions'))} "
        f"open_views={_size(live.get('open_cell_views'))} "
        f"process_alive={process.get('alive', False)}"
    )
    print(
        "  locks: "
        f"edit_locks={_size(locks.get('edit_locks'))} "
        f"flow_lock_held={flow_lock.get('held', False)}"
    )
    print("  native_attestation: explicit --testbench only")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sigilicon", description=__doc__)
    domains = parser.add_subparsers(dest="domain", required=True)

    layout = domains.add_parser("layout", help="run a cataloged layout workflow")
    commands = layout.add_subparsers(dest="action", required=True)

    list_parser = commands.add_parser("list", help="list cataloged layout targets")
    add_json_arg(list_parser)

    show_parser = commands.add_parser("show", help="show one layout target")
    show_parser.add_argument("target")
    add_json_arg(show_parser)

    check_parser = commands.add_parser(
        "check", help="validate a target and print its stable layout plan"
    )
    check_parser.add_argument("target")

    generate_parser = commands.add_parser(
        "generate", help="generate a cataloged target in OpenAccess"
    )
    generate_parser.add_argument("target")
    generate_parser.add_argument("--timeout", type=int, default=120)

    verify_parser = commands.add_parser(
        "verify", help="run audited XStream/Calibre physical verification"
    )
    verify_parser.add_argument("target")
    verify_parser.add_argument("--check", choices=("drc", "lvs", "all"), default="all")
    verify_parser.add_argument("--xstream-timeout", type=int, default=120)
    verify_parser.add_argument("--calibre-timeout", type=int, default=600)

    design = domains.add_parser("design", help="run a cataloged design workflow")
    design_commands = design.add_subparsers(dest="action", required=True)

    design_list = design_commands.add_parser(
        "list", help="list cataloged design targets"
    )
    add_json_arg(design_list)

    design_show = design_commands.add_parser("show", help="show one design target")
    design_show.add_argument("target")
    add_json_arg(design_show)

    design_run = design_commands.add_parser(
        "run", help="replace this process with the selected design runner"
    )
    design_run.add_argument("target")
    design_run.add_argument("mode")
    design_run.add_argument(
        "extra_args",
        nargs=argparse.REMAINDER,
        help="additional runner arguments, optionally following --",
    )

    oa = domains.add_parser("oa", help="assemble the unique OA library from canonical sources")
    oa_commands = oa.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("plan", "validate and print the canonical source assembly plan"),
        ("check", "check current source/OA parity and live safety state"),
        ("rebuild", "rebuild source-declared OA objects from Git"),
        ("attest", "run one read-only Cadence setup check"),
        (
            "simulate",
            "run the unique typed Flow target for a declared OA testbench",
        ),
    ):
        action_parser = oa_commands.add_parser(action, help=help_text)
        action_parser.add_argument(
            "--owner",
            required=True,
            help="cataloged project owner with one canonical OA assembly",
        )
        add_json_arg(action_parser)
        if action in {"attest", "simulate"}:
            action_parser.add_argument(
                "--testbench",
                required=True,
                help="declared OA testbench cell to run",
            )
        if action == "rebuild":
            target = action_parser.add_mutually_exclusive_group()
            target.add_argument(
                "--cell",
                help="rebuild exactly one non-testbench cell's generated views",
            )
            target.add_argument(
                "--testbench",
                help="rebuild exactly one testbench's complete generated view set",
            )
        if action not in {"plan", "simulate"}:
            action_parser.add_argument("--timeout", type=int, default=300)

    ip = domains.add_parser("ip", help="package and publish immutable custom IP")
    ip_commands = ip.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("plan", "validate the producer contract and show the release plan"),
        ("build", "build an immutable release package"),
        ("audit", "read-only audit of the release selected by its owner contract"),
        ("publish", "atomically select an audited release for consumers"),
    ):
        action_parser = ip_commands.add_parser(action, help=help_text)
        action_parser.add_argument("target")
        action_parser.add_argument(
            "--maturity",
            choices=("development", "implementation", "signoff"),
        )
        if action == "audit":
            action_parser.add_argument(
                "--artifact-root",
                type=Path,
                help="managed Flow/release root (defaults to project artifact_root)",
            )
        add_json_arg(action_parser)

    integration = ip_commands.add_parser(
        "integration", help="plan or check a composite IP"
    )
    integration_commands = integration.add_subparsers(
        dest="integration_action", required=True
    )
    integration_plan = integration_commands.add_parser(
        "plan", help="validate source-only composite-IP integration intent"
    )
    integration_plan.add_argument("target")
    add_json_arg(integration_plan)
    integration_check = integration_commands.add_parser(
        "check", help="resolve one IP variant through its exact dependency lock"
    )
    integration_check.add_argument("target")
    integration_check.add_argument("--variant", required=True)
    integration_check.add_argument(
        "--fileset", help="named variant fileset (defaults to the variant contract)"
    )
    add_json_arg(integration_check)
    return parser


def _run_ip(args: argparse.Namespace, project: Any) -> int:
    root = project.project_root
    if args.action == "integration":
        try:
            contract = ip_catalog_contract_path(
                project,
                args.target,
                section="components",
            )
            if args.integration_action == "plan":
                payload = plan_ip_integration(contract, project=project)
            else:
                payload = check_ip_integration(
                    contract,
                    project=project,
                    variant_name=args.variant,
                    fileset_name=args.fileset,
                )
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            die(f"ERROR: {exc}")
        if args.json:
            emit_json(payload)
        elif args.integration_action == "plan":
            variants = ", ".join(item["name"] for item in payload["variants"])
            print(f"IP integration plan passed: {payload['ip']} ({variants})")
        else:
            releases = ", ".join(
                item["release_id"] for item in payload["dependency_releases"]
            )
            print(
                f"IP integration check passed: {payload['ip']} "
                f"variant={payload['variant']} fileset={payload['fileset']} "
                f"dependencies={releases or 'source-only'}"
            )
        return 0
    try:
        contract = ip_catalog_contract_path(
            project,
            args.target,
        )
        operation = {
            "plan": plan_ip_release,
            "build": build_ip_release,
            "audit": audit_ip_release,
            "publish": publish_ip_release,
        }[args.action]
        keywords: dict[str, Any] = {"maturity": args.maturity}
        if args.action == "audit" and args.artifact_root is not None:
            keywords["artifact_root"] = (
                args.artifact_root
                if args.artifact_root.is_absolute()
                else (root / args.artifact_root)
            ).resolve()
        payload = operation(contract, project=project, **keywords)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        die(f"ERROR: {exc}")
    if args.json:
        emit_json(payload)
    elif args.action == "plan":
        print(
            f"IP release plan: {payload['ip_name']} {payload['release_id']} "
            f"maturity={payload['maturity_level']}"
        )
        if payload["missing_items"]:
            print(f"missing: {', '.join(payload['missing_items'])}")
    elif args.action == "publish":
        print(
            f"published IP release: {payload['ip_name']} {payload['release_id']}"
        )
    else:
        print(
            f"IP release {args.action} passed: {payload['ip_name']} "
            f"{payload['release_id']}"
        )
    return 0


def _run_layout(
    args: argparse.Namespace,
    project: Any,
    client_factory: Any,
) -> int:
    try:
        catalog = load_layout_target_catalog(project=project)
        if args.action == "list":
            payload = [_target_payload(target) for target in catalog.targets]
            if args.json:
                emit_json(payload)
            else:
                for target in catalog.targets:
                    print(
                        f"{target.name}\t{','.join(target.actions)}\t"
                        f"{target.spec_relative.as_posix()}"
                    )
            return 0
        target = catalog.get(args.target)
        if args.action == "show":
            payload = _target_payload(target)
            if args.json:
                emit_json(payload)
            else:
                print(f"target: {target.name}")
                print(f"description: {target.description}")
                print(f"spec: {target.spec_relative.as_posix()}")
                print(f"actions: {', '.join(target.actions)}")
            return 0
        target = catalog.get(args.target, action=args.action)
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")

    try:
        if args.action == "check":
            preview = plan_layout_spec(target.spec, project=project)
            print(preview.plan.canonical_json(), end="")
            return 0
        client = client_factory()
        if args.action == "generate":
            spec, result = execute_layout_generation_spec(
                target.spec,
                client,
                project=project,
                timeout=args.timeout,
            )
            print(
                f"[generated] {spec.library}/{spec.cell}/{spec.view} "
                f"instances={result.instance_count}"
            )
            print(f"[artifact] {result.manifest_path}")
            return 0
        checks = ("drc", "lvs") if args.check == "all" else (args.check,)
        results = execute_layout_verification_set(
            target.spec,
            client,
            project=project,
            checks=checks,
            xstream_timeout=args.xstream_timeout,
            calibre_timeout=args.calibre_timeout,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")
    for spec, result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{result.check}] {status} {spec.library}/{spec.cell}/{spec.view}")
        print(f"[details] {dict(result.details)}")
        print(f"[artifact] {result.manifest_path}")
    return 0 if all(result.passed for _spec, result in results) else 2


def _run_design(
    args: argparse.Namespace,
    project: Any,
    process_executor: Callable[[str, list[str]], Any] | None,
) -> int:
    try:
        catalog = load_design_target_catalog(project=project)
        if args.action == "list":
            payload = [_design_target_payload(target) for target in catalog.targets]
            if args.json:
                emit_json(payload)
            else:
                for target in catalog.targets:
                    print(
                        f"{target.name}\t"
                        f"{','.join(mode.name for mode in target.modes)}\t"
                        f"{target.spec_relative.as_posix() if target.spec_relative else '-'}"
                    )
            return 0
        target = catalog.get(args.target)
        if args.action == "show":
            payload = _design_target_payload(target)
            if args.json:
                emit_json(payload)
            else:
                print(f"target: {target.name}")
                print(f"description: {target.description}")
                print(f"entrypoint: {target.entrypoint}")
                spec = target.spec_relative.as_posix() if target.spec_relative else "-"
                print(f"spec: {spec}")
                print(f"modes: {', '.join(mode.name for mode in target.modes)}")
            return 0
        extra_args = tuple(args.extra_args)
        if extra_args[:1] == ("--",):
            extra_args = extra_args[1:]
        if process_executor is None:
            return execute_design_target(target, args.mode, extra_args)
        return execute_design_target(
            target,
            args.mode,
            extra_args,
            process_executor=process_executor,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")


def _run_oa(
    args: argparse.Namespace,
    workflow: ProjectOaWorkflow,
    client_factory: Any,
) -> int:
    if args.action == "check":
        try:
            try:
                client = client_factory()
            except (OSError, RuntimeError, ValueError) as exc:
                # Check remains useful when Bridge is down: the source plan and
                # lock inspection stay read-only while live evidence is marked
                # unavailable.
                client = UnavailableBridge(exc)
            payload = workflow.check(
                client=client,
                timeout=args.timeout,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            die(f"ERROR: {exc}")
        if args.json:
            emit_json(payload)
        else:
            _print_oa_check_summary(payload)
        return 0 if bool(payload.get("passed")) else 1
    try:
        if args.action == "plan":
            plan = workflow.plan()
            payload = plan.as_dict()
        elif args.action == "simulate":
            project_flow = ProjectFlow(workflow.project, workflow.owner_name)
            matches: list[tuple[str, str]] = []
            catalog = project_flow.catalog()
            for entry in catalog.entries:
                selection = resolve_catalog_selection(
                    catalog,
                    flow_id=entry.flow_id,
                )
                node_ids = {
                    node.node_id
                    for node in selection.spec.nodes
                    if node.action_kind == "native-oa.simulate"
                    and node.config.get("testbench") == args.testbench
                }
                matches.extend(
                    (entry.flow_id, target.target_id)
                    for target in selection.spec.targets
                    if len(target.goals) == 1 and target.goals[0] in node_ids
                )
            if len(matches) != 1:
                raise ValueError(
                    "OA testbench must resolve to exactly one cataloged typed "
                    f"Flow target: {args.testbench!r} resolved {matches!r}"
                )
            flow_id, target_id = matches[0]
            planned = project_flow.plan(flow=flow_id, target=target_id)
            result = project_flow.run(
                planned,
                ExecutionEnvironment(
                    capabilities={
                        "tool.virtuoso-bridge": ResolvedCapability(
                            "current-process:tool.virtuoso-bridge"
                        ),
                        "license.cadence-oa": ResolvedCapability(
                            "current-process:license.cadence-oa"
                        ),
                    }
                ),
            )
            payload = project_flow.read_result(
                flow=result.flow_id,
                target=result.target,
                run_id=result.run_id,
            )
        else:
            client = client_factory()
            if args.action == "attest":
                payload = workflow.attest(
                    testbench=args.testbench,
                    client=client,
                    timeout=args.timeout,
                )
            else:
                payload = workflow.rebuild(
                    client=client,
                    cell=args.cell,
                    testbench=args.testbench,
                    timeout=args.timeout,
                    report=(
                        None
                        if args.json
                        else lambda message: print(message, file=sys.stderr)
                    ),
                )
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")
    if args.json:
        emit_json(payload)
    else:
        if args.action == "plan":
            print(
                f"OA assembly plan passed: {plan.library} "
                f"({len(plan.cells)} cells, {len(plan.views)} views, "
                f"{len(plan.layouts)} layouts, {len(plan.testbenches)} testbenches)"
            )
            for step in plan.designs:
                print(
                    f"schematic\t{step.inspection.spec.cell}\t"
                    f"{','.join(step.imported_cells)}"
                )
            for step in plan.layouts:
                print(f"layout\t{step.spec.cell}\t{step.spec.view}")
            for step in plan.testbenches:
                print(f"testbench\t{step.cell}\tmaestro")
        elif args.action == "attest":
            print(
                f"OA setup check passed: {payload['library']}/"
                f"{payload['testbench']}"
            )
        elif args.action == "simulate":
            print(
                f"OA Maestro Flow completed: {payload['flow']}/"
                f"{payload['target']} status={payload['status']}"
            )
            print(f"managed run: {payload['run_id']}")
        else:
            if payload["passed"]:
                print(
                    f"OA library {payload['library']} passed: "
                    f"{payload['cell_count']} cells, "
                    f"{payload['layout_count']} layouts"
                )
            else:
                print(f"OA library {payload['library']} differs from canonical source")
    if args.action == "simulate":
        return 0 if payload.get("status") == "accepted" else 1
    return 0 if bool(payload.get("passed")) else 1

def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
    process_executor: Callable[[str, list[str]], Any] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    project_contract = discover_project_contract(__file__)
    project = load_project(project_contract)
    if args.domain == "oa":
        try:
            workflow = ProjectOaWorkflow(project, args.owner)
        except ValueError as exc:
            die(f"ERROR: {exc}")
        return _run_oa(
            args,
            workflow,
            client_factory,
        )
    if args.domain == "layout":
        return _run_layout(
            args,
            project,
            client_factory,
        )
    if args.domain == "design":
        return _run_design(
            args,
            project,
            process_executor,
        )
    if args.domain == "ip":
        return _run_ip(args, project)
    raise AssertionError(f"unhandled flow domain: {args.domain}")


if __name__ == "__main__":
    raise SystemExit(main())
