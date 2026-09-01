"""Stable project workflow command line."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import os
from pathlib import Path
import sys
from typing import Any

from sigilicon.cli.common import add_json_arg, die, emit_json
from sigilicon.execution import Resources
from sigilicon.project import Project
from sigilicon.paths import discover_project_contract
from sigilicon.virtuoso.client import get_client
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
from sigilicon.workflows.oa_check import UnavailableBridge
from sigilicon.workflows.project_oa import ProjectOaWorkflow


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

    oa = domains.add_parser("oa", help="assemble the unique OA library from canonical sources")
    oa_commands = oa.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("plan", "validate and print the canonical source assembly plan"),
        ("check", "check current source/OA parity and live safety state"),
        ("rebuild", "rebuild source-declared OA objects from Git"),
        ("attest", "run one read-only Cadence setup check"),
        (
            "simulate",
            "run a typed Flow target for a declared OA operation",
        ),
    ):
        action_parser = oa_commands.add_parser(action, help=help_text)
        action_parser.add_argument(
            "--owner",
            required=True,
            help="cataloged project owner with one canonical OA assembly",
        )
        add_json_arg(action_parser)
        if action == "attest":
            action_parser.add_argument(
                "--testbench",
                required=True,
                help="declared OA testbench cell to run",
            )
        if action == "simulate":
            action_parser.add_argument(
                "--target",
                required=True,
                help="typed Flow target to execute",
            )
            action_parser.add_argument(
                "--operation",
                required=True,
                help="operation exposed by the typed Flow target",
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
            planned = workflow.project.plan(
                f"{workflow.owner_name}/{args.target}:{args.operation}"
            )
            result = workflow.project.run(
                planned,
                Resources(
                    frozenset({"tool.virtuoso-bridge", "license.cadence-oa"}),
                    dict(os.environ),
                ),
            )
            stored = workflow.project.runs.read(
                owner=result.owner,
                target=result.target,
                operation=result.operation,
                run_id=result.run_id,
            )
            payload = stored.record
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
                f"OA Maestro operation completed: {payload['target']}/"
                f"{payload['operation']} status={payload['status']}"
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
        return 0 if payload.get("status") == "succeeded" else 1
    return 0 if bool(payload.get("passed")) else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
) -> int:
    args = _parser().parse_args(argv)
    project_contract = discover_project_contract(__file__)
    project = Project.open(project_contract.parent)
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
    if args.domain == "ip":
        return _run_ip(args, project)
    raise AssertionError(f"unhandled flow domain: {args.domain}")


if __name__ == "__main__":
    raise SystemExit(main())
