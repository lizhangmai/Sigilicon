"""关闭 cell 的窗口（find → dbSave → hiCloseWindow）。

用法::

    pixi run close-cell mylib mycell
    pixi run close-cell mylib mycell schematic
    pixi run close-cell mylib mycell --json

open-cell 的对偶。先存盘再关，dirty 窗口也不会弹"是否保存"阻塞对话框
（local 模式 dismiss_dialog 不可用，所以必须先 dbSave）。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any

from sigilicon.execution.model import Resources
from sigilicon.paths import discover_project_context
from sigilicon.project import Project
from sigilicon.cli.common import add_json_arg, die, emit_json
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.virtuoso_operations import close_cell


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[Resources], Any] = get_client,
) -> int:
    p = argparse.ArgumentParser(description="关闭 cell 窗口（先存盘再关）")
    p.add_argument("lib")
    p.add_argument("cell")
    p.add_argument("view", nargs="?", help="只关指定 view 的窗口；省略则关该 cell 全部 view")
    add_json_arg(p)
    args = p.parse_args(argv)

    try:
        paths = discover_project_context(__file__)
        resources = Project.open(paths.project_root)._execution_resources()
        client = client_factory(resources)
        result = close_cell(client, paths, args.lib, args.cell, args.view)
    except Exception as exc:  # noqa: BLE001
        die(f"关窗口失败：{exc}")
    label = f"{args.lib}/{args.cell}" + (f"/{args.view}" if args.view else "")

    if args.json:
        emit_json(
            {
                "lib": args.lib,
                "cell": args.cell,
                "view": args.view,
                "closed": result.closed,
                "remaining": result.remaining,
                "remaining_windows": [],
            }
        )
        return 0

    if result.closed == 0:
        print(f"没有匹配的窗口：{label}")
        return 0
    print(f"已关闭 {result.closed} 个窗口（{label}）；剩余 {result.remaining} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
