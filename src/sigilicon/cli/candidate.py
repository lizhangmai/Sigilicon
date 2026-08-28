"""Validate immutable Design Candidate artifact chains."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sigilicon.cli.common import add_json_arg, die, emit_json
from sigilicon.workflows.design_artifacts import DesignArtifactInterface


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sigilicon candidate", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    validate = commands.add_parser(
        "validate",
        help="validate one Candidate and its exact stage artifacts",
    )
    validate.add_argument("--candidate", type=Path, required=True)
    validate.add_argument("--artifact", type=Path, action="append", required=True)
    add_json_arg(validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = DesignArtifactInterface().validate_candidate(
            args.candidate.read_text(encoding="utf-8"),
            tuple(path.read_text(encoding="utf-8") for path in args.artifact),
        )
    except (OSError, ValueError) as exc:
        die(f"ERROR: {exc}")
    payload = {
        "candidate_identity": result.candidate_identity,
        "resolved_artifacts": list(result.resolved_artifacts),
        "valid": True,
    }
    if args.json:
        emit_json(payload)
    else:
        print(
            f"Candidate valid: {result.candidate_identity} "
            f"({len(result.resolved_artifacts)} artifacts)"
        )
    return 0


__all__ = ["main"]
