"""建立 Virtuoso 库（默认不绑技术库 → 不需要 PDK 也能用）。

用法::

    This module is an internal source-driven library adapter; it is
    intentionally not exposed as a standalone Pixi task.

建库后若 ``virtuoso/cds.lib`` 未 DEFINE 该库，会自动追加一行（幂等）。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.cli.common import die
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.virtuoso_operations import create_library


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
) -> int:
    paths = discover_project_context(__file__)
    virtuoso_dir = paths.workspace_root

    p = argparse.ArgumentParser(description="建立 Virtuoso 库（默认不绑技术库 / 无 PDK）")
    p.add_argument("library", help="库名，如 design")
    p.add_argument(
        "--path",
        type=Path,
        help=f"库目录（默认 {virtuoso_dir}/<library>）",
    )
    p.add_argument(
        "--tech",
        help="技术库名（需要 PDK 时指定，如 analogLib 或你的 PDK）；默认不绑",
    )
    p.add_argument("--if-missing", action="store_true", help="库已存在则跳过（幂等）")
    args = p.parse_args(argv)

    lib_path = (args.path or virtuoso_dir / args.library).resolve()
    try:
        result = create_library(
            client_factory(),
            paths,
            library=args.library,
            library_path=lib_path,
            technology_library=args.tech,
            if_missing=args.if_missing,
        )
    except Exception as exc:  # noqa: BLE001
        die(f"建库失败：{exc}")
    if result.action == "exists":
        print(f"[exists] {args.library}（已存在，跳过）")
        return 0
    tech = result.technology_library or "(无 — 不需要 PDK)"
    print(f"[created] {args.library} -> {result.path}")
    print(f"          cds.lib   : {result.cds_entry}")
    print(f"          technology: {tech}")
    if not args.tech:
        print("          说明：未绑技术库，可用于 schematic / symbol / netlist。")
        print("                日后需要版图或真实器件模型时，用 --tech <PDK> 另建一个库。")
    return 0
