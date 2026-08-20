"""Legacy: create OA SystemVerilog/config/Maestro views from an AMS spec."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sigilicon.workflows.legacy_ams_execution import execute_ade_setup_spec
from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.cli.common import die
from sigilicon.virtuoso.client import get_client


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="design AMS TOML spec")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing testbench cell")
    parser.add_argument(
        "--quarantine-stale-locks",
        action="store_true",
        help=(
            "capture hard-link evidence for proven dead local OA locks, then stop "
            "without moving the live lock pathname"
        ),
    )
    args = parser.parse_args(argv)
    try:
        root = discover_project_context(__file__).project_root
        execution = execute_ade_setup_spec(
            args.spec,
            root,
            client_factory(),
            overwrite=args.overwrite,
            quarantine_stale_locks=args.quarantine_stale_locks,
        )
        spec = execution.spec
        result = execution.result
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")
    design = spec.design
    if result.updated_testbench:
        print(f"[updated] {design.library}/{spec.testbench}")
    print(
        f"[systemVerilog] {design.library}/{spec.testbench}/systemVerilog "
        f"({result.systemverilog_source})"
    )
    print(f"[config] {design.library}/{spec.testbench}/config")
    print(f"[maestro] {design.library}/{spec.testbench}/maestro")
    print(f"[setup] {result.setup_dir}")
    print(f"[manifest] {result.manifest_path}")
    return 0
