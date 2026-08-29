"""Internal source-driven declarative layout adapter.

Use ``flow layout`` for the public workflow.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sigilicon.cli.common import die
from sigilicon.paths import discover_project_contract
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.project_layout import ProjectLayoutWorkflow


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
    workflow: ProjectLayoutWorkflow | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument(
        "--preview",
        action="store_true",
        help="validate the canonical topology and print the stable plan without OA access",
    )
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    project_layout = (
        workflow
        if workflow is not None
        else ProjectLayoutWorkflow.from_file(discover_project_contract(__file__))
    )
    try:
        if args.preview:
            preview = project_layout.plan(args.spec)
            plan = preview.plan
            print(plan.canonical_json(), end="")
            return 0
        spec, result = project_layout.generate(
            args.spec,
            client_factory(),
            timeout=args.timeout,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")
    print(
        f"[generated] {spec.library}/{spec.cell}/{spec.view} "
        f"instances={result.instance_count}"
    )
    print(f"[artifact] {result.manifest_path}")
    return 0
