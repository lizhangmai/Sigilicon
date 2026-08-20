"""读出 cell 的原理图（实例 / 网络 / 管脚 / 参数）。

用法::

    pixi run read-schematic mylib mycell
    pixi run read-schematic mylib mycell --json
    pixi run read-schematic mylib mycell --positions
"""

from __future__ import annotations

import argparse

from sigilicon.cli.common import add_json_arg, die, emit_json
from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.virtuoso_operations import read_project_schematic


def print_text(data: dict, include_positions: bool) -> None:
    instances = data.get("instances", [])
    nets = data.get("nets", {})
    pins = data.get("pins", {})
    notes = data.get("notes", [])

    print(
        f"Instances : {len(instances)}   Nets : {len(nets)}   "
        f"Pins : {len(pins)}   Notes : {len(notes)}"
    )

    if instances:
        width = max(len(i.get("name", "")) for i in instances)
        print(f"\n{'INSTANCE':<{width}}  LIB/CELL           TERMS")
        print("-" * (width + 50))
        for i in instances:
            terms = "  ".join(f"{t}={n}" for t, n in i.get("terms", {}).items())
            line = f"{i.get('name', ''):<{width}}  {i.get('lib', '?')}/{i.get('cell', '?')}"
            if include_positions:
                xy = i.get("xy", [0, 0])
                line += f"  @({xy[0]:.1f},{xy[1]:.1f}) {i.get('orient', '?')}"
            if terms:
                line += f"  {terms}"
            print(line)
            params = i.get("params", {})
            if params:
                ps = "  ".join(f"{k}={v}" for k, v in params.items())
                print(f"{'':<{width}}  params: {ps}")

    if nets:
        width = max(len(n) for n in nets)
        print(f"\n{'NET':<{width}}  BITS  TYPE     CONNECTIONS")
        print("-" * (width + 50))
        for name, n in nets.items():
            conns = "  ".join(n.get("connections", []))
            print(
                f"{name:<{width}}  {n.get('numBits', 1):<5} "
                f"{n.get('sigType', '?'):<9} {conns}"
            )

    if pins:
        width = max(len(p) for p in pins)
        print(f"\n{'PIN':<{width}}  DIR           BITS")
        print("-" * (width + 20))
        for name, p in pins.items():
            print(f"{name:<{width}}  {p.get('direction', '?'):<14}{p.get('numBits', 1)}")

    if notes:
        print(f"\nNOTES ({len(notes)}):")
        for n in notes:
            print(f"  \"{n.get('text', '')}\"")


def main() -> int:
    p = argparse.ArgumentParser(description="读原理图：实例 / 网络 / 管脚 / 参数")
    p.add_argument("lib")
    p.add_argument("cell")
    p.add_argument(
        "view",
        nargs="?",
        default="schematic",
        help="（保留位，目前固定读 schematic）",
    )
    p.add_argument(
        "--positions",
        action="store_true",
        help="包含 xy / 方向 / bBox（默认只读拓扑，大图更快）",
    )
    add_json_arg(p)
    args = p.parse_args()

    try:
        paths = discover_project_context(__file__)
        data = read_project_schematic(
            get_client(),
            paths,
            args.lib,
            args.cell,
            include_positions=args.positions,
        )
    except Exception as exc:  # noqa: BLE001
        die(f"读取 {args.lib}/{args.cell}/schematic 失败：{exc}")

    if args.json:
        emit_json(data)
    else:
        print(
            f"Reading {args.lib}/{args.cell}/schematic "
            f"(positions={'on' if args.positions else 'off'}) ..."
        )
        print_text(data, args.positions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
