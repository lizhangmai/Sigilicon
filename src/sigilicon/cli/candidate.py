"""Validate immutable Design Candidate artifact chains."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sigilicon.cli.common import add_json_arg, die, emit_json
from sigilicon.workflows.project import bind_agentic_read


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sigilicon candidate", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    validate = commands.add_parser(
        "validate",
        help="validate one Candidate and its exact stage artifacts",
    )
    validate.add_argument("--project-root", type=Path, required=True)
    validate.add_argument("--owner", required=True)
    validate.add_argument("--candidate", type=Path, required=True)
    validate.add_argument("--artifact", type=Path, action="append", required=True)
    add_json_arg(validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = bind_agentic_read(args.project_root).validate_candidate(
            owner=args.owner,
            candidate_json=args.candidate.read_text(encoding="utf-8"),
            artifact_json=tuple(
                path.read_text(encoding="utf-8") for path in args.artifact
            ),
        )
    except (OSError, ValueError) as exc:
        die(f"ERROR: {exc}")
    if args.json:
        emit_json(payload)
    else:
        data = payload["data"]
        print(
            f"Candidate valid: {data['candidate_identity']} "
            f"({len(data['resolved_artifacts'])} artifacts)"
        )
    return 0


__all__ = ["main"]
