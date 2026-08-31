"""Explicit opt-in CLI for experimental Sigilicon workflows."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from sigilicon.artifacts import read_nofollow_text
from sigilicon.cli.common import emit_json
from sigilicon.domain.agentic_execution import agentic_execution_grant_from_json
from sigilicon.domain.repository import Project
from sigilicon.experimental.agentic import (
    DesignCampaignExecutionInterface,
    DesignCampaignReadInterface,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon experimental",
        description="Explicitly opt in to experimental Sigilicon workflows.",
    )
    parser.add_argument("--project-root", type=Path, required=True)
    commands = parser.add_subparsers(dest="domain", required=True)
    campaign = commands.add_parser("campaign", help="bounded Design Campaign pilot")
    operations = campaign.add_subparsers(dest="operation", required=True)

    plan = operations.add_parser("plan", help="compile a Campaign without execution")
    plan.add_argument("--campaign", type=Path, required=True)

    run = operations.add_parser(
        "run", help="start or resume an authorized Campaign execution"
    )
    run.add_argument("campaign_identity", nargs="?")
    run.add_argument("--grant", type=Path, required=True)
    run.add_argument("--environment", type=Path)
    run.add_argument("--campaign", type=Path)
    run.add_argument("--run-id")
    run.add_argument("--proposal", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        project = Project.from_project_root(args.project_root)
        read = DesignCampaignReadInterface(project)
        if args.operation == "plan":
            payload = read.plan_campaign(
                campaign_json=args.campaign.read_text(encoding="utf-8")
            )
        else:
            starting = args.campaign is not None or args.campaign_identity is not None
            resuming = args.run_id is not None or args.proposal is not None
            if starting == resuming:
                raise ValueError(
                    "experimental campaign run requires either --campaign plus "
                    "campaign identity or --run-id plus --proposal"
                )
            if starting and (args.campaign is None or args.campaign_identity is None):
                raise ValueError(
                    "experimental campaign start requires --campaign and campaign identity"
                )
            if resuming and (args.run_id is None or args.proposal is None):
                raise ValueError(
                    "experimental campaign resume requires --run-id and --proposal"
                )
            execution = DesignCampaignExecutionInterface(
                read,
                grant=agentic_execution_grant_from_json(
                    read_nofollow_text(args.grant.resolve())
                ),
                environment_contract=args.environment,
            )
            if starting:
                payload = execution.run_campaign(
                    campaign_json=args.campaign.read_text(encoding="utf-8"),
                    campaign_identity=args.campaign_identity,
                )
            else:
                payload = execution.run_campaign(
                    run_id=args.run_id,
                    proposal_json=args.proposal.read_text(encoding="utf-8"),
                )
        emit_json(payload)
        status = payload["data"].get("management", {}).get("status")
        if args.operation == "plan" or status == "accepted":
            return 0
        return 0 if status in {"passed", "proposal_required"} else 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
