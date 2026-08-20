"""在 Virtuoso 里打开一个 cell 窗口（让你看到它）。

用法::

    pixi run open-cell mylib mycell
    pixi run open-cell mylib mycell symbol
    pixi run open-cell mylib mytest maestro
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any

from sigilicon.cli.common import die
from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.virtuoso_operations import open_project_cell


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
) -> int:
    p = argparse.ArgumentParser(description="打开 cell 窗口")
    p.add_argument("lib")
    p.add_argument("cell")
    p.add_argument(
        "view",
        nargs="?",
        default="schematic",
        help="view 名（schematic / layout / symbol / maestro …，默认 schematic）",
    )
    args = p.parse_args(argv)

    try:
        paths = discover_project_context(__file__)
        open_project_cell(
            client_factory(),
            paths,
            args.lib,
            args.cell,
            args.view,
        )
    except Exception as exc:  # noqa: BLE001
        die(f"打开 {args.lib}/{args.cell}/{args.view} 失败：{exc}")
    print(f"已打开 {args.lib}/{args.cell}/{args.view}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
