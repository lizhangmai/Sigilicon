"""Read-only CLI peer for the agent integration Interface."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from sigilicon.cli.common import emit_json
from sigilicon.workflows.agentic_read import AgenticReadInterface


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigilicon read",
        description="Inspect cataloged project, Flow, and run identities without execution.",
    )
    parser.add_argument("--project-root", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)

    project = commands.add_parser("project", help="inspect project ownership and targets")
    project.add_argument("--owner")

    plan = commands.add_parser("flow-plan", help="resolve one source-only Flow plan")
    plan.add_argument("owner")
    plan.add_argument("flow")
    plan.add_argument("target")
    plan.add_argument("--profile")

    run = commands.add_parser("run-inspect", help="read one persisted Flow result")
    run.add_argument("owner")
    run.add_argument("flow")
    run.add_argument("target")
    run.add_argument("run_id")

    candidate = commands.add_parser(
        "candidate-validate",
        help="validate a canonical Candidate and its exact stage artifacts",
    )
    candidate.add_argument("owner")
    candidate.add_argument("--candidate", type=Path, required=True)
    candidate.add_argument("--artifact", type=Path, action="append", required=True)
    campaign = commands.add_parser(
        "campaign-plan",
        help="compile one strict bounded Design Campaign without execution",
    )
    campaign.add_argument("--campaign", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        interface = AgenticReadInterface.from_project_root(args.project_root)
        if args.action == "project":
            result = interface.inspect_project(owner=args.owner)
        elif args.action == "flow-plan":
            result = interface.plan_flow(
                owner=args.owner,
                flow=args.flow,
                target=args.target,
                profile=args.profile,
            )
        elif args.action == "run-inspect":
            result = interface.inspect_run(
                owner=args.owner,
                flow=args.flow,
                target=args.target,
                run_id=args.run_id,
            )
        elif args.action == "candidate-validate":
            result = interface.validate_candidate(
                owner=args.owner,
                candidate_json=args.candidate.read_text(encoding="utf-8"),
                artifact_json=tuple(
                    path.read_text(encoding="utf-8") for path in args.artifact
                ),
            )
        elif args.action == "campaign-plan":
            result = interface.plan_campaign(
                campaign_json=args.campaign.read_text(encoding="utf-8")
            )
        else:  # pragma: no cover
            raise AssertionError(f"unhandled read action: {args.action}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    emit_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
