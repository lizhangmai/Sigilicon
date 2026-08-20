"""列出 Virtuoso 库 / cell。

用法::

    pixi run list-cells                # 列出所有库名
    pixi run list-cells design         # 列出 design 库的 cell + view
    pixi run list-cells design --json
"""

from __future__ import annotations

import argparse

from sigilicon.cli.common import add_json_arg, die, emit_json
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.virtuoso_operations import (
    list_project_cells,
    list_project_libraries,
)


def main() -> int:
    p = argparse.ArgumentParser(description="列出 Virtuoso 库 / cell")
    p.add_argument("lib", nargs="?", help="库名；省略则列出所有库")
    add_json_arg(p)
    args = p.parse_args()

    try:
        client = get_client()
        data = (
            list_project_libraries(client)
            if not args.lib
            else list_project_cells(client, args.lib)
        )
    except Exception as exc:  # noqa: BLE001
        die(f"读取 Virtuoso library/cell 失败：{exc}")

    if args.json:
        emit_json(data)
        return 0

    if not args.lib:
        libs = data["libraries"]
        print(f"共 {len(libs)} 个库：")
        for name in libs:
            print(f"  {name}")
    else:
        cells = data["cells"]
        print(f"库 {args.lib} 共 {len(cells)} 个 cell：")
        if cells:
            width = max(len(c["name"]) for c in cells)
            print(f"  {'CELL':<{width}}  VIEWS")
            for c in cells:
                print(f"  {c['name']:<{width}}  {', '.join(c['views'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
