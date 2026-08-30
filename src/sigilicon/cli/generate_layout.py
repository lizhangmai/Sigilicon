"""Internal source-driven declarative layout adapter.

Use ``sigilicon layout`` for the public workflow.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sigilicon.cli.common import die
from sigilicon.paths import discover_project_contract
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.layout_generation import (
    execute_layout_generation_spec,
    plan_layout_spec,
)
from sigilicon.workflows.project import load_project


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
    project: Any | None = None,
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
    canonical_project = (
        project
        if project is not None
        else load_project(discover_project_contract(__file__))
    )
    try:
        if args.preview:
            preview = plan_layout_spec(args.spec, project=canonical_project)
            plan = preview.plan
            print(plan.canonical_json(), end="")
            return 0
        spec, result = execute_layout_generation_spec(
            args.spec,
            client_factory(),
            project=canonical_project,
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
