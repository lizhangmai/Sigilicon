"""Run a declarative AMS design through the standalone Xcelium engine."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sigilicon.workflows.spec_execution import execute_standalone_spec
from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.cli.common import die


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="design AMS TOML spec")
    parser.add_argument("--xrun", type=Path, help="path to the Xcelium xrun executable")
    parser.add_argument("--timeout", type=int, default=600, help="Xrun timeout in seconds")
    args = parser.parse_args(argv)
    try:
        root = discover_project_context(__file__).project_root
        from sigilicon.virtuoso.client import get_client

        execution = execute_standalone_spec(
            args.spec,
            root,
            xrun=args.xrun,
            timeout=args.timeout,
            oa_client=get_client(),
        )
        result = execution.result
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")
    print(f"[xrun] {result.xrun}")
    print(f"[source] {result.source_netlist}")
    print(f"[namespace] {result.namespace_dir}")
    print(f"[run] {result.run_dir}")
    print(f"[truth] {result.truth_table} rows={result.vectors}")
    if result.waveform:
        print(f"[wave] {result.waveform}")
    print(f"[summary] vectors={result.vectors} failed=0")
    return 0
