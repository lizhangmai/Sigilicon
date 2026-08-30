"""Internal source-driven Spectre hierarchy importer.

Use ``sigilicon oa rebuild`` for the public workflow; this module is an adapter used
by guarded rebuild implementations and tests.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.cli.common import die
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.virtuoso_operations import import_spectre_hierarchy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("netlist", type=Path)
    parser.add_argument("--library", required=True)
    parser.add_argument("--ref-lib", action="append", default=[])
    parser.add_argument("--top")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
) -> int:
    args = build_parser().parse_args(argv)
    paths = discover_project_context(__file__)
    try:
        completed = import_spectre_hierarchy(
            client_factory(),
            paths,
            args.netlist,
            library=args.library,
            reference_libraries=tuple(args.ref_lib),
            top=args.top,
            overwrite=args.overwrite,
            timeout=args.timeout,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        die(str(exc))
    for cell in completed:
        print(f"[imported] {args.library}/{cell}/schematic")
        print(f"[symbol] {args.library}/{cell}/symbol")
    print(f"[done] imported {len(completed)} cells; top={completed[-1]}")
    return 0
