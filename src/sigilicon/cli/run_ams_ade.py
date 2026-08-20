"""Legacy: run an ADE AMS test from its design spec and enforce truth results."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sigilicon.workflows.legacy_ams_execution import execute_ade_run_spec
from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.cli.common import die, parse_kv_pairs
from sigilicon.virtuoso.client import get_client


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VAL")
    args = parser.parse_args(argv)
    try:
        root = discover_project_context(__file__).project_root
        execution = execute_ade_run_spec(
            args.spec,
            root,
            client_factory(),
            variables=parse_kv_pairs(args.var, ctx="--var NAME=VAL"),
            timeout=args.timeout,
        )
        result = execution.result
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")
    print(f"[history] {result.history} status={result.simulator_status}")
    print(f"[setup] {result.setup_dir}")
    print(f"[run] {result.run_dir}")
    print(f"[source-log] {result.source_log}")
    print(f"[log] {result.copied_log}")
    print(f"[truth] {result.truth_table} rows={result.vectors}")
    print(f"[summary] vectors={result.vectors} failed=0")
    return 0
