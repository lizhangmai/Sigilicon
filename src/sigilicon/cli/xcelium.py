"""Plan or execute one source-owned Xcelium verification cell."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sigilicon.cli.common import die, emit_json
from sigilicon.paths import discover_project_contract
from sigilicon.workflows import load_project
from sigilicon.workflows.xcelium import plan_xcelium_cell, run_xcelium_cell


def _display_path(path: Path, *, project_root: Path) -> str:
    """Keep project-local output stable while supporting external artifact roots."""

    return (
        path.relative_to(project_root).as_posix()
        if path.is_relative_to(project_root)
        else str(path)
    )


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
    project = load_project(discover_project_contract(__file__))
    try:
        if not args.execute:
            plan = plan_xcelium_cell(args.cell, project=project)
            payload = plan.as_dict()
            payload["executed"] = False
        else:
            result = run_xcelium_cell(
                args.cell,
                project=project,
                xrun=args.xrun,
                timeout=args.timeout,
            )
            root = result.plan.spec.project_root
            payload = {
                **result.plan.as_dict(),
                "executed": True,
                "returncode": result.returncode,
                "passed": result.passed,
                "run_id": result.run_id,
                "run_dir": _display_path(result.run_dir, project_root=root),
                "manifest": _display_path(
                    result.manifest_path, project_root=root
                ),
                "run_summary": _display_path(
                    result.run_summary, project_root=root
                ),
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
            f"Xcelium run: {payload['cell']} passed={payload['passed']} "
            f"returncode={payload['returncode']} "
            f"manifest={payload['manifest']}"
        )
    return 0 if payload.get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
