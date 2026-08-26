"""Plan or execute one source-owned Xcelium verification cell."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sigilicon.cli.common import die, emit_json
from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.workflows.xcelium import plan_xcelium_cell, run_xcelium_cell


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", type=Path, required=True, help="verification cell cell.toml")
    parser.add_argument("--xrun", type=Path, help="explicit Xcelium xrun executable")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="launch xrun; without this flag only print the source-bound plan",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    context = discover_project_context(__file__)
    root = context.project_root
    try:
        if not args.execute:
            payload = plan_xcelium_cell(args.cell, project_root=root).as_dict(
                project_root=root
            )
            payload["executed"] = False
        else:
            result = run_xcelium_cell(
                args.cell,
                project_root=root,
                artifact_root=context.artifact_root,
                xrun=args.xrun,
                timeout=args.timeout,
            )
            payload = {
                **result.plan.as_dict(project_root=root),
                "executed": True,
                "returncode": result.returncode,
                "manifest": result.manifest.relative_to(root).as_posix(),
                "product_qualification_conclusion": False,
            }
    except (OSError, RuntimeError, ValueError) as error:
        die(f"ERROR: {error}")
    if args.json:
        emit_json(payload)
    elif not args.execute:
        print(
            f"Xcelium plan: {payload['cell']} sources={len(payload['sources'])}"
        )
    else:
        print(
            f"Xcelium run: {payload['cell']} returncode={payload['returncode']} "
            f"manifest={payload['manifest']}"
        )
    return 0 if payload.get("returncode", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
