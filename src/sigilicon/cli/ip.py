"""Immutable IP release and integration command line."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sigilicon.cli.common import add_json_arg, die, emit_json
from sigilicon.paths import discover_project_contract
from sigilicon.project import Project
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sigilicon ip", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("plan", "validate the producer contract and show the release plan"),
        ("build", "build an immutable release package"),
        ("audit", "audit the release selected by its owner contract"),
        ("publish", "atomically select an audited release for consumers"),
    ):
        command = commands.add_parser(action, help=help_text)
        command.add_argument("target")
        command.add_argument(
            "--maturity",
            choices=("development", "implementation", "signoff"),
        )
        if action == "audit":
            command.add_argument(
                "--artifact-root",
                type=Path,
                help="managed release root (defaults to project artifact_root)",
            )
        add_json_arg(command)

    integration = commands.add_parser(
        "integration", help="plan or check a composite IP"
    )
    actions = integration.add_subparsers(dest="integration_action", required=True)
    plan = actions.add_parser(
        "plan", help="validate source-only composite-IP integration intent"
    )
    plan.add_argument("target")
    add_json_arg(plan)
    check = actions.add_parser(
        "check", help="resolve one IP variant through its exact dependency lock"
    )
    check.add_argument("target")
    check.add_argument("--variant", required=True)
    check.add_argument(
        "--fileset", help="named variant fileset (defaults to the variant contract)"
    )
    add_json_arg(check)
    return parser


def _run(args: argparse.Namespace, project: Project) -> int:
    if args.action == "integration":
        try:
            contract = ip_catalog_contract_path(
                project,
                args.target,
                section="components",
            )
            payload = (
                plan_ip_integration(contract, project=project)
                if args.integration_action == "plan"
                else check_ip_integration(
                    contract,
                    project=project,
                    variant_name=args.variant,
                    fileset_name=args.fileset,
                )
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
        contract = ip_catalog_contract_path(project, args.target)
        operation = {
            "plan": plan_ip_release,
            "build": build_ip_release,
            "audit": audit_ip_release,
            "publish": publish_ip_release,
        }[args.action]
        keywords: dict[str, Any] = {"maturity": args.maturity}
        if args.action == "audit" and args.artifact_root is not None:
            root = project.project_root
            keywords["artifact_root"] = (
                args.artifact_root
                if args.artifact_root.is_absolute()
                else root / args.artifact_root
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
        print(f"published IP release: {payload['ip_name']} {payload['release_id']}")
    else:
        print(
            f"IP release {args.action} passed: {payload['ip_name']} "
            f"{payload['release_id']}"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project = Project.open(discover_project_contract(__file__).parent)
    return _run(args, project)


if __name__ == "__main__":
    raise SystemExit(main())
