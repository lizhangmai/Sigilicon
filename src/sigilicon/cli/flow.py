"""Stable project workflow command line."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
import sys
from typing import Any

from sigilicon.cli.common import add_json_arg, die, emit_json
from sigilicon.cli.generate_layout import main as generate_layout_main
from sigilicon.cli.verify_layout import main as verify_layout_main
from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.soc import catalog_contract_path, check_soc, plan_soc
from sigilicon.workflows.design_targets import (
    DesignTarget,
    execute_design_target,
    load_design_target_catalog,
)
from sigilicon.workflows.layout_targets import LayoutTarget, load_layout_target_catalog
from sigilicon.workflows.ip_packaging import (
    audit_ip_release,
    build_ip_release,
    plan_ip_release,
    publish_ip_release,
)
from sigilicon.workflows.oa_library import (
    attest_oa_testbench,
    plan_oa_library_rebuild,
    rebuild_oa_library,
)
from sigilicon.workflows.oa_check import UnavailableBridge, check_oa_library
from sigilicon.workflows.oa_simulation import run_oa_maestro_testbench


def _target_payload(target: LayoutTarget) -> dict[str, object]:
    return {
        "name": target.name,
        "description": target.description,
        "spec": target.spec_relative.as_posix(),
        "actions": list(target.actions),
    }


def _design_target_payload(target: DesignTarget) -> dict[str, object]:
    return {
        "name": target.name,
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
        ("simulate", "run a canonical OA testbench through its Maestro view"),
    ):
        action_parser = oa_commands.add_parser(action, help=help_text)
        action_parser.add_argument(
            "--manifest",
            required=True,
            help="project-relative OA library contract",
        )
        action_parser.add_argument(
            "--library",
            help="assert the unique target library identity",
        )
        add_json_arg(action_parser)
        if action in {"attest", "simulate"}:
            action_parser.add_argument(
                "--testbench",
                required=True,
                help="declared OA testbench cell to run",
            )
        if action == "simulate":
            action_parser.add_argument(
                "--keep-work",
                action="store_true",
                help="retain this run's temporary outputs for manual inspection",
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
        if action != "plan":
            action_parser.add_argument("--timeout", type=int, default=300)

    ip = domains.add_parser("ip", help="package and publish immutable custom IP")
    ip_commands = ip.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("plan", "validate the producer contract and show the release plan"),
        ("build", "build an immutable qualified release package"),
        ("audit", "audit a built release against its source contract"),
        ("publish", "atomically select an audited release for consumers"),
    ):
        action_parser = ip_commands.add_parser(action, help=help_text)
        action_parser.add_argument("target")
        action_parser.add_argument(
            "--qualification",
            choices=("development", "implementation", "signoff"),
        )
        add_json_arg(action_parser)

    soc = domains.add_parser("soc", help="plan and check a reproducible SoC product")
    soc_commands = soc.add_subparsers(dest="action", required=True)
    soc_plan = soc_commands.add_parser(
        "plan", help="validate the source-only SoC integration plan"
    )
    soc_plan.add_argument("target")
    add_json_arg(soc_plan)
    soc_check = soc_commands.add_parser(
        "check", help="resolve one variant through its exact IP lock"
    )
    soc_check.add_argument("target")
    soc_check.add_argument("--variant", required=True)
    soc_check.add_argument(
        "--fileset", help="named variant fileset (defaults to the variant contract)"
    )
    add_json_arg(soc_check)
    return parser


def _catalog_contract(root: Path, domain: str, target: str) -> Path:
    return catalog_contract_path(root, domain, target)


def _run_ip(args: argparse.Namespace, root: Path) -> int:
    context = ProjectContext.from_project_root(root)
    try:
        contract = _catalog_contract(root, "ip", args.target)
        operation = {
            "plan": plan_ip_release,
            "build": build_ip_release,
            "audit": audit_ip_release,
            "publish": publish_ip_release,
        }[args.action]
        payload = operation(
            contract,
            project_root=root,
            artifact_root=context.artifact_root,
            qualification=args.qualification,
        )
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        die(f"ERROR: {exc}")
    if args.json:
        emit_json(payload)
    elif args.action == "plan":
        print(
            f"IP release plan: {payload['ip_name']} {payload['release_id']} "
            f"qualification={payload['qualification_level']}"
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


def _run_soc(args: argparse.Namespace, root: Path) -> int:
    context = ProjectContext.from_project_root(root)
    try:
        contract = _catalog_contract(root, "soc", args.target)
        if args.action == "plan":
            payload = plan_soc(
                contract,
                project_root=root,
                artifact_root=context.artifact_root,
            )
        else:
            payload = check_soc(
                contract,
                project_root=root,
                artifact_root=context.artifact_root,
                variant_name=args.variant,
                fileset_name=args.fileset,
            )
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        die(f"ERROR: {exc}")
    if args.json:
        emit_json(payload)
    elif args.action == "plan":
        variants = ", ".join(item["name"] for item in payload["variants"])
        print(f"SoC source-only plan passed: {payload['soc']} ({variants})")
    else:
        release_ids = ", ".join(
            item["release_id"] for item in payload["ip_releases"]
        )
        print(
            f"SoC check passed: {payload['soc']} variant={payload['variant']} "
            f"fileset={payload['fileset']} IP={release_ids}"
        )
    return 0


def _run_layout(args: argparse.Namespace, root: Path, client_factory: Any) -> int:
    try:
        catalog = load_layout_target_catalog(root)
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

    spec_args = ["--spec", str(target.spec)]
    if args.action == "check":
        return generate_layout_main(
            [*spec_args, "--preview"], client_factory=client_factory
        )
    if args.action == "generate":
        return generate_layout_main(
            [*spec_args, "--timeout", str(args.timeout)],
            client_factory=client_factory,
        )
    return verify_layout_main(
        [
            *spec_args,
            "--check",
            args.check,
            "--xstream-timeout",
            str(args.xstream_timeout),
            "--calibre-timeout",
            str(args.calibre_timeout),
        ],
        client_factory=client_factory,
    )


def _run_design(
    args: argparse.Namespace,
    root: Path,
    process_executor: Callable[[str, list[str]], Any] | None,
) -> int:
    try:
        catalog = load_design_target_catalog(root)
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


def _run_oa(args: argparse.Namespace, root: Path, client_factory: Any) -> int:
    manifest = Path(args.manifest)
    if not manifest.is_absolute():
        manifest = root / manifest
    if args.action == "check":
        try:
            try:
                client = client_factory()
            except (OSError, RuntimeError, ValueError) as exc:
                # Check remains useful when Bridge is down: the source plan and
                # lock inspection stay read-only while live evidence is marked
                # unavailable.
                client = UnavailableBridge(exc)
            payload = check_oa_library(
                manifest,
                project_root=root,
                library=args.library,
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
        plan = plan_oa_library_rebuild(
            manifest,
            project_root=root,
            library=args.library,
        )
        if args.action == "plan":
            payload = plan.as_dict()
        else:
            client = client_factory()
            if args.action == "attest":
                matches = [
                    step for step in plan.testbenches if step.cell == args.testbench
                ]
                if len(matches) != 1:
                    raise ValueError(
                        f"unknown OA testbench in assembly: {args.testbench}"
                    )
                payload = attest_oa_testbench(
                    plan,
                    matches[0],
                    client,
                    timeout=args.timeout,
                )
            elif args.action == "simulate":
                matches = [
                    step for step in plan.testbenches if step.cell == args.testbench
                ]
                if len(matches) != 1:
                    raise ValueError(
                        f"unknown OA testbench in assembly: {args.testbench}"
                    )
                result = run_oa_maestro_testbench(
                    plan,
                    matches[0],
                    client,
                    timeout=args.timeout,
                    keep_work=args.keep_work,
                )
                payload = {
                    "passed": True,
                    "library": result.library,
                    "testbench": result.testbench,
                    "history": result.history,
                    "run_dir": None if result.run_dir is None else str(result.run_dir),
                    "source_fingerprint": result.source_fingerprint,
                    "semantic_fingerprint": result.semantic_fingerprint,
                    "oa_materialization_fingerprint": (
                        result.oa_materialization_fingerprint
                    ),
                    "elaborated_netlist_fingerprint": (
                        result.elaborated_netlist_fingerprint
                    ),
                    "result_database_export": (
                        None
                        if result.result_database_export is None
                        else str(result.result_database_export)
                    ),
                    "normalized_result_database": (
                        None
                        if result.normalized_result_database is None
                        else str(result.normalized_result_database)
                    ),
                    "scalar_output_count": result.scalar_output_count,
                    "product_qualification_conclusion": False,
                }
            else:
                payload = rebuild_oa_library(
                    plan,
                    client,
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
                f"OA Maestro run completed: {payload['library']}/"
                f"{payload['testbench']} history={payload['history']}"
            )
            if payload["run_dir"] is None:
                print("temporary outputs: cleaned (use --keep-work to retain this run)")
            else:
                print(f"temporary outputs: {payload['run_dir']}")
        else:
            if payload["passed"]:
                print(
                    f"OA library {payload['library']} passed: "
                    f"{payload['cell_count']} cells, "
                    f"{payload['layout_count']} layouts"
                )
            else:
                print(f"OA library {payload['library']} differs from canonical source")
    return 0 if bool(payload.get("passed")) else 1

def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
    process_executor: Callable[[str, list[str]], Any] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    root = discover_project_context(__file__).project_root
    if args.domain == "layout":
        return _run_layout(args, root, client_factory)
    if args.domain == "design":
        return _run_design(args, root, process_executor)
    if args.domain == "oa":
        return _run_oa(args, root, client_factory)
    if args.domain == "ip":
        return _run_ip(args, root)
    if args.domain == "soc":
        return _run_soc(args, root)
    raise AssertionError(f"unhandled flow domain: {args.domain}")


if __name__ == "__main__":
    raise SystemExit(main())
