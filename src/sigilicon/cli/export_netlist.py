"""导出 schematic 的 netlist package（默认 Spectre）。

用法::

    pixi run export-netlist mylib mycell
    pixi run export-netlist mylib mycell -s spectre --view schematic

注意: Cadence `createNetlist` 会生成包含 `input.scs` 和辅助文件的目录。
     每次导出写入 design-scoped netlist run，并把稳定 netlist 与工具 work 分开。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.cli.common import die
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.virtuoso_operations import export_project_netlist


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
) -> int:
    p = argparse.ArgumentParser(description="导出 schematic netlist package")
    p.add_argument("lib")
    p.add_argument("cell")
    p.add_argument("-s", "--simulator", default="spectre", help="仿真器（spectre / cdl …，默认 spectre）")
    p.add_argument("--view", default="schematic", help="源 view（默认 schematic）")
    args = p.parse_args(argv)

    try:
        paths = discover_project_context(__file__)
        exported = export_project_netlist(
            client_factory(),
            paths,
            args.lib,
            args.cell,
            view=args.view,
            simulator=args.simulator,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        die(f"导出 netlist 失败：{exc}")

    size = exported.input_scs.stat().st_size
    suffix = f"  ({size} bytes)"
    print(f"已导出 {args.lib}/{args.cell}/{args.view} → {exported.input_scs}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
