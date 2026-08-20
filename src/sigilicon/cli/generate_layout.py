"""Internal source-driven declarative layout adapter.

Use ``flow layout`` for the public workflow.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sigilicon.cli.common import die
from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.layout_generation import (
    execute_layout_generation_spec,
    plan_layout_spec,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
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
    root = discover_project_context(__file__).project_root
    try:
        if args.preview:
            preview = plan_layout_spec(args.spec, root)
            plan = preview.plan
            print(plan.canonical_json(), end="")
            print(f"[fingerprint] {plan.fingerprint}")
            return 0
        spec, result = execute_layout_generation_spec(
            args.spec,
            root,
            client_factory(),
            timeout=args.timeout,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")
    print(
        f"[generated] {spec.library}/{spec.cell}/{spec.view} "
        f"instances={result.instance_count}"
    )
    print(f"[fingerprint] {result.fingerprint}")
    print(f"[artifact] {result.manifest_path}")
    return 0
