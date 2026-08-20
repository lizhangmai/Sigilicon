"""读 cell 的版图 geometry（每条 shape / via / instance 的层与坐标）。

用法::

    pixi run read-layout design <layout_cell>
    pixi run read-layout design <layout_cell> layout2 --json

注: ``layout_read_summary`` 需要 layout 窗口处于打开状态，CLI 场景不可靠；
    本脚本只输出 geometry（按 lib/cell 直接读，无需开窗，信息也更全）。
"""

from __future__ import annotations

import argparse

from sigilicon.cli.common import add_json_arg, die, emit_json
from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.virtuoso_operations import read_project_layout


def main() -> int:
    p = argparse.ArgumentParser(description="读版图 geometry")
    p.add_argument("lib")
    p.add_argument("cell")
    p.add_argument("view", nargs="?", default="layout")
    add_json_arg(p)
    args = p.parse_args()

    try:
        paths = discover_project_context(__file__)
        geometry = read_project_layout(
            get_client(),
            paths,
            args.lib,
            args.cell,
            args.view,
        )
    except Exception as exc:  # noqa: BLE001
        die(f"读 geometry 失败：{exc}")

    if args.json:
        emit_json({"lib": args.lib, "cell": args.cell, "view": args.view, "geometry": geometry})
    else:
        print(f"{args.lib}/{args.cell}/{args.view}  ({len(geometry)} 个图素)")
        for obj in geometry:
            fields = "  ".join(f"{k}={v}" for k, v in obj.items() if v is not None)
            print("  " + fields)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
