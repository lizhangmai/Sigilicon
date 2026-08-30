"""Internal source-driven Virtuoso symbol generator.

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
from sigilicon.workflows.virtuoso_operations import generate_cell_symbol


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lib")
    parser.add_argument("cell")
    parser.add_argument(
        "--sort-pins",
        choices=("alphanumeric", "geometric"),
        default="geometric",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
) -> int:
    args = build_parser().parse_args(argv)
    paths = discover_project_context(__file__)
    try:
        result = generate_cell_symbol(
            client_factory(),
            paths,
            args.lib,
            args.cell,
            sort_pins=args.sort_pins,
            overwrite=args.overwrite,
            timeout=args.timeout,
        )
    except Exception as exc:  # noqa: BLE001
        die(f"generate symbol failed for {args.lib}/{args.cell}: {exc}")
    print(
        f"[symbol] {args.lib}/{args.cell}/symbol action={result.action} "
        f"terminals={list(result.terminal_names)} pin_order={list(result.pin_order)}"
    )
    return 0
